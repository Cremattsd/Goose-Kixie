# app/schemas/kixie.py
from pydantic import BaseModel, HttpUrl
from typing import Optional

class KixieWebhook(BaseModel):
    event: str
    direction: str
    from_number: Optional[str] = None
    to_number: Optional[str] = None
    agent_email: str
    disposition: str
    duration_sec: int
    started_at: str
    ended_at: Optional[str] = None
    recording_url: Optional[HttpUrl] = None
    call_id: Optional[str] = None
