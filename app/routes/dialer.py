# app/routes/dialer.py
from __future__ import annotations

from fastapi import APIRouter, Request, HTTPException, Query, Header
import os, hmac, hashlib
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any

from ..schemas.kixie import KixieWebhook, SimpleContact
from ..services.realnex_api import (
    normalize_phone_e164ish,
    digits_only,
    search_by_phone,                          # CRM-native search (if tenant supports)
    create_contact,
    create_history,
    get_rn_token,
    search_contact_keys_by_phone_two_stage,   # OData probe + CRM verify
    attach_recording_from_url,
    is_valid_timezone,
)

router = APIRouter()

# ─────────────────────────── Health ───────────────────────────

@router.get("/health/realnex")
def health_realnex():
    """Surface that we have a RealNex token configured."""
    return {"has_jwt": bool(get_rn_token())}

# ───────────────────────── Signature ──────────────────────────

def _verify_kixie_signature(raw: bytes, header_sig: Optional[str]) -> None:
    """HMAC-SHA256 check if KIXIE_WEBHOOK_SECRET is set; otherwise no-op."""
    secret = os.getenv("KIXIE_WEBHOOK_SECRET")
    if not secret:
        return
    if not header_sig:
        raise HTTPException(status_code=401, detail="Missing signature")
    calc = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, header_sig):
        raise HTTPException(status_code=401, detail="Invalid signature")

