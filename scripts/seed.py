"""Seed the database with example problems.

Run with ``python -m scripts.seed`` (or the ``oj-seed`` console script). It is
idempotent: problems whose slug already exists are skipped.

Includes four problems, among them the headline demonstration that the time
limit actually bites — ``range-sum-queries`` ships a large test on which a naive
O(n·q) solution exceeds the limit while the O(n+q) prefix-sum solution passes
(see ``examples/solutions/range_sum/``).
"""

from __future__ import annotations

from app.db import SessionLocal, init_db
from app.repository import DuplicateSlugError, create_problem
from app.schemas import ProblemCreate, TestCaseIn


def _a_plus_b() -> ProblemCreate:
    return ProblemCreate(
        slug="a-plus-b",
        title="A + B",
        statement="Read two integers a and b on one line; output a + b.",
        time_limit_ms=1000,
        memory_limit_mb=128,
        test_cases=[
            TestCaseIn(input_data="1 2\n", expected_output="3\n", is_sample=True),
            TestCaseIn(input_data="100 200\n", expected_output="300\n"),
            TestCaseIn(input_data="-5 5\n", expected_output="0\n"),
            TestCaseIn(
                input_data="1000000000 1000000000\n",
                expected_output="2000000000\n",
            ),
        ],
    )


def _sum_of_array() -> ProblemCreate:
    return ProblemCreate(
        slug="sum-of-array",
        title="Sum of an Array",
        statement=(
            "First line: n. Second line: n integers. Output their sum.\n"
            "Constraints: 1 <= n <= 10^5; |a_i| <= 10^9."
        ),
        time_limit_ms=1000,
        memory_limit_mb=256,
        test_cases=[
            TestCaseIn(input_data="3\n1 2 3\n", expected_output="6\n", is_sample=True),
            TestCaseIn(input_data="1\n-5\n", expected_output="-5\n"),
            TestCaseIn(input_data="5\n1 1 1 1 1\n", expected_output="5\n"),
        ],
    )


def _count_pairs() -> ProblemCreate:
    return ProblemCreate(
        slug="count-pairs",
        title="Count Pairs With Given Sum",
        statement=(
            "First line: n and target. Second line: n integers. Output the number "
            "of unordered pairs (i, j) with i < j and a_i + a_j == target.\n"
            "The naive solution is O(n^2); an O(n) hash-map solution exists. This "
            "problem is also the worked example for the stress-test mode."
        ),
        time_limit_ms=1000,
        memory_limit_mb=256,
        test_cases=[
            TestCaseIn(input_data="4 6\n1 5 3 3\n", expected_output="2\n", is_sample=True),
            TestCaseIn(input_data="5 10\n5 5 5 5 5\n", expected_output="10\n"),
            TestCaseIn(input_data="3 100\n1 2 3\n", expected_output="0\n"),
        ],
    )


def _range_sum_queries() -> ProblemCreate:
    # A large worst-case test: n = q = 100000 with full-width [1, n] queries, so
    # a naive O(n*q) solution performs ~1e10 operations (TLE) while the O(n+q)
    # prefix-sum solution is instant. Output can be several MB, so the output
    # cap is raised accordingly.
    n = q = 100_000
    a = [(i % 1000) + 1 for i in range(1, n + 1)]
    total = sum(a)
    big_input = "\n".join([f"{n} {q}", " ".join(map(str, a)), *([f"1 {n}"] * q)]) + "\n"
    big_output = "\n".join([str(total)] * q) + "\n"

    return ProblemCreate(
        slug="range-sum-queries",
        title="Range Sum Queries (TLE demo)",
        statement=(
            "First line: n and q. Second line: n integers. Next q lines: l r "
            "(1-indexed, inclusive). For each query output the sum of a[l..r].\n"
            "A naive per-query scan is O(n*q) and will TLE on the large test; "
            "prefix sums give O(n+q)."
        ),
        time_limit_ms=1000,
        memory_limit_mb=256,
        output_limit_kb=8192,
        test_cases=[
            TestCaseIn(
                input_data="5 2\n1 2 3 4 5\n1 5\n2 4\n",
                expected_output="15\n9\n",
                is_sample=True,
            ),
            TestCaseIn(input_data="1 1\n7\n1 1\n", expected_output="7\n"),
            TestCaseIn(input_data=big_input, expected_output=big_output),
        ],
    )


PROBLEMS = [_a_plus_b, _sum_of_array, _count_pairs, _range_sum_queries]


def main() -> None:
    init_db()
    session = SessionLocal()
    created, skipped = [], []
    try:
        for factory in PROBLEMS:
            payload = factory()
            try:
                problem = create_problem(session, payload)
                created.append(problem.slug)
            except DuplicateSlugError:
                skipped.append(payload.slug)
    finally:
        session.close()

    if created:
        print(f"Created: {', '.join(created)}")
    if skipped:
        print(f"Skipped (already exist): {', '.join(skipped)}")
    if not created and not skipped:  # pragma: no cover
        print("Nothing to seed.")


if __name__ == "__main__":
    main()
