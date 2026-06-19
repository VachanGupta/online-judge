# Limitations & the production-hardening path

This judge implements **reasonable isolation for grading semi-trusted code** — a
portfolio/educational system, not a bulletproof multi-tenant sandbox for
arbitrary adversarial code at scale. This document is explicit about where the
line is and what a production system would add. Being clear about this is part
of the point of the project.

## What the sandbox already does

Every submission runs in a fresh, ephemeral container with: no network
(`--network none`), a memory cap with swap disabled (`--memory` ==
`--memory-swap`, `--memory-swappiness=0`), a CPU quota (`--cpus`), a process/thread
cap (`--pids-limit`), a read-only root filesystem (`--read-only`) with only a
locked-down `/tmp` tmpfs (`noexec,nosuid,nodev`) writable, all Linux capabilities
dropped (`--cap-drop ALL`), no privilege escalation (`--security-opt
no-new-privileges`), a private cgroup namespace, a capped `/dev/shm`, file
descriptor / file size / core-dump ulimits, a non-root user, and a hard
wall-clock kill. The default seccomp profile is kept. Containers are always
removed, and the program's output is captured by an in-container supervisor and
returned out-of-band rather than via a writable host mount.

## The core limitation: a shared kernel

Containers built on `runc` **share the host kernel**. A kernel
privilege-escalation vulnerability (or a container-runtime escape) lets
untrusted code break out of the sandbox. No amount of flag-tuning changes this.
For grading genuinely adversarial code, the right answer is a **non-shared
kernel**:

- **gVisor (`runsc`)** — a user-space kernel that intercepts syscalls, so the
  host kernel is not directly exposed. A drop-in OCI runtime.
- **Kata Containers / Firecracker microVMs** — each submission in a lightweight
  VM with its own kernel; the strongest practical isolation, used by managed
  judges and CI sandboxes.
- **`nsjail` / IOI `isolate`** — purpose-built process jails for judges, driving
  namespaces + seccomp + cgroups directly (what Codeforces-style judges use).

## Other known gaps and what production would change

- **The Docker socket in Compose.** The worker launches sandbox containers by
  talking to the host Docker daemon over a mounted socket — which is
  root-on-host-equivalent. Fine for a local demo; in production you would use a
  rootless daemon, a remote/dedicated build node, or a microVM runtime, and
  never expose the socket to code paths that handle untrusted input.
- **Seccomp.** The Docker default profile is applied (good), but a judge benefits
  from a *tightened* profile that also blocks `ptrace`, `mount`, `keyctl`,
  `bpf`, `clock_settime`, `userfaultfd`, etc.
- **cgroups directly.** We rely on Docker's translation of `--memory`/`--cpus`.
  Driving cgroups v2 directly (or via `isolate`) gives finer control and lets
  TLE be judged on **CPU time** from `cpu.stat` rather than wall time — more
  deterministic on a noisy host. We record CPU time but judge TLE on wall time
  by default; see ARCHITECTURE.md §3.2.
- **User namespaces.** Without `userns-remap`, container uid 1000 maps to host
  uid 1000. With the rootfs isolated, network off, and caps dropped this is
  acceptable here, but userns remapping adds a real layer on native-Linux
  deployments.
- **Compile/run cost.** Each test runs in a fresh container (~tens to hundreds of
  ms startup on Docker Desktop). Production judges amortize this with a warm
  container pool, compile caching, and snapshotting.
- **Storage throughput.** SQLite has a single-writer ceiling; the API insert,
  worker claim, verdict write, and reaper all serialize behind it. Fine for a
  demo and a small worker pool; switch `OJ_DATABASE_URL` to PostgreSQL (and use
  `SELECT … FOR UPDATE SKIP LOCKED` for claims) when write throughput matters.
  On macOS, never put the SQLite file on a Docker bind mount — POSIX advisory
  locking through the virtualization layer is unreliable; use a named volume.
- **Output / measurement fidelity.** stdout is captured as bytes (UTF-8 with
  replacement for comparison) and capped; peak memory is the child's `ru_maxrss`
  corroborated by the cgroup OOM signal. Multi-process submissions can use more
  aggregate memory than `ru_maxrss` reports; reading `memory.peak` would be more
  precise for those.
- **Image trust.** The sandbox image is built from a `debian:bookworm-slim` tag;
  production should pin image digests and scan them.
- **Stress mode** assumes the brute force is a correct oracle and the generator
  is deterministic (it checks this). A mismatch proves the two solutions
  *disagree*, not which is wrong; line-level shrinking is best-effort for
  count-prefixed formats (a problem-specific input validator would make it
  stronger).

## Explicitly out of scope

Authentication, users/roles, rate limiting, a web UI, contest/scoreboard logic,
interactive problems, and special judges beyond the provided checker hook. The
focus is the judging engine; these are noted as future work in the README.
