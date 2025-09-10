from __future__ import annotations
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Integer, Boolean, UniqueConstraint, DateTime, func
from ..services.db import Base

class UserMap(Base):
    __tablename__ = "user_map"
    __table_args__ = (UniqueConstraint("tenant_id", "agent_email", name="uq_user_map_tenant_agent"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    agent_email: Mapped[str] = mapped_column(String(255), index=True)  # Kixie agent email
    rn_user_key: Mapped[str] = mapped_column(String(64), index=True)
    rn_team_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class DispoMap(Base):
    __tablename__ = "dispo_map"
    __table_args__ = (UniqueConstraint("tenant_id", "kixie_disposition", name="uq_dispo_tenant_kixie"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    kixie_disposition: Mapped[str] = mapped_column(String(100), index=True)
    rn_status_key: Mapped[int] = mapped_column(Integer, default=0)     # maps to history.statusKey
    rn_event_type_key: Mapped[int] = mapped_column(Integer, default=0) # maps to history.eventTypeKey (optional override)

    auto_followup: Mapped[bool] = mapped_column(Boolean, default=False)
    followup_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
