"""The verdict engine: pure output comparison and verdict classification.

This module is deliberately **pure** — it consumes facts (an
:class:`~app.runner.result.ExecutionResult`, the problem's limits, the expected
output) and returns a :class:`~app.enums.Verdict`. It performs no I/O, reads no
clock, and has no Docker dependency, so its many edge cases can be unit-tested
exhaustively and cheaply. All the messy non-determinism lives upstream in the
runner; the classification of those facts is deterministic.

Two responsibilities:

1. ``compare_output`` — decide whether a program's stdout matches the expected
   output under a given :class:`~app.enums.CompareMode`.
2. ``classify_test`` / ``classify_compile`` / ``aggregate`` — turn execution
   facts into per-test and overall verdicts.

See ARCHITECTURE.md §3.2 for the rationale behind the precedence order and the
comparison policies.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from app.enums import CompareMode, Verdict
from app.runner.result import ExecutionResult

# A custom checker for problems with multiple valid answers (e.g. "output any
# shortest path"). It receives the test input, the reference output, and the
# program's actual output, and returns True iff the answer is acceptable.
# Checkers are trusted/admin-supplied (problem-setter code), not submitter code.
Checker = Callable[[str, str, str], bool]


# --------------------------------------------------------------------------- #
# Output comparison
# --------------------------------------------------------------------------- #


def _normalize_newlines(text: str) -> str:
    """Map CRLF and lone CR to LF so Windows submissions aren't penalized."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _trimmed_lines(text: str) -> list[str]:
    """Split into lines, strip trailing whitespace per line, drop trailing blanks.

    This makes ``"3\\n"`` and ``"3"`` and ``"3   \\n\\n"`` all equal, and makes
    the empty string equal to ``"\\n"`` — the common benign differences.
    """
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _tokens_equal(expected: str, actual: str) -> bool:
    """Whitespace-insensitive: compare the sequences of whitespace-split tokens."""
    return expected.split() == actual.split()


def _floats_equal(expected: str, actual: str, tolerance: float) -> bool:
    """Token compare where numeric tokens match within an abs/rel tolerance."""
    exp_tokens = expected.split()
    act_tokens = actual.split()
    if len(exp_tokens) != len(act_tokens):
        return False
    for exp_tok, act_tok in zip(exp_tokens, act_tokens, strict=True):  # lengths checked above
        if exp_tok == act_tok:
            continue
        try:
            exp_val, act_val = float(exp_tok), float(act_tok)
        except ValueError:
            return False  # a non-numeric token that differs textually -> mismatch
        diff = abs(exp_val - act_val)
        if diff <= tolerance or diff <= tolerance * abs(exp_val):
            continue
        return False
    return True


def compare_output(
    expected: str,
    actual: bytes,
    mode: CompareMode = CompareMode.TRIM,
    *,
    float_tolerance: float = 1e-6,
    input_data: str = "",
    checker: Checker | None = None,
) -> bool:
    """Return True iff ``actual`` is an acceptable answer for ``expected``.

    ``actual`` is the program's raw stdout bytes (preserving byte fidelity); all
    text modes decode as UTF-8 with replacement. ``EXACT`` compares bytes.
    """
    if checker is not None:
        text = actual.decode("utf-8", errors="replace")
        return checker(input_data, expected, text)

    if mode is CompareMode.EXACT:
        return expected.encode("utf-8") == actual

    expected_text = _normalize_newlines(expected)
    actual_text = _normalize_newlines(actual.decode("utf-8", errors="replace"))

    if mode is CompareMode.TRIM:
        return _trimmed_lines(expected_text) == _trimmed_lines(actual_text)
    if mode is CompareMode.TOKENS:
        return _tokens_equal(expected_text, actual_text)
    if mode is CompareMode.FLOAT:
        return _floats_equal(expected_text, actual_text, float_tolerance)

    raise ValueError(f"unknown compare mode: {mode!r}")


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


def classify_test(
    result: ExecutionResult,
    *,
    time_limit_ms: int,
    memory_limit_mb: int,
    expected_output: str,
    compare_mode: CompareMode = CompareMode.TRIM,
    float_tolerance: float = 1e-6,
    input_data: str = "",
    checker: Checker | None = None,
) -> Verdict:
    """Classify a single sandboxed run into a per-test verdict.

    Precedence (see ARCHITECTURE.md §3.2): **MLE → TLE → RE → OLE → AC/WA**.
    Memory evidence beats time because an over-limit program is over-limit
    regardless of how slow it was, and it's the more diagnostic verdict; the
    wall-clock watchdog and the kernel OOM killer both surface as SIGKILL, so we
    rely on the explicit ``oom_killed`` / ``timed_out`` flags, never on decoding
    exit code 137 by hand.
    """
    memory_limit_kb = memory_limit_mb * 1024

    # 1. Memory: a kernel OOM kill, or a measured peak past the announced limit.
    if result.oom_killed or (result.peak_kb and result.peak_kb > memory_limit_kb):
        return Verdict.MLE

    # 2. Time: our watchdog had to kill it, or it ran past the limit.
    if result.timed_out or result.wall_ms > time_limit_ms:
        return Verdict.TLE

    # 3. Runtime error: died by signal, or exited non-zero.
    if result.signal is not None or (result.exit_code is not None and result.exit_code != 0):
        return Verdict.RE

    # 4. Output limit: exited cleanly but produced more than the output cap.
    if result.stdout_truncated:
        return Verdict.OLE

    # 5. Compare the (clean, complete) output.
    matched = compare_output(
        expected_output,
        result.stdout,
        compare_mode,
        float_tolerance=float_tolerance,
        input_data=input_data,
        checker=checker,
    )
    return Verdict.AC if matched else Verdict.WA


def classify_compile(result: ExecutionResult) -> Verdict | None:
    """Return ``CE`` if the compile step failed, else ``None`` (compiled OK).

    Any non-clean exit counts as a compilation failure — including a compile
    that timed out or OOM'd (a template/constexpr bomb). The grader attaches the
    captured compiler diagnostics as ``compile_output``.
    """
    if result.exited_cleanly:
        return None
    return Verdict.CE


def aggregate(per_test: Iterable[Verdict]) -> Verdict:
    """Combine per-test verdicts into the submission verdict.

    AC iff every test is AC; otherwise the first non-AC verdict in test order
    (which, with fail-fast grading, is the test that stopped the run). An empty
    set of tests is treated as AC (nothing failed).
    """
    for verdict in per_test:
        if verdict is not Verdict.AC:
            return verdict
    return Verdict.AC
