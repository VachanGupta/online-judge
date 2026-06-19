"""The DB-backed submission queue: atomic claim, guarded completion, reaper.

Why this works on plain SQLite with multiple worker *processes* — and the
tradeoffs — is documented in ARCHITECTURE.md §3.3. The essentials:

- **Claiming is one statement.** ``UPDATE … WHERE id = (SELECT … 'pending' …)
  AND status='running'-guard … RETURNING id`` is race-free across processes
  because SQLite serializes every writer behind a single write lock held for the
  whole statement, so the inner SELECT and the row mutation cannot be
  interleaved by another writer. The ``AND status='pending'`` guard is the
  correctness anchor; RETURNING yields a row only if this statement actually
  claimed one. (RETURNING needs SQLite ≥ 3.35 — asserted at worker startup; the
  portable BEGIN IMMEDIATE + guarded UPDATE + rowcount form is the documented
  fallback.)

- **Grade outside any transaction.** Two tiny transactions bracket the slow
  Docker work: one to claim, one to write the verdict. The verdict write is
  guarded by ``worker_id`` + ``status='running'`` so a worker whose claim was
  reaped (and handed to someone else) can't clobber the new owner's result.

- **A reaper requeues crashed claims** (rows stuck ``running`` past a timeout),
  and parks submissions that have burned through their attempt budget.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import Settings, settings
from app.enums import SubmissionStatus, Verdict
from app.models import Submission, TestResult, utcnow

# RETURNING (used by the claim) requires SQLite 3.35.0+. What matters is the
# *linked* library version, not the Python version.
_MIN_SQLITE = (3, 35, 0)


def assert_sqlite_supports_returning() -> None:
    if sqlite3.sqlite_version_info < _MIN_SQLITE:
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} is too old; the claim query needs "
            f">= 3.35.0 for RETURNING. Upgrade, or use a different OJ_DATABASE_URL."
        )


def claim_next(session: Session, worker_id: str) -> int | None:
    """Atomically claim the oldest pending submission; return its id or None.

    Compiles to a single ``UPDATE submissions SET status='running' … WHERE
    id = (SELECT id … 'pending' ORDER BY id LIMIT 1) AND status='pending'
    RETURNING id`` — one statement, so SQLite's single write lock makes it
    race-free across processes (the subquery and the mutation can't be
    interleaved). Built with typed Core constructs so the timestamp uses
    SQLAlchemy's datetime handling rather than the deprecated sqlite3 adapter.
    """
    oldest_pending = (
        select(Submission.id)
        .where(Submission.status == SubmissionStatus.PENDING)
        .order_by(Submission.id)
        .limit(1)
        .scalar_subquery()
    )
    stmt = (
        update(Submission)
        .where(Submission.id == oldest_pending, Submission.status == SubmissionStatus.PENDING)
        .values(
            status=SubmissionStatus.RUNNING,
            worker_id=worker_id,
            claimed_at=utcnow(),
            attempts=Submission.attempts + 1,
        )
        .returning(Submission.id)
    )
    row = session.execute(stmt).first()
    session.commit()
    # The Core UPDATE bypasses the identity map; expire so any subsequent ORM
    # read in this session reflects the claim rather than a stale cached row.
    session.expire_all()
    return None if row is None else int(row[0])


def complete_submission(
    session: Session,
    *,
    submission_id: int,
    worker_id: str,
    verdict: Verdict,
    compile_output: str | None,
    max_time_ms: int | None,
    max_memory_kb: int | None,
    score: int | None,
    max_score: int | None,
    test_results: list[TestResult],
) -> bool:
    """Persist a finished grade. Guarded so a reaped worker can't overwrite.

    Returns True if the result was stored, False if this worker no longer owns
    the submission (its claim was reaped and reassigned) — in which case the
    result is discarded.
    """
    stmt = (
        update(Submission)
        .where(
            Submission.id == submission_id,
            Submission.worker_id == worker_id,
            Submission.status == SubmissionStatus.RUNNING,
        )
        .values(
            status=SubmissionStatus.COMPLETED,
            verdict=verdict,
            compile_output=compile_output,
            max_time_ms=max_time_ms,
            max_memory_kb=max_memory_kb,
            score=score,
            max_score=max_score,
            completed_at=utcnow(),
        )
    )
    result = session.execute(stmt)
    if result.rowcount != 1:
        session.rollback()
        return False

    for tr in test_results:
        tr.submission_id = submission_id
        session.add(tr)
    session.commit()
    return True


def fail_submission(
    session: Session,
    *,
    submission_id: int,
    worker_id: str,
    message: str,
    cfg: Settings = settings,
) -> str:
    """Handle an infrastructure failure while grading (a SandboxError).

    Requeues the submission for another attempt if it still has budget,
    otherwise parks it in a terminal ERROR state with an IE verdict. Expressed
    as guarded SQL UPDATEs (like ``complete_submission``) so it acts only on a
    claim this worker still owns and never relies on possibly-stale ORM state.
    Returns "errored", "requeued", or "lost".
    """
    base_guard = (
        Submission.id == submission_id,
        Submission.worker_id == worker_id,
        Submission.status == SubmissionStatus.RUNNING,
    )

    errored = session.execute(
        update(Submission)
        .where(*base_guard, Submission.attempts >= cfg.max_attempts)
        .values(
            status=SubmissionStatus.ERROR,
            verdict=Verdict.IE,
            error_message=message,
            completed_at=utcnow(),
        )
    ).rowcount
    if errored:
        session.commit()
        return "errored"

    requeued = session.execute(
        update(Submission)
        .where(*base_guard)
        .values(status=SubmissionStatus.PENDING, worker_id=None, claimed_at=None)
    ).rowcount
    session.commit()
    return "requeued" if requeued else "lost"


def reap_stale_claims(session: Session, cfg: Settings = settings) -> dict[str, int]:
    """Recover submissions abandoned by crashed workers.

    A row stuck in ``running`` past the stale timeout is either requeued (if it
    has attempts left) or parked in ERROR (if it has exhausted them). Returns a
    count of each action. This is itself a single-writer statement, so it is
    safe to run concurrently with active workers.
    """
    cutoff = utcnow() - timedelta(seconds=cfg.stale_claim_timeout_s)

    errored = session.execute(
        update(Submission)
        .where(
            Submission.status == SubmissionStatus.RUNNING,
            Submission.claimed_at < cutoff,
            Submission.attempts >= cfg.max_attempts,
        )
        .values(
            status=SubmissionStatus.ERROR,
            verdict=Verdict.IE,
            error_message="exceeded max attempts (worker repeatedly failed)",
            completed_at=utcnow(),
        )
    ).rowcount

    requeued = session.execute(
        update(Submission)
        .where(
            Submission.status == SubmissionStatus.RUNNING,
            Submission.claimed_at < cutoff,
        )
        .values(status=SubmissionStatus.PENDING, worker_id=None, claimed_at=None)
    ).rowcount

    session.commit()
    return {"requeued": requeued, "errored": errored}
