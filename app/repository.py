"""Data-access helpers — the thin layer between the API and the ORM.

Keeping CRUD here (rather than inline in the endpoints) means the queue and
worker can reuse the same helpers, and the API handlers stay focused on
HTTP concerns.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.models import Problem, Submission, TestCase
from app.schemas import ProblemCreate, SubmissionCreate


class DuplicateSlugError(ValueError):
    """A problem with the requested slug already exists."""


def create_problem(session: Session, data: ProblemCreate) -> Problem:
    if session.scalar(select(Problem).where(Problem.slug == data.slug)):
        raise DuplicateSlugError(data.slug)

    problem = Problem(
        slug=data.slug,
        title=data.title,
        statement=data.statement,
        time_limit_ms=data.time_limit_ms or settings.default_time_limit_ms,
        memory_limit_mb=data.memory_limit_mb or settings.default_memory_limit_mb,
        output_limit_kb=data.output_limit_kb or settings.default_output_limit_kb,
        compare_mode=data.compare_mode,
        float_tolerance=data.float_tolerance,
    )
    # Test cases get a stable 1-based ordinal in submission order; this ordering
    # is what "the first failing test" refers to under fail-fast grading.
    problem.test_cases = [
        TestCase(
            ordinal=index,
            input_data=tc.input_data,
            expected_output=tc.expected_output,
            is_sample=tc.is_sample,
            points=tc.points,
        )
        for index, tc in enumerate(data.test_cases, start=1)
    ]
    session.add(problem)
    session.commit()
    session.refresh(problem)
    return problem


def list_problems(session: Session) -> list[Problem]:
    return list(
        session.scalars(
            select(Problem).options(selectinload(Problem.test_cases)).order_by(Problem.id)
        )
    )


def get_problem(session: Session, problem_id: int) -> Problem | None:
    return session.scalar(
        select(Problem).options(selectinload(Problem.test_cases)).where(Problem.id == problem_id)
    )


def create_submission(session: Session, data: SubmissionCreate) -> Submission:
    submission = Submission(
        problem_id=data.problem_id,
        language=data.language,
        source_code=data.source_code,
    )
    session.add(submission)
    session.commit()
    session.refresh(submission)
    return submission


def get_submission(session: Session, submission_id: int) -> Submission | None:
    return session.scalar(
        select(Submission)
        .options(selectinload(Submission.test_results))
        .where(Submission.id == submission_id)
    )


def list_submissions(
    session: Session,
    *,
    problem_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[Submission]:
    query = select(Submission).order_by(Submission.id.desc()).limit(limit)
    if problem_id is not None:
        query = query.where(Submission.problem_id == problem_id)
    if status is not None:
        query = query.where(Submission.status == status)
    return list(session.scalars(query))
