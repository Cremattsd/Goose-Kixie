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
    """
    Return ISO8601 in UTC (millisecond precision).
    If dt is naive, attach assume_tz (if provided) or UTC.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=assume_tz or timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class KixieWebhook(BaseModel):
    """
    Canonical Kixie webhook payload (tolerant and future-proof).
    """
    model_config = ConfigDict(extra="ignore")  # ignore unexpected/extra keys

    event: str = Field(default="call.completed", description="Kixie event type")
    direction: Literal["outbound", "inbound"] = "outbound"

    from_number: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("from_number", "fromNumber", "from", "caller"),
    )
    to_number: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("to_number", "toNumber", "to", "callee"),
    )
    agent_email: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("agent_email", "agentEmail", "userEmail", "agent"),
    )
    disposition: Optional[str] = None

    duration_sec: int = Field(
        default=0,
        description="Duration of call in seconds",
        validation_alias=AliasChoices("duration_sec", "duration", "durationSeconds"),
        ge=0,
    )

    started_at: Optional[datetime] = Field(
        default=None,
        validation_alias=AliasChoices("started_at", "start_time", "startedAt", "startTime"),
    )
    ended_at: Optional[datetime] = Field(
        default=None,
        validation_alias=AliasChoices("ended_at", "end_time", "endedAt", "endTime"),
    )

    recording_url: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("recording_url", "recordingUrl", "recording"),
    )
    call_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("call_id", "callId", "id"),
    )

    # user-entered notes from dialer
    agent_notes: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("agent_notes", "agentNotes", "notes"),
    )

    # ---------------- Validators ----------------

    @field_validator("started_at", "ended_at", mode="before")
    @classmethod
    def _coerce_dt(cls, v):
        """
        Accept:
          - datetime objects
          - ISO strings with 'Z' or timezone offsets
          - 'YYYY-MM-DD HH:MM[:SS[.mmm]]' (space separator)
          - Unix epoch seconds or milliseconds (int/float)
        """
        if v in (None, ""):
            return None
        if isinstance(v, datetime):
            return v

        # Numeric epoch?
        if isinstance(v, (int, float)):
            # Heuristic: treat large numbers as ms
            if v > 10**12:
                v = v / 1000.0
            return datetime.fromtimestamp(v, tz=timezone.utc)

        if isinstance(v, str):
            s = v.strip()
            if not s:
                return None
            # Space-separated date/time → ISO with 'T'
            if " " in s and "T" not in s:
                s = s.replace(" ", "T")
            # Trailing Z → explicit +00:00
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s)

        return v  # let Pydantic try (unlikely)

    @field_validator("duration_sec", mode="before")
    @classmethod
    def _coerce_duration(cls, v):
        """
        Accept strings; ignore 'ms' vs 's' units here—route can normalize if needed.
        """
        if v in (None, ""):
            return 0
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return 0
            # best-effort parse integer-like strings
            try:
                v = int(float(v))
            except Exception:
                return 0
        # If someone passed milliseconds by mistake (very large number), softly downscale.
        try:
            if isinstance(v, (int, float)) and v > 24 * 60 * 60 * 5:  # > 5 days is suspicious
                v = int(v / 1000)
        except Exception:
            pass
        return int(v)

    @model_validator(mode="after")
    def _validate_times(self):
        if self.started_at and self.ended_at and self.ended_at < self.started_at:
            raise ValueError("ended_at is before started_at")
        return self

    # ---------------- Convenience helpers ----------------

    def target_number(self) -> Optional[str]:
        """The phone we should use to find the contact."""
        return self.to_number if self.direction == "outbound" else self.from_number

    def subject(self) -> str:
        disp = (self.disposition or "").strip() or "Unknown"
        return f"Call {self.direction} - {disp}"

    def notes(self) -> str:
        lines = [
            f"Kixie {self.event} • {self.duration_sec or 0}s",
            f"From: {self.from_number or 'n/a'} → To: {self.to_number or 'n/a'}",
            f"Agent: {self.agent_email or 'n/a'}",
        ]
        if self.recording_url:
            lines.append(f"Recording: {self.recording_url}")
        if self.call_id:
            lines.append(f"Call ID: {self.call_id}")
        if self.agent_notes:
            lines.append(f"Notes: {self.agent_notes}")
        return "\n".join(lines)

    def start_utc_ms(self, assume_tz: Optional[timezone] = None) -> Optional[str]:
        return _to_utc_ms(self.started_at, assume_tz)

    def end_utc_ms(self, assume_tz: Optional[timezone] = None) -> Optional[str]:
        return _to_utc_ms(self.ended_at, assume_tz)


class SimpleContact(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None

    def full_name(self) -> str:
        parts = [self.first_name or "", self.last_name or ""]
        return " ".join([p for p in parts if p]).strip() or "Unknown"
