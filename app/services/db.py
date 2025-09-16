# app/services/db.py
import os
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base, scoped_session

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./goose.db")

engine_kwargs = {}
if DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, pool_pre_ping=True, **engine_kwargs)
SessionLocal = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db() -> None:
    """
    Import models so SQLAlchemy sees them, then create tables.
    """
    from ..models import tenant          # noqa
    from ..models import eventlog        # noqa  (ok if not present in your repo)
    from ..models import dialer_queue    # noqa  (ok if not present in your repo)
    from ..models import call_state      # noqa

    Base.metadata.create_all(bind=engine)
