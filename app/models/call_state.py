# app/models/call_state.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, String, DateTime, Text
from sqlalchemy.orm import Mapped

from app.services.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CallState(Base):
    """
    Ephemeral state for an in-flight call so the UI can show a live timer and capture notes.
    Rows are created/updated by /dialer/call/* endpoints and cleaned up after webhook/end.
    """
    __tablename__ = "call_state"

    # We key this table by the external call_id provided by the client/Kixie
    call_id: Mapped[str] = Column(String(64), primary_key=True)

    # agent + target
    agent_email: Mapped[Optional[str]] = Column(String(255), nullable=True, index=True)
    phone_e164: Mapped[Optional[str]] = Column(String(32), nullable=True, index=True)

    # live inputs
    disposition: Mapped[Optional[str]] = Column(String(128), nullable=True)
    note: Mapped[Optional[str]] = Column(Text, nullable=True)

    # timing for live timer
    started_at: Mapped[Optional[datetime]] = Column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"CallState(call_id={self.call_id!r}, agent={self.agent_email!r}, phone={self.phone_e164!r})"
