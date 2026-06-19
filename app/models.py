"""SQLAlchemy ORM models — the judge's persistent state.

Four tables, mirroring the domain:

- ``Problem``      a problem statement plus its resource limits and compare mode
- ``TestCase``     one (input, expected output) pair belonging to a problem
- ``Submission``   a piece of code submitted against a problem; carries the
                   queue/lifecycle state and the final verdict
- ``TestResult``   the per-test outcome of grading a submission (timing, memory,
                   verdict), giving the detailed report the API exposes

Portability
-----------
Columns use only portable types (no SQLite- or Postgres-specific features), and
enums are stored as plain strings via ``enum_column`` so the same schema runs on
SQLite today and PostgreSQL in production. Switching is a URL change.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy import (
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.enums import CompareMode, SubmissionStatus, Verdict


def utcnow() -> datetime:
    """Timezone-aware UTC now (avoids the pitfalls of naive datetimes)."""
    return datetime.now(UTC)


def enum_column(enum_cls: type) -> SAEnum:
    """A string-backed enum column.

    ``native_enum=False`` stores the enum as VARCHAR with a CHECK constraint
    rather than a DB-native ENUM type — portable across SQLite/Postgres. The
    ``values_callable`` persists the member *value* (e.g. ``"pending"``) so the
    stored text matches exactly what the API serializes.
    """
    return SAEnum(
        enum_cls,
        native_enum=False,
        values_callable=lambda enum: [member.value for member in enum],
        validate_strings=True,
    )


class Base(DeclarativeBase):
    pass


class Problem(Base):
    __tablename__ = "problems"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(256))
    statement: Mapped[str] = mapped_column(Text, default="")

    # Resource limits applied to every test case of this problem.
    time_limit_ms: Mapped[int] = mapped_column(Integer)
    memory_limit_mb: Mapped[int] = mapped_column(Integer)
    output_limit_kb: Mapped[int] = mapped_column(Integer, default=256)

    # How a program's stdout is compared to the expected output.
    compare_mode: Mapped[CompareMode] = mapped_column(
        enum_column(CompareMode), default=CompareMode.TRIM
    )
    # Absolute/relative tolerance used only when compare_mode == FLOAT.
    float_tolerance: Mapped[float] = mapped_column(Float, default=1e-6)

    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    test_cases: Mapped[list[TestCase]] = relationship(
        back_populates="problem",
        cascade="all, delete-orphan",
        order_by="TestCase.ordinal",
    )
    submissions: Mapped[list[Submission]] = relationship(
        back_populates="problem",
        cascade="all, delete-orphan",
    )


class TestCase(Base):
    __tablename__ = "test_cases"
    # Tell pytest this domain model is not a test class (name starts with "Test").
    __test__ = False

    id: Mapped[int] = mapped_column(primary_key=True)
    problem_id: Mapped[int] = mapped_column(
        ForeignKey("problems.id", ondelete="CASCADE"), index=True
    )
    # Stable 1-based ordering; "first failing test" (fail-fast) is defined by it.
    ordinal: Mapped[int] = mapped_column(Integer)
    input_data: Mapped[str] = mapped_column(Text)
    expected_output: Mapped[str] = mapped_column(Text)
    # Sample tests may be shown to users; hidden tests are kept private.
    is_sample: Mapped[bool] = mapped_column(default=False)
    points: Mapped[int] = mapped_column(default=1)

    problem: Mapped[Problem] = relationship(back_populates="test_cases")


class Submission(Base):
    __tablename__ = "submissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    problem_id: Mapped[int] = mapped_column(
        ForeignKey("problems.id", ondelete="CASCADE"), index=True
    )
    language: Mapped[str] = mapped_column(String(32))
    source_code: Mapped[str] = mapped_column(Text)

    # --- Queue / lifecycle state -------------------------------------------
    # status is indexed because the worker claim query filters on it.
    status: Mapped[SubmissionStatus] = mapped_column(
        enum_column(SubmissionStatus), default=SubmissionStatus.PENDING, index=True
    )
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Number of times this submission has been claimed; capped so a submission
    # that repeatedly crashes its worker eventually goes to a terminal error.
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    # --- Result ------------------------------------------------------------
    verdict: Mapped[Verdict | None] = mapped_column(enum_column(Verdict), nullable=True)
    compile_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Worst-case resource usage across the tests that ran (for the summary view).
    max_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_memory_kb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Populated only when verdict == IE (the judge itself failed).
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    claimed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    problem: Mapped[Problem] = relationship(back_populates="submissions")
    test_results: Mapped[list[TestResult]] = relationship(
        back_populates="submission",
        cascade="all, delete-orphan",
        order_by="TestResult.ordinal",
    )


class TestResult(Base):
    __tablename__ = "test_results"
    # Tell pytest this domain model is not a test class (name starts with "Test").
    __test__ = False

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), index=True
    )
    test_case_id: Mapped[int | None] = mapped_column(
        ForeignKey("test_cases.id", ondelete="SET NULL"), nullable=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    verdict: Mapped[Verdict] = mapped_column(enum_column(Verdict))

    time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cpu_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_kb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    signal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # A bounded slice of stderr, useful for diagnosing RE without storing MBs.
    stderr_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)

    submission: Mapped[Submission] = relationship(back_populates="test_results")