# ───────────────────────── Helpers ────────────────────────────

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
      1) CRM /Contact(s)/search (strict 2xx only).
      2) OData probe → candidate keys → CRM confirm phones.
    Never auto-creates.
    """
    if not number_raw:
        return None
    e164 = normalize_phone_e164ish(number_raw)
    if not e164:
        return None

    # #1 CRM search (only trust 2xx)
    crm = await search_by_phone(token, e164)
    if int(crm.get("status", 0)) // 100 == 2:
        # Try to extract contact key from typical shapes
        data = crm.get("data") or crm.get("value") or crm.get("items") or crm
        rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        for row in rows:
            if isinstance(row, dict):
                for k in ("contactKey", "ContactKey", "Key", "key", "Id", "id"):
                    v = row.get(k)
                    if isinstance(v, str) and v:
                        return v

    # #2 OData two-stage (probe phone fields; verify via CRM read)
    od = await search_contact_keys_by_phone_two_stage(token, e164)
    if int(od.get("status", 0)) // 100 == 2 and od.get("contactKey"):
        return str(od["contactKey"])

    return None

# ───────────────────────── Routes ─────────────────────────────

@router.get("/contacts/search")
async def contacts_search(phone: str = Query(..., description="Phone number to search (any format)")):
    """
    Search a contact by phone with tenant-safe fallback.
    1) Normalize to E.164.
    2) Try CRM /Contact(s)/search (strict 2xx only).
    3) Fallback to OData two-stage (probe phone fields, then CRM verify).
    """
    token = get_rn_token()
    if not token:
        raise HTTPException(status_code=401, detail="REALNEX_JWT/REALNEX_TOKEN not configured")

    normalized = normalize_phone_e164ish(phone) or ""
    if len(digits_only(normalized) or "") < 11:  # Require full E.164-like length
        return {
            "status": 400,
            "error": "phone_too_short",
            "hint": "Use full E.164 (e.g. +18584581063)",
            "normalized": normalized,
        }

    # 1) CRM-native search
    crm = await search_by_phone(token, normalized)
    if int(crm.get("status", 0)) // 100 == 2:
        # Extract key if possible
        key = None
        data = crm.get("data") or crm.get("value") or crm.get("items") or crm
        rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        for row in rows:
            if isinstance(row, dict):
                for k in ("contactKey", "ContactKey", "Key", "key", "Id", "id"):
                    v = row.get(k)
                    if isinstance(v, str) and v:
                        key = v
                        break
            if key:
                break
        return {"status": 200, "source": "crm_search", "normalized": normalized, "contactKey": key, "raw": crm}

    # 2) OData two-stage fallback
    wide = await search_contact_keys_by_phone_two_stage(token, phone)
    if int(wide.get("status", 0)) // 100 == 2 and wide.get("contactKey"):
        return {
            "status": 200,
            "source": "odata_two_stage",
            "normalized": normalized,
            "contactKey": wide["contactKey"],
            "probe_fields": wide.get("probe_fields", []),
        }

    return {
        "status": 404,
        "error": "no_contact_match",
        "normalized": normalized,
        "crm": crm,
        "fallback": wide,
    }

@router.post("/webhooks/kixie")
async def kixie_webhook(
    payload: KixieWebhook,
    request: Request,
    x_user_tz: Optional[str] = Header(None, convert_underscores=False),  # pass IANA tz like "America/Chicago"
):
    """
    Receives Kixie webhook, finds an existing contact by phone, and logs a History
    linked to that contact. We do NOT create contacts. If no match → 202 skipped.
    """
    raw = await request.body()
    _verify_kixie_signature(raw, request.headers.get(os.getenv("KIXIE_SIGNATURE_HEADER", "X-Kixie-Signature")))

    token = get_rn_token()
    if not token:
        raise HTTPException(status_code=401, detail="REALNEX_JWT/REALNEX_TOKEN not configured")

    # Determine assumed timezone for naive timestamps
    tz_assume = None
    if x_user_tz:
        try:
            # Optionally verify against RealNex dictionary; if not valid, we still try ZoneInfo
            if await is_valid_timezone(token, x_user_tz):
                tz_assume = ZoneInfo(x_user_tz)
            else:
                tz_assume = ZoneInfo(x_user_tz)  # last-resort attempt
        except Exception:
            tz_assume = None

    # Which number represents the customer we are logging against?
    target_number = payload.to_number if (payload.direction or "outbound") == "outbound" else payload.from_number

    # Find existing contact (no auto-creates)
    contact_key = await _find_existing_contact_key(token, target_number)
    if not contact_key:
        return {
            "status": 202,
            "skipped": True,
            "reason": "No contact match; not creating contacts",
            "normalized_number": normalize_phone_e164ish(target_number or ""),
            "tz_used": x_user_tz or "UTC",
            "search": {"wide_two_stage": True},
        }

    # Build History payload (use schema’s UTC helpers to avoid fromisoformat errors)
    start_iso = payload.start_utc_ms(tz_assume)
    end_iso   = payload.end_utc_ms(tz_assume)

    event_type_key = int(os.getenv("RN_EVENT_TYPE_PHONE", os.getenv("RN_EVENTTYPEKEY_CALL", "1")))
    status_key     = int(os.getenv("RN_STATUS_COMPLETED", "0"))
    link_field     = os.getenv("RN_HISTORY_CONTACT_LINK_FIELD", "contactKey")

    hist: Dict[str, Any] = {
        "published": True,
        "timeless": False,
        "startDate": start_iso,
        "endDate": end_iso,
        "eventTypeKey": event_type_key,
        "statusKey": status_key,
        "subject": _subject_from(payload),
        "notes": _notes_from(payload),
        "user1": "Kixie",
        "user2": payload.event or "",
        "user3": payload.disposition or "",
        "user4": payload.direction or "",
        "logical1": True,
        link_field: contact_key,
    }
    # Optional attribution (only include if configured)
    if os.getenv("RN_USER_KEY"):
        hist["userKey"] = os.getenv("RN_USER_KEY")
    if os.getenv("RN_TEAM_KEY"):
        hist["teamKey"] = os.getenv("RN_TEAM_KEY")

    # Create History
    rn = await create_history(token, {k: v for k, v in hist.items() if v is not None})

    out: Dict[str, Any] = {
        "status": rn.get("status", 200),
        "link_field": link_field,
        "contactKey": contact_key,
        "history_post_body": hist,
        "realnex": rn,
    }

    # Optional: attach recording to the CONTACT (not the history)
    if os.getenv("ATTACH_RECORDING_TO_CONTACT", "0") == "1" and payload.recording_url:
        out["attachment"] = await attach_recording_from_url(token, contact_key, payload.recording_url)

    return out

# ─────────────────────── Contact create (manual) ───────────────────────

@router.post("/contacts")
async def contacts_create(body: SimpleContact):
    """
    Manual contact create (dev/testing). Webhook path never auto-creates.
    """
    token = get_rn_token()
    if not token:
        raise HTTPException(status_code=401, detail="REALNEX_JWT/REALNEX_TOKEN not configured")

    payload = {
        "FirstName": body.first_name or "",
        "LastName": body.last_name or "",
        "Email": body.email or None,
        "PrimaryPhone": normalize_phone_e164ish(body.phone) if body.phone else None,
        "Company": body.company or None,
        "Source": "Goose",
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    return await create_contact(token, payload)
