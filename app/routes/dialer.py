from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel
import os, hmac, hashlib, json
from typing import Optional

from ..services.realnex_api import (
    normalize_phone_e164ish,
    search_by_phone,
    create_contact,
    create_history,
    get_rn_token,
)

router = APIRouter()

class CallActivity(BaseModel):
    contact_id: Optional[str] = None
    company_id: Optional[str] = None
    agent_email: str
    direction: str
    disposition: str
    duration_sec: int
    started_at: str
    ended_at: str
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

@router.get("/health/realnex")
def health_realnex():
    return {"has_jwt": bool(get_rn_token())}

def _verify_kixie_signature(raw: bytes, header_sig: Optional[str]) -> None:
    secret = os.getenv("KIXIE_WEBHOOK_SECRET")
    if not secret:
        return
    if not header_sig:
        raise HTTPException(status_code=401, detail="Missing signature")
    calc = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, header_sig):
        raise HTTPException(status_code=401, detail="Invalid signature")

async def _ensure_contact_id(rn_token: str, phone_raw: Optional[str]) -> Optional[str]:
    phone = normalize_phone_e164ish(phone_raw) if phone_raw else None
    if not phone:
        return None
    found = await search_by_phone(rn_token, phone)
    if (found.get("status") == 200) and (found.get("data") or found.get("value") or found.get("items")):
        candidates = found.get("data") or found.get("value") or found.get("items") or []
        first = candidates[0] if isinstance(candidates, list) and candidates else None
        if isinstance(first, dict):
            for key in ("Id","id","ContactId","contactId","Key","key"):
                if key in first:
                    return str(first[key])
    created = await create_contact(rn_token, {
        "FirstName": "", "LastName": "", "PrimaryPhone": phone, "Source": "Kixie Auto-Create",
    })
    if created.get("status") in (200,201):
        data = created.get("data") or created
        for key in ("Id","id","ContactId","contactId","Key","key"):
            if key in data:
                return str(key)
    return None

def _history_payload(a: CallActivity) -> dict:
    return {
        "Subject": f"Call {a.direction} - {a.disposition}",
        "Notes": a.notes or f"Call duration {a.duration_sec} sec",
        "DurationSeconds": a.duration_sec,
        "StartedAt": a.started_at,
        "EndedAt": a.ended_at,
        "ContactId": a.contact_id,
        "CompanyId": a.company_id,
        "OwnerEmail": a.agent_email,
        "ExternalId": a.call_id,
        "Source": "Kixie",
        "IsCompleted": a.is_completed,
        "ActivityType": "Phone Call",
        "Result": a.disposition,
    }

@router.post("/webhooks/kixie")
async def kixie_webhook(request: Request):
    rn_token = get_rn_token()
    dry_run = not bool(rn_token)

    raw = await request.body()
    _verify_kixie_signature(raw, request.headers.get("X-Kixie-Signature"))
    p = json.loads(raw.decode("utf-8"))

    direction = p.get("direction","outbound")
    from_number = p.get("from_number") or p.get("caller_number")
    to_number   = p.get("to_number")   or p.get("callee_number")
    customer_num = to_number if direction == "outbound" else from_number

    a = CallActivity(
        agent_email=p.get("agent_email","unknown@realnex.com"),
        direction=direction,
        disposition=p.get("disposition") or p.get("call_result","unknown"),
        duration_sec=int(p.get("duration_sec") or p.get("call_duration") or 0),
        started_at=p.get("started_at") or p.get("start_time") or "",
        ended_at=p.get("ended_at") or p.get("end_time") or "",
        notes=f"Kixie event {p.get('event','call.completed')} (auto-logged)",
        recording_url=p.get("recording_url"),
        from_number=from_number, to_number=to_number, call_id=p.get("call_id"),
        is_completed=True
    )

    if not dry_run:
        a.contact_id = await _ensure_contact_id(rn_token, customer_num)

    if dry_run:
        return {"status":"dry-run","reason":"REALNEX_JWT/REALNEX_TOKEN not set","activity":a.model_dump()}

    resp = await create_history(rn_token, _history_payload(a))
    return {"status": resp.get("status"), "realnex": resp, "activity": a.model_dump()}

@router.get("/contacts/search")
async def contacts_search(phone: str = Query(...)):
    rn_token = get_rn_token()
    if not rn_token:
        return {"status":"dry-run","reason":"REALNEX_JWT/REALNEX_TOKEN not set"}
    norm = normalize_phone_e164ish(phone)
    return await search_by_phone(rn_token, norm or phone)

class SimpleContactIn(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None

@router.post("/contacts")
async def contacts_create(body: SimpleContactIn):
    rn_token = get_rn_token()
    if not rn_token:
        return {"status":"dry-run","reason":"REALNEX_JWT/REALNEX_TOKEN not set"}
    payload = {
        "FirstName": body.first_name or "",
        "LastName": body.last_name or "",
        "Email": body.email or None,
        "PrimaryPhone": normalize_phone_e164ish(body.phone) if body.phone else None,
        "Company": body.company or None,
        "Source": "Goose",
    }
    payload = {k:v for k,v in payload.items() if v is not None}
    return await create_contact(rn_token, payload)
