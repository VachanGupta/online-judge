"""The verdict matrix: submit known-good/known-bad solutions, assert the verdict.

This is the proof the judge works end to end. Each case takes a real solution
file from ``examples/solutions/``, submits it against a real problem, grades it
in the Docker sandbox, and checks the verdict — across both C++ and Python and
across every verdict the judge can issue (AC/WA/TLE/MLE/RE/CE).

The TLE case is the headline demonstration that the *time limit* (not language
speed) does the work: the same problem, in the same language, passes with an
O(n+q) algorithm and fails with an O(n·q) one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.enums import SubmissionStatus, Verdict
from app.models import Problem, Submission, TestCase
from app.worker import grade_one

pytestmark = pytest.mark.docker

SOLUTIONS = Path(__file__).resolve().parents[2] / "examples" / "solutions"


def _read(problem: str, filename: str) -> str:
    return (SOLUTIONS / problem / filename).read_text()


def _a_plus_b(session) -> Problem:
    problem = Problem(slug="a-plus-b", title="A + B", time_limit_ms=2000, memory_limit_mb=256)
    problem.test_cases = [
        TestCase(ordinal=1, input_data="1 2\n", expected_output="3\n"),
        TestCase(ordinal=2, input_data="10 20\n", expected_output="30\n"),
    ]
    session.add(problem)
    session.commit()
    return problem


def _grade(session, problem: Problem, filename: str, language: str) -> Submission:
    sub = Submission(
        problem_id=problem.id,
        language=language,
        source_code=_read("a_plus_b", filename),
    )
    session.add(sub)
    session.commit()
    grade_one(session, "w1")
    session.expire_all()
    return session.get(Submission, sub.id)


@pytest.mark.parametrize(
    "filename,language,expected",
    [
        ("ac.py", "python", Verdict.AC),
        ("ac.cpp", "cpp", Verdict.AC),
        ("wa.py", "python", Verdict.WA),
        ("ce.py", "python", Verdict.CE),
        ("ce.cpp", "cpp", Verdict.CE),
        ("re.py", "python", Verdict.RE),
        ("re.cpp", "cpp", Verdict.RE),
        ("mle.cpp", "cpp", Verdict.MLE),
    ],
)
def test_a_plus_b_verdict_matrix(sandbox_image, db_session, filename, language, expected):
    problem = _a_plus_b(db_session)
    graded = _grade(db_session, problem, filename, language)
    assert graded.status is SubmissionStatus.COMPLETED
    assert graded.verdict is expected


# --------------------------------------------------------------------------- #
# The TLE demonstration: O(n+q) passes, O(n*q) fails on the same large input.
# --------------------------------------------------------------------------- #


def _range_sum_problem(session, *, n=100_000, q=100_000) -> Problem:
    a = [(i % 1000) + 1 for i in range(1, n + 1)]
    total = sum(a)
    big_input = "\n".join([f"{n} {q}", " ".join(map(str, a)), *([f"1 {n}"] * q)]) + "\n"
    big_output = "\n".join([str(total)] * q) + "\n"

    problem = Problem(
        slug="range-sum-queries",
        title="Range Sum Queries",
        time_limit_ms=1000,
        memory_limit_mb=256,
        output_limit_kb=8192,
    )
    problem.test_cases = [
        TestCase(ordinal=1, input_data="5 2\n1 2 3 4 5\n1 5\n2 4\n", expected_output="15\n9\n"),
        TestCase(ordinal=2, input_data=big_input, expected_output=big_output),
    ]
    session.add(problem)
    session.commit()
    return problem


def _grade_range_sum(session, problem, filename) -> Submission:
    sub = Submission(
        problem_id=problem.id, language="cpp", source_code=_read("range_sum", filename)
    )
    session.add(sub)
    session.commit()
    grade_one(session, "w1")
    session.expire_all()
    return session.get(Submission, sub.id)


@pytest.mark.slow
def test_prefix_sum_solution_accepted(sandbox_image, db_session):
    problem = _range_sum_problem(db_session)
    graded = _grade_range_sum(db_session, problem, "ac.cpp")
    assert graded.verdict is Verdict.AC


@pytest.mark.slow
def test_naive_solution_times_out(sandbox_image, db_session):
    problem = _range_sum_problem(db_session)
    graded = _grade_range_sum(db_session, problem, "tle.cpp")
    assert graded.verdict is Verdict.TLE
