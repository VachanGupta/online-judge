"""Exhaustive unit tests for the pure verdict engine."""

from __future__ import annotations

import pytest

from app.enums import CompareMode, Verdict
from app.runner.result import ExecutionResult
from app.verdict import aggregate, classify_compile, classify_test, compare_output


def er(**overrides) -> ExecutionResult:
    """An ExecutionResult that defaults to a clean, fast, correct-ish run."""
    base = {
        "exit_code": 0,
        "signal": None,
        "timed_out": False,
        "oom_killed": False,
        "wall_ms": 10,
        "cpu_ms": 8,
        "peak_kb": 4096,
        "stdout": b"",
        "stderr": b"",
        "stdout_truncated": False,
        "stderr_truncated": False,
    }
    base.update(overrides)
    return ExecutionResult(**base)


# --------------------------------------------------------------------------- #
# compare_output
# --------------------------------------------------------------------------- #


class TestTrimMode:
    @pytest.mark.parametrize(
        "expected,actual",
        [
            ("3\n", b"3\n"),
            ("3\n", b"3"),  # missing trailing newline
            ("3\n", b"3   \n"),  # trailing spaces
            ("3", b"3\n\n\n"),  # extra trailing blank lines
            ("a\nb\n", b"a\nb"),
            ("", b""),  # empty == empty
            ("", b"\n"),  # empty == just a newline
            ("a\nb\n", b"a\r\nb\r\n"),  # CRLF normalized
        ],
    )
    def test_matches(self, expected, actual):
        assert compare_output(expected, actual, CompareMode.TRIM) is True

    @pytest.mark.parametrize(
        "expected,actual",
        [
            ("3\n", b"4\n"),
            ("a\nb\n", b"a\nc\n"),
            ("hello", b"Hello"),  # case sensitive
            ("1 2\n", b"1  2\n"),  # interior whitespace matters in TRIM
        ],
    )
    def test_mismatches(self, expected, actual):
        assert compare_output(expected, actual, CompareMode.TRIM) is False


class TestExactMode:
    def test_byte_exact(self):
        assert compare_output("3\n", b"3\n", CompareMode.EXACT) is True

    def test_trailing_newline_matters(self):
        assert compare_output("3\n", b"3", CompareMode.EXACT) is False

    def test_crlf_not_normalized(self):
        assert compare_output("a\nb", b"a\r\nb", CompareMode.EXACT) is False


class TestTokensMode:
    @pytest.mark.parametrize(
        "expected,actual",
        [
            ("1 2 3", b"1  2   3"),  # collapse whitespace runs
            ("1 2 3", b"1\n2\n3\n"),  # newlines are whitespace too
            ("  1 2 ", b"1 2"),  # leading/trailing ignored
        ],
    )
    def test_matches(self, expected, actual):
        assert compare_output(expected, actual, CompareMode.TOKENS) is True

    def test_token_count_mismatch(self):
        assert compare_output("1 2", b"1 2 3", CompareMode.TOKENS) is False


class TestFloatMode:
    def test_within_absolute_tolerance(self):
        assert compare_output("1.0000001", b"1.0000002", CompareMode.FLOAT) is True

    def test_outside_tolerance(self):
        assert compare_output("1.0", b"1.5", CompareMode.FLOAT) is False

    def test_relative_tolerance_for_large_numbers(self):
        # diff 1000 but relative to 1e9 is within 1e-6.
        assert compare_output("1000000000", b"1000001000", CompareMode.FLOAT) is True

    def test_length_mismatch(self):
        assert compare_output("1.0 2.0", b"1.0", CompareMode.FLOAT) is False

    def test_non_numeric_token_must_match_exactly(self):
        assert compare_output("yes 1.0", b"yes 1.0000001", CompareMode.FLOAT) is True
        assert compare_output("yes 1.0", b"no 1.0", CompareMode.FLOAT) is False

    def test_custom_tolerance(self):
        assert compare_output("1.0", b"1.4", CompareMode.FLOAT, float_tolerance=0.5) is True


