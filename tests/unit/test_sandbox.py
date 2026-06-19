"""Unit tests for the runner's pure logic: command building and result parsing.

These need no Docker daemon — they verify the security flags we emit and that
we correctly fold the supervisor's JSON together with host-side corroboration
(including the tricky fallbacks when the supervisor couldn't report at all).
"""

from __future__ import annotations

import base64
import json

import pytest

from app.config import Settings
from app.runner.result import SandboxError
from app.runner.sandbox import (
    RESULT_MARKER,
    SandboxSpec,
    build_docker_command,
    parse_execution_result,
    supervisor_spec,
)


@pytest.fixture
def cfg() -> Settings:
    return Settings()


def _spec(**overrides) -> SandboxSpec:
    base = {
        "workdir_host": "/tmp/oj-runs/sub-1",
        "command": ["./main"],
        "time_limit_ms": 1000,
        "memory_limit_mb": 256,
        "output_limit_bytes": 262144,
    }
    base.update(overrides)
    return SandboxSpec(**base)


def test_build_docker_command_has_isolation_flags(cfg):
    args = build_docker_command(_spec(), "oj-test", "{}", cfg)
    joined = " ".join(args)

    # Network, filesystem, capability, and namespace hardening.
    assert "--network" in args and args[args.index("--network") + 1] == "none"
    assert "--read-only" in args
    assert "--cap-drop" in args and args[args.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges:true" in args
    assert "--cgroupns" in args and args[args.index("--cgroupns") + 1] == "private"
    assert args[args.index("--user") + 1] == cfg.sandbox_user

    # Swap disabled by matching --memory and --memory-swap.
    mem = args[args.index("--memory") + 1]
    swap = args[args.index("--memory-swap") + 1]
    assert mem == swap
    assert "--memory-swappiness" in args

    # Fork-bomb defense is pids-limit, NOT ulimit nproc.
    assert "--pids-limit" in args
    assert "nproc" not in joined

    # tmpfs is locked down.
    tmpfs = args[args.index("--tmpfs") + 1]
    assert "/tmp:" in tmpfs and "noexec" in tmpfs and "nosuid" in tmpfs

    # The image and the supervisor job spec are the final two args.
    assert args[-2] == cfg.sandbox_image
    assert args[-1] == "{}"


def test_container_memory_includes_overhead(cfg):
    args = build_docker_command(_spec(memory_limit_mb=128), "oj-test", "{}", cfg)
    expected = f"{128 + cfg.memory_overhead_mb}m"
    assert args[args.index("--memory") + 1] == expected


def test_run_mount_is_readonly_compile_is_writable(cfg):
    run_args = build_docker_command(_spec(writable=False), "n", "{}", cfg)
    compile_args = build_docker_command(_spec(writable=True), "n", "{}", cfg)
    assert any(v.endswith(":/work:ro") for v in run_args)
    assert any(v.endswith(":/work:rw") for v in compile_args)


def test_supervisor_spec_sets_wall_cap_and_stdin(cfg):
    spec = supervisor_spec(_spec(time_limit_ms=2000, stdin_filename="in.txt"), cfg)
    assert spec["wall_limit_ms"] == 2000 + cfg.wall_time_grace_ms
    assert spec["stdin_path"] == "/work/in.txt"

    no_stdin = supervisor_spec(_spec(stdin_filename=None), cfg)
    assert no_stdin["stdin_path"] is None


def _logs(payload: dict, *, noise: str = "") -> bytes:
    return (noise + RESULT_MARKER + json.dumps(payload) + "\n").encode()


def test_parse_clean_run():
    payload = {
        "ok": True,
        "exit_code": 0,
        "signal": None,
        "timed_out": False,
        "oom_killed": False,
        "wall_ms": 12,
        "cpu_ms": 8,
        "peak_kb": 2048,
        "stdout_b64": base64.b64encode(b"42\n").decode(),
        "stderr_b64": "",
        "stdout_truncated": False,
        "stderr_truncated": False,
    }
    result = parse_execution_result(_logs(payload, noise="garbage\n"), 0, False, False, 20)
    assert result.exited_cleanly
    assert result.stdout == b"42\n"
    assert result.wall_ms == 12 and result.cpu_ms == 8 and result.peak_kb == 2048


def test_parse_ok_false_raises():
    with pytest.raises(SandboxError, match="boom"):
        parse_execution_result(_logs({"ok": False, "error": "boom"}), 1, False, False, 5)


def test_host_corroboration_overrides_supervisor():
    payload = {
        "ok": True,
        "exit_code": None,
        "signal": 9,
        "timed_out": False,
        "oom_killed": False,
        "wall_ms": 1,
        "cpu_ms": 1,
        "peak_kb": 1,
        "stdout_b64": "",
        "stderr_b64": "",
        "stdout_truncated": False,
        "stderr_truncated": False,
    }
    # Supervisor didn't notice OOM/timeout, but the host did — host wins.
    result = parse_execution_result(_logs(payload), None, True, True, 100)
    assert result.oom_killed is True
    assert result.timed_out is True


def test_missing_result_with_host_timeout_is_tle():
    result = parse_execution_result(b"no marker here", None, False, True, 1500)
    assert result.timed_out is True
    assert result.wall_ms == 1500


def test_missing_result_with_oom_flag_is_mle():
    result = parse_execution_result(b"", None, True, False, 30)
    assert result.oom_killed is True


def test_missing_result_with_137_is_mle():
    result = parse_execution_result(b"", 137, False, False, 30)
    assert result.oom_killed is True


def test_missing_result_otherwise_raises():
    with pytest.raises(SandboxError):
        parse_execution_result(b"", 1, False, False, 30)
