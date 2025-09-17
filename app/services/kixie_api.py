# app/services/kixie_api.py
from __future__ import annotations

import os
from typing import Optional, Dict, Any, List
import httpx


# ───────────────────────── Config Helpers ─────────────────────────

def _kx_base() -> str:
    return os.getenv("KIXIE_BASE_URL", "https://api.kixie.com").rstrip("/")


def _kx_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "X-API-KEY": api_key,  # some tenants require this
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


# ───────────────────────── Click-to-Dial ─────────────────────────

async def make_call(
    email: str,
    target_e164: str,
    displayname: Optional[str] = None,
    api_key: Optional[str] = None,
    business_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Trigger a Kixie click-to-dial.

    If api_key/business_id not supplied, falls back to env:
      KIXIE_API_KEY, KIXIE_BUSINESS_ID
    """
    key = (api_key or os.getenv("KIXIE_API_KEY", "")).strip()
    biz = (business_id or os.getenv("KIXIE_BUSINESS_ID", "")).strip()

    if not key or not biz:
        # return a safe stub in dev
        return {
            "status": 202,
            "skipped": True,
            "reason": "Kixie credentials not configured",
            "echo": {"email": email, "target": target_e164, "displayname": displayname or target_e164},
        }

    base = _kx_base()
    path = os.getenv("KIXIE_CALL_PATH", "/v1/calls").lstrip("/")
    url = f"{base}/{path}"

    body = {
        "business_id": biz,
        "email": email,
        "to": target_e164,
        "displayname": displayname or target_e164,
    }
        # Add optional caller ID if provided
    caller_id = os.getenv("KIXIE_CALLER_ID")
    if caller_id:
        body["from"] = caller_id

    # Provide alternate field names some tenants expect
    body.setdefault("agent_email", email)
    body.setdefault("user_email", email)

    # Some tenants require business id in a header
    headers = _kx_headers(key)
    headers["X-Business-Id"] = biz

    return await _post(url, headers, body)


# ───────────────────────── Webhook Admin ─────────────────────────
# NOTE: Kixie API shapes can vary by account; these endpoints/fields
#       are written to be defensive and env-overridable.

def _webhook_paths() -> Dict[str, str]:
    return {
        "list": os.getenv("KIXIE_WEBHOOK_LIST_PATH", "/v1/webhooks").lstrip("/"),
        "create": os.getenv("KIXIE_WEBHOOK_CREATE_PATH", "/v1/webhooks").lstrip("/"),
        # delete format string with {id}
        "delete": os.getenv("KIXIE_WEBHOOK_DELETE_PATH", "/v1/webhooks/{id}").lstrip("/"),
    }


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
    base = _kx_base()
    url = f"{base}/{_webhook_paths()['list']}"
    # Some tenants require business_id as a param
    return await _get(url, _kx_headers(api_key), params={"business_id": business_id})


async def delete_webhook(api_key: str, business_id: str, webhook_id: str) -> Dict[str, Any]:
    base = _kx_base()
    delete_path = _webhook_paths()["delete"].replace("{id}", str(webhook_id))
    url = f"{base}/{delete_path}"
    # business_id may not be required for delete, but keep headers consistent
    return await _delete(url, _kx_headers(api_key))


async def create_or_update_webhook(api_key: str, business_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Idempotent-ish create:
      - list existing webhooks
      - if one with same name exists, keep it if location matches; otherwise delete and recreate
      - else create new
    """
    # 1) list & try to match by name
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

    # 2) If found and same location, return ok w/ short-circuit
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
        # 3) Otherwise delete and recreate
        if wid:
            await delete_webhook(api_key, business_id, wid)

    # 4) Create
    base = _kx_base()
    url = f"{base}/{_webhook_paths()['create']}"
    body = dict(payload)
    # Many tenants require business_id field on creation
    body.setdefault("business_id", business_id)
    return await _post(url, _kx_headers(api_key), body)
