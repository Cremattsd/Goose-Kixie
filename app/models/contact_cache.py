from __future__ import annotations
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, DateTime, func, UniqueConstraint
from ..services.db import Base

class ContactCache(Base):
    __tablename__ = "contact_cache"
    __table_args__ = (UniqueConstraint("tenant_id", "phone_e164", name="uq_contactcache_tenant_phone"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(index=True)
    phone_e164: Mapped[str] = mapped_column(String(20), index=True)
    rn_contact_key: Mapped[str] = mapped_column(String(64), index=True)

    last_seen_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
