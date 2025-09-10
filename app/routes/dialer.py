# app/routes/dialer.py
from fastapi import APIRouter, Request, HTTPException, Query
import os, hmac, hashlib, re
from datetime import datetime, timezone
from typing import Optional

from ..schemas.kixie import KixieWebhook, SimpleContact
from ..services.realnex_api import (
    get_rn_token,
    resolve_rn_context,
    get_or_create_contact_by_phone,
    post_history_for_contact,
    normalize_phone_e164ish,
    search_by_phone,
    create_contact,
)

router = APIRouter()

# ---------- Utilities ----------

def _verify_kixie_signature(raw: bytes, header_sig: Optional[str]) -> None:
    """Enforce HMAC if KIXIE_WEBHOOK_SECRET is set."""
    secret = os.getenv("KIXIE_WEBHOOK_SECRET")
    if not secret:
        return  # no enforcement in dev
    if not header_sig:
        raise HTTPException(status_code=401, detail="Missing signature")
    calc = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, header_sig):
        raise HTTPException(status_code=401, detail="Invalid signature")

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)

def _looks_like_guid(s: Optional[str]) -> bool:
    return bool(s and _UUID_RE.match(s))

def _to_utc_z(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return None

def _str_or_none(x: Optional[str]) -> Optional[str]:
    if not x:
        return None
    x = x.strip()
    return x or None

# ---------- Health & debug ----------

@router.get("/health/realnex")
async def health_realnex():
    token = get_rn_token()
    has = bool(token)
    resolved = {}
    if has:
        resolved = await resolve_rn_context(token)
    return {
        "has_jwt": has,
        "resolved_user_key": bool(resolved.get("user_key")),
        "resolved_team_key": bool(resolved.get("team_key")),
        "sources": resolved.get("sources") or [],
        "email": resolved.get("email"),
    }

# ---------- Contacts helper endpoints (unchanged) ----------

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
        "Email": _str_or_none(body.email),
        "PrimaryPhone": normalize_phone_e164ish(body.phone) if body.phone else None,
        "Company": _str_or_none(body.company),
        "Source": "Goose",
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    return await create_contact(rn_token, payload)

# ---------- Kixie Webhook ----------

@router.post("/webhooks/kixie")
async def kixie_webhook(payload: KixieWebhook, request: Request):
    """
    Receives Kixie webhook, resolves RN user/team, finds/creates contact,
    and posts a History. Omits projectKey when empty or not a GUID.
    """
    # 1) optional HMAC verify
    raw = await request.body()
    _verify_kixie_signature(raw, request.headers.get("X-Kixie-Signature"))

    # 2) RN context (user/team)
    rn_token = get_rn_token()
    if not rn_token:
        raise HTTPException(status_code=500, detail="REALNEX_JWT/REALNEX_TOKEN not set")

    resolved = await resolve_rn_context(rn_token)
    user_key = resolved.get("user_key")
    team_key = resolved.get("team_key")
    if not user_key:
        raise HTTPException(status_code=500, detail="Unable to resolve userKey from RealNex")
    if not team_key:
        # We can still log history w/out team in many tenants, but better to surface it.
        # Continue anyway; omit teamKey if missing.
        pass

    # 3) Determine the customer phone (depends on direction)
    customer_num = payload.to_number if (payload.direction or "outbound").lower() == "outbound" else payload.from_number
    if not customer_num:
        raise HTTPException(status_code=500, detail="No customer phone found in payload")

    # 4) Find or create contact
    contact_res = await get_or_create_contact_by_phone(rn_token, customer_num, team_key=team_key)
    contact_key = contact_res.get("contactKey")
    if not contact_key:
        raise HTTPException(status_code=500, detail={"error": "Unable to resolve contactKey", "contact": contact_res})

    # 5) Build History body (RealNex History model)
    #    Convert start/end to UTC Z; if start is missing, derive from end - duration
    start_utc = _to_utc_z(payload.started_at)
    end_utc = _to_utc_z(payload.ended_at) or start_utc

    # Subject & notes
    direction = (payload.direction or "unknown").lower()
    subject = f"Call {direction} - {payload.disposition or 'Unknown'}"
    notes = (
        f"Kixie {payload.event or 'event'} • {payload.duration_sec or 0}s\n"
        f"From: {payload.from_number or ''} → To: {payload.to_number or ''}\n"
        f"Agent: {payload.agent_email or ''}\n"
        f"Recording: {payload.recording_url or ''}\n"
        f"Call ID: {payload.call_id or ''}"
    )

    # Safe projectKey (omit unless valid GUID or env provided and valid)
    env_proj = _str_or_none(os.getenv("RN_PROJECT_KEY"))
    project_key: Optional[str] = env_proj if _looks_like_guid(env_proj) else None

    # Base body
    history_body = {
        "userKey": user_key,
        "published": True,
        "timeless": False,
        "startDate": start_utc or end_utc,
        "endDate": end_utc or start_utc,
        "eventTypeKey": int(os.getenv("RN_EVENTTYPEKEY_CALL", os.getenv("RN_EVENTTYPEKEY_DEFAULT", "1"))),
        "statusKey": int(os.getenv("RN_STATUSKEY_DEFAULT", "0")),
        "subject": subject,
        "notes": notes,
        # metadata slots for QA/filters
        "user1": "Kixie",
        "user2": payload.event or "",
        "user3": payload.disposition or "",
        "user4": payload.direction or "",
        "logical1": True,
    }
    # only include team if we have it
    if team_key:
        history_body["teamKey"] = team_key
    # only include projectKey if it's a real GUID
    if project_key:
        history_body["projectKey"] = project_key

    # 6) Post History, linking to contact via services layer (uses RN_HISTORY_CONTACT_LINK_FIELD)
    rn_resp = await post_history_for_contact(rn_token, contact_key, history_body)

    # 7) Return composite view for debugging
    return {
        "status": rn_resp.get("status", 200) if isinstance(rn_resp, dict) else 200,
        "link_field": os.getenv("RN_HISTORY_CONTACT_LINK_FIELD", "contactKey"),
        "resolved": resolved,
        "contactKey": contact_key,
        "history_post_body": history_body,
        "realnex": rn_resp,
    }
