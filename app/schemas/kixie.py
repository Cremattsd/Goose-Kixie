# schemas/kixie.py
from __future__ import annotations

from typing import Optional, Literal
from pydantic import BaseModel, Field, field_validator, model_validator
from datetime import datetime, timezone

def _to_utc_ms(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

class KixieWebhook(BaseModel):
    # Minimal set; extend as needed
    event: str = Field(default="call.completed", description="Kixie event type")
    direction: Literal["outbound", "inbound"] = "outbound"
    from_number: Optional[str] = None
    to_number: Optional[str] = None
    agent_email: Optional[str] = None
    disposition: Optional[str] = None
    duration_sec: int = 0
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    recording_url: Optional[str] = None
    call_id: Optional[str] = None

    # --- Validators ---
    @field_validator("started_at", "ended_at", mode="before")
    @classmethod
    def _coerce_dt(cls, v):
        """Accept ISO strings with Z or timezone offsets; return datetime."""
        if v in (None, ""):
            return None
        if isinstance(v, datetime):
            return v
        # Pydantic handles most ISO forms, but normalize 'Z' → '+00:00'
        if isinstance(v, str) and v.endswith("Z"):
            v = v.replace("Z", "+00:00")
        return datetime.fromisoformat(v)

    @model_validator(mode="after")
    def _validate_times(self):
        if self.started_at and self.ended_at and self.ended_at < self.started_at:
            raise ValueError("ended_at is before started_at")
        return self

    # --- Convenience helpers for routes/services ---
    def start_utc_ms(self) -> Optional[str]:
        return _to_utc_ms(self.started_at)

    def end_utc_ms(self) -> Optional[str]:
        return _to_utc_ms(self.ended_at)

    def target_number(self) -> Optional[str]:
        """The phone we should use to find/create the contact."""
        return self.to_number if self.direction == "outbound" else self.from_number

    def subject(self) -> str:
        disp = (self.disposition or "").strip() or "Unknown"
        return f"Call {self.direction} - {disp}"

    def notes(self) -> str:
        lines = [
            f"Kixie {self.event} • {self.duration_sec or 0}s",
            f"From: {self.from_number or 'n/a'} → To: {self.to_number or 'n/a'}",
            f"Agent: {self.agent_email or 'n/a'}",
            f"Recording: {self.recording_url or 'n/a'}",
            f"Call ID: {self.call_id or 'n/a'}",
        ]
        return "\n".join(lines)

class SimpleContact(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None

    def full_name(self) -> str:
        parts = [self.first_name or "", self.last_name or ""]
        return " ".join([p for p in parts if p]).strip() or "Unknown"
