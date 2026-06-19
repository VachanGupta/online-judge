# Online Judge

A production-style **online judge**: an HTTP service that accepts code
submissions for algorithmic problems, runs each one inside a locked-down,
ephemeral Docker sandbox under strict time and memory limits against a set of
test cases, and returns a verdict — **AC / WA / TLE / MLE / RE / CE** — with
per-test timing and peak-memory usage.

| Verdict | Meaning |
| --- | --- |
| `AC` | Accepted — correct output on every test |
| `WA` | Wrong Answer |
| `TLE` | Time Limit Exceeded |
| `MLE` | Memory Limit Exceeded |
| `RE` | Runtime Error (non-zero exit or killed by a signal) |
| `CE` | Compilation Error (or syntax error for interpreted languages) |
| `OLE` / `IE` | Output Limit Exceeded / Internal (judge) Error |

## What makes it interesting

This project focuses on the parts of a grader that are genuinely hard:

- **Real sandboxed execution.** Every run is network-disabled, memory-capped
  (swap off), CPU-limited, PID-limited, on a read-only root filesystem, as a
  non-root user with all capabilities dropped, killed on a hard wall-clock
  timeout — with accurate wall/CPU time and peak-memory measurement.
- **Honest verdict classification** from the messy reality of exit codes,
  signals, OOM kills, and output comparison, with the edge cases handled and
  tested (e.g. `docker inspect .State.OOMKilled` is unreliable on cgroups v2, so
  MLE is corroborated by the cgroup `memory.events` counter).
- **A concurrent grading pipeline:** a DB-backed queue with a race-free atomic
  claim and a pool of worker processes, written so storage (SQLite → Postgres)
  and queue (DB → Redis/Celery) are swappable.
- **Stress-test mode:** property-based testing for algorithms — pit a brute force
  against an optimized solution over generated inputs, find a disagreement, and
  shrink it to a minimal failing case.

Design decisions, tradeoffs, the security model, and the request→verdict data
flow are written up in [`ARCHITECTURE.md`](ARCHITECTURE.md); the security
boundaries and production-hardening path in [`LIMITATIONS.md`](LIMITATIONS.md).

## Tech stack

Python 3.11+ · FastAPI · SQLAlchemy 2.0 + SQLite (Postgres-ready) · Docker ·
pytest. Languages: C++17 (g++) and Python 3, each a config entry in
[`app/languages.py`](app/languages.py).

## Run it with Docker Compose (one command)

Requires Docker. Brings up the API and a worker pool; the worker builds the
sandbox image on first start.

```bash
docker compose up --build
# in another shell: load the example problems
docker compose exec api python -m scripts.seed
```

Open the interactive API docs at **http://localhost:8000/docs**.

## Run it locally (without Compose)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

docker build -t online-judge-sandbox:latest sandbox/   # build the sandbox image
python -m app.db                                        # create the schema
python -m scripts.seed                                  # load example problems

# terminal 1 — the API
uvicorn app.main:app --reload
# terminal 2 — a pool of grading workers
python -m app.worker --workers 2
```

## Request → verdict walkthrough

Create a problem (or use a seeded one), submit a solution, and poll for the
verdict:

```bash
# 1. Create a problem with its limits and test cases
curl -s localhost:8000/problems -H 'content-type: application/json' -d '{
  "slug": "a-plus-b",
  "title": "A + B",
  "time_limit_ms": 1000,
  "memory_limit_mb": 128,
  "test_cases": [
    {"input_data": "1 2\n",   "expected_output": "3\n", "is_sample": true},
    {"input_data": "10 20\n", "expected_output": "30\n"}
  ]
}'
# => {"id": 1, "slug": "a-plus-b", "num_test_cases": 2, ...}

# 2. Submit a solution (returns immediately; grading happens in the background)
curl -s localhost:8000/submissions -H 'content-type: application/json' -d '{
  "problem_id": 1,
  "language": "python",
  "source_code": "a, b = map(int, input().split())\nprint(a + b)\n"
}'
# => {"id": 1, "status": "pending"}

# 3. Poll until it is graded
curl -s localhost:8000/submissions/1
# => {"status": "completed", "verdict": "AC", "max_time_ms": 11,
#     "max_memory_kb": 11580, "test_results": [...]}
```

### Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | liveness probe |
| `GET` | `/languages` | supported languages |
| `POST` | `/problems` | create a problem (test cases + limits) |
| `GET` | `/problems`, `/problems/{id}` | list / detail (sample tests only) |
| `POST` | `/submissions` | submit a solution (enqueues; `202`) |
| `GET` | `/submissions/{id}` | poll status, verdict, per-test report |
| `GET` | `/submissions` | list (filter by `problem_id` / `status`) |
| `POST` | `/stress-test` | run the counterexample finder |

## The TLE demonstration

The seeded `range-sum-queries` problem ships a large test on which a naive
`O(n·q)` per-query scan exceeds the time limit while an `O(n+q)` prefix-sum
solution passes — the *same problem in the same language*, proving the limit
(not language speed) does the work. Both solutions are in
[`examples/solutions/range_sum/`](examples/solutions/range_sum/); the assertion
is in `tests/integration/test_verdict_matrix.py`.

## Stress-test mode

Find an input where an optimized solution disagrees with a trusted brute force,
then shrink it. The generator reads `"seed size"` on stdin and prints a test.

```bash
python -m app.stress \
  --brute     examples/solutions/count_pairs/brute.cpp \
  --optimized examples/solutions/count_pairs/fast_buggy.cpp \
  --generator examples/solutions/count_pairs/gen.cpp \
  --lang cpp --iterations 100 --size 12
# => COUNTEREXAMPLE FOUND: input "5 6 / 5 3 4 4 2", brute=2, optimized=1
```

Swap `fast_buggy.cpp` for the correct `fast.cpp` and it reports no
counterexample. Also available at `POST /stress-test`.

## Adding a language

Add one `Language` entry to [`app/languages.py`](app/languages.py) (source
filename, optional compile command, run command) and ensure the toolchain is in
the sandbox image. Nothing else changes — the runner, grader, and verdict engine
are language-agnostic.

## Testing

```bash
pytest                 # everything (Docker tests auto-skip if no daemon)
pytest -m "not docker" # fast unit/queue/API tests only
pytest -m docker       # the real-sandbox integration tests
ruff check . && ruff format --check app tests scripts sandbox
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the fast layer
and the Docker layer (including the TLE demo and stress mode) on every push.

## Project layout

```
app/          API, config, models, runner (Docker sandbox), verdict engine,
              queue, grader, workers, stress mode
sandbox/      the sandbox image + the in-container supervisor
scripts/      seed script
examples/     example solutions per verdict + the stress worked example
tests/        unit (no Docker) + integration (Docker-marked)
```

## Future work

Auth/users, rate limiting, a web UI, contest/scoreboard logic, interactive
problems, sandboxed special-judge checkers, a warm container pool, and a
microVM/gVisor runtime for adversarial-grade isolation — see
[`LIMITATIONS.md`](LIMITATIONS.md).

## License

[MIT](LICENSE)
