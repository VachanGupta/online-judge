"""Host-side Docker orchestration: run untrusted code under hard isolation.

Responsibilities:

- Translate a :class:`SandboxSpec` into a fully-locked-down ``docker run``
  invocation (the security model — see ARCHITECTURE.md §3.1).
- Drive the container lifecycle robustly: start detached, wait with a host-side
  timeout backstop, inspect for the exit code and OOM flag, read the
  supervisor's JSON result from the logs, and **always** remove the container.
- Parse all of that into a single :class:`ExecutionResult`.

The Docker-touching code and the pure logic (command building, result parsing)
are kept separate so the latter can be unit-tested without a daemon.

Note on getting code in / results out: the per-submission scratch directory is
bind-mounted **read-only** for graded runs (so the untrusted program cannot
write to a host-visible path), and the program's stdout/stderr are captured by
the in-container supervisor and returned as a JSON blob on the container's
stdout — read back here via ``docker logs``. The scratch dir is mounted writable
only for the *compile* step, which needs to emit the build artifact.
"""

from __future__ import annotations

import base64
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.config import Settings, settings
from app.runner.result import ExecutionResult, SandboxError

# Must match sandbox/supervisor.py.
RESULT_MARKER = "__OJ_RESULT__"

# Label applied to every judge container so orphans (from a hard crash) can be
# swept: `docker ps -aq --filter label=oj-judge=1 | xargs docker rm -f`.
CONTAINER_LABEL = "oj-judge=1"

SANDBOX_DIR = Path(__file__).resolve().parents[2] / "sandbox"


@dataclass
class SandboxSpec:
    """One thing to run in the sandbox (a compile step or a single test run)."""

    workdir_host: str  # per-submission scratch dir containing source/binary/input
    command: list[str]  # argv to execute inside the container (cwd /work)
    time_limit_ms: int  # the program's wall-clock budget
    memory_limit_mb: int  # the program's memory budget (becomes the cgroup cap + overhead)
    output_limit_bytes: int  # cap on captured stdout
    stdin_filename: str | None = None  # file under /work fed to stdin (None => /dev/null)
    writable: bool = False  # True for compile (rw /work); False for graded runs (ro /work)


def supervisor_spec(spec: SandboxSpec, cfg: Settings) -> dict:
    """Build the JSON job spec handed to the in-container supervisor."""
    wall_limit_ms = spec.time_limit_ms + cfg.wall_time_grace_ms
    return {
        "command": spec.command,
        "stdin_path": f"/work/{spec.stdin_filename}" if spec.stdin_filename else None,
        "workdir": "/work",
        "wall_limit_ms": wall_limit_ms,
        "output_limit_bytes": spec.output_limit_bytes,
        "stderr_limit_bytes": 64 * 1024,
    }


def build_docker_command(
    spec: SandboxSpec,
    container_name: str,
    job_json: str,
    cfg: Settings = settings,
) -> list[str]:
    """Construct the hardened ``docker run`` argv. Pure — no side effects.

    Every flag here is part of the isolation/limit posture; see ARCHITECTURE.md
    §3.1 for the rationale behind each (and why, e.g., --pids-limit rather than
    --ulimit nproc, and why we disable swap by matching --memory-swap).
    """
    # The cgroup hard cap sits above the problem limit so the supervisor and
    # language runtime don't eat the submitter's budget; MLE is judged from the
    # measured peak vs the problem limit, with this cap as a backstop.
    container_mem_mb = spec.memory_limit_mb + cfg.memory_overhead_mb
    fsize_bytes = cfg.tmpfs_size_mb * 1024 * 1024
    mount_mode = "rw" if spec.writable else "ro"

    return [
        cfg.docker_binary,
        "run",
        "--detach",
        "--name",
        container_name,
        "--label",
        CONTAINER_LABEL,
        # --- isolation ---
        "--network",
        "none",  # no networking at all
        "--read-only",  # read-only root filesystem
        "--cap-drop",
        "ALL",  # drop all Linux capabilities
        "--security-opt",
        "no-new-privileges:true",  # block setuid privesc
        "--cgroupns",
        "private",  # own cgroup namespace
        "--user",
        cfg.sandbox_user,  # non-root
        # --- resource limits ---
        "--memory",
        f"{container_mem_mb}m",
        "--memory-swap",
        f"{container_mem_mb}m",  # == memory => swap disabled
        "--memory-swappiness",
        "0",
        "--cpus",
        str(cfg.docker_cpus),
        "--pids-limit",
        str(cfg.pids_limit),  # fork-bomb defense (per-container)
        "--shm-size",
        "16m",  # cap /dev/shm so it can't be uncapped RAM
        "--ulimit",
        f"nofile={cfg.open_files_limit}:{cfg.open_files_limit}",
        "--ulimit",
        f"fsize={fsize_bytes}:{fsize_bytes}",
        "--ulimit",
        "core=0",  # no core dumps
        # --- writable scratch (root fs stays read-only) ---
        "--tmpfs",
        f"/tmp:rw,noexec,nosuid,nodev,size={cfg.tmpfs_size_mb}m",
        "--volume",
        f"{spec.workdir_host}:/work:{mount_mode}",
        "--workdir",
        "/work",
        # --- image + the supervisor's job spec (single argv; no shell) ---
        cfg.sandbox_image,
        job_json,
    ]


