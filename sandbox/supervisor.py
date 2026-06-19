#!/usr/bin/env python3
"""In-container supervisor: run one untrusted command, measure it, enforce a kill.

This script is baked into the sandbox image and is the container's entrypoint.
The *host* runner (``app.runner.sandbox``) sets the hard isolation/limit flags on
``docker run``; this supervisor handles the things that must be done from inside
the container:

1. Wire the test input file to the child's stdin and run the user command in a
   fresh **session** (so the whole process group can be killed at once).
2. Concurrently drain stdout/stderr — capping at a byte budget at *write* time
   so a runaway ``print`` loop can neither deadlock the pipe nor exhaust memory,
   while still reading to EOF so the child never blocks on a full pipe.
3. Enforce a hard **wall-clock** kill (SIGKILL to the process group) via a
   watchdog timer; record whether it had to fire (``timed_out``).
4. Measure wall time (monotonic clock), child CPU time and peak RSS (rusage),
   and read the cgroup ``memory.events`` OOM counter so memory kills can be
   detected reliably (``docker inspect .State.OOMKilled`` is unreliable on
   cgroups v2 — see ARCHITECTURE.md §3.1).
5. Emit a single JSON result on stdout, behind a marker. The child's own output
   is captured into that JSON (base64) rather than passed through, so the
   container's stdout carries only the structured result and the host can read
   it back with ``docker logs`` without any stream-multiplexing ambiguity.

The supervisor exits 0 whenever it successfully ran and reported (even if the
child crashed); a non-zero supervisor exit, or a missing result marker, signals
an internal judge error (or that the supervisor itself was OOM-killed) to the
host. It depends only on the Python standard library.
"""

from __future__ import annotations

import base64
import json
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass

RESULT_MARKER = "__OJ_RESULT__"

# Where cgroup v2 exposes this container's own memory accounting when the
# container runs with --cgroupns=private (the default we set on the host side).
CGROUP_MEMORY_EVENTS = "/sys/fs/cgroup/memory.events"
CGROUP_MEMORY_PEAK = "/sys/fs/cgroup/memory.peak"


@dataclass
class _Drain:
    """Reads a stream to EOF, keeping at most ``cap`` bytes (truncating the rest)."""

    stream: object
    cap: int
    data: bytes = b""
    truncated: bool = False

    def run(self) -> None:
        chunks: list[bytes] = []
        total = 0
        while True:
            block = self.stream.read(65536)
            if not block:
                break
            if total < self.cap:
                take = block[: self.cap - total]
                chunks.append(take)
                total += len(take)
                if len(take) < len(block):
                    self.truncated = True
            else:
                # Past the cap: keep draining so the child never blocks on a
                # full pipe, but discard the bytes.
                self.truncated = True
        self.data = b"".join(chunks)


def _read_oom_kill_count() -> int | None:
    """Return the cgroup's cumulative ``oom_kill`` count, or None if unavailable."""
    try:
        with open(CGROUP_MEMORY_EVENTS) as handle:
            for line in handle:
                key, _, value = line.partition(" ")
                if key == "oom_kill":
                    return int(value)
    except (OSError, ValueError):
        return None
    return None


def _read_cgroup_peak_kb() -> int | None:
    try:
        with open(CGROUP_MEMORY_PEAK) as handle:
            return int(handle.read().strip()) // 1024
    except (OSError, ValueError):
        return None


def _child_env() -> dict[str, str]:
    """A minimal, controlled environment for the untrusted child.

    Note this deliberately does NOT inherit the supervisor's environment (which
    carries the job spec), and sets HOME/TMPDIR to the writable tmpfs so tools
    like g++ and python3 don't fail against the read-only root filesystem.
    """
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        # Avoid writing .pyc into the (read-only at run time) work dir.
        "PYTHONDONTWRITEBYTECODE": "1",
        # Keep CPU-time ~ wall-time for honest single-threaded programs.
        "OMP_NUM_THREADS": "1",
    }


def _lower_own_oom_priority() -> None:
    """Best-effort: make the supervisor a *less* likely OOM victim than the child.

    Lowering oom_score_adj requires CAP_SYS_RESOURCE, which the unprivileged
    sandbox user does not have, so this usually no-ops — that's fine: the
    authoritative OOM signal is the cgroup oom_kill counter, not this.
    """
    try:
        with open("/proc/self/oom_score_adj", "w") as handle:
            handle.write("-1000")
    except OSError:
        pass


