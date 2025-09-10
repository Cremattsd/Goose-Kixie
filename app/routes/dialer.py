# routes/dialer.py
from fastapi import APIRouter, Request, HTTPException, Query
import os, hmac, hashlib
from typing import Optional, Dict, Any
from datetime import datetime, timezone

from ..schemas.kixie import KixieWebhook, SimpleContact
from ..services.realnex_api import (
    get_rn_token,
    get_or_create_contact_by_phone,
    post_history_for_contact,
    normalize_phone_e164ish,
    search_by_phone,
    create_contact,
    resolve_rn_context,   # NEW
)

router = APIRouter()

# ---- Env / Config ----
RN_PROJECT_KEY = os.getenv("RN_PROJECT_KEY")                 # Optional
RN_EVENT_TYPE_PHONE = int(os.getenv("RN_EVENT_TYPE_PHONE", "1"))
RN_STATUS_COMPLETED = int(os.getenv("RN_STATUS_COMPLETED", "0"))
RN_HISTORY_CONTACT_LINK_FIELD = os.getenv("RN_HISTORY_CONTACT_LINK_FIELD", "contactKey")

KIXIE_SIGNATURE_HEADER = os.getenv("KIXIE_SIGNATURE_HEADER", "X-Kixie-Signature")
KIXIE_WEBHOOK_SECRET = os.getenv("KIXIE_WEBHOOK_SECRET")  # optional HMAC secret

@router.get("/health/realnex")
async def health_realnex():
    token = get_rn_token()
    if not token:
        return {"has_jwt": False}
    ctx = await resolve_rn_context(token)
    return {
        "has_jwt": True,
        "resolved_user_key": bool(ctx.get("user_key")),
        "resolved_team_key": bool(ctx.get("team_key")),
        "sources": ctx.get("sources", []),
        "email": ctx.get("email"),
    }

def _verify_kixie_signature(raw: bytes, header_sig: Optional[str]) -> None:
    if not KIXIE_WEBHOOK_SECRET:
        return
    if not header_sig:
        raise HTTPException(status_code=401, detail="Missing signature")
    calc = hmac.new(KIXIE_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, header_sig):
        raise HTTPException(status_code=401, detail="Invalid signature")

def _to_utc_ms(iso_or_dt: Optional[str | datetime]) -> Optional[str]:
    if iso_or_dt is None:
        return None
    if isinstance(iso_or_dt, datetime):
        dt = iso_or_dt
    else:
        s = iso_or_dt
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

def _subject(a: KixieWebhook) -> str:
    try:
        return a.subject()
    except Exception:
        disp = (a.disposition or "").strip() or "Unknown"
        direction = (a.direction or "outbound").strip()
        return f"Call {direction} - {disp}"

def _notes(a: KixieWebhook) -> str:
    try:
        return a.notes()
    except Exception:
        lines = [
            f"Kixie {a.event} • {a.duration_sec or 0}s",
            f"From: {a.from_number or 'n/a'} → To: {a.to_number or 'n/a'}",
            f"Agent: {a.agent_email or 'n/a'}",
            f"Recording: {a.recording_url or 'n/a'}",
            f"Call ID: {a.call_id or 'n/a'}",
        ]
        return "\n".join(lines)

def _start_utc(a: KixieWebhook) -> Optional[str]:
    try:
        return a.start_utc_ms()
    except Exception:
        return _to_utc_ms(getattr(a, "started_at", None))

def _end_utc(a: KixieWebhook) -> Optional[str]:
    try:
        return a.end_utc_ms()
    except Exception:
        return _to_utc_ms(getattr(a, "ended_at", None))

def _history_payload(a: KixieWebhook, user_key: str, team_key: str) -> Dict[str, Any]:
    payload = {
        "userKey": user_key,
        "teamKey": team_key,
        "projectKey": RN_PROJECT_KEY,
        "published": True,
        "timeless": False,
        "startDate": _start_utc(a),
        "endDate": _end_utc(a),
        "eventTypeKey": RN_EVENT_TYPE_PHONE,
        "statusKey": RN_STATUS_COMPLETED,
        "subject": _subject(a),
        "notes": _notes(a),
        "user1": "Kixie",
        "user2": a.event,
        "user3": a.disposition,
        "user4": a.direction,
        "logical1": True,
    }
    return {k: v for k, v in payload.items() if v is not None}

@router.post("/webhooks/kixie")
async def kixie_webhook(payload: KixieWebhook, request: Request):
    raw = await request.body()
    _verify_kixie_signature(raw, request.headers.get(KIXIE_SIGNATURE_HEADER))

    rn_token = get_rn_token()
    if not rn_token:
        return {
            "status": "dry-run",
            "reason": "REALNEX_TOKEN/REALNEX_JWT not set",
            "history_body": _history_payload(payload, user_key="(unset)", team_key="(unset)"),
        }

    # 🔑 Resolve user/team from JWT + OData (no .env needed)
    ctx = await resolve_rn_context(rn_token)
    user_key, team_key = ctx.get("user_key"), ctx.get("team_key")
    if not user_key or not team_key:
        raise HTTPException(status_code=502, detail={"error": "Could not resolve user/team from JWT/OData", "context": ctx})

    # 1) Find/create contact
    target_raw = payload.to_number if (payload.direction or "outbound").lower() == "outbound" else payload.from_number
    contact_res = await get_or_create_contact_by_phone(
        rn_token,
        target_raw or "",
        team_key=team_key,
        first_name="Kixie",
        last_name="Unknown",
    )
    contact_key = contact_res.get("contactKey")
    if not contact_key:
        raise HTTPException(status_code=502, detail={"error": "Unable to resolve contactKey", "contact": contact_res})

    # 2) Build History payload (with resolved user/team)
    history_body = _history_payload(payload, user_key=user_key, team_key=team_key)

    # 3) Attach to contact + POST
    rn_resp = await post_history_for_contact(rn_token, contact_key, history_body)

    return {
        "status": rn_resp.get("status"),
        "link_field": RN_HISTORY_CONTACT_LINK_FIELD,
        "resolved": {"email": ctx.get("email"), "user_key": user_key, "team_key": team_key, "sources": ctx.get("sources", [])},
        "contactKey": contact_key,
        "history_post_body": {**history_body, RN_HISTORY_CONTACT_LINK_FIELD: contact_key},
        "realnex": rn_resp,
    }

# --- Helpers for manual testing ---
@router.get("/contacts/search")
async def contacts_search(phone: str = Query(..., description="Phone number to search (any format)")):
    rn_token = get_rn_token()
    if not rn_token:
        return {"status":"dry-run","reason":"REALNEX_TOKEN not set"}
    norm = normalize_phone_e164ish(phone)
    return await search_by_phone(rn_token, norm or phone)

@router.post("/contacts")
async def contacts_create(body: SimpleContact):
    rn_token = get_rn_token()
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
    payload = {k: v for k, v in payload.items() if v is not None}
    return await create_contact(rn_token, payload)
