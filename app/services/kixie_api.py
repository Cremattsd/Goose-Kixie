from __future__ import annotations

import os
from typing import Optional, Dict, Any, List
import httpx

# ───────────────────────── Config Helpers ─────────────────────────
def _rest_base() -> str:
    """Your legacy/general REST base (kept for call flows you already use)."""
    return os.getenv("KIXIE_BASE_URL", "https://api.kixie.com").rstrip("/")

def _apig_base() -> str:
    """Kixie webhook admin lives here."""
    return os.getenv("KIXIE_APIG_BASE_URL", "https://apig.kixie.com/app/v1/api").rstrip("/")

def _kx_headers(api_key: str, business_id: Optional[str] = None) -> Dict[str, str]:
    # Keep your header-style auth for REST calls you already rely on
    h = {
        "Authorization": f"Bearer {api_key}",
        "X-API-KEY": api_key,   # some tenants expect this
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if business_id:
        h["X-Business-Id"] = business_id
    return h

# ───────────────────────── Generic HTTP (kept) ─────────────────────────
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

# ───────────────────────── APIG HTTP helper (new) ─────────────────────────
async def _apig_post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST to apig.kixie.com/app/v1/api/* with JSON body (apikey & businessid)."""
    url = f"{_apig_base()}/{path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, json=payload)
            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text[:2000]}
            return {
                "status": r.status_code,
                "url": url,
                "method": "POST",
                "request": {"json": payload},
                "response": data,
            }
    except httpx.HTTPError as e:
        return {"status": 599, "error": str(e), "url": url, "request": {"json": payload}}

# ───────────────────────── Make-a-Call (kept) ─────────────────────────
async def _make_call_with_key(
    api_key: str,
    business_id: str,
    agent_email: str,
    to: str,
    displayname: Optional[str] = None,
    caller_id: Optional[str] = None,
    from_number: Optional[str] = None,
) -> Dict[str, Any]:
    url = f"{_rest_base()}/{os.getenv('KIXIE_CALL_PATH', '/v1/calls').lstrip('/')}"
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
    if os.getenv("KIXIE_BYPASS", "").strip().lower() in {"1", "true", "yes", "on"}:
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

# ───────────────────────── Webhook Admin (fixed) ─────────────────────────
# Kixie webhook admin is via APIG and expects JSON body with apikey & businessid.

def _normalize_listing_payload(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = resp.get("response", {})
    if isinstance(data, list):
        return data
    for key in ("webhooks", "data", "items", "value"):
        if isinstance(data, dict) and isinstance(data.get(key), list):
            return data.get(key)  # type: ignore[return-value]
    return []

async def list_webhooks(api_key: str, business_id: str) -> Dict[str, Any]:
    payload = {"apikey": api_key, "businessid": business_id, "call": "getWebhooks"}
    return await _apig_post("getWebhooks", payload)

async def delete_webhook(api_key: str, business_id: str, webhook_id: str) -> Dict[str, Any]:
    payload = {"apikey": api_key, "businessid": business_id, "call": "removeWebhook", "webhookid": str(webhook_id)}
    return await _apig_post("deleteWebhooks", payload)

async def create_or_update_webhook(api_key: str, business_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
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
        loc = found.get("location") or found.get("url") or ""
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
    body = dict(payload)
    body.setdefault("call", "postWebhook")
    body["apikey"] = api_key
    body["businessid"] = business_id
    return await _apig_post("postwebhook", body)
