"""API tests via FastAPI's TestClient.

These exercise the HTTP contract end to end against an in-memory DB. They do not
grade anything (that's the worker's job, tested in Phase 5/6) — a submission
here simply lands in the queue as ``pending``.
"""

from __future__ import annotations

import pytest

A_PLUS_B = {
    "slug": "a-plus-b",
    "title": "A + B",
    "statement": "Read two integers, output their sum.",
    "time_limit_ms": 1000,
    "memory_limit_mb": 128,
    "test_cases": [
        {"input_data": "1 2\n", "expected_output": "3\n", "is_sample": True},
        {"input_data": "10 20\n", "expected_output": "30\n"},
    ],
}


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_languages_lists_cpp_and_python(client):
    langs = client.get("/languages").json()["languages"]
    assert "cpp" in langs and "python" in langs


def test_create_and_fetch_problem(client):
    resp = client.post("/problems", json=A_PLUS_B)
    assert resp.status_code == 201
    summary = resp.json()
    assert summary["slug"] == "a-plus-b"
    assert summary["num_test_cases"] == 2
    assert summary["time_limit_ms"] == 1000

    detail = client.get(f"/problems/{summary['id']}").json()
    assert detail["statement"].startswith("Read two integers")
    # Only the sample test case is exposed; the hidden one is not.
    assert len(detail["sample_test_cases"]) == 1
    assert detail["sample_test_cases"][0]["input_data"] == "1 2\n"


def test_problem_limits_default_when_omitted(client):
    minimal = {
        "slug": "defaults",
        "title": "Defaults",
        "test_cases": [{"input_data": "x", "expected_output": "y"}],
    }
    summary = client.post("/problems", json=minimal).json()
    # Falls back to the server-configured defaults (see app.config).
    assert summary["time_limit_ms"] == 2000
    assert summary["memory_limit_mb"] == 256


def test_duplicate_slug_conflicts(client):
    client.post("/problems", json=A_PLUS_B)
    resp = client.post("/problems", json=A_PLUS_B)
    assert resp.status_code == 409


def test_problem_requires_at_least_one_test_case(client):
    bad = {"slug": "empty", "title": "Empty", "test_cases": []}
    assert client.post("/problems", json=bad).status_code == 422


def test_invalid_slug_rejected(client):
    bad = {**A_PLUS_B, "slug": "Has Spaces"}
    assert client.post("/problems", json=bad).status_code == 422


def test_get_missing_problem_404(client):
    assert client.get("/problems/999").status_code == 404


def test_submit_and_poll(client):
    problem_id = client.post("/problems", json=A_PLUS_B).json()["id"]
    resp = client.post(
        "/submissions",
        json={
            "problem_id": problem_id,
            "language": "python",
            "source_code": "print(sum(map(int, input().split())))",
        },
    )
    assert resp.status_code == 202
    created = resp.json()
    assert created["status"] == "pending"

    polled = client.get(f"/submissions/{created['id']}").json()
    assert polled["status"] == "pending"
    assert polled["verdict"] is None
    assert polled["test_results"] == []
    assert polled["language"] == "python"


def test_submit_to_missing_problem_404(client):
    resp = client.post(
        "/submissions",
        json={"problem_id": 999, "language": "python", "source_code": "x=1"},
    )
    assert resp.status_code == 404


def test_submit_unknown_language_422(client):
    problem_id = client.post("/problems", json=A_PLUS_B).json()["id"]
    resp = client.post(
        "/submissions",
        json={"problem_id": problem_id, "language": "brainfuck", "source_code": "+"},
    )
    assert resp.status_code == 422


def test_list_submissions_filters_by_problem(client):
    pid1 = client.post("/problems", json=A_PLUS_B).json()["id"]
    pid2 = client.post("/problems", json={**A_PLUS_B, "slug": "other"}).json()["id"]
    for pid in (pid1, pid1, pid2):
        client.post(
            "/submissions",
            json={"problem_id": pid, "language": "python", "source_code": "x=1"},
        )

    all_subs = client.get("/submissions").json()
    assert len(all_subs) == 3
    only_p1 = client.get("/submissions", params={"problem_id": pid1}).json()
    assert len(only_p1) == 2
    assert {s["problem_id"] for s in only_p1} == {pid1}


@pytest.mark.parametrize("limit,expected_status", [(0, 422), (1, 200), (200, 200)])
def test_list_submissions_limit_bounds(client, limit, expected_status):
    assert client.get("/submissions", params={"limit": limit}).status_code == expected_status
