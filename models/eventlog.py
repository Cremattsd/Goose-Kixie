from sqlalchemy import Column, Integer, String, DateTime, func, Text
from ..services.db import Base

class EventLog(Base):
    __tablename__ = "event_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, nullable=False)
    event_type = Column(String, nullable=False)
    callid = Column(String, nullable=True)
    idem_key = Column(String, nullable=False, unique=True)
    payload_json = Column(Text, nullable=False)
    status = Column(String, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
