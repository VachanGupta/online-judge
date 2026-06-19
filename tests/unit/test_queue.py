"""Tests for the DB-backed queue: atomic claim, guarded completion, reaper.

These exercise the concurrency-correctness logic without Docker. True
multi-process contention is hard to assert deterministically in a unit test, so
we verify the invariants the design relies on: a claim flips exactly one row and
marks it running; completion/failure are guarded by worker ownership; and the
reaper recovers abandoned claims (and parks exhausted ones).
"""

from __future__ import annotations

from datetime import timedelta

from app import queue
from app.config import Settings
from app.enums import SubmissionStatus, Verdict
from app.models import Problem, Submission, TestResult, utcnow


def _problem(session) -> Problem:
    problem = Problem(slug="p", title="P", time_limit_ms=1000, memory_limit_mb=128)
    session.add(problem)
    session.commit()
    return problem


def _pending(session, problem, n=1) -> list[Submission]:
    subs = [
        Submission(problem_id=problem.id, language="python", source_code=f"# {i}") for i in range(n)
    ]
    session.add_all(subs)
    session.commit()
    return subs


def test_claim_marks_oldest_running(db_session):
    problem = _problem(db_session)
    subs = _pending(db_session, problem, n=3)

    claimed = queue.claim_next(db_session, "w1")
    assert claimed == subs[0].id  # oldest first (FIFO by id)

    db_session.expire_all()
    claimed_sub = db_session.get(Submission, claimed)
    assert claimed_sub.status is SubmissionStatus.RUNNING
    assert claimed_sub.worker_id == "w1"
    assert claimed_sub.attempts == 1


def test_claim_empty_queue_returns_none(db_session):
    _problem(db_session)
    assert queue.claim_next(db_session, "w1") is None


def test_two_claims_get_distinct_submissions(db_session):
    problem = _problem(db_session)
    _pending(db_session, problem, n=2)
    first = queue.claim_next(db_session, "w1")
    second = queue.claim_next(db_session, "w2")
    assert first is not None and second is not None and first != second
    assert queue.claim_next(db_session, "w3") is None  # only two existed


def test_complete_is_guarded_by_worker(db_session):
    problem = _problem(db_session)
    sub = _pending(db_session, problem)[0]
    queue.claim_next(db_session, "w1")  # claimed by w1

    # A different worker cannot complete it.
    stored = queue.complete_submission(
        db_session,
        submission_id=sub.id,
        worker_id="intruder",
        verdict=Verdict.AC,
        compile_output=None,
        max_time_ms=1,
        max_memory_kb=1,
        score=1,
        max_score=1,
        test_results=[],
    )
    assert stored is False
    db_session.expire_all()
    assert db_session.get(Submission, sub.id).status is SubmissionStatus.RUNNING


def test_complete_persists_results(db_session):
    problem = _problem(db_session)
    sub = _pending(db_session, problem)[0]
    queue.claim_next(db_session, "w1")

    stored = queue.complete_submission(
        db_session,
        submission_id=sub.id,
        worker_id="w1",
        verdict=Verdict.AC,
        compile_output=None,
        max_time_ms=12,
        max_memory_kb=2048,
        score=2,
        max_score=2,
        test_results=[
            TestResult(ordinal=1, verdict=Verdict.AC, time_ms=5, memory_kb=2000),
            TestResult(ordinal=2, verdict=Verdict.AC, time_ms=12, memory_kb=2048),
        ],
    )
    assert stored is True
    db_session.expire_all()
    completed = db_session.get(Submission, sub.id)
    assert completed.status is SubmissionStatus.COMPLETED
    assert completed.verdict is Verdict.AC
    assert completed.max_memory_kb == 2048
    assert len(completed.test_results) == 2


def test_fail_requeues_until_attempts_exhausted(db_session):
    cfg = Settings(max_attempts=2)
    problem = _problem(db_session)
    sub = _pending(db_session, problem)[0]

    queue.claim_next(db_session, "w1")  # attempts -> 1
    assert (
        queue.fail_submission(
            db_session, submission_id=sub.id, worker_id="w1", message="boom", cfg=cfg
        )
        == "requeued"
    )
    db_session.expire_all()
    assert db_session.get(Submission, sub.id).status is SubmissionStatus.PENDING

    queue.claim_next(db_session, "w1")  # attempts -> 2
    assert (
        queue.fail_submission(
            db_session, submission_id=sub.id, worker_id="w1", message="boom", cfg=cfg
        )
        == "errored"
    )
    db_session.expire_all()
    errored = db_session.get(Submission, sub.id)
    assert errored.status is SubmissionStatus.ERROR
    assert errored.verdict is Verdict.IE
    assert "boom" in errored.error_message


def test_reaper_requeues_stale_claim(db_session):
    cfg = Settings(stale_claim_timeout_s=300, max_attempts=3)
    problem = _problem(db_session)
    sub = _pending(db_session, problem)[0]
    queue.claim_next(db_session, "dead-worker")

    # Pretend the worker claimed it long ago and then crashed.
    db_session.get(Submission, sub.id).claimed_at = utcnow() - timedelta(seconds=400)
    db_session.commit()

    counts = queue.reap_stale_claims(db_session, cfg)
    assert counts == {"requeued": 1, "errored": 0}
    db_session.expire_all()
    assert db_session.get(Submission, sub.id).status is SubmissionStatus.PENDING


def test_reaper_parks_exhausted_claim(db_session):
    cfg = Settings(stale_claim_timeout_s=300, max_attempts=1)
    problem = _problem(db_session)
    sub = _pending(db_session, problem)[0]
    queue.claim_next(db_session, "dead-worker")  # attempts -> 1 == max

    s = db_session.get(Submission, sub.id)
    s.claimed_at = utcnow() - timedelta(seconds=400)
    db_session.commit()

    counts = queue.reap_stale_claims(db_session, cfg)
    assert counts == {"requeued": 0, "errored": 1}
    db_session.expire_all()
    assert db_session.get(Submission, sub.id).status is SubmissionStatus.ERROR
