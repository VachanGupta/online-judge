"""Database engine, session factory, and SQLite tuning.

Storage is intentionally swappable. The default is a single SQLite file, which
is perfect for a self-contained demo and even handles a small pool of
concurrent worker *processes* once WAL mode is enabled. For production you would
point ``OJ_DATABASE_URL`` at PostgreSQL and delete nothing else — the ORM models
and queue logic are written to portable SQL (see ARCHITECTURE.md).

SQLite concurrency notes
------------------------
Multiple worker processes hammering one SQLite file is the classic "database is
locked" trap. Three pragmas, applied on *every* new connection via a connect
event listener, make it well-behaved:

- ``journal_mode=WAL``   readers never block the single writer, and vice versa.
- ``busy_timeout=5000``  a writer waits (up to 5s) for the lock instead of
                         immediately erroring — combined with keeping write
                         transactions tiny (see ``app.queue``), contention is
                         resolved transparently.
- ``synchronous=NORMAL`` safe under WAL and markedly faster than FULL.

``foreign_keys=ON`` is also set because SQLite ignores FK constraints otherwise.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings


def _make_engine(database_url: str) -> Engine:
    """Build an Engine with storage-appropriate connection arguments."""
    connect_args: dict = {}
    if database_url.startswith("sqlite"):
        # check_same_thread=False: a single FastAPI process serves requests on a
        # thread pool; each request still gets its own Session, so this is safe.
        # timeout mirrors busy_timeout as a belt-and-braces against lock waits.
        connect_args = {"check_same_thread": False, "timeout": 5.0}

    engine = create_engine(
        database_url,
        connect_args=connect_args,
        pool_pre_ping=True,
        future=True,
    )

    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


# Process-wide engine and session factory. Worker processes are started with the
# "spawn" method, so each re-imports this module and gets its own engine — the
# correct setup (an Engine/connection must never be shared across processes).
engine: Engine = _make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create all tables. Idempotent; safe to call on every startup."""
    from app import models  # noqa: F401  (import registers the mapped classes)

    models.Base.metadata.create_all(bind=engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session context: commit on success, roll back on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency that yields a request-scoped session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


if __name__ == "__main__":  # pragma: no cover - tiny operational helper
    # `python -m app.db` creates the schema against OJ_DATABASE_URL.
    init_db()
    print(f"Initialised schema at {settings.database_url}")
