"""Runtime configuration.

All settings are environment-driven (prefix ``OJ_``) with sensible local
defaults, so the system runs with zero configuration during development and is
fully tunable in Docker Compose / production via environment variables.

Example::

    OJ_DATABASE_URL=postgresql+psycopg://user:pass@db/judge
    OJ_WORKER_COUNT=4
    OJ_DEFAULT_MEMORY_LIMIT_MB=512
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OJ_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ----- Persistence -------------------------------------------------------
    # SQLite by default; storage is swappable. Point this at PostgreSQL in prod
    # (e.g. "postgresql+psycopg://user:pass@host/db") — see ARCHITECTURE.md.
    database_url: str = "sqlite:///./judge.db"

    # ----- Sandbox image -----------------------------------------------------
    sandbox_image: str = "online-judge-sandbox:latest"
    docker_binary: str = "docker"
    # Host directory under which per-submission scratch dirs are created. Each
    # run gets an isolated subdir that is bind-mounted into the container and
    # removed afterwards (even on failure).
    run_root: str = "/tmp/oj-runs"

    # ----- Default problem limits (overridable per problem) ------------------
    default_time_limit_ms: int = 2000
    default_memory_limit_mb: int = 256
    default_output_limit_kb: int = 256

    # ----- Sandbox enforcement knobs -----------------------------------------
    # CPUs available to a single run (cgroup cpu quota). 1.0 == one core.
    docker_cpus: float = 1.0
    # Hard cap on processes/threads inside the container (fork-bomb protection).
    pids_limit: int = 128
    # Open file descriptor cap (ulimit nofile).
    open_files_limit: int = 256
    # Size of the writable /tmp tmpfs handed to the program, in MiB.
    tmpfs_size_mb: int = 64
    # Non-root user the submission runs as inside the container ("uid:gid").
    sandbox_user: str = "1000:1000"
    # The supervisor and language runtime need a little headroom above the
    # *problem* memory limit; the container's hard --memory cap is set to
    # (problem_limit + this) so that MLE is judged from the measured peak RSS of
    # the user's process, with the container cap acting as a backstop. See
    # ARCHITECTURE.md ("Why two memory limits").
    memory_overhead_mb: int = 64
    # Grace added to the wall-clock limit before the in-container watchdog kills
    # the process (absorbs scheduling jitter).
    wall_time_grace_ms: int = 500
    # Extra wall-clock budget the *host* allows for container spin-up/teardown
    # before it force-kills a stuck container as a last resort.
    container_startup_grace_s: float = 8.0

    # ----- Compilation limits ------------------------------------------------
    compile_time_limit_ms: int = 15000
    compile_memory_limit_mb: int = 1024
    compile_output_limit_kb: int = 64

    # ----- Worker pool / queue ----------------------------------------------
    worker_count: int = 2
    worker_poll_interval_s: float = 0.5
    # A submission stuck in RUNNING longer than this (e.g. a crashed worker) is
    # requeued by the stale-claim reaper.
    stale_claim_timeout_s: int = 300
    # Cap on claim attempts before a repeatedly-failing submission is parked in a
    # terminal error state instead of being retried forever.
    max_attempts: int = 3
    # How often a worker runs the stale-claim reaper sweep (seconds).
    reaper_interval_s: float = 30.0
    # Stop grading at the first non-AC test (standard judge behaviour). When
    # False, every test runs so the full per-test report is always populated.
    fail_fast: bool = True

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton (cached)."""
    return Settings()


# Module-level convenience handle. Tests that need to override values should use
# ``get_settings.cache_clear()`` after monkeypatching the environment.
settings = get_settings()
