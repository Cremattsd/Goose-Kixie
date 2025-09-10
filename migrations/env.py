# migrations/env.py
from __future__ import annotations
import os, sys
from logging.config import fileConfig
from alembic import context
from sqlalchemy import create_engine, pool
from dotenv import load_dotenv

# Load .env for DATABASE_URL etc.
load_dotenv()

# Add project root to PYTHONPATH so we can import app.*
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# ----- Import your SQLAlchemy Base and models -----
from app.services.db import Base, DATABASE_URL  # Base from your app
# Import all models so Alembic "sees" their tables
from app.models.tenant import Tenant
from app.models.mappings import UserMap, DispoMap
from app.models.eventlog import EventLog
from app.models.dialer_queue import DialerQueue
from app.models.contact_cache import ContactCache

# ----- Alembic config -----
config = context.config
fileConfig(config.config_file_name)

# Use DATABASE_URL from env (fallback to your app default)
sqlalchemy_url = os.getenv("DATABASE_URL", DATABASE_URL)
config.set_main_option("sqlalchemy.url", sqlalchemy_url)

target_metadata = Base.metadata

def run_migrations_offline():
    context.configure(
        url=sqlalchemy_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online():
    connectable = create_engine(sqlalchemy_url, poolclass=pool.NullPool, future=True)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
