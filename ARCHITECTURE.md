# Architecture

This document explains *why* the online judge is built the way it is: the
design decisions, the tradeoffs, the security model and its limits, and the
data flow from an HTTP submission to a persisted verdict. It is written
alongside the code, phase by phase, so the reasoning is captured — not just the
result.

> Many of the decisions below were pressure-tested against real Docker / Linux /
> SQLite behaviour before implementation. Where a "naïve" approach is subtly
> wrong (e.g. trusting `docker inspect .State.OOMKilled` for MLE detection), the
> decision log records both the trap and the chosen alternative.

---

## 1. System overview

```
                 ┌──────────────┐     enqueue (INSERT pending)     ┌───────────────┐
 client ───────▶ │  API service │ ───────────────────────────────▶│  submissions  │
   HTTP          │  (FastAPI)   │ ◀─── poll status/verdict ────────│  table (queue)│
                 └──────────────┘                                  └───────┬───────┘
                                                                  atomic claim │ (UPDATE)
                                                                              ▼
   verdict + per-test report  ◀──── persist ────────┐               ┌───────────────┐
                                                     └───────────────│ worker pool   │
                                                                     │ (N processes) │
                                                                     └───────┬───────┘
                                                  compile → run each test    │
                                                                              ▼
                                                                     ┌───────────────┐
                                                                     │   runner /    │
                                                                     │ Docker sandbox│
                                                                     └───────────────┘
```

The system is split into cleanly separated modules so each concern can be read,
tested, and swapped independently:

| Module          | Responsibility                                                        |
| --------------- | --------------------------------------------------------------------- |
| `app.config`    | env-driven settings and tunable limits                                |
| `app.enums`     | the judge's vocabulary (verdicts, statuses, compare modes)            |
| `app.languages` | the language registry — adding a language is a config entry           |
| `app.db`        | SQLAlchemy engine/session wiring and SQLite pragmas                   |
| `app.models`    | ORM models (Problem, TestCase, Submission, TestResult)                |
| `app.runner`    | the Docker execution sandbox + resource-limit enforcement *(Phase 2)* |
| `app.verdict`   | pure output comparison + verdict classification *(Phase 3)*           |
| `app.main`      | the FastAPI application *(Phase 4)*                                    |
| `app.queue`     | DB-backed submission queue: atomic claim + reaper *(Phase 5)*         |
| `app.grader`    | compile → run tests → aggregate verdict → persist *(Phase 5)*         |
| `app.worker`    | the worker process loop / pool *(Phase 5)*                            |
| `app.stress`    | stress-test / counterexample finder *(Phase 7)*                       |

---

## 2. Storage & data model (Phase 1)

Four tables mirror the domain. SQLite is the default backend; the schema uses
only portable types and string-backed enums, so moving to PostgreSQL in
production is a `OJ_DATABASE_URL` change and nothing else.

- **Problem** — statement + the resource limits (`time_limit_ms`,
  `memory_limit_mb`, `output_limit_kb`) and the `compare_mode` applied to every
  test case.
- **TestCase** — an `(input_data, expected_output)` pair with a stable
  `ordinal` (the ordering that defines "the first failing test"), an
  `is_sample` flag (samples may be shown; hidden tests stay private), and
  `points`.
- **Submission** — the submitted `source_code` + `language`, the queue
  lifecycle (`status`, `worker_id`, `attempts`, timestamps), and the final
  result (`verdict`, `compile_output`, worst-case `max_time_ms` /
  `max_memory_kb`, `score`).
- **TestResult** — the per-test outcome (`verdict`, `time_ms`, `cpu_ms`,
  `memory_kb`, `exit_code`, `signal`, a bounded `stderr_snippet`).

**Decision — store enums as strings, not DB-native enums.** `native_enum=False`
with `values_callable` persists the enum *value* (`"pending"`, `"AC"`) as
VARCHAR with a CHECK constraint. This is portable across SQLite/Postgres and
makes the stored data identical to the JSON the API emits.

**Decision — `status` is indexed.** The worker claim query filters on
`status='pending'`; the index keeps that hot path cheap.

---

## 3. Validated design decisions (the hard parts)

These are recorded up front because they shape the modules built in later
phases. Each entry notes the naïve approach, why it's wrong/risky, and the
chosen design.

