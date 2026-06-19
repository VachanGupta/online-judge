"""The FastAPI application: problem management and the submission lifecycle.

Endpoints (see README for examples and a request→verdict walkthrough):

- ``GET  /health``           liveness probe
- ``GET  /languages``        supported submission languages
- ``POST /problems``         create a problem with its test cases and limits
- ``GET  /problems``         list problems
- ``GET  /problems/{id}``    problem detail (sample test cases only)
- ``POST /submissions``      submit a solution (enqueued; graded asynchronously)
- ``GET  /submissions/{id}`` poll status + verdict + per-test report
- ``GET  /submissions``      list submissions

The API only *enqueues* submissions (writes a ``pending`` row); a separate pool
of worker processes (see ``app.worker``) claims and grades them. This keeps the
request path fast and the judging capacity independently scalable.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, status
from sqlalchemy.orm import Session

from app import repository
from app.db import get_db, init_db
from app.languages import supported_languages
from app.models import Problem, Submission
from app.schemas import (
    LanguagesOut,
    ProblemCreate,
    ProblemDetail,
    ProblemSummary,
    StressRequest,
    StressResponse,
    SubmissionCreate,
    SubmissionCreated,
    SubmissionOut,
    SubmissionSummary,
    TestCaseOut,
)
from app.stress import ProgramSpec, StressParams, run_stress


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Create the schema on startup so the service is usable out of the box.
    init_db()
    yield


app = FastAPI(
    title="Online Judge",
    version="0.1.0",
    summary="Sandboxed, resource-limited automated code grading.",
    lifespan=lifespan,
)

# A request-scoped database session, the modern FastAPI dependency style.
DbSession = Annotated[Session, Depends(get_db)]


# --------------------------------------------------------------------------- #
# Response builders (handle the one computed field; everything else maps 1:1)
# --------------------------------------------------------------------------- #


def _summary(problem: Problem) -> ProblemSummary:
    return ProblemSummary(
        id=problem.id,
        slug=problem.slug,
        title=problem.title,
        time_limit_ms=problem.time_limit_ms,
        memory_limit_mb=problem.memory_limit_mb,
        output_limit_kb=problem.output_limit_kb,
        compare_mode=problem.compare_mode,
        num_test_cases=len(problem.test_cases),
    )


def _detail(problem: Problem) -> ProblemDetail:
    return ProblemDetail(
        **_summary(problem).model_dump(),
        statement=problem.statement,
        float_tolerance=problem.float_tolerance,
        sample_test_cases=[
            TestCaseOut.model_validate(tc) for tc in problem.test_cases if tc.is_sample
        ],
    )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/languages", response_model=LanguagesOut)
def languages() -> LanguagesOut:
    return LanguagesOut(languages=supported_languages())


@app.post("/problems", response_model=ProblemSummary, status_code=status.HTTP_201_CREATED)
def create_problem(payload: ProblemCreate, db: DbSession) -> ProblemSummary:
    try:
        problem = repository.create_problem(db, payload)
    except repository.DuplicateSlugError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a problem with slug {payload.slug!r} already exists",
        ) from None
    return _summary(problem)


@app.get("/problems", response_model=list[ProblemSummary])
def list_problems(db: DbSession) -> list[ProblemSummary]:
    return [_summary(p) for p in repository.list_problems(db)]


@app.get("/problems/{problem_id}", response_model=ProblemDetail)
def get_problem(problem_id: int, db: DbSession) -> ProblemDetail:
    problem = repository.get_problem(db, problem_id)
    if problem is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="problem not found")
    return _detail(problem)


@app.post(
    "/submissions",
    response_model=SubmissionCreated,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_submission(payload: SubmissionCreate, db: DbSession) -> SubmissionCreated:
    if repository.get_problem(db, payload.problem_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"problem {payload.problem_id} not found",
        )
    submission = repository.create_submission(db, payload)
    return SubmissionCreated(id=submission.id, status=submission.status)


@app.get("/submissions/{submission_id}", response_model=SubmissionOut)
def get_submission(submission_id: int, db: DbSession) -> Submission:
    submission = repository.get_submission(db, submission_id)
    if submission is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="submission not found")
    return submission


@app.get("/submissions", response_model=list[SubmissionSummary])
def list_submissions(
    db: DbSession,
    problem_id: Annotated[int | None, Query()] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[Submission]:
    return repository.list_submissions(db, problem_id=problem_id, status=status_filter, limit=limit)


@app.post("/stress-test", response_model=StressResponse)
def stress_test(payload: StressRequest) -> StressResponse:
    """Find a minimal input on which the optimized solution disagrees with the
    brute-force one. Runs synchronously (it spawns many sandboxed containers);
    iterations and size are bounded by the request schema. A sync handler keeps
    the blocking Docker work off the event loop (FastAPI runs it in a threadpool).
    """
    result = run_stress(
        ProgramSpec(payload.brute_source, payload.brute_language),
        ProgramSpec(payload.optimized_source, payload.optimized_language),
        ProgramSpec(payload.generator_source, payload.generator_language),
        StressParams(
            iterations=payload.iterations,
            size=payload.size,
            time_limit_ms=payload.time_limit_ms,
            memory_limit_mb=payload.memory_limit_mb,
            compare_mode=payload.compare_mode,
        ),
    )
    return StressResponse(**result.to_dict())


def run() -> None:  # pragma: no cover - thin uvicorn entry point
    """Entry point for the ``oj-api`` console script / ``python -m app.main``."""
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":  # pragma: no cover
    run()
