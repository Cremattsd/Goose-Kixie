# app/services/db.py
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./goose.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    # Import models so SQLAlchemy knows them
    from ..models.tenant import Tenant  # existing in your repo
    from ..models.mappings import UserMap, DispoMap  # existing in your repo
    from ..models.eventlog import EventLog  # existing in your repo
    from ..models.dialer_queue import DialerQueue  # new
    Base.metadata.create_all(bind=engine)
