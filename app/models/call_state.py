from datetime import datetime, timezone
from sqlalchemy import Column, String, DateTime, Text, Index
from ..services.db import Base

class CallState(Base):
    """
    Ephemeral state for an in-flight call. Cleaned after webhook merge.
    """
    __tablename__ = "call_state"

    # use call_id from Kixie (or your UI) as primary key
    call_id = Column(String(64), primary_key=True)

    # optional context
    agent_email = Column(String(255), nullable=True)
    phone_e164  = Column(String(32),  nullable=True)

    # live inputs
    disposition = Column(String(120), nullable=True)
    note        = Column(Text,        nullable=True)

    # timer
    started_at  = Column(DateTime(timezone=True), nullable=True)

    # housekeeping
    updated_at  = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("idx_call_state_agent", "agent_email"),
    )
