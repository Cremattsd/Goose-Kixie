from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, DateTime, Boolean, ForeignKey, Index, func, UniqueConstraint
)
from sqlalchemy.orm import relationship
from .tenant import Tenant
from ..services.db import Base

class DialerQueue(Base):
    __tablename__ = "dialer_queue"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False)

    campaign = Column(String(120), nullable=True)
    object_key = Column(String(64), nullable=False)  # RealNex Contact Key (GUID)
    name_first = Column(String(120), nullable=True)
    name_last  = Column(String(120), nullable=True)
    company    = Column(String(200), nullable=True)
    phone_e164 = Column(String(40),  nullable=False)
    email      = Column(String(200), nullable=True)

    status     = Column(String(20),  nullable=False, default="pending")  # pending|locked|done|skipped|error
    locked_by  = Column(String(200), nullable=True)
    locked_at  = Column(DateTime,    nullable=True)

    attempts       = Column(Integer,     nullable=False, default=0)
    last_result    = Column(String(400), nullable=True)
    last_called_at = Column(DateTime,    nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    tenant = relationship(Tenant, backref="dialer_items")

    __table_args__ = (
        UniqueConstraint("tenant_id", "campaign", "object_key", name="uq_queue_tenant_campaign_key"),
        Index("idx_queue_pending", "tenant_id", "campaign", "status"),
        Index("idx_queue_obj", "tenant_id", "object_key"),
    )
