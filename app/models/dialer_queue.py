from __future__ import annotations
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Integer, DateTime, func, JSON, Enum, UniqueConstraint, Text
import enum
from ..services.db import Base

class QueueStatus(str, enum.Enum):
    queued = "queued"
    processing = "processing"
    done = "done"
    dead = "dead"

class DialerQueue(Base):
    __tablename__ = "dialer_queue"
    __table_args__ = (UniqueConstraint("tenant_id", "call_id", name="uq_queue_tenant_callid"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    call_id: Mapped[str] = mapped_column(String(128), index=True)   # from Kixie
    direction: Mapped[str] = mapped_column(String(16))
    target_phone: Mapped[str] = mapped_column(String(20))           # normalized +E164
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[QueueStatus] = mapped_column(Enum(QueueStatus), default=QueueStatus.queued)
    next_attempt_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    payload: Mapped[dict] = mapped_column(JSON)                     # full Kixie payload (safe)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