### 3.1 Sandbox isolation & measurement (drives Phase 2)

- **Get untrusted output *out* without a read-write host bind mount.** A
  writable host bind mount into an untrusted container is both a
  data-tampering surface and, on Docker Desktop for macOS, subject to flaky
  POSIX-locking/fsync semantics through the virtualization layer. Instead: the
  submission's source/input go in via a **read-only** mount; a small
  **supervisor** process (baked into the image, runs as the container's
  entrypoint) captures the program's stdout/stderr itself and writes a single
  **JSON result to *its own* stdout**, which the host reads back with
  `docker logs`. This also sidesteps `docker logs`' stdout/stderr
  multiplexing, because the only thing on container stdout is the supervisor's
  structured result.

- **Two memory limits, on purpose.** The container's hard `--memory` cap is set
  to `problem_limit + overhead` so the supervisor and language runtime don't
  eat into the submitter's budget; **MLE is judged from the measured peak vs the
  problem limit**, with the container cap as a backstop.

- **MLE detection cannot trust `docker inspect .State.OOMKilled` alone.** On
  cgroups v2 that flag is unreliable: it reflects an OOM event on PID 1's
  cgroup and reads *false* when the kernel OOM-kills a *child* process. Robust
  signal = **exit 137 corroborated by the cgroup `memory.events` `oom_kill`
  counter and/or measured peak ≥ limit.** The supervisor reads
  `/sys/fs/cgroup/memory.peak` and `memory.events` from inside the container;
  the host reads `OOMKilled` only as corroboration.

- **A SIGKILL / exit-137 is ambiguous** — it can be our wall-clock watchdog
  (TLE), the kernel OOM killer (MLE), or the pids-limit. Disambiguation relies
  on an explicit `timed_out` flag set by *our* watchdog vs. OOM evidence from
  the cgroup, never on arithmetic over the exit code.

- **Flags adopted beyond the obvious.** `--network none`, `--memory` +
  `--memory-swap` equal (+ `--memory-swappiness=0`) to disable swap,
  `--read-only`, `--pids-limit` (the *correct* fork-bomb defense — **not**
  `--ulimit nproc`, which is enforced per host-UID across the whole kernel and
  is shared/misleading when every submission runs as the same uid), `--cpus`,
  `--user`, `--cap-drop ALL`, `--security-opt no-new-privileges`,
  `--cgroupns=private`, a small `--shm-size` (so `/dev/shm` can't be used as
  uncapped RAM), `--ulimit nofile`, `--ulimit fsize`, `--ulimit core=0`, and a
  `--tmpfs /tmp` mounted `noexec,nosuid,nodev`. The default seccomp profile is
  kept (never `seccomp=unconfined`).

