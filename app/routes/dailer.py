# app/routes/dialer.py
from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel
import os, hmac, hashlib, json
from typing import Optional

from ..services.realnex_api import (
    normalize_phone_e164ish,
    search_by_phone,
    create_contact,
    create_activity,
)

router = APIRouter()

# ---------- Models ----------
class CallActivity(BaseModel):
    contact_id: Optional[str] = None
    company_id: Optional[str] = None
    agent_email: str
    direction: str            # inbound|outbound
    disposition: str          # answered|voicemail|missed|busy|etc
    duration_sec: int
    started_at: str           # ISO8601
    ended_at: str             # ISO8601
    notes: Optional[str] = None
    recording_url: Optional[str] = None
    from_number: Optional[str] = None
    to_number: Optional[str] = None
    call_id: Optional[str] = None

class PushListBody(BaseModel):
    list_name: str
    contacts: list[dict]

class CreateContactBody(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None

# ---------- Health ----------
@router.get("/health")
def health():
    return {"ok": True, "service": "goose-kixie-realnex"}

# ---------- Signature ----------
def _verify_kixie_signature(raw: bytes, header_sig: Optional[str]) -> None:
    secret = os.getenv("KIXIE_WEBHOOK_SECRET")
    if not secret:
        return  # signature not enforced if secret isn't set
    if not header_sig:
        raise HTTPException(status_code=401, detail="Missing signature")
    calc = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, header_sig):
        raise HTTPException(status_code=401, detail="Invalid signature")

# ---------- Utility: ensure ContactId (search → create) ----------
async def _ensure_contact_id(rn_token: str, phone_raw: Optional[str]) -> Optional[str]:
    phone = normalize_phone_e164ish(phone_raw) if phone_raw else None
    if not phone:
        return None

    # 1) search by phone
    found = await search_by_phone(rn_token, phone)
    if (found.get("status") == 200) and found.get("data") or found.get("value") or found.get("items"):
        # normalize possible shapes; prefer first id-like field
        candidates = found.get("data") or found.get("value") or found.get("items") or []
        first = candidates[0] if isinstance(candidates, list) and candidates else None
        if isinstance(first, dict):
            for key in ("Id","id","ContactId","contactId","Key","key"):
                if key in first:
                    return str(first[key])

    # 2) quick-create if not found
    created = await create_contact(rn_token, {
        "FirstName": "",
        "LastName": "",
        "PrimaryPhone": phone,
        "Source": "Kixie Auto-Create",
    })
    if created.get("status") in (200,201):
        for key in ("Id","id","ContactId","contactId","Key","key"):
            if key in created:
                return str(created[key])
        # sometimes payload returns in 'data'
        data = created.get("data") or {}
        for key in ("Id","id","ContactId","contactId","Key","key"):
            if key in data:
                return str(data[key])
    return None

# ---------- Webhook (Kixie → Goose) ----------
@router.post("/webhooks/kixie")
async def kixie_webhook(request: Request):
    rn_token = os.getenv("REALNEX_TOKEN","")
    if not rn_token:
        # We still accept, but only echo/dry-run
        dry_run = True
    else:
        dry_run = False

    raw = await request.body()
    _verify_kixie_signature(raw, request.headers.get("X-Kixie-Signature"))
    payload = json.loads(raw.decode("utf-8"))

    # Determine which number is the "customer" based on direction
    direction = payload.get("direction","outbound")
    from_number = payload.get("from_number")
    to_number   = payload.get("to_number")
    customer_num = to_number if direction == "outbound" else from_number

    # Build activity
    activity = CallActivity(
        agent_email=payload.get("agent_email","unknown@realnex.com"),
        direction=direction,
        disposition=payload.get("disposition","unknown"),
        duration_sec=int(payload.get("duration_sec",0)),
        started_at=payload.get("started_at",""),
        ended_at=payload.get("ended_at",""),
        notes=f"Kixie event {payload.get('event')} (auto-logged)",
        recording_url=payload.get("recording_url"),
        from_number=from_number,
        to_number=to_number,
        call_id=payload.get("call_id"),
    )

    # Resolve / create Contact
    contact_id = None
    if not dry_run:
        contact_id = await _ensure_contact_id(rn_token, customer_num)
    activity.contact_id = contact_id

    if dry_run:
        return {
            "status": "dry-run",
            "reason": "REALNEX_TOKEN not set",
            "activity": activity.model_dump(),
        }

    # Create RealNex Activity
    resp = await create_activity(rn_token, {
        "Subject": f"Call {activity.direction} - {activity.disposition}",
        "Notes": activity.notes,
        "DurationSeconds": activity.duration_sec,
        "StartedAt": activity.started_at,
        "EndedAt": activity.ended_at,
        "ContactId": activity.contact_id,
        "CompanyId": activity.company_id,   # optional
        "OwnerEmail": activity.agent_email,
        "ExternalId": activity.call_id,     # helpful for idempotency
        "Source": "Kixie",
    })

    return {"status": resp.get("status"), "realnex": resp, "activity": activity.model_dump()}

# ---------- Manual call log (Goose → RealNex) ----------
@router.post("/activities/call")
async def log_call(activity: CallActivity):
    rn_token = os.getenv("REALNEX_TOKEN","")
    if not rn_token:
        return {"status":"dry-run","reason":"REALNEX_TOKEN not set","activity":activity.model_dump()}
    resp = await create_activity(rn_token, {
        "Subject": f"Call {activity.direction} - {activity.disposition}",
        "Notes": activity.notes,
        "DurationSeconds": activity.duration_sec,
        "StartedAt": activity.started_at,
        "EndedAt": activity.ended_at,
        "ContactId": activity.contact_id,
        "CompanyId": activity.company_id,
        "OwnerEmail": activity.agent_email,
        "ExternalId": activity.call_id,
        "Source": "Kixie",
    })
    return {"status": resp.get("status"), "realnex": resp}

# ---------- Pass-through helpers ----------
@router.get("/contacts/search")
async def contacts_search(phone: str = Query(..., description="E.164 or raw; normalized in server")):
    rn_token = os.getenv("REALNEX_TOKEN","")
    if not rn_token:
        return {"status":"dry-run","reason":"REALNEX_TOKEN not set"}
    norm = normalize_phone_e164ish(phone)
    return await search_by_phone(rn_token, norm or phone)

@router.post("/contacts")
async def contacts_create(body: CreateContactBody):
    rn_token = os.getenv("REALNEX_TOKEN","")
    if not rn_token:
        return {"status":"dry-run","reason":"REALNEX_TOKEN not set"}
    payload = {
        "FirstName": body.first_name or "",
        "LastName": body.last_name or "",
        "Email": body.email or None,
        "PrimaryPhone": normalize_phone_e164ish(body.phone) if body.phone else None,
        "Company": body.company or None,
        "Source": "Goose",
    }
    # prune None
    payload = {k:v for k,v in payload.items() if v is not None}
    return await create_contact(rn_token, payload)

# ---------- Kixie power dialer (stub for now) ----------
@router.post("/kixie/lists/push")
async def push_list(body: PushListBody):
    # TODO: implement real Kixie list API call
    return {"pushed": True, "list_name": body.list_name, "count": len(body.contacts)}
