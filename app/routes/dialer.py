# app/routes/dialer.py
from fastapi import APIRouter, Request, HTTPException, Query
import os, hmac, hashlib
from typing import Optional
from datetime import datetime
from zoneinfo import ZoneInfo

from ..schemas.kixie import KixieWebhook, SimpleContact
from ..services.realnex_api import (
    normalize_phone_e164ish,
    search_by_phone,
    create_contact,          # kept for completeness; not used when creation disabled
    create_history,
    get_rn_token,
    is_valid_timezone,
    attach_recording_from_url,
)

router = APIRouter()

# ---------- Config ----------
ALLOW_CREATE = os.getenv("RN_ALLOW_CONTACT_CREATE", "0").lower() not in ("0","false","no")
ATTACH_RECORDINGS = os.getenv("RN_ATTACH_RECORDINGS", "1").lower() not in ("0","false","no")
DEFAULT_TZ = os.getenv("RN_DEFAULT_TZ", "UTC")

def _domain_map() -> dict:
    raw = os.getenv("RN_TZ_DOMAIN_MAP", "")
    out = {}
    for pair in raw.split(";"):
        if "=" in pair:
            k,v = pair.split("=",1)
            out[k.strip().lower()] = v.strip()
    return out

TZ_DOMAIN_MAP = _domain_map()

# ---------- Health ----------
@router.get("/health/realnex")
async def health_realnex():
    token = get_rn_token()
    has = bool(token)
    return {
        "has_jwt": has,
    }

# ---------- Sig verify ----------
def _verify_kixie_signature(raw: bytes, header_sig: Optional[str]) -> None:
    secret = os.getenv("KIXIE_WEBHOOK_SECRET")
    if not secret:
        return
    if not header_sig:
        raise HTTPException(status_code=401, detail="Missing signature")
    calc = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, header_sig):
        raise HTTPException(status_code=401, detail="Invalid signature")

# ---------- Helpers ----------
async def _resolve_user_tz(rn_token: str, req: Request, agent_email: Optional[str]) -> str:
    # 1) X-User-TZ header wins
    hdr = req.headers.get("X-User-TZ")
    if hdr:
        if await is_valid_timezone(rn_token, hdr):
            return hdr

    # 2) domain→TZ map
    if agent_email and "@" in agent_email:
        dom = agent_email.split("@",1)[1].lower()
        tz = TZ_DOMAIN_MAP.get(dom)
        if tz and await is_valid_timezone(rn_token, tz):
            return tz

    # 3) default
    return DEFAULT_TZ

def _as_utc(s: Optional[str], local_tz: str) -> Optional[str]:
    if not s: return None
    # If offset provided, trust it; else assume local_tz.
    dt = None
    try:
        # with offset or 'Z'
        dt = datetime.fromisoformat(s.replace("Z","+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(local_tz))
    except Exception:
        try:
            naive = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
            dt = naive.replace(tzinfo=ZoneInfo(local_tz))
        except Exception:
            return None
    return dt.astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds").replace("+00:00","Z")

async def _ensure_contact_id(rn_token: str, phone_raw: Optional[str]) -> Optional[str]:
    """Find by phone only; DO NOT create."""
    phone = normalize_phone_e164ish(phone_raw) if phone_raw else None
    if not phone:
        return None
    found = await search_by_phone(rn_token, phone)
    items = found.get("data") or found.get("value") or found.get("items") or []
    if isinstance(items, list) and items:
        first = items[0] if items else None
        if isinstance(first, dict):
            for k in ("Id","id","ContactId","contactId","Key","key"):
                if k in first and first[k]:
                    return str(first[k])
    # no match
    return None

def _history_payload(a: KixieWebhook, start_utc: Optional[str], end_utc: Optional[str]) -> dict:
    event_type = int(os.getenv("RN_EVENTTYPEKEY_CALL", os.getenv("RN_EVENTTYPEKEY_DEFAULT", "1")))
    payload = {
        "Subject": f"Call {a.direction or 'unknown'} - {a.disposition or 'unknown'}",
        "Notes": (
            f"Kixie {a.event} • {a.duration_sec or 0}s\n"
            f"From: {a.from_number or ''} → To: {a.to_number or ''}\n"
            f"Agent: {a.agent_email or ''}\n"
            f"Recording: {a.recording_url or ''}\n"
            f"Call ID: {a.call_id or ''}"
        ),
        "DurationSeconds": a.duration_sec or 0,
        "startDate": start_utc,
        "endDate": end_utc,
        "EventTypeKey": event_type,
        "StatusKey": int(os.getenv("RN_STATUSKEY_DEFAULT","0")),
        "Published": True,
        # RN will attach to contact/lead/party when we add the proper link field later.
        # We DO NOT include a link field unless we have a valid GUID.
        "User1": "Kixie",
        "User2": a.event,
        "User3": a.disposition,
        "User4": a.direction,
        "Logical1": True,
    }
    # strip Nones
    return {k: v for k, v in payload.items() if v is not None}

# ---------- Routes ----------
@router.post("/webhooks/kixie")
async def kixie_webhook(payload: KixieWebhook, request: Request):
    """
    Receives Kixie webhook -> find contact (by phone) -> post History -> (optional) attach recording to contact.
    Never creates contacts.
    """
    raw = await request.body()
    _verify_kixie_signature(raw, request.headers.get(os.getenv("KIXIE_SIGNATURE_HEADER", "X-Kixie-Signature")))

    rn_token = get_rn_token()
    if not rn_token:
        raise HTTPException(status_code=500, detail="REALNEX_JWT/REALNEX_TOKEN not set")

    # Which number is the customer?
    customer_num = payload.to_number if (payload.direction or "outbound").lower() == "outbound" else payload.from_number

    # Resolve user timezone
    tz_used = await _resolve_user_tz(rn_token, request, payload.agent_email)

    # Convert to UTC strings
    start_utc = _as_utc(payload.started_at, tz_used)
    end_utc   = _as_utc(payload.ended_at, tz_used)

    # Find contact by phone (no auto-create)
    contact_id = await _ensure_contact_id(rn_token, customer_num)

    # If no match, do not post a History (prevents "Unknown" records downstream)
    if not contact_id:
        return {
            "status": 202,
            "skipped": True,
            "reason": "No contact match; not creating contacts",
            "normalized_number": normalize_phone_e164ish(customer_num) if customer_num else None,
            "tz_used": tz_used,
        }

    # Build History payload and inject link field
    history = _history_payload(payload, start_utc, end_utc)
    link_field = os.getenv("RN_HISTORY_CONTACT_LINK_FIELDS", "contactKey,leadKey,partyKey,linkedTo").split(",")[0].strip()
    if link_field:
        history[link_field] = contact_id

    # Post to RealNex
    resp = await create_history(rn_token, history)

    out = {
        "status": resp.get("status"),
        "link_field": link_field,
        "contactKey": contact_id,
        "tz_used": tz_used,
        "history_post_body": history,
        "realnex": resp,
    }

    # Attach recording to the contact (optional, only when we have a valid contact)
    if ATTACH_RECORDINGS and payload.recording_url:
        attach = await attach_recording_from_url(rn_token, contact_id, payload.recording_url)
        out["attachment"] = attach

    return out

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
    if not ALLOW_CREATE:
        raise HTTPException(status_code=403, detail="Contact creation disabled by RN_ALLOW_CONTACT_CREATE=0")
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
