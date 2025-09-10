from __future__ import annotations
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Integer, DateTime, func, JSON, Boolean
from ..services.db import Base

class Tenant(Base):
    __tablename__ = "tenant"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    # RealNex config
    rn_user_key: Mapped[str] = mapped_column(String(64), index=True)
    rn_team_key: Mapped[str] = mapped_column(String(64), index=True)
    rn_project_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rn_event_type_phone: Mapped[int] = mapped_column(Integer, default=0)
    rn_status_completed: Mapped[int] = mapped_column(Integer, default=0)
    rn_history_link_field: Mapped[str] = mapped_column(String(32), default="contactKey")  # or partyKey

    # Encrypted secrets (store encrypted strings; use services/crypto to wrap)
    realnex_token_enc: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    kixie_api_key_enc: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    kixie_business_id_enc: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    kixie_webhook_secret_enc: Mapped[str | None] = mapped_column(String(4096), nullable=True)

    # Misc
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
