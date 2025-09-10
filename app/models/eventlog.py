from __future__ import annotations
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Integer, DateTime, func, JSON, Index
from ..services.db import Base

class EventLog(Base):
    __tablename__ = "event_log"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    tenant_id: Mapped[int] = mapped_column(index=True)
    level: Mapped[str] = mapped_column(String(16), default="info")  # info|warn|error
    event_type: Mapped[str] = mapped_column(String(64), index=True) # e.g., "kixie.webhook", "realnex.post_history"
    source: Mapped[str] = mapped_column(String(64), default="app")
    correlation_id: Mapped[str | None] = mapped_column(String(128), index=True)  # call_id
    message: Mapped[str | None] = mapped_column(String(512))
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())

Index("ix_eventlog_tenant_event_created", EventLog.tenant_id, EventLog.event_type, EventLog.created_at)
