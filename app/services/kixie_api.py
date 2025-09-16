from __future__ import annotations

import os
from typing import Optional, Dict, Any, List
import httpx


def _kx_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "X-API-KEY": api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _kx_base() -> str:
    return os.getenv("KIXIE_BASE_URL", "https://api.kixie.com").rstrip("/")


def _kx_key_and_biz() -> tuple[str, str]:
    return os.getenv("KIXIE_API_KEY", "").strip(), os.getenv("KIXIE_BUSINESS_ID", "").strip()


async def _req(method: str, path: str, json: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    api_key, biz_id = _kx_key_and_biz()
    if not api_key or not biz_id:
        # Safe stub for dev if creds aren’t set
        return {"status": 202, "skipped": True, "reason": "Kixie credentials not configured", "request": {"method": method, "path": path, "json": json, "params": params}}

    base = _kx_base()
    url = f"{base}/{path.lstrip('/')}"
    if params is None:
        params = {}
    # many APIs require the business id in either params or body—include in both to be safe
    params.setdefault("business_id", biz_id)
    if isinstance(json, dict):
        json.setdefault("business_id", biz_id)

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.request(method, url, headers=_kx_headers(api_key), json=json, params=params)
            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text[:2000]}
            return {
                "status": r.status_code,
                "url": str(r.request.url),
                "method": r.request.method,
                "request": {"json": json, "params": params},
                "response": data,
            }
    except httpx.HTTPError as e:
        return {"status": 599, "error": str(e), "url": url, "request": {"json": json, "params": params}}


# ───────────────────────── Calls ─────────────────────────

async def make_call(
    email: str,
    target_e164: str,
    displayname: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Trigger a Kixie click-to-dial.
    Env: KIXIE_API_KEY, KIXIE_BUSINESS_ID, (optional) KIXIE_BASE_URL, KIXIE_CALL_PATH
    """
    path = os.getenv("KIXIE_CALL_PATH", "/v1/calls")
    body = {
        "email": email,
        "to": target_e164,
        "displayname": displayname or target_e164,
    }
    return await _req("POST", path, json=body)


# ─────────────────────── Webhooks (for /install) ───────────────────────

async def list_webhooks() -> Dict[str, Any]:
    """
    List configured webhooks. Path is configurable in case your tenant differs.
    Env: KIXIE_WEBHOOKS_PATH (default /v1/webhooks)
    """
    path = os.getenv("KIXIE_WEBHOOKS_PATH", "/v1/webhooks")
    return await _req("GET", path, json=None, params={})


async def create_or_update_webhook(target_url: str, events: Optional[List[str]] = None, secret: Optional[str] = None) -> Dict[str, Any]:
    """
    Idempotently create/update a webhook pointing to target_url.
    Env: KIXIE_WEBHOOKS_PATH (default /v1/webhooks), KIXIE_WEBHOOK_SECRET (optional)
    """
    path = os.getenv("KIXIE_WEBHOOKS_PATH", "/v1/webhooks")
    if events is None:
        # Default to the common call events your app handles
        events = ["call.completed", "call.ended", "call.started"]

    if secret is None:
        secret = os.getenv("KIXIE_WEBHOOK_SECRET") or None

    # Try to find existing by URL
    existing = await list_webhooks()
    if int(existing.get("status", 0)) // 100 == 2:
        items = existing.get("response") or existing.get("data") or existing
        rows = []
        if isinstance(items, dict):
            # look for common container names
            for c in ("items", "data", "value", "webhooks"):
                if isinstance(items.get(c), list):
                    rows = items[c]
                    break
        elif isinstance(items, list):
            rows = items

        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                url = row.get("url") or row.get("target") or row.get("endpoint")
                wid = row.get("id") or row.get("webhook_id") or row.get("Id") or row.get("ID")
                if url and str(url).strip().lower() == target_url.strip().lower() and wid:
                    # update/PUT
                    body = {"url": target_url, "events": events}
                    if secret:
                        body["secret"] = secret
                    upd_path = f"{path.rstrip('/')}/{wid}"
                    return await _req("PUT", upd_path, json=body)

    # create/POST
    body = {"url": target_url, "events": events}
    if secret:
        body["secret"] = secret
    return await _req("POST", path, json=body)


async def delete_webhook(webhook_id: str) -> Dict[str, Any]:
    """
    Delete a webhook by id.
    Env: KIXIE_WEBHOOKS_PATH (default /v1/webhooks)
    """
    path = os.getenv("KIXIE_WEBHOOKS_PATH", "/v1/webhooks")
    del_path = f"{path.rstrip('/')}/{webhook_id}"
    return await _req("DELETE", del_path, json=None, params={})
