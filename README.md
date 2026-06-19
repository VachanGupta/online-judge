# Online Judge

A production-style **online judge**: an HTTP service that accepts code
submissions for algorithmic problems, runs each one inside a locked-down,
ephemeral Docker sandbox under strict time and memory limits against a set of
test cases, and returns a verdict — **AC / WA / TLE / MLE / RE / CE** — with
per-test timing and peak-memory usage.

> ⚠️ **Status:** under active construction. This repository is being built in
> documented phases (scaffold → runner → verdict engine → API → queue/workers →
> seed problems & integration tests → stress-test mode → compose/CI/docs). See
> the commit history and `ARCHITECTURE.md` for the design narrative.

## Why this exists

It is a portfolio project that demonstrates the parts of a code-grading system
that are actually hard:

- **Sandboxed execution** of untrusted code with real resource enforcement
  (network off, memory-capped, CPU-limited, read-only root FS, non-root user,
  PID-limited, hard wall-clock kill) and accurate measurement of time and
  memory.
- **Verdict classification** from the messy reality of exit codes, signals, OOM
  kills, and output comparison — with the edge cases handled and tested.
- **A concurrent grading pipeline**: a DB-backed queue and a pool of worker
  processes, written so the storage and queue backends are swappable.
- A distinctive **stress-test / counterexample mode**: property-based testing
  for algorithms that pits a brute-force solution against an optimized one over
  random inputs and shrinks any mismatch to a minimal failing case.

## Tech stack

| Concern        | Choice                                                        |
| -------------- | ------------------------------------------------------------- |
| API            | FastAPI (async) on Uvicorn                                    |
| Persistence    | SQLAlchemy 2.0 + SQLite (swappable; PostgreSQL for production)|
| Sandbox        | Docker (ephemeral, hardened containers)                       |
| Languages      | C++17 (g++) and Python 3, added via a config registry         |
| Tests          | pytest                                                        |

## Quick start

> Full instructions, API examples, and a request→verdict walkthrough land in
> Phase 8. For now:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m app.db            # create the SQLite schema
pytest                      # run the test suite (Docker tests auto-skip if no daemon)
```

## Documentation

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — design decisions, tradeoffs, the
  security model and its limits, and the request→verdict data flow.

## License

[MIT](LICENSE)
