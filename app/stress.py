"""Stress-test / counterexample mode — property-based testing for algorithms.

Given a trusted **brute-force** solution, an **optimized** solution to check, and
a random **test generator**, this:

1. **finds** a counterexample — generate inputs from many seeds, run both
   solutions in the sandbox, and stop at the first input where they disagree;
2. **shrinks** it — reduce to a small failing input that's easy to read.

Design decisions (validated up front — see ARCHITECTURE.md §3.4):

- A seed counts as a counterexample only when the **oracle ran cleanly** (brute
  exited 0 within limits) and the optimized solution either crashed/timed-out or
  produced a different answer. If the brute itself fails on a generated input
  (it's slow by design), that's an "oracle failure", not a bug — it's skipped.
- The **generator is untrusted code** and runs in the same sandbox as the
  solutions. Its determinism is *checked* (run twice, diff) rather than assumed.
- Shrinking re-verifies the counterexample after every reduction, so it can
  never report a spurious one. **Parametric size-shrinking** (regenerate at the
  same seed with a smaller size) is the effective reducer; it is a heuristic
  ("smallest size found", not provably minimal, because changing the size
  redraws the PRNG). A generic line-level **delta-debugging (ddmin)** pass
  refines further when the input format allows.
- Every result carries a **reproducibility manifest** (seed, sizes, source
  hashes, limits, compare mode) so a finding can be replayed.

Exposed via the CLI (``python -m app.stress``) and the ``POST /stress-test``
endpoint.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from app.config import Settings, settings
from app.enums import CompareMode
from app.languages import Language, get_language
from app.runner.result import ExecutionResult
from app.runner.sandbox import SandboxSpec, run_in_sandbox
from app.verdict import compare_output

INPUT_FILENAME = "input.txt"


# --------------------------------------------------------------------------- #
# Delta debugging (pure, unit-tested without Docker)
# --------------------------------------------------------------------------- #


def ddmin(items: list, predicate: Callable[[list], bool]) -> list:
    """Return a 1-minimal sublist still satisfying ``predicate`` (Zeller's ddmin).

    ``predicate(items)`` is assumed True on entry. Reduction proceeds by removing
    progressively finer chunks and keeping any removal that preserves the
    predicate. The result is locally minimal, not necessarily globally smallest.
    """
    items = list(items)
    n = 2
    while len(items) >= 2:
        chunk_size = max(1, len(items) // n)
        boundaries = [
            (lo, min(lo + chunk_size, len(items))) for lo in range(0, len(items), chunk_size)
        ]
        reduced = False
        for lo, hi in boundaries:
            complement = items[:lo] + items[hi:]
            if complement and predicate(complement):
                items = complement
                n = max(n - 1, 2)
                reduced = True
                break
        if reduced:
            continue
        if n >= len(items):
            break
        n = min(len(items), n * 2)
    return items


# --------------------------------------------------------------------------- #
# Configuration and result types
# --------------------------------------------------------------------------- #


@dataclass
class ProgramSpec:
    source: str
    language: str


@dataclass
class StressParams:
    iterations: int = 100  # seeds to try while searching
    size: int = 12  # generator size parameter for the search
    time_limit_ms: int = 2000  # per-run limit for the optimized solution and generator
    brute_time_limit_ms: int = 8000  # the oracle is slow by design, so it gets more time
    memory_limit_mb: int = 256
    output_limit_kb: int = 1024
    compare_mode: CompareMode = CompareMode.TRIM
    float_tolerance: float = 1e-6
    max_shrink_iterations: int = 200  # cap on ddmin oracle calls


@dataclass
class StressResult:
    found: bool
    message: str
    seed: int | None = None
    base_size: int | None = None
    shrunk_size: int | None = None
    counterexample_input: str | None = None
    brute_output: str | None = None
    optimized_output: str | None = None
    iterations_run: int = 0
    oracle_failures: int = 0
    manifest: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "found": self.found,
            "message": self.message,
            "seed": self.seed,
            "base_size": self.base_size,
            "shrunk_size": self.shrunk_size,
            "counterexample_input": self.counterexample_input,
            "brute_output": self.brute_output,
            "optimized_output": self.optimized_output,
            "iterations_run": self.iterations_run,
            "oracle_failures": self.oracle_failures,
            "manifest": self.manifest,
        }


class StressCompileError(RuntimeError):
    def __init__(self, name: str, detail: str):
        super().__init__(f"{name} failed to compile")
        self.name = name
        self.detail = detail


# --------------------------------------------------------------------------- #
# Compiled programs (compile once, run many times)
# --------------------------------------------------------------------------- #


@dataclass
class _Program:
    name: str
    language: Language
    workdir: Path


def _prepare(name: str, spec: ProgramSpec, cfg: Settings) -> _Program:
    language = get_language(spec.language)
    workdir = Path(cfg.run_root) / f"stress-{name}-{uuid4().hex[:8]}"
    workdir.mkdir(parents=True, exist_ok=True)
    workdir.chmod(0o777)
    (workdir / language.source_filename).write_text(spec.source)

    if language.compile_cmd is not None:
        result = run_in_sandbox(
            SandboxSpec(
                workdir_host=str(workdir),
                command=language.compile_cmd,
                time_limit_ms=cfg.compile_time_limit_ms,
                memory_limit_mb=cfg.compile_memory_limit_mb,
                output_limit_bytes=cfg.compile_output_limit_kb * 1024,
                writable=True,
            ),
            cfg,
        )
        if not result.exited_cleanly:
            raise StressCompileError(name, result.stderr_snippet())
    return _Program(name, language, workdir)


def _run(
    program: _Program, stdin_text: str, time_limit_ms: int, params: StressParams, cfg: Settings
) -> ExecutionResult:
    (program.workdir / INPUT_FILENAME).write_text(stdin_text)
    return run_in_sandbox(
        SandboxSpec(
            workdir_host=str(program.workdir),
            command=program.language.run_cmd,
            time_limit_ms=time_limit_ms,
            memory_limit_mb=params.memory_limit_mb,
            output_limit_bytes=params.output_limit_kb * 1024,
            stdin_filename=INPUT_FILENAME,
            writable=False,
        ),
        cfg,
    )


# --------------------------------------------------------------------------- #
# Core logic
# --------------------------------------------------------------------------- #


def _generate(
    gen: _Program, seed: int, size: int, params: StressParams, cfg: Settings
) -> str | None:
    """Produce a test input for (seed, size), or None if the generator misbehaved."""
    result = _run(gen, f"{seed} {size}\n", params.time_limit_ms, params, cfg)
    if not result.exited_cleanly:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def _disagree(brute: ExecutionResult, optimized: ExecutionResult, params: StressParams) -> bool:
    """Whether (brute, optimized) constitute a counterexample.

    Precondition: the caller has confirmed the brute ran cleanly (it's the
    oracle). The optimized solution fails the check if it crashed/timed-out or
    produced a different answer.
    """
    if not optimized.exited_cleanly:
        return True
    return not compare_output(
        brute.stdout.decode("utf-8", errors="replace"),
        optimized.stdout,
        params.compare_mode,
        float_tolerance=params.float_tolerance,
    )


@dataclass
class _Eval:
    is_counterexample: bool
    oracle_failed: bool
    brute: ExecutionResult
    optimized: ExecutionResult


def _evaluate(brute_p, optimized_p, input_text, params, cfg) -> _Eval:
    brute = _run(brute_p, input_text, params.brute_time_limit_ms, params, cfg)
    optimized = _run(optimized_p, input_text, params.time_limit_ms, params, cfg)
    if not brute.exited_cleanly:
        return _Eval(False, True, brute, optimized)
    return _Eval(_disagree(brute, optimized, params), False, brute, optimized)


def run_stress(
    brute_spec: ProgramSpec,
    optimized_spec: ProgramSpec,
    generator_spec: ProgramSpec,
    params: StressParams | None = None,
    cfg: Settings = settings,
) -> StressResult:
    params = params or StressParams()
    programs: list[_Program] = []
    try:
        try:
            brute = _prepare("brute", brute_spec, cfg)
            programs.append(brute)
            optimized = _prepare("optimized", optimized_spec, cfg)
            programs.append(optimized)
            generator = _prepare("generator", generator_spec, cfg)
            programs.append(generator)
        except StressCompileError as exc:
            return StressResult(found=False, message=f"{exc.name} failed to compile:\n{exc.detail}")

        # Determinism gate: a generator must be a pure function of (seed, size).
        first = _generate(generator, 0, params.size, params, cfg)
        if first is None:
            return StressResult(found=False, message="generator failed to run on seed 0")
        if _generate(generator, 0, params.size, params, cfg) != first:
            return StressResult(
                found=False,
                message=(
                    "generator is non-deterministic for a fixed (seed, size) — pin its "
                    "RNG seed (and PYTHONHASHSEED for Python) so findings can be replayed"
                ),
            )

        # FIND phase.
        oracle_failures = 0
        for seed in range(params.iterations):
            input_text = _generate(generator, seed, params.size, params, cfg)
            if input_text is None:
                oracle_failures += 1
                continue
            ev = _evaluate(brute, optimized, input_text, params, cfg)
            if ev.oracle_failed:
                oracle_failures += 1
                continue
            if ev.is_counterexample:
                return _build_found_result(
                    brute,
                    optimized,
                    generator,
                    seed,
                    input_text,
                    ev,
                    seed + 1,
                    oracle_failures,
                    brute_spec,
                    optimized_spec,
                    generator_spec,
                    params,
                    cfg,
                )

        return StressResult(
            found=False,
            message=f"no counterexample found in {params.iterations} seeds (size {params.size})",
            iterations_run=params.iterations,
            oracle_failures=oracle_failures,
        )
    finally:
        for program in programs:
            shutil.rmtree(program.workdir, ignore_errors=True)


def _build_found_result(
    brute,
    optimized,
    generator,
    seed,
    base_input,
    base_eval,
    iterations_run,
    oracle_failures,
    brute_spec,
    optimized_spec,
    generator_spec,
    params,
    cfg,
) -> StressResult:
    shrunk_size, input_text, ev = _shrink(
        brute, optimized, generator, seed, base_input, base_eval, params, cfg
    )
    return StressResult(
        found=True,
        message="counterexample found",
        seed=seed,
        base_size=params.size,
        shrunk_size=shrunk_size,
        counterexample_input=input_text,
        brute_output=ev.brute.stdout.decode("utf-8", errors="replace"),
        optimized_output=ev.optimized.stdout.decode("utf-8", errors="replace"),
        iterations_run=iterations_run,
        oracle_failures=oracle_failures,
        manifest={
            "seed": seed,
            "base_size": params.size,
            "shrunk_size": shrunk_size,
            "time_limit_ms": params.time_limit_ms,
            "brute_time_limit_ms": params.brute_time_limit_ms,
            "memory_limit_mb": params.memory_limit_mb,
            "compare_mode": params.compare_mode.value,
            "languages": {
                "brute": brute_spec.language,
                "optimized": optimized_spec.language,
                "generator": generator_spec.language,
            },
            "source_sha256": {
                "brute": _sha(brute_spec.source),
                "optimized": _sha(optimized_spec.source),
                "generator": _sha(generator_spec.source),
            },
            "note": "shrunk_size is the smallest size found, not provably minimal",
        },
    )


def _shrink(brute, optimized, generator, seed, base_input, base_eval, params, cfg):
    """Reduce the failing input: smallest reproducing size, then line-level ddmin."""
    best_size = params.size
    best_input = base_input
    best_eval = base_eval

    # (a) Parametric size shrink: smallest size that still reproduces at this seed.
    for size in range(1, params.size):
        candidate = _generate(generator, seed, size, params, cfg)
        if candidate is None:
            continue
        ev = _evaluate(brute, optimized, candidate, params, cfg)
        if not ev.oracle_failed and ev.is_counterexample:
            best_size, best_input, best_eval = size, candidate, ev
            break  # ascending scan => first hit is the smallest

    # (b) Generic line-level ddmin, re-verifying each reduction (bounded).
    calls = {"n": 0}

    def still_fails(lines: list[str]) -> bool:
        if calls["n"] >= params.max_shrink_iterations:
            return False
        calls["n"] += 1
        candidate = "\n".join(lines)
        if not candidate.strip():
            return False
        ev = _evaluate(brute, optimized, candidate, params, cfg)
        return not ev.oracle_failed and ev.is_counterexample

    lines = best_input.split("\n")
    minimal_lines = ddmin(lines, still_fails)
    minimal_input = "\n".join(minimal_lines)
    if minimal_input != best_input:
        ev = _evaluate(brute, optimized, minimal_input, params, cfg)
        if not ev.oracle_failed and ev.is_counterexample:
            best_input, best_eval = minimal_input, ev

    return best_size, best_input, best_eval


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main() -> None:  # pragma: no cover - thin CLI wrapper
    parser = argparse.ArgumentParser(
        description="Find a counterexample distinguishing two solutions."
    )
    parser.add_argument("--brute", required=True, help="path to the trusted brute-force source")
    parser.add_argument("--optimized", required=True, help="path to the solution under test")
    parser.add_argument("--generator", required=True, help="path to the test generator source")
    parser.add_argument("--lang", default="cpp", help="language for all three (default: cpp)")
    parser.add_argument("--brute-lang", default=None)
    parser.add_argument("--optimized-lang", default=None)
    parser.add_argument("--generator-lang", default=None)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--size", type=int, default=12)
    parser.add_argument("--time-limit-ms", type=int, default=2000)
    parser.add_argument("--memory-mb", type=int, default=256)
    args = parser.parse_args()

    spec = lambda path, lang: ProgramSpec(Path(path).read_text(), lang or args.lang)  # noqa: E731
    params = StressParams(
        iterations=args.iterations,
        size=args.size,
        time_limit_ms=args.time_limit_ms,
        memory_limit_mb=args.memory_mb,
    )
    result = run_stress(
        spec(args.brute, args.brute_lang),
        spec(args.optimized, args.optimized_lang),
        spec(args.generator, args.generator_lang),
        params,
    )

    if not result.found:
        print(f"No counterexample: {result.message}")
        print(f"(ran {result.iterations_run} seeds, {result.oracle_failures} oracle failures)")
        return
    print("=== COUNTEREXAMPLE FOUND ===")
    print(f"seed={result.seed} size {result.base_size} -> {result.shrunk_size}")
    print("--- input ---")
    print(result.counterexample_input, end="")
    print("--- brute (reference) output ---")
    print(result.brute_output, end="")
    print("--- optimized output ---")
    print(result.optimized_output, end="")


if __name__ == "__main__":  # pragma: no cover
    main()
