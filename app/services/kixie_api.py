from __future__ import annotations

import os
from typing import Optional, Dict, Any, List

import httpx


# ───────────────────────── Config ─────────────────────────

def _kx_base() -> str:
    return os.getenv("KIXIE_BASE_URL", "https://api.kixie.com").rstrip("/")

def _kx_headers(api_key: str, business_id: Optional[str] = None) -> Dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-API-KEY": api_key,  # some tenants require this
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if business_id:
        headers["X-Business-Id"] = business_id
    return headers


# ───────────────────────── HTTP helpers ─────────────────────────

async def _post(url: str, headers: Dict[str, str], json_body: Dict[str, Any]) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, headers=headers, json=json_body)
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text[:2000]}
        return {
            "status": r.status_code,
            "url": str(r.request.url),
            "method": r.request.method,
            "request": {"json": json_body},
            "response": data,
        }
    except httpx.HTTPError as e:
        return {"status": 599, "error": str(e), "url": url, "request": {"json": json_body}}

async def _get(url: str, headers: Dict[str, str], params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
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
        async with httpx.AsyncClient(timeout=30) as client:
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
# We provide BOTH:
#  - make_call_keys(...)  : requires key + business id
#  - make_call(...)       : env-friendly wrapper (what the route imports)

async def make_call_keys(
    key: str,
    bizid: str,
    agent_email: str,
    to: str,
    displayname: Optional[str] = None,
    caller_id: Optional[str] = None,
    from_number: Optional[str] = None,
) -> Dict[str, Any]:
    """Kixie Make-a-Call with Business header + caller id support."""
    url = f"{_kx_base()}/{os.getenv('KIXIE_CALL_PATH', '/v1/calls').lstrip('/')}"
    body: Dict[str, Any] = {
        "business_id": bizid,
        "email": agent_email,
        "agent_email": agent_email,
        "user_email": agent_email,
        "to": to,
    }
    if displayname:
        body["displayname"] = displayname
    # Optional caller-id/from
    if caller_id:
        body["caller_id"] = caller_id
        body.setdefault("from", caller_id)
    if from_number:
        body["from"] = from_number
    # Fallback to env caller-id if none provided
    if "from" not in body:
        env_caller = os.getenv("KIXIE_CALLER_ID")
        if env_caller:
            body["from"] = env_caller

    headers = _kx_headers(key, bizid)
    return await _post(url, headers, body)

async def make_call(
    email: str,
    target_e164: str,
    displayname: Optional[str] = None,
    api_key: Optional[str] = None,
    business_id: Optional[str] = None,
    caller_id: Optional[str] = None,
    from_number: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Env-friendly wrapper that most routes call.
    Falls back to KIXIE_API_KEY / KIXIE_BUSINESS_ID if not provided.
    """
    key = (api_key or os.getenv("KIXIE_API_KEY", "")).strip()
    biz = (business_id or os.getenv("KIXIE_BUSINESS_ID", "")).strip()

    if not key or not biz:
        # Return a safe stub in dev if creds missing
        return {
            "status": 202,
            "skipped": True,
            "reason": "Kixie credentials not configured",
            "echo": {"email": email, "target": target_e164, "displayname": displayname or target_e164},
        }

    return await make_call_keys(
        key=key,
        bizid=biz,
        agent_email=email,
        to=target_e164,
        displayname=displayname,
        caller_id=caller_id,
        from_number=from_number,
    )


# ───────────────────────── Webhook Admin ─────────────────────────

def _normalize_listing_payload(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Return a simple list of webhook dicts from an arbitrary listing payload.
    Accepts shapes like:
      { "webhooks": [...] } or { "data": [...] } or { "items": [...] } or [...]
    """
    data = resp.get("response", {})
    if isinstance(data, list):
        return data
    for key in ("webhooks", "data", "items", "value"):
        if isinstance(data, dict) and isinstance(data.get(key), list):
            return data.get(key)  # type: ignore[return-value]
    return []

async def list_webhooks(api_key: str, business_id: str) -> Dict[str, Any]:
    url = f"{_kx_base()}/{os.getenv('KIXIE_WEBHOOK_LIST_PATH', '/v1/webhooks').lstrip('/')}"
    return await _get(url, _kx_headers(api_key, business_id), params={"business_id": business_id})

async def delete_webhook(api_key: str, business_id: str, webhook_id: str) -> Dict[str, Any]:
    path = os.getenv("KIXIE_WEBHOOK_DELETE_PATH", "/v1/webhooks/{id}").lstrip("/").replace("{id}", str(webhook_id))
    url = f"{_kx_base()}/{path}"
    return await _delete(url, _kx_headers(api_key, business_id))

async def create_or_update_webhook(api_key: str, business_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Idempotent-ish create:
      - list existing webhooks
      - if one with same name exists and location matches => noop
      - else delete and create
    """
    listing = await list_webhooks(api_key, business_id)
    items = _normalize_listing_payload(listing)
    desired_name = payload.get("name", "")
    desired_loc = payload.get("location", "")

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

    url = f"{_kx_base()}/{os.getenv('KIXIE_WEBHOOK_CREATE_PATH', '/v1/webhooks').lstrip('/')}"
    body = dict(payload)
    body.setdefault("business_id", business_id)
    return await _post(url, _kx_headers(api_key, business_id), body)