def test_checker_hook_overrides_mode():
    # A checker that accepts any answer whose tokens are a permutation.
    def permutation_checker(_input: str, expected: str, actual: str) -> bool:
        return sorted(expected.split()) == sorted(actual.split())

    assert compare_output("1 2 3", b"3 1 2", CompareMode.EXACT, checker=permutation_checker)
    assert not compare_output("1 2 3", b"1 2 4", CompareMode.EXACT, checker=permutation_checker)


# --------------------------------------------------------------------------- #
# classify_test
# --------------------------------------------------------------------------- #

LIMITS = {"time_limit_ms": 1000, "memory_limit_mb": 256}


def classify(result, expected=b"42\n", **kw):
    return classify_test(result, expected_output="42\n", **LIMITS, **kw)


def test_accepted():
    assert classify(er(stdout=b"42\n")) is Verdict.AC


def test_wrong_answer():
    assert classify(er(stdout=b"41\n")) is Verdict.WA


def test_runtime_error_on_signal():
    assert classify(er(exit_code=None, signal=11, stdout=b"42\n")) is Verdict.RE


def test_runtime_error_on_nonzero_exit():
    assert classify(er(exit_code=1, stdout=b"42\n")) is Verdict.RE


def test_tle_when_timed_out():
    assert classify(er(timed_out=True, exit_code=None, signal=9)) is Verdict.TLE


def test_tle_when_wall_exceeds_limit():
    # Finished on its own but over the limit -> still TLE.
    assert classify(er(wall_ms=1500, stdout=b"42\n")) is Verdict.TLE


def test_mle_when_oom_killed():
    assert classify(er(oom_killed=True, exit_code=None, signal=9)) is Verdict.MLE


def test_mle_when_peak_exceeds_limit():
    # 300 MiB peak against a 256 MiB limit, no OOM (headroom absorbed it).
    assert classify(er(peak_kb=300 * 1024, stdout=b"42\n")) is Verdict.MLE


def test_peak_at_limit_is_not_mle():
    assert classify(er(peak_kb=256 * 1024, stdout=b"42\n")) is Verdict.AC


def test_ole_when_output_truncated():
    assert classify(er(stdout_truncated=True, stdout=b"42\n")) is Verdict.OLE


def test_precedence_memory_beats_time():
    # Both slow and memory-heavy -> MLE wins (more diagnostic).
    result = er(oom_killed=True, timed_out=True, exit_code=None, signal=9)
    assert classify(result) is Verdict.MLE


def test_precedence_time_beats_runtime_error():
    # Watchdog kill shows up as SIGKILL, but timed_out makes it TLE, not RE.
    assert classify(er(timed_out=True, exit_code=None, signal=9)) is Verdict.TLE


def test_precedence_time_beats_ole():
    # A program that prints forever then is killed is TLE, not OLE.
    result = er(timed_out=True, exit_code=None, signal=9, stdout_truncated=True)
    assert classify(result) is Verdict.TLE


# --------------------------------------------------------------------------- #
# classify_compile / aggregate
# --------------------------------------------------------------------------- #


def test_compile_success_returns_none():
    assert classify_compile(er(exit_code=0)) is None


def test_compile_failure_is_ce():
    assert classify_compile(er(exit_code=1, stderr=b"error: ...")) is Verdict.CE


def test_compile_timeout_is_ce():
    assert classify_compile(er(timed_out=True, exit_code=None, signal=9)) is Verdict.CE


def test_aggregate_all_accepted():
    assert aggregate([Verdict.AC, Verdict.AC, Verdict.AC]) is Verdict.AC


def test_aggregate_reports_first_failure():
    assert aggregate([Verdict.AC, Verdict.WA, Verdict.TLE]) is Verdict.WA


def test_aggregate_empty_is_accepted():
    assert aggregate([]) is Verdict.AC
