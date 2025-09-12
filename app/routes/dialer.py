from fastapi import APIRouter, Request, HTTPException, Query, Header
import os, hmac, hashlib, datetime as dt
from typing import Optional, Dict, Any

from ..schemas.kixie import KixieWebhook, SimpleContact
from ..services.realnex_api import (
    normalize_phone_e164ish,
    digits_only,
    search_by_phone,
    create_contact,
    create_history,
    get_rn_token,
    search_contact_keys_by_phone_two_stage,
    attach_recording_from_url,
)

router = APIRouter()

@router.get("/health/realnex")
def health_realnex():
    # Surface that we have a token
    has = bool(get_rn_token())
    return {"has_jwt": has}

def _verify_kixie_signature(raw: bytes, header_sig: Optional[str]) -> None:
    secret = os.getenv("KIXIE_WEBHOOK_SECRET")
    if not secret:
        return
    if not header_sig:
        raise HTTPException(status_code=401, detail="Missing signature")
    calc = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, header_sig):
        raise HTTPException(status_code=401, detail="Invalid signature")

# ─────────────── helpers ───────────────

def _as_utc_str(s: Optional[str], tz_hint: Optional[str]) -> Optional[str]:
    """
    Accept bare 'YYYY-MM-DDTHH:MM:SS' as local (tz_hint if valid, else US/Pacific),
    or ISO with offset. Return Zulu.
    """
    if not s:
        return None
    try:
        # if it already includes timezone
        dtobj = dt.datetime.fromisoformat(s)
        if dtobj.tzinfo is None:
            import zoneinfo
            tzname = tz_hint or os.getenv("DEFAULT_TZ", "America/Los_Angeles")
            try:
                tz = zoneinfo.ZoneInfo(tzname)
            except Exception:
                tz = zoneinfo.ZoneInfo("America/Los_Angeles")
            dtobj = dtobj.replace(tzinfo=tz)
        return dtobj.astimezone(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"
    except Exception:
        return None

def _subject_from(a: KixieWebhook) -> str:
    dirn = a.direction or "unknown"
    dispo = a.disposition or "unknown"
    return f"Call {dirn} - {dispo}"

def _notes_from(a: KixieWebhook) -> str:
    parts = [
        f"Kixie {a.event} • {a.duration_sec or 0}s",
        f"From: {a.from_number or ''} → To: {a.to_number or ''}",
        f"Agent: {a.agent_email or ''}",
    ]
    if a.recording_url:
        parts.append(f"Recording: {a.recording_url}")
    if a.call_id:
        parts.append(f"Call ID: {a.call_id}")
    if getattr(a, "agent_notes", None):
        parts.append(f"Notes: {getattr(a, 'agent_notes')}")
    return "\n".join(parts)

async def _find_existing_contact_key(token: str, number_raw: Optional[str]) -> Optional[str]:
    """
    Pipeline:
      1) CRM "search" (strict 2xx only).
      2) OData probe → candidate keys → CRM confirm phones.
    No auto-create here.
    """
    if not number_raw:
        return None
    e164 = normalize_phone_e164ish(number_raw)
    if not e164:
        return None

    # #1 CRM search (only trust 2xx)
    crm = await search_by_phone(token, e164)
    if int(crm.get("status", 0)) // 100 == 2:
        items = crm.get("data") or crm.get("value") or crm.get("items") or []
        if isinstance(items, list) and items:
            v0 = items[0]
            if isinstance(v0, dict):
                for k in ("Key", "key", "Id", "id", "ContactId", "contactId"):
                    if v0.get(k):
                        return str(v0[k])

    # #2 OData two-stage
    od = await search_contact_keys_by_phone_two_stage(token, e164)
    if int(od.get("status", 0)) // 100 == 2:
        return str(od.get("contactKey"))

    return None

# ─────────────── Routes ───────────────

@router.post("/webhooks/kixie")
async def kixie_webhook(
    payload: KixieWebhook,
    request: Request,
    x_user_tz: Optional[str] = Header(None, convert_underscores=False),  # "X-User-TZ"
):
    """
    Receives Kixie webhook, finds a *real* contact by phone via OData→CRM confirm, then logs History linked to Contact.
    If no match, we skip (no more blank contacts).
    """
    raw = await request.body()
    _verify_kixie_signature(raw, request.headers.get(os.getenv("KIXIE_SIGNATURE_HEADER", "X-Kixie-Signature")))

    rn_token = get_rn_token()
    if not rn_token:
        return {"status": 401, "error": "REALNEX_JWT not set"}

    # timezone preference (header wins; otherwise infer from JWT or env; we default in _as_utc_str)
    tz_pref = x_user_tz

    # which number is the customer?
    customer_num = payload.to_number if (payload.direction or "outbound") == "outbound" else payload.from_number

    # find contact
    contact_key = await _find_existing_contact_key(rn_token, customer_num)
    if not contact_key:
        return {
            "status": 202,
            "skipped": True,
            "reason": "No contact match; not creating contacts",
            "normalized_number": normalize_phone_e164ish(customer_num or ""),
            "tz_used": tz_pref or os.getenv("DEFAULT_TZ", "America/Los_Angeles"),
            "search": {"standard": True, "wide_two_stage": True},
        }

    # build History body
    start_utc = _as_utc_str(payload.started_at, tz_pref)
    end_utc = _as_utc_str(payload.ended_at, tz_pref)
    sub = _subject_from(payload)
    notes = _notes_from(payload)

    event_type_key = int(os.getenv("RN_EVENT_TYPE_PHONE", os.getenv("RN_EVENTTYPEKEY_CALL", "1")))
    status_key = int(os.getenv("RN_STATUS_COMPLETED", "0"))

    body: Dict[str, Any] = {
        "userKey": os.getenv("RN_USER_KEY") or None,  # optional; we often resolve from JWT server-side
        "published": True,
        "timeless": False,
        "startDate": start_utc,
        "endDate": end_utc,
        "eventTypeKey": event_type_key,
        "statusKey": status_key,
        "subject": sub,
        "notes": notes,
        "user1": "Kixie",
        "user2": payload.event or "",
        "user3": payload.disposition or "",
        "user4": payload.direction or "",
        "logical1": True,
        # Link directly to Contact (tenant uses contactKey)
        os.getenv("RN_HISTORY_CONTACT_LINK_FIELD", "contactKey"): contact_key,
    }
    # strip None
    body = {k: v for k, v in body.items() if v is not None}

    # create History
    created = await create_history(rn_token, body)

    out = {
        "status": created.get("status"),
        "link_field": os.getenv("RN_HISTORY_CONTACT_LINK_FIELD", "contactKey"),
        "contactKey": contact_key,
        "history_post_body": body,
        "realnex": created,
    }

    # optional: attach recording to the *contact* (not the history)
    if os.getenv("ATTACH_RECORDING_TO_CONTACT", "0") == "1" and payload.recording_url:
        attach = await attach_recording_from_url(rn_token, contact_key, payload.recording_url)
        out["attachment"] = attach

    return out

@router.get("/contacts/search")
async def contacts_search(phone: str = Query(..., description="Phone number to search (any format)")):
    rn_token = get_rn_token()
    if not rn_token:
        return {"status": "dry-run", "reason": "REALNEX_JWT/REALNEX_TOKEN not set"}
    norm = normalize_phone_e164ish(phone)
    return await search_by_phone(rn_token, norm or phone)

@router.post("/contacts")
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
