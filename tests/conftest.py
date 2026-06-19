"""Shared pytest fixtures.

Unit tests run against an isolated in-memory SQLite database so they are fast
and never touch the developer's real ``judge.db``. Docker-backed tests use
markers (see ``pyproject.toml``) and are skipped automatically when Docker or
the sandbox image is unavailable.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator

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


def pytest_collection_modifyitems(config, items) -> None:
    """Skip ``@pytest.mark.docker`` tests when no Docker daemon is reachable."""
    if _docker_available():
        return
    skip_docker = pytest.mark.skip(reason="Docker daemon not available")
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip_docker)
