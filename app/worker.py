"""Worker: claim pending submissions, grade them, persist verdicts.

A worker is a simple loop — claim → grade → persist — that polls when the queue
is empty and periodically runs the stale-claim reaper. Parallelism comes from
running a *pool of worker processes* (``--workers N``); each process is
single-threaded with its own DB engine, which sidesteps SQLite's
one-thread-per-connection rule and matches the multiprocessing ``spawn`` model.

Scale-up path (documented in ARCHITECTURE.md §3.3): swap the DB-backed queue for
Redis/RQ or Celery and the worker loop becomes a task consumer with no other
changes to the grader or runner.

Usage::

    python -m app.worker                 # run a pool of OJ_WORKER_COUNT workers
    python -m app.worker --workers 4     # run 4 workers
    python -m app.worker --once          # claim+grade a single submission, exit
    python -m app.worker --reap          # run the stale-claim reaper once, exit
"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import signal
import threading
import time

from app import queue, repository
from app.config import Settings, settings
from app.db import SessionLocal, init_db
from app.grader import GradeResult, grade_submission
from app.models import Submission, TestResult
from app.runner.result import SandboxError


def grade_one(session, worker_id: str, cfg: Settings = settings) -> str | None:
    """Claim, grade, and persist a single submission.

    Returns the resulting verdict (e.g. ``"AC"``), or a status string for
    infrastructure failures, or ``None`` when the queue is empty. Designed to be
    callable in-process (the integration tests use it directly).
    """
    submission_id = queue.claim_next(session, worker_id)
    if submission_id is None:
        return None

    submission = session.get(Submission, submission_id)
    problem = repository.get_problem(session, submission.problem_id)
    # End the read transaction so no DB lock/snapshot is held during the slow
    # Docker grading (expire_on_commit=False keeps the loaded objects usable).
    session.commit()

    try:
        result = grade_submission(submission, problem, cfg)
    except SandboxError as exc:
        outcome = queue.fail_submission(
            session,
            submission_id=submission_id,
            worker_id=worker_id,
            message=f"sandbox error: {exc}",
            cfg=cfg,
        )
        return f"IE/{outcome}"
    except Exception as exc:  # noqa: BLE001 - any grader bug is an infra failure, not the user's fault
        outcome = queue.fail_submission(
            session,
            submission_id=submission_id,
            worker_id=worker_id,
            message=f"grader error: {type(exc).__name__}: {exc}",
            cfg=cfg,
        )
        return f"IE/{outcome}"

    stored = queue.complete_submission(
        session,
        submission_id=submission_id,
        worker_id=worker_id,
        verdict=result.verdict,
        compile_output=result.compile_output,
        max_time_ms=result.max_time_ms,
        max_memory_kb=result.max_memory_kb,
        score=result.score,
        max_score=result.max_score,
        test_results=_to_test_results(result),
    )
    return result.verdict.value if stored else "lost"


def _to_test_results(result: GradeResult) -> list[TestResult]:
    return [
        TestResult(
            test_case_id=t.test_case_id,
            ordinal=t.ordinal,
            verdict=t.verdict,
            time_ms=t.time_ms,
            cpu_ms=t.cpu_ms,
            memory_kb=t.memory_kb,
            exit_code=t.exit_code,
            signal=t.signal,
            stderr_snippet=t.stderr_snippet,
        )
        for t in result.tests
    ]


def run_worker(worker_id: str, stop: threading.Event, cfg: Settings = settings) -> None:
    """Run the claim→grade loop until ``stop`` is set."""
    if cfg.is_sqlite:
        queue.assert_sqlite_supports_returning()

    last_reap = 0.0
    while not stop.is_set():
        now = time.monotonic()
        if now - last_reap >= cfg.reaper_interval_s:
            _safe_reap(worker_id, cfg)
            last_reap = now

        session = SessionLocal()
        try:
            verdict = grade_one(session, worker_id, cfg)
        except Exception as exc:  # noqa: BLE001 - never let one bad submission kill the worker
            print(f"[{worker_id}] unexpected error: {exc!r}", flush=True)
            verdict = None
        finally:
            session.close()

        if verdict is None:
            stop.wait(cfg.worker_poll_interval_s)  # queue empty: back off
        else:
            print(f"[{worker_id}] graded submission -> {verdict}", flush=True)


def _safe_reap(worker_id: str, cfg: Settings) -> None:
    session = SessionLocal()
    try:
        counts = queue.reap_stale_claims(session, cfg)
        if counts["requeued"] or counts["errored"]:
            print(f"[{worker_id}] reaper: {counts}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[{worker_id}] reaper error: {exc!r}", flush=True)
    finally:
        session.close()


def _worker_process(worker_index: int) -> None:  # pragma: no cover - runs in a child process
    """Entry point for a worker subprocess (must be importable for spawn)."""
    worker_id = f"w{worker_index}-{os.getpid()}"
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    print(f"[{worker_id}] started", flush=True)
    run_worker(worker_id, stop)
    print(f"[{worker_id}] stopped", flush=True)


def run_pool(count: int) -> None:  # pragma: no cover - process orchestration
    """Spawn ``count`` worker processes and supervise them until interrupted."""
    init_db()
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_worker_process, args=(i,), daemon=False) for i in range(count)]
    for proc in procs:
        proc.start()
    print(f"started {count} worker(s); press Ctrl-C to stop", flush=True)
    try:
        for proc in procs:
            proc.join()
    except KeyboardInterrupt:
        print("\nstopping workers...", flush=True)
        for proc in procs:
            proc.terminate()
        for proc in procs:
            proc.join()


def run() -> None:  # pragma: no cover - CLI entry point
    parser = argparse.ArgumentParser(description="Online judge grading worker(s).")
    parser.add_argument("--workers", type=int, default=settings.worker_count)
    parser.add_argument("--once", action="store_true", help="grade one submission and exit")
    parser.add_argument("--reap", action="store_true", help="run the stale-claim reaper and exit")
    args = parser.parse_args()

    if args.reap:
        init_db()
        session = SessionLocal()
        try:
            print(queue.reap_stale_claims(session))
        finally:
            session.close()
        return

    if args.once:
        init_db()
        session = SessionLocal()
        try:
            print(grade_one(session, f"once-{os.getpid()}") or "queue empty")
        finally:
            session.close()
        return

    run_pool(args.workers)


if __name__ == "__main__":  # pragma: no cover
    run()
