"""SQLAlchemy engine + session wiring.

The engine is created lazily on first use and cached for the lifetime of the process.
Tests that change `DATA_DIR` mid-process should call `reset_engine()` afterward.

`init_db()` is idempotent and called automatically the first time a session is opened,
so callers do not need to invoke it explicitly.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from scholarapp.config import load_settings
from scholarapp.db.models import Base

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
_db_initialized: bool = False


def get_engine() -> Engine:
    """Return a cached SQLAlchemy Engine bound to DATA_DIR/scholar.db."""
    global _engine
    if _engine is None:
        settings = load_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(f"sqlite:///{settings.db_path}", future=True)
    return _engine


def init_db() -> None:
    """Create all tables if missing. Idempotent."""
    global _db_initialized
    if _db_initialized:
        return
    Base.metadata.create_all(get_engine())
    _db_initialized = True


def reset_engine() -> None:
    """Drop the cached engine, factory, and initialization flag.

    Used by tests that override DATA_DIR after import time. Production code should not
    need this.
    """
    global _engine, _session_factory, _db_initialized
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
    _db_initialized = False


@contextmanager
def get_session() -> Iterator[Session]:
    """Yield a Session, committing on clean exit and rolling back on exception.

    `expire_on_commit=False` lets callers continue to read attributes from returned ORM
    objects after the context manager closes the session.
    """
    global _session_factory
    init_db()
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
