from __future__ import annotations

import os
from typing import Optional, Dict, Any, List
import httpx

# ───────────────────────── Config Helpers ─────────────────────────
def _kx_base() -> str:
    """Classic REST base used for calls (unchanged)."""
    return os.getenv("KIXIE_BASE_URL", "https://api.kixie.com").rstrip("/")

def _apig_base() -> str:
    """New APIG base used for webhooks admin."""
    return os.getenv("KIXIE_APIG_BASE_URL", "https://apig.kixie.com/app/v1/api").rstrip("/")

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

# ───────────────────────── Calls ─────────────────────────
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

# ───────────────────────── Webhook Admin via APIG ─────────────────────────
async def apig_get_webhooks(api_key: str, business_id: str) -> Dict[str, Any]:
    url = f"{_apig_base()}/getWebhooks"
    body = {"apikey": api_key, "businessid": business_id, "call": "getWebhooks"}
    # APIG expects JSON body (no bearer header)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, json=body, headers={"Content-Type": "application/json"})
            data = r.json() if r.content else {}
            return {"status": r.status_code, "url": url, "method": "POST", "request": {"json": body}, "response": data}
    except httpx.HTTPError as e:
        return {"status": 599, "url": url, "error": str(e), "request": {"json": body}}

async def apig_post_webhook(
    api_key: str,
    business_id: str,
    eventname: str,
    location: str,
    name: str,
    secret: str,
) -> Dict[str, Any]:
    url = f"{_apig_base()}/postwebhook"
    headers_json = f'[{{"name": "X-Goose-Secret", "value": "{secret}"}}]'
    body = {
        "apikey": api_key,
        "businessid": business_id,
        "call": "postWebhook",
        "eventname": eventname,
        "direction": "all",
        "callresult": "all",
        "disposition": "all",
        "runtime": "realtime",
        "name": name,
        "location": location,
        "headers": headers_json,
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, json=body, headers={"Content-Type": "application/json"})
            data = r.json() if r.content else {}
            return {"status": r.status_code, "url": url, "method": "POST", "request": {"json": body}, "response": data}
    except httpx.HTTPError as e:
        return {"status": 599, "url": url, "error": str(e), "request": {"json": body}}

async def create_or_update_webhook(api_key: str, business_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Idempotently ensure a webhook on APIG. We don't try to delete on dup—APIG
    returns an ER_DUP_ENTRY we can treat as success/noop.
    """
    desired_name = payload.get("name", "")
    desired_loc  = payload.get("location", "")
    eventname    = payload.get("eventname") or payload.get("event") or payload.get("event_name") or ""
    secret       = payload.get("secret") or payload.get("webhook_secret") or ""

    # First, see what's there (useful for debug/log)
    listing = await apig_get_webhooks(api_key, business_id)

    # Create/ensure
    resp = await apig_post_webhook(api_key, business_id, eventname, desired_loc, desired_name, secret)

    # Treat duplicate as success
    ok = int(resp.get("status", 0)) // 100 == 2
    if not ok:
        # Some APIG returns 200 but success:false with ER_DUP_ENTRY.
        res_body = resp.get("response") or {}
        if isinstance(res_body, dict) and res_body.get("success") is False:
            inner = res_body.get("result") or {}
            if isinstance(inner, dict) and str(inner.get("code")) == "ER_DUP_ENTRY":
                ok = True

    return {
        "status": resp.get("status"),
        "action": "ensure",
        "ok": ok,
        "request": resp.get("request"),
        "response": resp.get("response"),
        "listing_status": listing.get("status"),
    }
