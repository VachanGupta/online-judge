"""Shared pytest fixtures.

Unit tests run against an isolated in-memory SQLite database so they are fast
and never touch the developer's real ``judge.db``. Docker-backed tests use
markers (see ``pyproject.toml``) and are skipped automatically when Docker or
the sandbox image is unavailable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base


@pytest.fixture
def db_session() -> Iterator[Session]:
    """A fresh, isolated in-memory database session per test."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        # StaticPool keeps a single underlying connection so the in-memory DB
        # (which lives only as long as its connection) persists across the
        # create_all and the test's queries.
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def client(db_session: Session):
    """A FastAPI TestClient backed by an isolated in-memory database.

    The ``get_db`` dependency is overridden to share the test's in-memory engine
    so data persists across requests within a test. The app is *not* entered as
    a context manager, so its lifespan (which would create the real on-disk
    schema) does not run — keeping tests hermetic.
    """
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=15,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


@pytest.fixture(scope="session")
def docker_available() -> bool:
    return _docker_available()


@pytest.fixture(scope="session")
def sandbox_image(docker_available) -> str:
    """Ensure the sandbox image exists (build it if needed) and return its tag.

    Session-scoped so the (slow) build happens at most once per test run.
    """
    if not docker_available:
        pytest.skip("Docker daemon not available")
    from app.config import settings
    from app.runner import sandbox

    # Always build (not just when missing): Docker layer caching makes this ~1s
    # when nothing changed, but it guarantees tests never run against a stale
    # supervisor.py baked into an old image.
    sandbox.build_image()
    return settings.sandbox_image


@pytest.fixture
def sandbox_workdir() -> Iterator[Path]:
    """A scratch dir the sandbox container's non-root user (uid 1000) can access.

    Created under ``run_root`` (default /tmp/oj-runs) and made world-traversable,
    mirroring how the grader prepares scratch dirs. This matters on Linux, where
    bind mounts preserve host permissions: pytest's ``tmp_path`` lives under a
    ``0700`` directory the container user can't traverse (on macOS Docker Desktop
    the virtiofs uid mapping hides this, but CI on Linux does not).
    """
    from app.config import settings

    root = Path(settings.run_root)
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o777)
    except OSError:
        pass
    workdir = Path(tempfile.mkdtemp(prefix="oj-test-", dir=root))
    os.chmod(workdir, 0o777)
    try:
        yield workdir
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def pytest_collection_modifyitems(config, items) -> None:
    """Skip ``@pytest.mark.docker`` tests when no Docker daemon is reachable."""
    if _docker_available():
        return
    skip_docker = pytest.mark.skip(reason="Docker daemon not available")
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip_docker)
