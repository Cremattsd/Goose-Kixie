# app/routes/dialer.py
from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel
import os, hmac, hashlib, json
from typing import Optional, Dict, Any

from ..services.realnex_api import (
    normalize_phone_e164ish,
    search_by_phone,
    create_contact,
    create_history,
    get_rn_token,
)
from ..schemas.kixie import KixieWebhook

router = APIRouter()

# ------- Models -------

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

# ------- Helpers -------

def _verify_kixie_signature(raw: bytes, header_sig: Optional[str]) -> None:
    secret = os.getenv("KIXIE_WEBHOOK_SECRET")
    if not secret:
        return
    if not header_sig:
        raise HTTPException(status_code=401, detail="Missing signature")
    calc = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, header_sig):
        raise HTTPException(status_code=401, detail="Invalid signature")

def _event_type_key(disposition: str) -> int:
    """Choose an EventTypeKey via env (defaults: call=1, sms=2)."""
    disp = (disposition or "").lower()
    if "sms" in disp or "text" in disp:
        return int(os.getenv("RN_EVENTTYPEKEY_SMS", "2"))
    if "dispo" in disp:
        return int(os.getenv("RN_EVENTTYPEKEY_DISPOSITION", "1"))
    if "call" in disp or True:
        return int(os.getenv("RN_EVENTTYPEKEY_CALL", "1"))

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
    if created.get("status") in (200, 201):
        data = created.get("data") or created
        for key in ("Id","id","ContactId","contactId","Key","key"):
            if key in data:
                return str(data[key])
    return None

def _history_payload(a: CallActivity) -> Dict[str, Any]:
    payload = {
        "Subject": f"Call {a.direction} - {a.disposition}",
        "Notes": a.notes,
        "DurationSeconds": a.duration_sec,
        "StartDate": a.started_at,
        "EndDate": a.ended_at,
        "ContactKey": a.contact_id,
        "CompanyKey": a.company_id,
        "OwnerEmail": a.agent_email,
        "ExternalId": a.call_id,
        "Source": "Kixie",
        "IsCompleted": a.is_completed,
        "EventTypeKey": _event_type_key(a.disposition),
        "Direction": a.direction,
        "Result": a.disposition,
        "FromNumber": a.from_number,
        "ToNumber": a.to_number,
        "RecordingUrl": a.recording_url,
    }
    # strip None
    return {k: v for k, v in payload.items() if v is not None}

# ------- Routes -------

@router.get("/health/realnex")
def health_realnex():
    return {"has_jwt": bool(get_rn_token())}

@router.post("/webhooks/kixie", summary="Kixie Webhook")
async def kixie_webhook(payload: KixieWebhook, request: Request):
    rn_token = get_rn_token()
    dry_run = not bool(rn_token)

    raw = await request.body()
    _verify_kixie_signature(raw, request.headers.get("X-Kixie-Signature"))

    a = CallActivity(
        agent_email=payload.agent_email,
        direction=payload.direction,
        disposition=payload.disposition,
        duration_sec=payload.duration_sec,
        started_at=payload.started_at,
        ended_at=payload.ended_at or payload.started_at,
        notes=f"Kixie event {payload.event} (auto-logged)",
        recording_url=str(payload.recording_url) if payload.recording_url else None,
        from_number=payload.from_number,
        to_number=payload.to_number,
        call_id=payload.call_id,
        is_completed=True,
    )

    if not dry_run:
        customer_num = payload.to_number if payload.direction == "outbound" else payload.from_number
        a.contact_id = await _ensure_contact_id(rn_token, customer_num)

    hist_payload = _history_payload(a)
    if dry_run:
        return {"status": "dry-run", "reason": "REALNEX_JWT/REALNEX_TOKEN not set", "history": hist_payload, "activity": a.model_dump()}

    resp = await create_history(rn_token, hist_payload)
    return {"status": resp.get("status"), "realnex": resp, "history": hist_payload}

@router.post("/activities/call", summary="Create RealNex Call History")
async def log_call(activity: CallActivity):
    rn_token = get_rn_token()
    hist_payload = _history_payload(activity)
    if not rn_token:
        return {"status": "dry-run", "reason": "REALNEX_JWT/REALNEX_TOKEN not set", "history": hist_payload}
    resp = await create_history(rn_token, hist_payload)
    return {"status": resp.get("status"), "realnex": resp}

@router.get("/contacts/search", summary="Search contact by phone")
async def contacts_search(phone: str = Query(...)):
    rn_token = get_rn_token()
    if not rn_token:
        return {"status": "dry-run", "reason": "REALNEX_JWT/REALNEX_TOKEN not set"}
    norm = normalize_phone_e164ish(phone)
    return await search_by_phone(rn_token, norm or phone)

@router.post("/contacts", summary="Create contact")
async def contacts_create(body: SimpleContact):
    rn_token = get_rn_token()
    if not rn_token:
        return {"status": "dry-run", "reason": "REALNEX_JWT/REALNEX_TOKEN not set"}
    payload = {
        "FirstName": body.first_name or "",
        "LastName": body.last_name or "",
        "Email": body.email or None,
        "PrimaryPhone": normalize_phone_e164ish(body.phone) if body.phone else None,
        "Company": body.company or None,
        "Source": "Goose",
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    return await create_contact(rn_token, payload)
