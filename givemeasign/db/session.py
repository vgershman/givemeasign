"""Sync SQLAlchemy engine + session helpers.

Async is fine too, but the pipeline is predominantly CPU/IO-bound batch work
with well-bounded concurrency; sync sessions keep error handling simple.
Swap to asyncio later if a stage truly needs it.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from givemeasign.config import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Transactional session. Commits on clean exit, rolls back on exception."""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
