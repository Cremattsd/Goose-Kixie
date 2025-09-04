from sqlalchemy import Column, Integer, String, Boolean, DateTime, func
from ..services.db import Base

class Tenant(Base):
    __tablename__ = "tenants"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=True)
    kixie_business_id = Column(String, nullable=False)
    kixie_api_key_enc = Column(String, nullable=False)
    rn_jwt_enc = Column(String, nullable=False)
    webhook_secret = Column(String, nullable=False)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