- **Measurement.** Wall time via `time.monotonic()`; child CPU time via
  `rusage` (`ru_utime + ru_stime`); peak memory via the cgroup `memory.peak`
  (true high-water mark) with `ru_maxrss` (KiB on Linux → ×1024) as a fallback.
  `ru_maxrss` is *not* a process-tree total (it's the largest single child), so
  the cgroup figure is authoritative for multi-process programs. The child runs
  in a new session (`start_new_session`) so the watchdog can SIGKILL the whole
  process group; stdout/stderr are drained concurrently to avoid pipe-buffer
  deadlock, and capped at write time so a runaway `print` can't fill memory.

- **A host-side hard timeout backstops the in-container watchdog**, because a
  wedged container — or one whose supervisor was itself OOM-killed — won't
  self-terminate. Containers are always removed in a `finally` and labelled for
  a sweep reaper.

### 3.2 Verdict classification (drives Phase 3)

- **Precedence:** `CE` (compile stage, short-circuits) → `MLE` (memory
  evidence) → `TLE` (`timed_out` or wall > limit) → `RE` (signal or non-zero
  exit) → `OLE` (clean exit but output truncated) → compare → `AC`/`WA`. An
  `IE` (internal/judge error) verdict exists for judge failures (image missing,
  docker error) so they trigger a re-judge instead of unfairly penalizing the
  submitter. The function is **pure** (no clocks/IO): all nondeterminism lives
  in the upstream facts it consumes.

- **`py_compile` is a syntax gate, not a full compile gate.** Using
  `python -m py_compile` makes Python *syntax* errors surface as `CE` (like
  C++), but `NameError`/`ImportError` correctly remain `RE` — they only arise at
  runtime. Documented as intended behaviour; run with the exact interpreter used
  for execution and a writable bytecode path.

- **Output comparison** normalizes CRLF→LF, then per `compare_mode`: `TRIM`
  (default; strip trailing whitespace per line, ignore trailing blank lines),
  `EXACT` (byte-for-byte), `TOKENS` (whitespace-insensitive), `FLOAT` (token
  compare with absolute+relative epsilon). Case-sensitive by default. A
  special-checker hook is provided for multiple-valid-answer problems.

- **Fail-fast aggregation** (stop at first non-AC, report that test's verdict)
  is the default — it matches competitive judges and avoids running every test
  for a wrong submission. A run-all mode is available for partial scoring.

### 3.3 Concurrent queue on SQLite (drives Phase 5)

- **Atomic claim.** A single `UPDATE … WHERE id = (SELECT … 'pending' …) AND
  status='pending'` is race-free across processes because SQLite serializes all
  writers behind one write lock held for the whole statement; the
  `AND status='pending'` guard is the correctness anchor. The **default
  implementation uses the portable `BEGIN IMMEDIATE` + guarded `UPDATE` +
  `rowcount`** form (RETURNING is gated behind a runtime
  `sqlite_version_info >= (3,35,0)` assert, since the *linked* library — not the
  Python version — is what matters).

- **Keep grading *outside* the transaction.** Two tiny transactions per
  submission — claim, then write verdict — with the Docker work in between and
  no DB lock held. The verdict write is guarded by `worker_id` + `status` so a
  worker whose claim was reaped can't clobber the new owner's result.

- **Per-connection pragmas.** `journal_mode=WAL` is persistent, but
  `busy_timeout` and `synchronous` reset on every new connection, so they're
  re-applied via a `connect` event listener. App-level retry-with-jitter wraps
  writes (`busy_timeout` is necessary, not sufficient).

- **Reaper.** A worker that dies leaves its row stuck in `running`; a sweep
  requeues rows whose `claimed_at` is older than a timeout set *well above* the
  worst-case grade time. An `attempts` cap sends repeatedly-failing submissions
  to a terminal error instead of looping forever.

- **SQLite must live on a native volume, not a macOS Docker bind mount**
  (broken POSIX advisory locking through the virtualization layer). Compose
  uses a named volume; PostgreSQL is the production recommendation if write
  throughput outgrows SQLite's single-writer ceiling.

### 3.4 Stress-test mode (drives Phase 7)

- **A mismatch counts only if *both* solutions ran cleanly (exit 0, within
  limits) *and* their outputs differ.** Otherwise a brute-force solution that
  TLEs/REs on a large generated input would be mislabelled a counterexample —
  it's an "oracle failed", not a bug.

- **The generator is untrusted code too** and runs in the same sandbox.

- **Determinism is enforced, not assumed:** the generator is run twice and
  diffed during intake; `PYTHONHASHSEED` is pinned and nondeterministic seeding
  is documented as forbidden. The shrinker re-verifies the mismatch after every
  reduction (so it can never emit a spurious counterexample), and parametric
  size-shrinking is labelled a heuristic ("smallest found, not proven minimal")
  because changing the size re-draws the PRNG stream. A reproducibility manifest
  (seed, size, source hashes, image, limits, compare mode) accompanies every
  reported counterexample.

---

## 4. Security model and its limits

**This is reasonable isolation for grading semi-trusted code, not a
bulletproof multi-tenant sandbox.** Containers share the host kernel (runc), so
a kernel privilege-escalation bug escapes the container. What a production judge
handling adversarial code at scale would add — and why — is documented in
[`LIMITATIONS.md`](LIMITATIONS.md) *(Phase 8)*: seccomp profile tightening,
`nsjail`/`isolate` or **gVisor**/Kata/Firecracker microVMs for a non-shared
kernel, cgroups v2 driven directly, user-namespace remapping, and compile
caching.

---

## 5. Data flow (request → verdict)

Filled in as the API and worker land *(Phases 4–5)*.
