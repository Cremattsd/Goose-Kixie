from fastapi import APIRouter, Request, HTTPException, Query
import os, hmac, hashlib
from typing import Optional

from ..schemas.kixie import KixieWebhook, SimpleContact
from ..services.realnex_api import (
    normalize_phone_e164ish,
    search_by_phone,
    create_contact,
    create_history,   # we'll post RealNex "History"
    get_rn_token,
)

router = APIRouter()

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
    # If Kixie uses a different scheme, adjust here:
    if not hmac.compare_digest(calc, header_sig):
        raise HTTPException(status_code=401, detail="Invalid signature")

async def _ensure_contact_id(rn_token: str, phone_raw: Optional[str]) -> Optional[str]:
    """Find by phone; if not found, create minimal contact and return its id."""
    phone = normalize_phone_e164ish(phone_raw) if phone_raw else None
    if not phone:
        return None

    found = await search_by_phone(rn_token, phone)
    # accept a few common response shapes
    items = found.get("data") or found.get("value") or found.get("items") or []
    if isinstance(items, list) and items:
        first = items[0]
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
    if created.get("status") in (200, 201):
        data = created.get("data") or created
        for k in ("Id","id","ContactId","contactId","Key","key"):
            if k in data and data[k]:
                return str(data[k])
    return None

def _history_payload(a: KixieWebhook, contact_id: Optional[str]) -> dict:
    # Map to RealNex History fields (adjust per your tenant)
    event_type = int(os.getenv("RN_EVENTTYPEKEY_CALL", os.getenv("RN_EVENTTYPEKEY_DEFAULT", "1")))
    payload = {
        "Subject": f"Call {a.direction or 'unknown'} - {a.disposition or 'unknown'}",
        "Notes": f"Kixie event {a.event} (auto-logged)",
        "DurationSeconds": a.duration_sec or 0,
        # Many tenants accept either of these; they can be adjusted:
        "Date": a.ended_at or a.started_at,   # RN_ODATA_HISTORY_DATE_FIELD default is "Date"
        "ContactId": contact_id,
        "OwnerEmail": a.agent_email,
        "ExternalId": a.call_id,
        "Source": "Kixie",
        "IsCompleted": True,
        "ActivityType": "Phone Call",
        "Result": a.disposition,
        "EventTypeKey": event_type,
        "RecordingUrl": a.recording_url,
        "FromNumber": a.from_number,
        "ToNumber": a.to_number,
    }
    # strip Nones
    return {k: v for k, v in payload.items() if v is not None}

@router.post("/webhooks/kixie")
async def kixie_webhook(payload: KixieWebhook, request: Request):
    """
    Receives Kixie webhook. Uses Pydantic model so Swagger shows a request body.
    """
    raw = await request.body()
    _verify_kixie_signature(raw, request.headers.get("X-Kixie-Signature"))

    rn_token = get_rn_token()
    dry_run = not bool(rn_token)

    customer_num = payload.to_number if (payload.direction or "outbound") == "outbound" else payload.from_number
    contact_id = None
    if rn_token and customer_num:
        contact_id = await _ensure_contact_id(rn_token, customer_num)

    activity = _history_payload(payload, contact_id)

    if dry_run:
        return {"status": "dry-run", "reason": "REALNEX_JWT/REALNEX_TOKEN not set", "activity": activity}

    resp = await create_history(rn_token, activity)
    return {"status": resp.get("status"), "realnex": resp, "activity": activity}

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
    payload = {k: v for k, v in payload.items() if v is not None}
    return await create_contact(rn_token, payload)
