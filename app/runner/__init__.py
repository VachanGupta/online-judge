"""The runner: Docker-sandboxed execution and resource-limit enforcement.

This package isolates everything that talks to Docker. The rest of the system
depends only on a small, well-typed :class:`ExecutionResult` and the
:func:`run_in_sandbox` entry point — never on the details of container
orchestration.
"""

from app.runner.result import ExecutionResult, SandboxError
from app.runner.sandbox import (
    SandboxSpec,
    build_docker_command,
    build_image,
    image_exists,
    parse_execution_result,
    run_in_sandbox,
)

__all__ = [
    "ExecutionResult",
    "SandboxError",
    "SandboxSpec",
    "build_docker_command",
    "build_image",
    "image_exists",
    "parse_execution_result",
    "run_in_sandbox",
]
