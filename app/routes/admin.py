# app/routes/admin.py
from __future__ import annotations

import os, json
from typing import Optional, Dict, Any
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from ..services.db import get_db
from ..models.tenant import Tenant
from ..models.app_setting import AppSetting

router = APIRouter(prefix="/admin", tags=["admin"])

# ───────────────────────── Security ─────────────────────────

def _verify_goose_shared_secret(db: Session, request: Request) -> None:
    rows = db.query(Tenant.id, Tenant.webhook_secret).all()
    if not rows:
        return
    provided = request.headers.get("X-Goose-Secret")
    if not provided:
        raise HTTPException(status_code=401, detail="X-Goose-Secret required")
    if provided not in {r.webhook_secret for r in rows}:
        raise HTTPException(status_code=401, detail="Invalid X-Goose-Secret")

# ───────────────────────── Schema ───────────────────────────

class SettingsPayload(BaseModel):
    """
    All fields are optional; only provided ones will be updated.
    Values are persisted to DB and reflected to process env.
    """
    AUTO_TASKS_DEFAULT: Optional[bool] = Field(None, description="Create follow-up task automatically on certain dispositions")
    AUTO_TASK_DISPOSITIONS: Optional[list[str]] = Field(None, description="Dispositions that trigger auto-task, e.g. ['Left VM','Call Back']")
    ATTACH_RECORDING_TO_CONTACT: Optional[bool] = Field(None, description="Attach recording URL/file to the Contact after call")
    RN_EVENT_TYPE_PHONE: Optional[int] = Field(None, description="RealNex EventTypeKey for phone call histories")
    RN_STATUS_COMPLETED: Optional[int] = Field(None, description="RealNex StatusKey for completed histories")
    RN_HISTORY_CONTACT_LINK_FIELD: Optional[str] = Field(None, description="Field name used to link history to contact (default: contactKey)")

# Env key → (type, encoder, decoder)
_TYPES: Dict[str, Any] = {
    "AUTO_TASKS_DEFAULT": (bool, lambda v: "1" if v else "0", lambda s: s in ("1","true","yes","y","on","True","TRUE")),
    "AUTO_TASK_DISPOSITIONS": (list, lambda v: ",".join(v), lambda s: [x.strip() for x in s.split(",")] if s else []),
    "ATTACH_RECORDING_TO_CONTACT": (bool, lambda v: "1" if v else "0", lambda s: s in ("1","true","yes","y","on","True","TRUE")),
    "RN_EVENT_TYPE_PHONE": (int, lambda v: str(int(v)), int),
    "RN_STATUS_COMPLETED": (int, lambda v: str(int(v)), int),
    "RN_HISTORY_CONTACT_LINK_FIELD": (str, str, str),
}

_DEFAULTS: Dict[str, str] = {
    "AUTO_TASKS_DEFAULT": "0",
    "AUTO_TASK_DISPOSITIONS": "Left VM,Voicemail,No Answer,Call Back,Follow Up",
    "ATTACH_RECORDING_TO_CONTACT": "0",
    "RN_EVENT_TYPE_PHONE": "1",
    "RN_STATUS_COMPLETED": "0",
    "RN_HISTORY_CONTACT_LINK_FIELD": "contactKey",
}

def _get_effective(db: Session) -> Dict[str, str]:
    # start with env, then DB overrides
    eff = dict(_DEFAULTS)
    # env first
    for k in _DEFAULTS:
        if (v := os.getenv(k)) is not None:
            eff[k] = v
    # DB overrides
    for row in db.query(AppSetting).all():
        eff[row.key] = row.value
    return eff

# ───────────────────────── Endpoints ────────────────────────

@router.get("/settings")
def get_settings(request: Request, db: Session = Depends(get_db)):
    _verify_goose_shared_secret(db, request)
    eff = _get_effective(db)
    # decode for friendly output
    out: Dict[str, Any] = {}
    for k, v in eff.items():
        _, _, dec = _TYPES[k]
        try:
            out[k] = dec(v)
        except Exception:
            out[k] = v
    return {"settings": out}

@router.put("/settings")
def update_settings(body: SettingsPayload, request: Request, db: Session = Depends(get_db)):
    _verify_goose_shared_secret(db, request)

    updates: Dict[str, str] = {}
    as_dict = body.model_dump(exclude_none=True)

    # validate & encode
    for k, val in as_dict.items():
        typ, enc, _ = _TYPES[k]
        if typ is list:
            if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
                raise HTTPException(400, f"{k} must be an array of strings")
        elif typ is bool and not isinstance(val, bool):
            raise HTTPException(400, f"{k} must be a boolean")
        elif typ is int:
            try:
                val = int(val)
            except Exception:
                raise HTTPException(400, f"{k} must be an integer")
        updates[k] = enc(val)

    # persist & mirror to env
    now = datetime.now(timezone.utc)
    for k, v in updates.items():
        row = db.get(AppSetting, k)
        if row is None:
            row = AppSetting(key=k, value=v, updated_at=now)
            db.add(row)
        else:
            row.value = v
            row.updated_at = now
        os.environ[k] = v  # reflect immediately for code paths using os.getenv
    db.commit()

    eff = _get_effective(db)
    return {"ok": True, "effective": eff}
