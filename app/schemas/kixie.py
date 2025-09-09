cat > app/schemas/kixie.py <<'PY'
from pydantic import BaseModel, Field
from typing import Optional

class KixieWebhook(BaseModel):
    event: str = Field(default="call.completed")
    direction: str = Field(default="outbound")
    from_number: Optional[str] = None
    to_number: Optional[str] = None
    agent_email: str = "unknown@realnex.com"
    disposition: str = "Unknown"
    duration_sec: int = 0
    started_at: str = ""
    ended_at: Optional[str] = None
    recording_url: Optional[str] = None
    call_id: Optional[str] = None

class CallActivity(BaseModel):
    contact_id: Optional[str] = None
    company_id: Optional[str] = None
    agent_email: str
    direction: str
    disposition: str
    duration_sec: int
    started_at: str
    ended_at: Optional[str] = None
    notes: Optional[str] = None
    recording_url: Optional[str] = None
    from_number: Optional[str] = None
    to_number: Optional[str] = None
    call_id: Optional[str] = None
    is_completed: bool = True
    due_date: Optional[str] = None

class SimpleContact(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None
PY
