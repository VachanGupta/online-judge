"""End-to-end tests of the Docker sandbox.

These actually start containers, so they're marked ``docker`` and skip when no
daemon is present. They prove the supervisor + isolation flags correctly
observe every outcome the verdict engine later classifies: clean exit, non-zero
exit, death-by-signal, wall-clock kill (TLE), OOM kill (MLE), and output
capping. The submissions here are tiny Python one-liners run directly — no
compile step — which keeps the focus squarely on the runner.
"""

from __future__ import annotations

import pytest

from app.runner.sandbox import SandboxSpec, run_in_sandbox

pytestmark = pytest.mark.docker


def _run(
    sandbox_workdir,
    command,
    *,
    time_limit_ms=2000,
    memory_limit_mb=128,
    output_limit_bytes=262144,
    stdin=None,
):
    if stdin is not None:
        (sandbox_workdir / "in.txt").write_text(stdin)
    spec = SandboxSpec(
        workdir_host=str(sandbox_workdir),
        command=command,
        time_limit_ms=time_limit_ms,
        memory_limit_mb=memory_limit_mb,
        output_limit_bytes=output_limit_bytes,
        stdin_filename="in.txt" if stdin is not None else None,
    )
    return run_in_sandbox(spec)


def test_clean_run_captures_stdout_and_usage(sandbox_image, sandbox_workdir):
    result = _run(
        sandbox_workdir,
        ["python3", "-c", "import sys; print(sys.stdin.read().strip().upper())"],
        stdin="hello world",
    )
    assert result.exited_cleanly
    assert result.stdout == b"HELLO WORLD\n"
    assert result.exit_code == 0 and result.signal is None
    assert not result.timed_out and not result.oom_killed
    assert result.wall_ms >= 0 and result.cpu_ms >= 0
    assert result.peak_kb > 0


def test_nonzero_exit_is_observed(sandbox_image, sandbox_workdir):
    result = _run(sandbox_workdir, ["python3", "-c", "import sys; sys.exit(3)"])
    assert result.exit_code == 3
    assert result.signal is None
    assert not result.exited_cleanly


def test_death_by_signal_is_observed(sandbox_image, sandbox_workdir):
    result = _run(
        sandbox_workdir,
        ["python3", "-c", "import os, signal; os.kill(os.getpid(), signal.SIGSEGV)"],
    )
    assert result.signal == 11  # SIGSEGV
    assert result.exit_code is None
    assert result.killed_by_signal


def test_wall_clock_timeout_is_killed(sandbox_image, sandbox_workdir):
    result = _run(sandbox_workdir, ["python3", "-c", "while True: pass"], time_limit_ms=400)
    assert result.timed_out is True
    assert not result.oom_killed
    # Killed at roughly the limit + grace, not allowed to run unbounded.
    assert result.wall_ms < 400 + 2000


def test_network_is_disabled(sandbox_image, sandbox_workdir):
    # Any socket connect should fail because the container has no network.
    code = (
        "import socket, sys\n"
        "try:\n"
        "    socket.create_connection(('8.8.8.8', 53), timeout=2)\n"
        "    print('CONNECTED')\n"
        "except OSError:\n"
        "    print('NO_NETWORK')\n"
    )
    result = _run(sandbox_workdir, ["python3", "-c", code])
    assert result.exited_cleanly
    assert result.stdout.strip() == b"NO_NETWORK"


def test_memory_limit_triggers_oom(sandbox_image, sandbox_workdir):
    # Allocate well past the cap (limit 64MB + overhead) so the kernel OOM-kills.
    result = _run(
        sandbox_workdir,
        ["python3", "-c", "b = bytearray(400 * 1024 * 1024); print(len(b))"],
        memory_limit_mb=64,
    )
    assert result.oom_killed is True
    assert not result.timed_out


def test_output_is_capped(sandbox_image, sandbox_workdir):
    result = _run(
        sandbox_workdir,
        ["python3", "-c", "print('x' * 1_000_000)"],
        output_limit_bytes=1024,
    )
    assert result.stdout_truncated is True
    assert len(result.stdout) <= 1024


def test_root_filesystem_is_read_only(sandbox_image, sandbox_workdir):
    # Writing outside the tmpfs /tmp must fail on the read-only root fs.
    code = (
        "try:\n"
        "    open('/oops.txt', 'w').write('x')\n"
        "    print('WROTE')\n"
        "except OSError:\n"
        "    print('READONLY')\n"
    )
    result = _run(sandbox_workdir, ["python3", "-c", code])
    assert result.stdout.strip() == b"READONLY"
