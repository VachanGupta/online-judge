"""The judge's core vocabulary.

These enums are subclasses of ``str`` so they serialize transparently to JSON
(FastAPI/Pydantic emit the ``value``) and read naturally in the database. The
DB columns are configured (see ``models.enum_column``) to persist the *value*
string rather than the member *name*, which keeps the stored data identical to
what the API exposes and portable across SQLite and PostgreSQL.
"""

from __future__ import annotations

import enum


class Verdict(enum.StrEnum):
    """The outcome of grading a submission (or a single test case).

    The non-AC verdicts are deliberately ordered by *diagnostic priority* in the
    classifier: a single run can trip several conditions at once (e.g. a program
    that is both slow and memory-hungry), and we report the most specific cause.
    """

    AC = "AC"  # Accepted — output matched on every test case
    WA = "WA"  # Wrong Answer — ran cleanly but produced incorrect output
    TLE = "TLE"  # Time Limit Exceeded
    MLE = "MLE"  # Memory Limit Exceeded
    RE = "RE"  # Runtime Error — non-zero exit or killed by a signal
    CE = "CE"  # Compilation Error (or syntax error for interpreted languages)
    OLE = "OLE"  # Output Limit Exceeded — wrote more than the output cap
    IE = "IE"  # Internal Error — the judge itself failed (not the submitter's fault)
    SKIPPED = "SKIPPED"  # Test not run because an earlier test already failed (fail-fast)

    @property
    def is_accepted(self) -> bool:
        return self is Verdict.AC


class SubmissionStatus(enum.StrEnum):
    """Lifecycle of a submission as it moves through the DB-backed queue."""

    PENDING = "pending"  # enqueued, awaiting a worker
    RUNNING = "running"  # claimed by a worker and currently grading
    COMPLETED = "completed"  # graded; ``verdict`` is populated
    ERROR = "error"  # the judge failed to grade it (verdict = IE)


class CompareMode(enum.StrEnum):
    """How a program's stdout is compared against the expected output.

    The implementations live in ``app.verdict``; the enum is defined here so the
    models layer can reference it without importing the comparison logic.
    """

    TRIM = "trim"  # default: trim trailing whitespace per line; ignore trailing blank lines
    EXACT = "exact"  # byte-for-byte identical
    TOKENS = "tokens"  # compare whitespace-separated tokens; ignore all whitespace runs
    FLOAT = "float"  # like TOKENS, but numeric tokens compared within a tolerance
