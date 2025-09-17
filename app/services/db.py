# app/services/db.py
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base, Session


# ───────────────────────── Engine / Session ─────────────────────────

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./goose.db").strip()

# SQLite needs a special connect arg when used in a single-threaded dev server
_connect_args = {}
if DATABASE_URL.startswith("sqlite:///"):
    _connect_args = {"check_same_thread": False}

engine = create_engine(
    DATABASE_URL,
    future=True,
    pool_pre_ping=True,
    connect_args=_connect_args,
)

SessionLocal = sessionmaker(
    bind=engine,
    future=True,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)

Base = declarative_base()


# ───────────────────────── DB Helpers ─────────────────────────

def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency: yields a SQLAlchemy session and closes it after the request.
    """
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """
    Optional context manager for scripts/jobs.
    """
    db: Session = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """
    Import models and create tables (used by app/main.py when DB_CREATE_ALL=1).
    Safe to call multiple times.
    """
    # Import models so their metadata is registered on Base
    # (keep imports local to avoid circular import issues)
    from app.models import tenant  # noqa: F401
    from app.models import call_state  # noqa: F401

    Base.metadata.create_all(bind=engine)
