# app/schemas/kixie.py
from __future__ import annotations

from typing import Optional, Literal
from datetime import datetime, timezone
from pydantic import (
    BaseModel,
    Field,
    field_validator,
    model_validator,
    ConfigDict,
    AliasChoices,
)

def _to_utc_ms(dt: Optional[datetime], assume_tz: Optional[timezone] = None) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=assume_tz or timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

class KixieWebhook(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event: str = Field(default="call.completed")
    direction: Literal["outbound", "inbound"] = "outbound"

    from_number: Optional[str] = Field(default=None, validation_alias=AliasChoices("from_number","fromNumber","from","caller"))
    to_number: Optional[str]   = Field(default=None, validation_alias=AliasChoices("to_number","toNumber","to","callee"))
    agent_email: Optional[str] = Field(default=None, validation_alias=AliasChoices("agent_email","agentEmail","userEmail","agent"))
    disposition: Optional[str] = None

    duration_sec: int = Field(default=0, validation_alias=AliasChoices("duration_sec","duration","durationSeconds"), ge=0)

    started_at: Optional[datetime] = Field(default=None, validation_alias=AliasChoices("started_at","start_time","startedAt","startTime"))
    ended_at:   Optional[datetime] = Field(default=None, validation_alias=AliasChoices("ended_at","end_time","endedAt","endTime"))

    recording_url: Optional[str] = Field(default=None, validation_alias=AliasChoices("recording_url","recordingUrl","recording"))
    call_id: Optional[str]       = Field(default=None, validation_alias=AliasChoices("call_id","callId","id"))

    agent_notes: Optional[str]   = Field(default=None, validation_alias=AliasChoices("agent_notes","agentNotes","notes"))

    @field_validator("started_at", "ended_at", mode="before")
    @classmethod
    def _coerce_dt(cls, v):
        if v in (None, ""):
            return None
        if isinstance(v, datetime):
            return v
        if isinstance(v, (int, float)):
            if v > 10**12:
                v = v / 1000.0
            return datetime.fromtimestamp(v, tz=timezone.utc)
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return None
            if " " in s and "T" not in s:
                s = s.replace(" ", "T")
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s)
        return v

    @field_validator("duration_sec", mode="before")
    @classmethod
    def _coerce_duration(cls, v):
        if v in (None, ""):
            return 0
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return 0
            try:
                v = int(float(v))
            except Exception:
                return 0
        try:
            if isinstance(v, (int, float)) and v > 24 * 60 * 60 * 5:
                v = int(v / 1000)
        except Exception:
            pass
        return int(v)

    @model_validator(mode="after")
    def _validate_times(self):
        if self.started_at and self.ended_at and self.ended_at < self.started_at:
            raise ValueError("ended_at is before started_at")
        return self

    def target_number(self) -> Optional[str]:
        return self.to_number if self.direction == "outbound" else self.from_number

    def start_utc_ms(self, assume_tz: Optional[timezone] = None) -> Optional[str]:
        return _to_utc_ms(self.started_at, assume_tz)

    def end_utc_ms(self, assume_tz: Optional[timezone] = None) -> Optional[str]:
        return _to_utc_ms(self.ended_at, assume_tz)

class SimpleContact(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str]  = None
    email: Optional[str]      = None
    phone: Optional[str]      = None
    company: Optional[str]    = None

    def full_name(self) -> str:
        parts = [self.first_name or "", self.last_name or ""]
        return " ".join([p for p in parts if p]).strip() or "Unknown"
