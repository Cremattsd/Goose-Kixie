import os, json, hashlib
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Request, HTTPException, Header, Query
from sqlalchemy.orm import Session
from ..services.db import get_db
from ..models.tenant import Tenant
from ..models.eventlog import EventLog
from ..services.crypto import decrypt
from ..services.realnex_api import search_any, get_contacts, create_contact, create_history

router = APIRouter()

def find_tenant(db: Session, business_id: str):
    return db.query(Tenant).filter(Tenant.kixie_business_id == business_id, Tenant.active == True).first()

def idem_key(tenant_id: int, callid: str | None, event: str) -> str:
    src = f"{tenant_id}|{callid or ''}|{event}"
    return hashlib.sha256(src.encode()).hexdigest()

async def _find_or_create_contact(token: str, number_e164: str) -> dict:
    # Try contacts endpoint first
    try:
        res = await get_contacts(token, {"q": number_e164})
        candidates = res.get("value", [])
    except Exception:
        candidates = []

    if not candidates:
        # fallback: global search (filter to Contact)
        try:
            res = await search_any(token, number_e164)
            candidates = [x for x in res.get("value", []) if x.get("entityType","").lower()=="contact"]
        except Exception:
            candidates = []

    if candidates:
        return candidates[0]

    # create lightweight contact if none found
    payload = {"mobile": number_e164, "firstName": "", "lastName": "", "source": "kixie"}
    return await create_contact(token, payload)

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

@router.post("/webhooks")
async def webhooks(req: Request, x_goose_secret: str | None = Header(None), db: Session = Depends(get_db)):
    payload = await req.json()
    businessid = payload.get("businessid") or payload.get("data",{}).get("businessid")
    if not businessid:
        raise HTTPException(400, "Missing businessid")
    tenant = find_tenant(db, businessid)
    if not tenant:
        raise HTTPException(404, "Unknown businessid")

    # Verify the shared secret we set during /install
    if not x_goose_secret or x_goose_secret != tenant.webhook_secret:
        raise HTTPException(401, "Invalid signature")

    hookevent = payload.get("hookevent") or payload.get("eventname") or payload.get("event") or "unknown"
    data = payload.get("data", payload)
    callid = data.get("callid") or data.get("id")
    key = idem_key(tenant.id, callid, hookevent)

    # dedupe
    if db.query(EventLog).filter_by(idem_key=key).first():
        return {"ok": True, "duplicate": True}

    status, error = "ok", None
    try:
        rn_token = decrypt(tenant.rn_jwt_enc)

        # choose a counterparty number (from > to > customer)
        num = data.get("fromnumber164") or data.get("fromnumber") \
           or data.get("customernumber") \
           or data.get("tonumber164") or data.get("tonumber") or ""

        if not num:
            raise HTTPException(400, "No phone number in payload")

        contact = await _find_or_create_contact(rn_token, num)
        cid = contact.get("id") or contact.get("objectKey") or contact.get("contactKey")
        if not cid:
            raise HTTPException(500, "Unable to resolve RealNex contact id")

        # MVP: write history only on endcall/disposition/sms
        if hookevent.lower() in ("endcall", "disposition", "sms"):
            parts = []
            d = data.get("calltype") or data.get("direction")
            if d: parts.append(f"Direction: {d}")
            dur = data.get("duration")
            if dur is not None: parts.append(f"Duration: {dur}s")
            dispo = data.get("disposition")
            if dispo: parts.append(f"Disposition: {dispo}")
            rec = data.get("recordingurl")
            if rec: parts.append(f"Recording: {rec}")
            agent = data.get("userid") or data.get("agent")
            if agent: parts.append(f"Agent: {agent}")
            note = " | ".join(parts) if parts else hookevent

            await create_history(rn_token, {
                "entityType": "Contact",
                "entityId": cid,
                "note": note,
                "date": datetime.now(timezone.utc).isoformat()
            })

        # (optional) add dispo->task rules later

    except Exception as e:
        status, error = "error", str(e)

    # Log every event (success or failure)
    ev = EventLog(
        tenant_id=tenant.id, event_type=hookevent, callid=callid,
        idem_key=key, payload_json=json.dumps(payload), status=status, error=error
    )
    db.add(ev); db.commit()

    if error:
        raise HTTPException(500, error)
    return {"ok": True}
