"""End-to-end grading through the worker's grade_one (real Docker).

Proves the whole pipeline — claim → compile → run each test → classify →
aggregate → persist — works for a real submission. The exhaustive
verdict-matrix (AC/WA/TLE/MLE/RE/CE across C++ and Python) lives in Phase 6's
integration suite; here we confirm the loop and the queue mechanics.
"""

from __future__ import annotations

import pytest

from app.enums import SubmissionStatus, Verdict
from app.models import Problem, Submission, TestCase
from app.worker import grade_one

pytestmark = pytest.mark.docker

CORRECT_SUM = "a, b = map(int, input().split())\nprint(a + b)\n"


def _a_plus_b(session) -> Problem:
    problem = Problem(
        slug="a-plus-b",
        title="A + B",
        time_limit_ms=2000,
        memory_limit_mb=128,
    )
    problem.test_cases = [
        TestCase(ordinal=1, input_data="1 2\n", expected_output="3\n", is_sample=True),
        TestCase(ordinal=2, input_data="10 20\n", expected_output="30\n"),
    ]
    session.add(problem)
    session.commit()
    return problem


def _submit(session, problem, source, language="python") -> Submission:
    sub = Submission(problem_id=problem.id, language=language, source_code=source)
    session.add(sub)
    session.commit()
    return sub


def test_correct_python_solution_is_accepted(sandbox_image, db_session):
    problem = _a_plus_b(db_session)
    sub = _submit(db_session, problem, CORRECT_SUM)

    verdict = grade_one(db_session, "w1")
    assert verdict == "AC"

    db_session.expire_all()
    graded = db_session.get(Submission, sub.id)
    assert graded.status is SubmissionStatus.COMPLETED
    assert graded.verdict is Verdict.AC
    assert len(graded.test_results) == 2
    assert all(tr.verdict is Verdict.AC for tr in graded.test_results)
    assert graded.score == graded.max_score == 2
    assert graded.max_time_ms is not None and graded.max_memory_kb > 0


def test_wrong_answer_is_reported(sandbox_image, db_session):
    problem = _a_plus_b(db_session)
    sub = _submit(db_session, problem, "print(0)\n")

    verdict = grade_one(db_session, "w1")
    assert verdict == "WA"

    db_session.expire_all()
    graded = db_session.get(Submission, sub.id)
    assert graded.verdict is Verdict.WA
    # Fail-fast: stopped at the first failing test.
    assert graded.test_results[0].verdict is Verdict.WA


def test_compile_error_short_circuits(sandbox_image, db_session):
    problem = _a_plus_b(db_session)
    sub = _submit(db_session, problem, "def broken(:\n")  # SyntaxError

    verdict = grade_one(db_session, "w1")
    assert verdict == "CE"

    db_session.expire_all()
    graded = db_session.get(Submission, sub.id)
    assert graded.verdict is Verdict.CE
    assert graded.compile_output  # carries the interpreter's diagnostic
    assert graded.test_results == []  # never ran a test


def test_grade_one_drains_then_returns_none(sandbox_image, db_session):
    problem = _a_plus_b(db_session)
    _submit(db_session, problem, CORRECT_SUM)
    _submit(db_session, problem, "print(0)\n")

    assert grade_one(db_session, "w1") == "AC"
    assert grade_one(db_session, "w1") == "WA"
    assert grade_one(db_session, "w1") is None  # queue empty
