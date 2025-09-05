import os, json, hashlib
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..services.db import get_db
from ..models.tenant import Tenant
from ..models.eventlog import EventLog
from ..services.crypto import decrypt
from ..services.realnex_api import (
    search_any, get_contacts, create_contact_by_number, create_history
)

router = APIRouter()

# ---------- helpers ----------
def find_tenant(db: Session, business_id: str):
    return (
        db.query(Tenant)
        .filter(Tenant.kixie_business_id == business_id, Tenant.active == True)
        .first()
    )

def idem_key(tenant_id: int, callid: str | None, event: str) -> str:
    return hashlib.sha256(f"{tenant_id}|{callid or ''}|{event}".encode()).hexdigest()

async def _find_or_create_contact(token: str, number_e164: str) -> dict:
    # try contacts search
    try:
        res = await get_contacts(token, {"q": number_e164})
        candidates = res.get("value", [])
    except Exception:
        candidates = []
    # fallback to global search (Contact only)
    if not candidates:
        try:
            res = await search_any(token, number_e164)
            candidates = [x for x in res.get("value", []) if x.get("entityType","").lower()=="contact"]
        except Exception:
            candidates = []
    # create if still nothing (robust helper handles RN validation quirks)
    if not candidates:
        created = await create_contact_by_number(token, number_e164)
        candidates = [created]
    return candidates[0]

# ---------- models ----------
class WebhookBody(BaseModel):
    businessid: str
    hookevent: str
    data: Dict[str, Any] = {}

# ---------- routes ----------
@router.get("/lookup")
async def lookup(number: str = Query(..., alias="number"), businessid: str = Query(None), db: Session = Depends(get_db)):
    tenant = db.query(Tenant).first() if not businessid else find_tenant(db, businessid)
    if not tenant:
        raise HTTPException(404, "No tenant configured")
    rn_token = decrypt(tenant.rn_jwt_enc)
    contact = await _find_or_create_contact(rn_token, number)

    cid = contact.get("id") or contact.get("objectKey") or contact.get("contactKey")
    url = f"https://crm.realnex.com/Contact/{cid}" if cid else "https://crm.realnex.com/Contact"
    return {
        "found": True,
        "contact": {
            "first_name": contact.get("firstName", ""),
            "last_name":  contact.get("lastName",  ""),
            "email":      contact.get("email",     ""),
            "phone_number": number,
            "contact_id": cid,
            "url": url
        }
    }

@router.post("/webhooks", summary="Kixie → Goose webhook (validated)")
async def webhooks(
    body: WebhookBody,
    x_goose_secret: str = Header(..., alias="X-Goose-Secret"),
    db: Session = Depends(get_db)
):
    tenant = find_tenant(db, body.businessid)
    if not tenant:
        raise HTTPException(404, "Unknown businessid")

    if x_goose_secret != tenant.webhook_secret:
        raise HTTPException(401, "Invalid signature")

    callid = body.data.get("callid") or body.data.get("id")
    key = idem_key(tenant.id, callid, body.hookevent)
    if db.query(EventLog).filter_by(idem_key=key).first():
        return {"ok": True, "duplicate": True}

    status, error = "ok", None
    try:
        rn_token = decrypt(tenant.rn_jwt_enc)
        num = (
            body.data.get("fromnumber164") or body.data.get("fromnumber")
            or body.data.get("customernumber")
            or body.data.get("tonumber164") or body.data.get("tonumber") or ""
        )
        if not num:
            raise HTTPException(400, "No phone number in payload")

        contact = await _find_or_create_contact(rn_token, num)
        cid = contact.get("id") or contact.get("objectKey") or contact.get("contactKey")
        if not cid:
            raise HTTPException(500, "Unable to resolve RealNex contact id")

        if body.hookevent.lower() in ("endcall", "disposition", "sms"):
            parts = []
            d = body.data.get("calltype") or body.data.get("direction")
            if d: parts.append(f"Direction: {d}")
            dur = body.data.get("duration")
            if dur is not None: parts.append(f"Duration: {dur}s")
            dispo = body.data.get("disposition")
            if dispo: parts.append(f"Disposition: {dispo}")
            rec = body.data.get("recordingurl")
            if rec: parts.append(f"Recording: {rec}")
            agent = body.data.get("userid") or body.data.get("agent")
            if agent: parts.append(f"Agent: {agent}")
            note = " | ".join(parts) if parts else body.hookevent

            await create_history(rn_token, {
                "entityType": "Contact",
                "entityId": cid,
                "note": note,
                "date": datetime.now(timezone.utc).isoformat()
            })

    except Exception as e:
        status, error = "error", str(e)

    ev = EventLog(
        tenant_id=tenant.id, event_type=body.hookevent, callid=callid,
        idem_key=key, payload_json=json.dumps(body.model_dump()), status=status, error=error
    )
    db.add(ev); db.commit()

    if error:
        raise HTTPException(500, error)
    return {"ok": True}
