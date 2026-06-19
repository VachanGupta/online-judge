"""Smoke tests for the ORM layer: relationships, cascades, and enum storage."""

from __future__ import annotations

from sqlalchemy import select

from app.enums import CompareMode, SubmissionStatus, Verdict
from app.models import Problem, Submission, TestCase, TestResult


def _make_problem(**overrides) -> Problem:
    defaults = {
        "slug": "a-plus-b",
        "title": "A + B",
        "statement": "Read two integers, print their sum.",
        "time_limit_ms": 1000,
        "memory_limit_mb": 256,
    }
    defaults.update(overrides)
    return Problem(**defaults)


def test_problem_with_test_cases_round_trips(db_session):
    problem = _make_problem()
    problem.test_cases = [
        TestCase(ordinal=1, input_data="1 2\n", expected_output="3\n", is_sample=True),
        TestCase(ordinal=2, input_data="10 20\n", expected_output="30\n"),
    ]
    db_session.add(problem)
    db_session.commit()

    fetched = db_session.scalar(select(Problem).where(Problem.slug == "a-plus-b"))
    assert fetched is not None
    assert fetched.compare_mode is CompareMode.TRIM  # default
    assert len(fetched.test_cases) == 2
    # Relationship is ordered by ordinal.
    assert [tc.ordinal for tc in fetched.test_cases] == [1, 2]
    assert fetched.test_cases[0].is_sample is True


def test_submission_defaults_to_pending(db_session):
    problem = _make_problem()
    db_session.add(problem)
    db_session.commit()

    submission = Submission(
        problem_id=problem.id,
        language="python",
        source_code="print(sum(map(int, input().split())))",
    )
    db_session.add(submission)
    db_session.commit()

    assert submission.status is SubmissionStatus.PENDING
    assert submission.verdict is None
    assert submission.attempts == 0


def test_enum_columns_persist_as_values(db_session):
    """The DB should store the enum *value* string, matching the API output."""
    problem = _make_problem()
    db_session.add(problem)
    db_session.commit()

    submission = Submission(
        problem_id=problem.id,
        language="cpp",
        source_code="int main(){}",
        status=SubmissionStatus.COMPLETED,
        verdict=Verdict.AC,
    )
    db_session.add(submission)
    db_session.commit()

    # Read the raw stored text to confirm it's the value, not the member name.
    raw_status = db_session.execute(
        select(Submission.status).where(Submission.id == submission.id)
    ).scalar_one()
    assert raw_status is SubmissionStatus.COMPLETED
    assert SubmissionStatus.COMPLETED.value == "completed"


def test_cascade_delete_removes_children(db_session):
    problem = _make_problem()
    problem.test_cases = [TestCase(ordinal=1, input_data="x", expected_output="y")]
    db_session.add(problem)
    db_session.commit()

    submission = Submission(problem_id=problem.id, language="python", source_code="x")
    submission.test_results = [TestResult(ordinal=1, verdict=Verdict.AC, time_ms=5)]
    db_session.add(submission)
    db_session.commit()

    db_session.delete(problem)
    db_session.commit()

    assert db_session.scalar(select(TestCase)) is None
    assert db_session.scalar(select(Submission)) is None
    assert db_session.scalar(select(TestResult)) is None
