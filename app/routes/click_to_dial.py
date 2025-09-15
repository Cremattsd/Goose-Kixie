# app/routes/click_to_dial.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Dict, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..services.db import get_db
from ..models.tenant import Tenant
from ..models.call_state import CallState
from ..services.kixie_api import make_call
from ..services.realnex_api import (
    get_rn_token,
    search_by_phone,
    search_contact_keys_by_phone_two_stage,
    normalize_phone_e164ish,
)
from ..services.links import contact_link

router = APIRouter(tags=["dialer"])

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

# ───────────────────────── Schemas ─────────────────────────

class MakeCallBody(BaseModel):
    agent_email: str = Field(..., description="Kixie user email to originate the call from")
    phone: str = Field(..., description="Target phone (any format)")
    displayname: Optional[str] = Field(None, description="Optional display name shown in Kixie")
    call_id: Optional[str] = Field(None, min_length=2, max_length=64, description="Client-generated call id; if omitted we generate")

# ───────────────────────── Helpers ─────────────────────────

async def _find_contact_key(token: str, e164: str) -> Optional[str]:
    # Try CRM-native search first
    crm = await search_by_phone(token, e164)
    if int(crm.get("status", 0)) // 100 == 2:
        data = crm.get("data") or crm.get("value") or crm.get("items") or crm
        rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        for row in rows:
            if isinstance(row, dict):
                for k in ("contactKey", "ContactKey", "Key", "key", "Id", "id"):
                    v = row.get(k)
                    if isinstance(v, str) and v:
                        return v
    # Fallback: OData two-stage
    od = await search_contact_keys_by_phone_two_stage(token, e164)
    if int(od.get("status", 0)) // 100 == 2 and od.get("contactKey"):
        return str(od["contactKey"])
    return None

# ───────────────────────── Endpoints ───────────────────────

@router.post("/dialer/call/make")
async def dialer_make_call(body: MakeCallBody, request: Request, db: Session = Depends(get_db)):
    """
    Click-to-dial: triggers Kixie Make-a-Call, starts CallState timer,
    and returns an optional RealNex deep link if a contact match exists.
    """
    _verify_goose_shared_secret(db, request)

    call_id = body.call_id or uuid4().hex[:16]
    target = normalize_phone_e164ish(body.phone)
    if not target:
        raise HTTPException(400, "Invalid phone")

    # Start/ensure call state
    row = db.get(CallState, call_id)
    if row is None:
        row = CallState(call_id=call_id)
        db.add(row)
    row.agent_email = body.agent_email or row.agent_email
    row.phone_e164  = target
    if not row.started_at:
        row.started_at = datetime.now(timezone.utc)
    row.updated_at  = datetime.now(timezone.utc)
    db.commit()

    # Fire Kixie event
    kx = await make_call(email=body.agent_email, target_e164=target, displayname=body.displayname or target)

    # Try to find CRM contact + return deeplink if template provided
    link: Optional[str] = None
    token = get_rn_token()
    if token:
        ck = await _find_contact_key(token, target)
        if ck:
            link = contact_link(ck)

    return {
        "ok": True,
        "call_id": call_id,
        "kixie": kx,
        "contact_link": link,
    }

@router.get("/contacts/{contact_key}/link")
def contact_deeplink(contact_key: str):
    """
    Return a RealNex deep link for a contact using RN_CONTACT_URL_TEMPLATE.
    """
    url = contact_link(contact_key)
    return {"contact_key": contact_key, "url": url}