def parse_execution_result(
    logs_stdout: bytes,
    inspect_exit_code: int | None,
    inspect_oom_killed: bool,
    host_timed_out: bool,
    host_wall_ms: int,
) -> ExecutionResult:
    """Turn the supervisor's JSON (+ host-side corroboration) into a result.

    Robust to the supervisor not reporting at all — which happens when the
    cgroup OOM killer takes down the whole group (PID 1 included). In that case
    we fall back to the container's exit code / OOM flag from ``docker inspect``.
    """
    payload = _extract_result_json(logs_stdout)

    if payload is not None:
        if not payload.get("ok", False):
            raise SandboxError(payload.get("error", "supervisor reported failure"))
        # Combine in-container observations with host corroboration: either
        # source seeing OOM/timeout is authoritative.
        oom = bool(payload.get("oom_killed")) or inspect_oom_killed
        timed_out = bool(payload.get("timed_out")) or host_timed_out
        return ExecutionResult(
            exit_code=payload.get("exit_code"),
            signal=payload.get("signal"),
            timed_out=timed_out,
            oom_killed=oom,
            wall_ms=int(payload.get("wall_ms", host_wall_ms)),
            cpu_ms=int(payload.get("cpu_ms", 0)),
            peak_kb=int(payload.get("peak_kb", 0)),
            stdout=base64.b64decode(payload.get("stdout_b64", "")),
            stderr=base64.b64decode(payload.get("stderr_b64", "")),
            stdout_truncated=bool(payload.get("stdout_truncated")),
            stderr_truncated=bool(payload.get("stderr_truncated")),
        )

    # No supervisor result. Infer from the container's exit status.
    if host_timed_out:
        return _synthetic(timed_out=True, host_wall_ms=host_wall_ms)
    if inspect_oom_killed or inspect_exit_code == 137:
        # 137 == 128 + SIGKILL(9): the OOM killer (or a kill) took the whole group.
        return _synthetic(oom_killed=True, host_wall_ms=host_wall_ms)
    raise SandboxError(
        f"sandbox produced no result (container exit code {inspect_exit_code}); "
        "the supervisor may have crashed"
    )


def _synthetic(
    *, timed_out: bool = False, oom_killed: bool = False, host_wall_ms: int
) -> ExecutionResult:
    """A best-effort result for the case where the supervisor couldn't report."""
    return ExecutionResult(
        exit_code=None,
        signal=9,
        timed_out=timed_out,
        oom_killed=oom_killed,
        wall_ms=host_wall_ms,
        cpu_ms=0,
        peak_kb=0,
        stdout=b"",
        stderr=b"",
        stdout_truncated=False,
        stderr_truncated=False,
    )


def _extract_result_json(logs_stdout: bytes) -> dict | None:
    text = logs_stdout.decode("utf-8", errors="replace")
    marker_at = text.rfind(RESULT_MARKER)
    if marker_at == -1:
        return None
    blob = text[marker_at + len(RESULT_MARKER) :].strip()
    # The marker is followed by a single JSON object on the rest of that line.
    blob = blob.splitlines()[0] if blob else ""
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------- #
# Docker-touching orchestration
# --------------------------------------------------------------------------- #


