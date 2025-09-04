from sqlalchemy import Column, Integer, String, Boolean, ForeignKey
from ..services.db import Base

class UserMap(Base):
    __tablename__ = "user_map"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    kixie_user_id = Column(String, nullable=True)
    kixie_email = Column(String, nullable=True)
    rn_user_id = Column(String, nullable=True)
    rn_email = Column(String, nullable=True)

class DispoMap(Base):
    __tablename__ = "dispo_map"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    kixie_dispo = Column(String, nullable=False)
    rn_history_status = Column(String, nullable=True)
    create_task_bool = Column(Boolean, default=False)
    task_due_days = Column(Integer, default=2)
