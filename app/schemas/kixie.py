from typing import Optional
from pydantic import BaseModel, Field

class KixieWebhook(BaseModel):
    # minimal set we use; you can extend anytime
    event: str = Field(default="call.completed", description="Kixie event type")
    direction: Optional[str] = Field(default="outbound")
    from_number: Optional[str] = None
    to_number: Optional[str] = None
    agent_email: Optional[str] = None
    disposition: Optional[str] = None
    duration_sec: Optional[int] = 0
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    recording_url: Optional[str] = None
    call_id: Optional[str] = None

class SimpleContact(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None