def run_in_sandbox(spec: SandboxSpec, cfg: Settings = settings) -> ExecutionResult:
    """Run ``spec`` in a fresh hardened container and return the result.

    Raises :class:`SandboxError` on infrastructure failure (the grader maps that
    to an IE verdict). Always removes the container, even on error/timeout.
    """
    container_name = f"oj-{uuid4().hex}"
    job_json = json.dumps(supervisor_spec(spec, cfg))
    args = build_docker_command(spec, container_name, job_json, cfg)

    wall_cap_s = (spec.time_limit_ms + cfg.wall_time_grace_ms) / 1000.0
    host_timeout_s = wall_cap_s + cfg.container_startup_grace_s

    container_id: str | None = None
    started = time.monotonic()
    try:
        start = subprocess.run(
            args, capture_output=True, text=True, timeout=cfg.container_startup_grace_s + 30
        )
        if start.returncode != 0:
            raise SandboxError(f"docker run failed: {start.stderr.strip()}")
        container_id = start.stdout.strip()

        host_timed_out = _wait_for_exit(container_id, host_timeout_s, cfg)
        host_wall_ms = int(round((time.monotonic() - started) * 1000))

        exit_code, oom_killed = _inspect(container_id, cfg)
        logs_stdout = _logs(container_id, cfg)

        return parse_execution_result(
            logs_stdout=logs_stdout,
            inspect_exit_code=exit_code,
            inspect_oom_killed=oom_killed,
            host_timed_out=host_timed_out,
            host_wall_ms=host_wall_ms,
        )
    finally:
        if container_id:
            _remove(container_id, cfg)


def _wait_for_exit(container_id: str, timeout_s: float, cfg: Settings) -> bool:
    """Block until the container exits; return True if the host backstop fired.

    The in-container watchdog should terminate the program well before this, so
    hitting this timeout means the container itself is wedged (or the supervisor
    was OOM-killed). In that case we kill it ourselves.
    """
    try:
        subprocess.run(
            [cfg.docker_binary, "wait", container_id],
            capture_output=True,
            timeout=timeout_s,
        )
        return False
    except subprocess.TimeoutExpired:
        subprocess.run([cfg.docker_binary, "kill", container_id], capture_output=True, timeout=30)
        subprocess.run([cfg.docker_binary, "wait", container_id], capture_output=True, timeout=30)
        return True


def _inspect(container_id: str, cfg: Settings) -> tuple[int | None, bool]:
    result = subprocess.run(
        [
            cfg.docker_binary,
            "inspect",
            "--format",
            "{{.State.ExitCode}} {{.State.OOMKilled}}",
            container_id,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return None, False
    parts = result.stdout.split()
    exit_code = int(parts[0]) if parts and parts[0].lstrip("-").isdigit() else None
    oom = len(parts) > 1 and parts[1] == "true"
    return exit_code, oom


def _logs(container_id: str, cfg: Settings) -> bytes:
    result = subprocess.run(
        [cfg.docker_binary, "logs", container_id],
        capture_output=True,
        timeout=30,
    )
    # The supervisor writes its result to stdout; that's all we need.
    return result.stdout


def _remove(container_id: str, cfg: Settings) -> None:
    subprocess.run(
        [cfg.docker_binary, "rm", "--force", container_id],
        capture_output=True,
        timeout=30,
    )


# --------------------------------------------------------------------------- #
# Image helpers
# --------------------------------------------------------------------------- #


def image_exists(cfg: Settings = settings) -> bool:
    result = subprocess.run(
        [cfg.docker_binary, "image", "inspect", cfg.sandbox_image],
        capture_output=True,
    )
    return result.returncode == 0


def build_image(cfg: Settings = settings) -> None:
    """Build the sandbox image from ``sandbox/Dockerfile``."""
    subprocess.run(
        [cfg.docker_binary, "build", "-t", cfg.sandbox_image, str(SANDBOX_DIR)],
        check=True,
    )
