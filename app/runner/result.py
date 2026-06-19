"""The result of executing one command in the sandbox.

This is the *only* type the rest of the system needs from the runner: the
verdict engine and grader consume an ``ExecutionResult`` and never touch Docker.
Keeping stdout/stderr as ``bytes`` preserves byte fidelity for output
comparison; the verdict engine decodes when it compares.
"""

from __future__ import annotations

from dataclasses import dataclass


class SandboxError(RuntimeError):
    """Raised when the *judge infrastructure* fails (image missing, docker error,
    unreadable result) — as opposed to the submission misbehaving. The grader
    maps this to the IE (Internal Error) verdict so the submitter is not blamed.
    """


@dataclass(frozen=True)
class ExecutionResult:
    """Everything observed about a single sandboxed run."""

    # Process outcome. Exactly one of exit_code / signal is set for a process
    # that ran: exit_code for normal termination, signal for death-by-signal.
    exit_code: int | None
    signal: int | None

    # Limit signals, determined with defense in depth (see ARCHITECTURE.md §3.1).
    timed_out: bool  # our wall-clock watchdog had to kill it
    oom_killed: bool  # the kernel cgroup OOM killer fired

    # Measurements.
    wall_ms: int
    cpu_ms: int
    peak_kb: int  # child peak RSS (ru_maxrss), in KiB

    # Captured output (already capped at the configured limit).
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool

    @property
    def killed_by_signal(self) -> bool:
        return self.signal is not None

    @property
    def exited_cleanly(self) -> bool:
        """True iff the process exited 0 on its own (no signal, no limit kill)."""
        return (
            self.exit_code == 0
            and self.signal is None
            and not self.timed_out
            and not self.oom_killed
        )

    def stderr_snippet(self, limit: int = 4096) -> str:
        text = self.stderr.decode("utf-8", errors="replace")
        if len(text) > limit:
            return text[:limit] + "\n...[truncated]"
        return text
