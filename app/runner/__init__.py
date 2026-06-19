"""The runner: Docker-sandboxed execution and resource-limit enforcement.

Populated in Phase 2. This package isolates everything that talks to Docker so
the rest of the system depends only on a small, well-typed ``ExecutionResult``
and never on the details of container orchestration.
"""
