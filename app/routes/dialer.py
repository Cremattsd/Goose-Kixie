cat > app/routes/dialer.py <<'PY'
from fastapi import APIRouter, Request, HTTPException, Header, Query
from typing import Optional, Any, Dict
import os, hmac, hashlib

from ..services.realnex_api import (
    normalize_phone_e164ish, search_by_phone, create_contact, create_history, get_rn_token
)
from ..schemas.kixie import KixieWebhook, CallActivity, SimpleContact

router = APIRouter()

@router.get("/health/realnex")
def health_realnex():
    return {"has_jwt": bool(get_rn_token())}

def _verify_kixie_signature(raw: bytes, header_sig: Optional[str]) -> None:
    secret = os.getenv("KIXIE_WEBHOOK_SECRET")
    if not secret:
        return  # signature optional in dev / swagger
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
    candidates = found.get("data") or found.get("value") or found.get("items") or []
    if isinstance(candidates, list) and candidates:
        first = candidates[0]
        if isinstance(first, dict):
            for k in ("Id","id","ContactId","contactId","Key","key"):
                if k in first and first[k]:
                    return str(first[k])
    created = await create_contact(rn_token, {
        "FirstName": "",
        "LastName": "",
        "PrimaryPhone": phone,
        "Source": "Kixie Auto-Create",
    })
    data = created.get("data") or created
    if isinstance(data, dict):
        for k in ("Id","id","ContactId","contactId","Key","key"):
            if k in data and data[k]:
                return str(data[k])
    return None

def _event_type_key(direction: str, kind: str = "CALL") -> int:
    if kind.upper() == "SMS":
        return int(os.getenv("RN_EVENTTYPEKEY_SMS", "2"))
    return int(os.getenv("RN_EVENTTYPEKEY_CALL", os.getenv("RN_EVENTTYPEKEY_DEFAULT","1")))

def _history_payload(a: CallActivity) -> Dict[str, Any]:
    payload = {
        "Subject": f"Call {a.direction} - {a.disposition}",
        "Notes": a.notes,
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
        "EventTypeKey": _event_type_key(a.direction, "CALL"),
    }
    date_field = os.getenv("RN_ODATA_HISTORY_DATE_FIELD", "Date")
    if a.ended_at:
        payload.setdefault(date_field, a.ended_at)
    elif a.started_at:
        payload.setdefault(date_field, a.started_at)
    if not a.is_completed and a.due_date:
        payload["DueDate"] = a.due_date
    return {k: v for k, v in payload.items() if v is not None}

@router.post("/webhooks/kixie")
async def kixie_webhook(
    payload: KixieWebhook,
    request: Request,
    x_kixie_signature: Optional[str] = Header(default=None, alias="X-Kixie-Signature")
):
    raw = await request.body()
    _verify_kixie_signature(raw, x_kixie_signature)

    rn_token = get_rn_token()
    dry_run = not bool(rn_token)

    direction = payload.direction or "outbound"
    customer_num = payload.to_number if direction == "outbound" else payload.from_number

    a = CallActivity(
        agent_email=payload.agent_email or "unknown@realnex.com",
        direction=direction,
        disposition=payload.disposition or "Unknown",
        duration_sec=payload.duration_sec or 0,
        started_at=payload.started_at or "",
        ended_at=payload.ended_at,
        notes=f"Kixie event {payload.event or 'call.completed'} (auto-logged)",
        recording_url=payload.recording_url,
        from_number=payload.from_number,
        to_number=payload.to_number,
        call_id=payload.call_id,
        is_completed=True,
    )

    if not dry_run:
        a.contact_id = await _ensure_contact_id(rn_token, customer_num)

    if dry_run:
        return {"status":"dry-run","reason":"REALNEX_JWT/REALNEX_TOKEN not set","activity":a.model_dump()}

    resp = await create_history(rn_token, _history_payload(a))
    return {"status": resp.get("status"), "realnex": resp, "activity": a.model_dump()}

@router.post("/activities/call")
async def log_call(activity: CallActivity):
    rn_token = get_rn_token()
    if not rn_token:
        return {"status":"dry-run","reason":"REALNEX_JWT/REALNEX_TOKEN not set","activity":activity.model_dump()}
    resp = await create_history(rn_token, _history_payload(activity))
    return {"status": resp.get("status"), "realnex": resp}

@router.get("/contacts/search")
async def contacts_search(phone: str = Query(..., description="Phone number to search (any format)")):
    rn_token = get_rn_token()
    if not rn_token:
        return {"status":"dry-run","reason":"REALNEX_JWT/REALNEX_TOKEN not set"}
    norm = normalize_phone_e164ish(phone)
    return await search_by_phone(rn_token, norm or phone)

@router.post("/contacts")
async def contacts_create(body: SimpleContact):
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
PY
