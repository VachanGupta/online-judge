"""Validate the seed content without needing Docker or the on-disk DB."""

from __future__ import annotations

from sqlalchemy import select

from app import repository
from app.models import Problem
from scripts.seed import PROBLEMS


def test_seed_problems_are_valid_and_create(db_session):
    slugs = [repository.create_problem(db_session, factory()).slug for factory in PROBLEMS]
    assert slugs == ["a-plus-b", "sum-of-array", "count-pairs", "range-sum-queries"]


def test_range_sum_ships_a_large_tle_test(db_session):
    for factory in PROBLEMS:
        repository.create_problem(db_session, factory())
    rs = db_session.scalar(select(Problem).where(Problem.slug == "range-sum-queries"))
    # A small sample, a tiny test, and the big O(n*q)-busting test.
    assert len(rs.test_cases) == 3
    assert max(len(tc.input_data) for tc in rs.test_cases) > 1_000_000
    # Output can be multi-MB, so the cap must be raised above the default.
    assert rs.output_limit_kb >= 1024
