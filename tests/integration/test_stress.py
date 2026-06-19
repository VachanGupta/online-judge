"""End-to-end stress-test mode (real Docker).

Uses the worked example in ``examples/solutions/count_pairs/``: a brute-force
oracle, a correct O(n) solution, a deliberately buggy O(n) solution (it
undercounts duplicates), and a deterministic generator. The finder must catch
the buggy one and clear the correct one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.stress import ProgramSpec, StressParams, run_stress

pytestmark = pytest.mark.docker

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "solutions" / "count_pairs"


def _spec(name: str) -> ProgramSpec:
    return ProgramSpec(source=(EXAMPLES / name).read_text(), language="cpp")


def test_finds_counterexample_for_buggy_solution(sandbox_image):
    params = StressParams(iterations=40, size=8, time_limit_ms=2000, brute_time_limit_ms=4000)
    result = run_stress(_spec("brute.cpp"), _spec("fast_buggy.cpp"), _spec("gen.cpp"), params)

    assert result.found, result.message
    assert result.counterexample_input
    # The two solutions genuinely disagree on the reported input.
    assert result.brute_output != result.optimized_output
    # Shrinking didn't make it bigger, and the manifest can replay it.
    assert result.shrunk_size <= result.base_size
    assert result.manifest["seed"] == result.seed
    assert "source_sha256" in result.manifest


@pytest.mark.slow
def test_no_counterexample_for_correct_solution(sandbox_image):
    params = StressParams(iterations=20, size=8, time_limit_ms=2000, brute_time_limit_ms=4000)
    result = run_stress(_spec("brute.cpp"), _spec("fast.cpp"), _spec("gen.cpp"), params)
    assert not result.found
    assert result.iterations_run == 20


def test_compile_error_is_reported(sandbox_image):
    bad = ProgramSpec(source="this is not valid c++", language="cpp")
    result = run_stress(bad, _spec("fast.cpp"), _spec("gen.cpp"), StressParams(iterations=5))
    assert not result.found
    assert "compile" in result.message.lower()


def test_stress_endpoint_finds_counterexample(sandbox_image, client):
    payload = {
        "brute_source": (EXAMPLES / "brute.cpp").read_text(),
        "brute_language": "cpp",
        "optimized_source": (EXAMPLES / "fast_buggy.cpp").read_text(),
        "optimized_language": "cpp",
        "generator_source": (EXAMPLES / "gen.cpp").read_text(),
        "generator_language": "cpp",
        "iterations": 20,
        "size": 8,
    }
    response = client.post("/stress-test", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["found"] is True
    assert body["counterexample_input"]
    assert body["brute_output"] != body["optimized_output"]
