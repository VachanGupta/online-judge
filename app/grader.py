"""The grader: turn a submission into a verdict.

This is the worker's workhorse. Given a submission and its problem, it:

1. writes the source into a fresh per-submission scratch directory,
2. compiles it in the sandbox (a syntax check for interpreted languages), short-
   circuiting to **CE** on failure,
3. runs it against each test case in the sandbox, classifying each result, and
4. aggregates a final verdict (fail-fast by default).

It returns a plain :class:`GradeResult` (no ORM, no DB) so it stays easy to test;
the worker is responsible for persistence. Compilation happens once (the
artifact is reused across tests); each test runs in its own fresh container so
no state leaks between tests. The scratch directory is always removed, even on
failure. A :class:`~app.runner.result.SandboxError` propagates out for the
worker to handle as an infrastructure failure (IE / retry).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from app.config import Settings, settings
from app.enums import Verdict
from app.languages import get_language
from app.models import Problem, Submission
from app.runner.result import ExecutionResult
from app.runner.sandbox import SandboxSpec, run_in_sandbox
from app.verdict import aggregate, classify_compile, classify_test

INPUT_FILENAME = "input.txt"


@dataclass
class TestOutcome:
    ordinal: int
    test_case_id: int | None
    verdict: Verdict
    time_ms: int
    cpu_ms: int
    memory_kb: int
    exit_code: int | None
    signal: int | None
    stderr_snippet: str | None


@dataclass
class GradeResult:
    verdict: Verdict
    compile_output: str | None = None
    max_time_ms: int | None = None
    max_memory_kb: int | None = None
    score: int = 0
    max_score: int = 0
    tests: list[TestOutcome] = field(default_factory=list)


def grade_submission(
    submission: Submission, problem: Problem, cfg: Settings = settings
) -> GradeResult:
    language = get_language(submission.language)
    run_dir = _make_scratch_dir(submission.id, cfg)
    try:
        (run_dir / language.source_filename).write_text(submission.source_code)

        # --- compile / syntax-check (short-circuits to CE) ------------------
        if language.compile_cmd is not None:
            compile_result = run_in_sandbox(
                SandboxSpec(
                    workdir_host=str(run_dir),
                    command=language.compile_cmd,
                    time_limit_ms=cfg.compile_time_limit_ms,
                    memory_limit_mb=cfg.compile_memory_limit_mb,
                    output_limit_bytes=cfg.compile_output_limit_kb * 1024,
                    writable=True,  # the compiler needs to emit its artifact
                ),
                cfg,
            )
            if classify_compile(compile_result) is Verdict.CE:
                return GradeResult(
                    verdict=Verdict.CE,
                    compile_output=_compile_message(compile_result),
                    max_score=sum(tc.points for tc in problem.test_cases),
                )

        # --- run each test case --------------------------------------------
        return _run_tests(submission, problem, language, run_dir, cfg)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def _run_tests(submission, problem, language, run_dir, cfg) -> GradeResult:
    test_cases = sorted(problem.test_cases, key=lambda tc: tc.ordinal)
    max_score = sum(tc.points for tc in test_cases)
    outcomes: list[TestOutcome] = []
    score = 0
    max_time_ms = 0
    max_memory_kb = 0

    for test_case in test_cases:
        (run_dir / INPUT_FILENAME).write_text(test_case.input_data)
        result = run_in_sandbox(
            SandboxSpec(
                workdir_host=str(run_dir),
                command=language.run_cmd,
                time_limit_ms=problem.time_limit_ms,
                memory_limit_mb=problem.memory_limit_mb,
                output_limit_bytes=problem.output_limit_kb * 1024,
                stdin_filename=INPUT_FILENAME,
                writable=False,  # graded runs get a read-only work dir
            ),
            cfg,
        )
        verdict = classify_test(
            result,
            time_limit_ms=problem.time_limit_ms,
            memory_limit_mb=problem.memory_limit_mb,
            expected_output=test_case.expected_output,
            compare_mode=problem.compare_mode,
            float_tolerance=problem.float_tolerance,
            input_data=test_case.input_data,
        )
        outcomes.append(_to_outcome(test_case, verdict, result))
        max_time_ms = max(max_time_ms, result.wall_ms)
        max_memory_kb = max(max_memory_kb, result.peak_kb)
        if verdict is Verdict.AC:
            score += test_case.points
        elif cfg.fail_fast:
            break  # standard judge behaviour: stop at the first failing test

    return GradeResult(
        verdict=aggregate(o.verdict for o in outcomes),
        max_time_ms=max_time_ms,
        max_memory_kb=max_memory_kb,
        score=score,
        max_score=max_score,
        tests=outcomes,
    )


def _to_outcome(test_case, verdict: Verdict, result: ExecutionResult) -> TestOutcome:
    return TestOutcome(
        ordinal=test_case.ordinal,
        test_case_id=test_case.id,
        verdict=verdict,
        time_ms=result.wall_ms,
        cpu_ms=result.cpu_ms,
        memory_kb=result.peak_kb,
        exit_code=result.exit_code,
        signal=result.signal,
        stderr_snippet=result.stderr_snippet() or None,
    )


def _compile_message(result: ExecutionResult) -> str:
    if result.timed_out:
        return "Compilation timed out.\n" + result.stderr_snippet()
    if result.oom_killed:
        return "Compilation exceeded the memory limit.\n" + result.stderr_snippet()
    message = result.stderr_snippet()
    return message or "Compilation failed."


def _make_scratch_dir(submission_id: int, cfg: Settings) -> Path:
    root = Path(cfg.run_root)
    root.mkdir(parents=True, exist_ok=True)
    run_dir = root / f"sub-{submission_id}-{uuid4().hex[:8]}"
    run_dir.mkdir()
    # World-accessible so the container's non-root user (uid 1000) can read the
    # source/input (and write the compile artifact) regardless of the host uid
    # the worker runs as. The directory is ephemeral and removed after grading.
    os.chmod(run_dir, 0o777)
    return run_dir
