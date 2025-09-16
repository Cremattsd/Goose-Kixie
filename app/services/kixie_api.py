from __future__ import annotations

import os
from typing import Optional, Dict, Any
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


def _have_creds() -> bool:
    return bool(os.getenv("KIXIE_API_KEY", "").strip() and os.getenv("KIXIE_BUSINESS_ID", "").strip())


# ───────────────────────── Click-to-dial (real call if creds provided) ─────────────────────────
async def make_call(
    email: str,
    target_e164: str,
    displayname: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Trigger a Kixie click-to-dial. If creds aren't configured, return a harmless stub.
    Env:
      KIXIE_API_KEY, KIXIE_BUSINESS_ID
      KIXIE_BASE_URL (optional, default https://api.kixie.com)
      KIXIE_CALL_PATH (optional, default /v1/calls)
    """
    api_key = os.getenv("KIXIE_API_KEY", "").strip()
    biz_id  = os.getenv("KIXIE_BUSINESS_ID", "").strip()

    if not api_key or not biz_id:
        return {
            "status": 202,
            "skipped": True,
            "reason": "Kixie credentials not configured",
            "echo": {"email": email, "target": target_e164, "displayname": displayname or target_e164},
        }

    base = _kx_base()
    path = os.getenv("KIXIE_CALL_PATH", "/v1/calls").lstrip("/")
    url  = f"{base}/{path}"

    body = {
        "business_id": biz_id,
        "email": email,
        "to": target_e164,
        "displayname": displayname or target_e164,
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, headers=_kx_headers(api_key), json=body)
            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text[:2000]}
            return {
                "status": r.status_code,
                "url": str(r.request.url),
                "method": r.request.method,
                "request": {"json": body},
                "response": data,
            }
    except httpx.HTTPError as e:
        return {"status": 599, "error": str(e), "url": url, "request": {"json": body}}


# ───────────────────────── Webhook helpers (dev-safe no-ops by default) ─────────────────────────
# Many tenants don’t want us touching Kixie webhooks during dev. We provide stubs that keep
# the /install flow working. Flip KIXIE_WEBHOOK_INSTALL=1 if you want to actually hit Kixie.

def _webhook_ops_enabled() -> bool:
    return os.getenv("KIXIE_WEBHOOK_INSTALL", "0").strip().lower() in {"1", "true", "yes", "on"}


async def list_webhooks() -> Dict[str, Any]:
    if not _have_creds() or not _webhook_ops_enabled():
        return {"status": 202, "skipped": True, "reason": "webhook ops disabled", "response": []}

    api_key = os.getenv("KIXIE_API_KEY", "").strip()
    biz_id  = os.getenv("KIXIE_BUSINESS_ID", "").strip()
    base    = _kx_base()
    url     = f"{base}/v1/webhooks?business_id={biz_id}"

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url, headers=_kx_headers(api_key))
            data = r.json() if r.content else {}
            return {"status": r.status_code, "url": str(r.request.url), "method": r.request.method, "response": data}
    except httpx.HTTPError as e:
        return {"status": 599, "error": str(e), "url": url}


async def delete_webhook(webhook_id: str) -> Dict[str, Any]:
    if not _have_creds() or not _webhook_ops_enabled():
        return {"status": 202, "skipped": True, "reason": "webhook ops disabled", "webhook_id": webhook_id}

    api_key = os.getenv("KIXIE_API_KEY", "").strip()
    base    = _kx_base()
    url     = f"{base}/v1/webhooks/{webhook_id}"

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.delete(url, headers=_kx_headers(api_key))
            data = r.json() if r.content else {}
            return {"status": r.status_code, "url": str(r.request.url), "method": r.request.method, "response": data}
    except httpx.HTTPError as e:
        return {"status": 599, "error": str(e), "url": url, "webhook_id": webhook_id}


async def create_or_update_webhook(target_url: str) -> Dict[str, Any]:
    if not _have_creds() or not _webhook_ops_enabled():
        return {"status": 202, "skipped": True, "reason": "webhook ops disabled", "target_url": target_url}

    api_key = os.getenv("KIXIE_API_KEY", "").strip()
    biz_id  = os.getenv("KIXIE_BUSINESS_ID", "").strip()
    base    = _kx_base()
    url     = f"{base}/v1/webhooks"

    body = {
        "business_id": biz_id,
        "target_url": target_url,
        # add event types as needed for your tenant:
        "events": ["call.completed", "call.answered", "call.missed"],
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, headers=_kx_headers(api_key), json=body)
            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text[:2000]}
            return {
                "status": r.status_code,
                "url": str(r.request.url),
                "method": r.request.method,
                "request": {"json": body},
                "response": data,
            }
    except httpx.HTTPError as e:
        return {"status": 599, "error": str(e), "url": url, "request": {"json": body}}
