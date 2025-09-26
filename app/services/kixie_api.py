from __future__ import annotations

cat > app/services/kixie_api.py <<'PY'

import os, json
from typing import Optional, Dict, Any, List
import httpx

# ───────────────────────── Bases ─────────────────────────
def _kx_base() -> str:
    """Legacy REST base (still used for /v1/calls)."""
    return os.getenv("KIXIE_BASE_URL", "https://api.kixie.com").rstrip("/")

def _apig_base() -> str:
    """New APIG base for webhook admin."""
    return os.getenv("KIXIE_APIG_BASE_URL", "https://apig.kixie.com/app/v1/api").rstrip("/")

# ───────────────────────── Headers ─────────────────────────
def _kx_headers(api_key: str, business_id: Optional[str] = None) -> Dict[str, str]:
    h = {
        "Authorization": f"Bearer {api_key}",
        "X-API-KEY": api_key,   # many tenants require this
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if business_id:
        h["X-Business-Id"] = business_id
    return h

def _apig_headers() -> Dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

# ───────────────────────── Generic HTTP ─────────────────────────
async def _post(url: str, headers: Dict[str, str], json: Dict[str, Any]) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, headers=headers, json=json)
            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text[:2000]}
            return {
                "status": r.status_code,
                "url": str(r.request.url),
                "method": r.request.method,
                "request": {"json": json},
                "response": data,
            }
    except httpx.HTTPError as e:
        return {"status": 599, "error": str(e), "url": url, "request": {"json": json}}

async def _get(url: str, headers: Dict[str, str], params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url, headers=headers, params=params or {})
            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text[:2000]}
            return {
                "status": r.status_code,
                "url": str(r.request.url),
                "method": r.request.method,
                "params": params or {},
                "response": data,
            }
    except httpx.HTTPError as e:
        return {"status": 599, "error": str(e), "url": url, "params": params or {}}

async def _delete(url: str, headers: Dict[str, str]) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.delete(url, headers=headers)
            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text[:2000]}
            return {
                "status": r.status_code,
                "url": str(r.request.url),
                "method": r.request.method,
                "response": data,
            }
    except httpx.HTTPError as e:
        return {"status": 599, "error": str(e), "url": url}

# ───────────────────────── Make-a-Call (legacy /v1/calls) ─────────────────────────
async def _make_call_with_key(
    api_key: str,
    business_id: str,
    agent_email: str,
    to: str,
    displayname: Optional[str] = None,
    caller_id: Optional[str] = None,
    from_number: Optional[str] = None,
) -> Dict[str, Any]:
    url = f"{_kx_base()}/{os.getenv('KIXIE_CALL_PATH', '/v1/calls').lstrip('/')}"
    body: Dict[str, Any] = {
        "business_id": business_id,
        "email": agent_email,
        "agent_email": agent_email,
        "user_email": agent_email,
        "to": to,
    }
    if displayname:
        body["displayname"] = displayname
    if caller_id:
        body["caller_id"] = caller_id
        body.setdefault("from", caller_id)
    if from_number:
        body["from"] = from_number

    return await _post(url, _kx_headers(api_key, business_id), body)

async def make_call(
    agent_email: str,
    to: str,
    displayname: Optional[str] = None,
    caller_id: Optional[str] = None,
    from_number: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Env-wrapper used by routes.
    make_call(agent_email=..., to=..., displayname=..., caller_id=..., from_number=...)
    """
    # BYPASS for dev/testing
    if os.getenv("KIXIE_BYPASS", "").strip().lower() in {"1", "true", "yes"}:
        return {
            "status": 200,
            "bypass": True,
            "message": "KIXIE_BYPASS active—skipped remote call",
            "request": {
                "agent_email": agent_email,
                "to": to,
                "displayname": displayname or to,
                "caller_id": caller_id,
                "from": from_number or caller_id,
            },
        }

    api_key = os.getenv("KIXIE_API_KEY", "").strip()
    biz_id  = os.getenv("KIXIE_BUSINESS_ID", "").strip()
    from_number = from_number or os.getenv("KIXIE_CALLER_ID") or None

    if not api_key or not biz_id:
        return {
            "status": 202,
            "skipped": True,
            "reason": "Kixie credentials not configured",
            "echo": {"email": agent_email, "target": to, "displayname": displayname or to},
        }

    return await _make_call_with_key(api_key, biz_id, agent_email, to, displayname, caller_id, from_number)

# ───────────────────────── APIG Webhook Admin ─────────────────────────
def _normalize_listing_payload(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = resp.get("response") or resp.get("data") or resp
    if isinstance(data, list):
        return data
    for key in ("result", "webhooks", "data", "items", "value"):
        v = data.get(key) if isinstance(data, dict) else None
        if isinstance(v, list):
            return v
    return []

async def list_webhooks(api_key: str, business_id: str) -> Dict[str, Any]:
    """Compatibility shim: returns APIG getWebhooks response."""
    url = f"{_apig_base()}/getWebhooks"
    body = {"apikey": api_key, "businessid": business_id, "call": "getWebhooks"}
    return await _post(url, _apig_headers(), body)

async def delete_webhook(api_key: str, business_id: str, webhook_id: str | int) -> Dict[str, Any]:
    """Compatibility shim: APIG deletewebhook call."""
    url = f"{_apig_base()}/deletewebhook"
    body = {"apikey": api_key, "businessid": business_id, "call": "deletewebhook", "webhookid": int(webhook_id)}
    return await _post(url, _apig_headers(), body)

async def create_or_update_webhook(api_key: str, business_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure a webhook exists in APIG. If a webhook with the same name exists:
      - If location matches → noop
      - Else delete and recreate
    Expected payload keys (APIG): eventname, direction, callresult, disposition, runtime, name, location, headers(list or stringified), business_id optional
    """
    # list existing
    listing = await list_webhooks(api_key, business_id)
    items = _normalize_listing_payload(listing)
    desired_name = payload.get("name", "")
    desired_loc  = payload.get("location", "")

    found = None
    for item in items:
        nm = item.get("name") or item.get("webhookname") or ""
        if nm == desired_name:
            found = item
            break

    if found:
        wid = str(found.get("webhookid") or found.get("id") or "")
        loc = found.get("webhookLocation") or found.get("location") or found.get("url") or ""
        if wid and str(loc).strip() == str(desired_loc).strip():
            return {
                "status": 200,
                "action": "noop",
                "reason": "matching webhook already present",
                "webhook": {"id": wid, "name": desired_name, "location": loc},
                "listing_status": listing.get("status"),
            }
        if wid:
            await delete_webhook(api_key, business_id, wid)

    # create
    url  = f"{_apig_base()}/postwebhook"
    body = {
        "apikey": api_key,
        "businessid": business_id,
        "call": "postWebhook",
        "eventname": payload.get("eventname") or payload.get("event"),
        "direction": payload.get("direction", "all"),
        "callresult": payload.get("callresult", "all"),
        "disposition": payload.get("disposition", "all"),
        "runtime": payload.get("runtime", "realtime"),
        "name": desired_name,
        "location": desired_loc,
    }
    # headers: APIG accepts a stringified JSON array in many tenants
    hdrs = payload.get("headers")
    if isinstance(hdrs, list):
        body["headers"] = json.dumps(hdrs)
    elif isinstance(hdrs, str):
        body["headers"] = hdrs

    return await _post(url, _apig_headers(), body)
PY
