from fastapi import APIRouter, Request, HTTPException, Query
import os, hmac, hashlib, re
from datetime import datetime, timezone, timedelta
from typing import Optional, Union, List
from zoneinfo import ZoneInfo

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

# ---------- HMAC ----------

def _verify_kixie_signature(raw: bytes, header_sig: Optional[str]) -> None:
    secret = os.getenv("KIXIE_WEBHOOK_SECRET")
    if not secret:
        return
    if not header_sig:
        raise HTTPException(status_code=401, detail="Missing signature")
    calc = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, header_sig):
        raise HTTPException(status_code=401, detail="Invalid signature")

# ---------- Timezone helpers ----------

def _zone_or_utc(name: Optional[str]) -> ZoneInfo:
    if not name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")

def _domain_map_tz(email: Optional[str]) -> Optional[str]:
    """
    RN_TZ_DOMAIN_MAP like:
      realnex.com=America/Chicago;acme.co=Europe/London
    """
    if not email or "@" not in email:
        return None
    dom = email.split("@", 1)[1].lower().strip()
    raw = os.getenv("RN_TZ_DOMAIN_MAP", "")
    for pair in [p for p in raw.split(";") if p.strip()]:
        if "=" not in pair:
            continue
        d, t = pair.split("=", 1)
        if d.strip().lower() == dom:
            return t.strip()
    return None

def _pick_tz(request: Request, resolved: dict) -> ZoneInfo:
    # 1) Explicit header from upstream (ideal if Kixie can pass it)
    hdr = request.headers.get("X-User-TZ") or request.headers.get("X-Timezone")
    if hdr:
        return _zone_or_utc(hdr.strip())

    # 2) RealNex user record had timezone
    tz = resolved.get("tz")
    if tz:
        return _zone_or_utc(str(tz))

    # 3) Domain map
    dom_tz = _domain_map_tz(resolved.get("email"))
    if dom_tz:
        return _zone_or_utc(dom_tz)

    # 4) Tenant default
    return _zone_or_utc(os.getenv("RN_DEFAULT_TZ"))

# ---------- Misc utils ----------

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
def _looks_like_guid(s: Optional[str]) -> bool:
    return bool(s and _UUID_RE.match(s))

def _str_or_none(x: Optional[str]) -> Optional[str]:
    if not x:
        return None
    x = x.strip()
    return x or None

def _parse_dt(x: Union[str, int, float, datetime, None], tzinfo: ZoneInfo) -> Optional[datetime]:
    """Parse ISO8601/Z/epoch; if naive, assume tzinfo (user tz), then convert later."""
    if x is None:
        return None
    if isinstance(x, datetime):
        return x if x.tzinfo else x.replace(tzinfo=tzinfo)
    if isinstance(x, (int, float)):
        return datetime.fromtimestamp(x, tz=tzinfo)
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=tzinfo)
        except Exception:
            return None
    return None

def _to_utc_z(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

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
        "tz": resolved.get("tz"),
    }

# ---------- Contacts helpers ----------

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

# ---------- Webhook ----------

@router.post("/webhooks/kixie")
async def kixie_webhook(payload: KixieWebhook, request: Request):
    """
    Receives Kixie webhook, resolves RN user/team/tz, finds/creates contact by PHONE,
    and posts a History. Naive times use the user's timezone, then we store UTC.
    """
    raw = await request.body()
    _verify_kixie_signature(raw, request.headers.get("X-Kixie-Signature"))

    rn_token = get_rn_token()
    if not rn_token:
        raise HTTPException(status_code=500, detail="REALNEX_JWT/REALNEX_TOKEN not set")

    resolved = await resolve_rn_context(rn_token)
    user_key = resolved.get("user_key")
    team_key = resolved.get("team_key")
    if not user_key:
        raise HTTPException(status_code=500, detail="Unable to resolve userKey from RealNex")

    # per-user timezone
    user_tz = _pick_tz(request, resolved)

    direction = (payload.direction or "outbound").lower()
    customer_num = payload.to_number if direction == "outbound" else payload.from_number
    if not customer_num:
        raise HTTPException(status_code=500, detail="No customer phone found in payload")

    contact_res = await get_or_create_contact_by_phone(rn_token, customer_num, team_key=team_key)
    contact_key = contact_res.get("contactKey")
    if not contact_key:
        raise HTTPException(status_code=500, detail={"error": "Unable to resolve contactKey", "contact": contact_res})
    link_pref: List[str] = contact_res.get("link_preference") or ["contactKey","leadKey","partyKey","linkedTo"]

    # Time math in user's tz
    start_dt = _parse_dt(payload.started_at, user_tz)
    end_dt = _parse_dt(payload.ended_at, user_tz)
    dur = payload.duration_sec or 0
    if not start_dt and end_dt and dur:
        start_dt = end_dt - timedelta(seconds=dur)
    if not end_dt and start_dt and dur:
        end_dt = start_dt + timedelta(seconds=dur)
    if not start_dt and not end_dt:
        now_local = datetime.now(user_tz)
        end_dt = now_local
        start_dt = end_dt - timedelta(seconds=dur) if dur else end_dt
    start_utc = _to_utc_z(start_dt)
    end_utc = _to_utc_z(end_dt)

    subject = f"Call {direction} - {payload.disposition or 'Unknown'}"
    notes = (
        f"Kixie {payload.event or 'event'} • {dur}s\n"
        f"From: {payload.from_number or ''} → To: {payload.to_number or ''}\n"
        f"Agent: {payload.agent_email or ''}\n"
        f"Recording: {payload.recording_url or ''}\n"
        f"Call ID: {payload.call_id or ''}"
    )

    env_proj = os.getenv("RN_PROJECT_KEY")
    project_key = env_proj if (env_proj and _looks_like_guid(env_proj)) else None

    history_body = {
        "userKey": user_key,
        "published": True,
        "timeless": False,
        "startDate": start_utc,
        "endDate": end_utc,
        "eventTypeKey": int(os.getenv("RN_EVENTTYPEKEY_CALL", os.getenv("RN_EVENTTYPEKEY_DEFAULT", "1"))),
        "statusKey": int(os.getenv("RN_STATUSKEY_DEFAULT", "0")),
        "subject": subject,
        "notes": notes,
        "user1": "Kixie",
        "user2": payload.event or "",
        "user3": payload.disposition or "",
        "user4": payload.direction or "",
        "logical1": True,
    }
    if team_key:
        history_body["teamKey"] = team_key
    if project_key:
        history_body["projectKey"] = project_key

    rn_resp = await post_history_for_contact(rn_token, contact_key, history_body, preferred_fields=link_pref)
    link_field_used = rn_resp.get("link_field") if isinstance(rn_resp, dict) else None
    return {
        "status": rn_resp.get("status", 200) if isinstance(rn_resp, dict) else 200,
        "link_field": link_field_used,
        "resolved": resolved | {"tz_used": str(user_tz)},
        "contactKey": contact_key,
        "history_post_body": history_body,
        "realnex": rn_resp,
    }
