# app/services/db.py  (ADD one import line inside init_db)
import os
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base, scoped_session

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./goose_kixie.db")

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
    # Import all models here (order doesn't matter)
    from ..models import tenant          # noqa
    from ..models import eventlog        # noqa
    from ..models import dialer_queue    # noqa
    from ..models import call_state      # noqa
    from ..models import app_setting     # ← add this import  # noqa

    Base.metadata.create_all(bind=engine)
