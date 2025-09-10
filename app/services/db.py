# app/services/db.py
import os
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, text, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase

# -------------------------------------------------------------------
# Config
# -------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./goose.db").strip()
CREATE_ALL = os.getenv("DB_CREATE_ALL", "0") in ("1", "true", "True")

# -------------------------------------------------------------------
# Base (SQLAlchemy 2.x style)
# -------------------------------------------------------------------
class Base(DeclarativeBase):
    pass

# -------------------------------------------------------------------
# Engine factory with sane defaults per dialect
# -------------------------------------------------------------------
def _make_engine(url: str):
    is_sqlite = url.startswith("sqlite")
    kwargs = dict(
        pool_pre_ping=True,  # kill stale connections
        future=True,
    )

    if is_sqlite:
        # needed for FastAPI + threads; add SQLite niceties via events below
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # Postgres / MySQL style pools
        kwargs.update(
            pool_size=int(os.getenv("DB_POOL_SIZE", "5")),
            max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "10")),
            pool_recycle=int(os.getenv("DB_POOL_RECYCLE", "1800")),  # 30m
        )

    engine = create_engine(url, **kwargs)

    if is_sqlite:
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            # WAL improves concurrency; FK for integrity
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA foreign_keys=ON;")
            cur.close()

    return engine

engine = _make_engine(DATABASE_URL)

# -------------------------------------------------------------------
# Session
# -------------------------------------------------------------------
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    future=True,
)

# -------------------------------------------------------------------
# FastAPI dependency
# -------------------------------------------------------------------
def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

# Optional context manager for scripts / tasks
@contextmanager
def db_session() -> Generator:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

# -------------------------------------------------------------------
# Health check
# -------------------------------------------------------------------
def db_health() -> dict:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True, "url": DATABASE_URL.split('@')[-1]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# -------------------------------------------------------------------
# Schema init (dev-friendly)
# -------------------------------------------------------------------
def init_db() -> None:
    """
    Dev convenience. In prod, prefer Alembic migrations.
    Runs automatically if DB_CREATE_ALL=1 or if using SQLite default.
    """
    # Import models so SQLAlchemy registers them
    from ..models.tenant import Tenant            # your existing models
    from ..models.mappings import UserMap, DispoMap
    from ..models.eventlog import EventLog
    try:
        from ..models.dialer_queue import DialerQueue  # new
    except Exception:
        pass

    Base.metadata.create_all(bind=engine)

# Auto-init for local SQLite or when explicitly requested
if CREATE_ALL or DATABASE_URL.startswith("sqlite"):
    try:
        init_db()
    except Exception as _e:
        # Don’t crash app on create_all failure; just surface in logs
        import logging
        logging.getLogger("db").exception("init_db failed")
