# app/models/app_setting.py
from datetime import datetime, timezone
from sqlalchemy import Column, String, Text, DateTime
from ..services.db import Base

class AppSetting(Base):
    """
    Simple key/value store for app-level settings.
    We also mirror values into os.environ at runtime for legacy code paths.
    """
    __tablename__ = "app_setting"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