def run(spec: dict) -> dict:
    command: list[str] = spec["command"]
    stdin_path: str | None = spec.get("stdin_path")
    workdir: str = spec.get("workdir", "/work")
    wall_limit_ms: int = int(spec["wall_limit_ms"])
    stdout_cap: int = int(spec.get("output_limit_bytes", 256 * 1024))
    stderr_cap: int = int(spec.get("stderr_limit_bytes", 64 * 1024))

    _lower_own_oom_priority()

    import subprocess  # local import keeps module import cheap if ever reused

    stdin_handle = open(stdin_path, "rb") if stdin_path else subprocess.DEVNULL

    oom_before = _read_oom_kill_count()
    rusage_before = _rusage_children()
    started = time.monotonic()

    try:
        proc = subprocess.Popen(
            command,
            stdin=stdin_handle,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workdir,
            env=_child_env(),
            # New session => child is its own process-group leader (pgid == pid),
            # so the watchdog can SIGKILL the entire group (catches fork bombs and
            # detached children). start_new_session is the safe modern equivalent
            # of preexec_fn=os.setsid.
            start_new_session=True,
            close_fds=True,
        )
    finally:
        if stdin_handle not in (None, subprocess.DEVNULL):
            stdin_handle.close()

    out_drain = _Drain(proc.stdout, stdout_cap)
    err_drain = _Drain(proc.stderr, stderr_cap)
    out_thread = threading.Thread(target=out_drain.run, daemon=True)
    err_thread = threading.Thread(target=err_drain.run, daemon=True)
    out_thread.start()
    err_thread.start()

    timed_out = threading.Event()

    def _on_timeout() -> None:
        timed_out.set()
        _killpg(proc.pid)

    watchdog = threading.Timer(wall_limit_ms / 1000.0, _on_timeout)
    watchdog.start()

    return_code = proc.wait()
    elapsed = time.monotonic() - started
    watchdog.cancel()

    # Clean up any survivors in the group (e.g. the program forked and exited),
    # then reap them so their resource usage is accounted and no zombies remain.
    _killpg(proc.pid)
    _reap_all()

    out_thread.join(timeout=5)
    err_thread.join(timeout=5)

    rusage_after = _rusage_children()
    oom_after = _read_oom_kill_count()

    # Decode the exit status: a negative returncode means death-by-signal.
    if return_code < 0:
        exit_code: int | None = None
        term_signal: int | None = -return_code
    else:
        exit_code = return_code
        term_signal = None

    oom_killed = oom_before is not None and oom_after is not None and oom_after > oom_before

    cpu_seconds = (rusage_after.ru_utime - rusage_before.ru_utime) + (
        rusage_after.ru_stime - rusage_before.ru_stime
    )
    # ru_maxrss is in KiB on Linux and is the peak RSS of the largest single
    # child — accurate for the typical single-process solution. The cgroup peak
    # (whole container, includes this supervisor) is reported separately for
    # transparency; MLE classification prefers ru_maxrss + the OOM signal.
    peak_kb = int(rusage_after.ru_maxrss)

    return {
        "ok": True,
        "exit_code": exit_code,
        "signal": term_signal,
        "timed_out": timed_out.is_set(),
        "oom_killed": oom_killed,
        "wall_ms": int(round(elapsed * 1000)),
        "cpu_ms": int(round(cpu_seconds * 1000)),
        "peak_kb": peak_kb,
        "cgroup_peak_kb": _read_cgroup_peak_kb(),
        "stdout_b64": base64.b64encode(out_drain.data).decode("ascii"),
        "stderr_b64": base64.b64encode(err_drain.data).decode("ascii"),
        "stdout_truncated": out_drain.truncated,
        "stderr_truncated": err_drain.truncated,
    }


def _rusage_children():
    import resource

    return resource.getrusage(resource.RUSAGE_CHILDREN)


def _killpg(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _reap_all() -> None:
    while True:
        try:
            reaped, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if reaped == 0:
            break


def main(argv: list[str]) -> int:
    try:
        spec = json.loads(argv[1])
    except (IndexError, json.JSONDecodeError) as exc:
        _emit({"ok": False, "error": f"bad job spec: {exc}"})
        return 0

    try:
        result = run(spec)
    except Exception as exc:  # noqa: BLE001 - report any internal failure as IE upstream
        result = {"ok": False, "error": f"supervisor error: {type(exc).__name__}: {exc}"}

    _emit(result)
    return 0


def _emit(result: dict) -> None:
    sys.stdout.write(RESULT_MARKER + json.dumps(result) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
