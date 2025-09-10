# services/kixie_api.py
import os
import asyncio
from typing import Dict, Any, Optional, Iterable, Tuple, List

import httpx

# ---- Config ----
KIXIE_BASE = os.getenv("KIXIE_API_BASE", "https://apig.kixie.com/app/v1/api").rstrip("/")
KIXIE_TIMEOUT = float(os.getenv("KIXIE_HTTP_TIMEOUT", "15"))

class KixieAPIError(Exception):
    """Raised when Kixie returns a non-2xx response with details."""
    def __init__(self, status: int, url: str, body: Any):
        super().__init__(f"Kixie API error {status} for {url}: {body!r}")
        self.status = status
        self.url = url
        self.body = body

# ---- Low-level HTTP helper (POST-only per Kixie endpoints you’re using) ----
async def _post(
    path: str,
    json: Dict[str, Any],
    *,
    client: Optional[httpx.AsyncClient] = None,
    retries: int = 2,
    backoff_sec: float = 0.6,
) -> Dict[str, Any]:
    """
    POST to Kixie; small retry on 429/5xx; raises KixieAPIError with parsed body when possible.
    """
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=KIXIE_TIMEOUT)

    url = f"{KIXIE_BASE}/{path.lstrip('/')}"
    last_exc = None

    try:
        for attempt in range(retries + 1):
            try:
                r = await client.post(url, json=json)
            except httpx.HTTPError as e:
                last_exc = e
                if attempt >= retries:
                    raise
                await asyncio.sleep(backoff_sec * (attempt + 1))
                continue

            if 200 <= r.status_code < 300:
                try:
                    return r.json()
                except Exception:
                    # Fallback if Kixie returns non-JSON success (unlikely)
                    return {"ok": True, "raw": r.text}

            # Non-2xx: try to surface JSON body
            try:
                body = r.json()
            except Exception:
                body = r.text

            # Retry on 429/5xx
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                await asyncio.sleep(backoff_sec * (attempt + 1))
                continue

            raise KixieAPIError(r.status_code, url, body)
    finally:
        if owns_client:
            await client.aclose()

    # Should never hit here
    raise RuntimeError(f"Unexpected fallthrough for {url}: {last_exc!r}")

# ---- Raw endpoints (preserve your current shapes) ----
async def create_or_update_webhook(
    apikey: str,
    businessid: str,
    payload: Dict[str, Any],
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """
    Kixie 'postwebhook' – payload pass-through.
    Example payload typically includes: url, events, secret, method, etc.
    """
    body = {"apikey": apikey, "businessid": businessid, **payload}
    return await _post("postwebhook", body, client=client)

async def list_webhooks(
    apikey: str,
    businessid: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """
    Kixie 'getWebhooks' – returns the registered webhooks.
    """
    body = {"apikey": apikey, "businessid": businessid, "call": "getWebhooks"}
    return await _post("getWebhooks", body, client=client)

async def delete_webhook(
    apikey: str,
    businessid: str,
    webhookid: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """
    Kixie 'deleteWebhooks' – remove a webhook by ID.
    """
    body = {
        "apikey": apikey,
        "businessid": businessid,
        "call": "removeWebhook",
        "webhookid": webhookid,
    }
    return await _post("deleteWebhooks", body, client=client)

# ---- Helpers: tolerant parsing + idempotent upsert ----
def _iter_webhooks(obj: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    """
    Tolerantly yield webhook dicts from various common response shapes:
      {"webhooks":[...]}, {"items":[...]}, {"data":[...]}, or a single dict.
    """
    if not isinstance(obj, dict):
        return []
    for key in ("webhooks", "items", "data"):
        if isinstance(obj.get(key), list):
            return obj[key]
    # Some Kixie responses might return directly a list
    if isinstance(obj.get("result"), list):
        return obj["result"]
    # Single object fallback
    if any(k in obj for k in ("id", "webhookid", "url")):
        return [obj]
    return []

def _matches(
    existing: Dict[str, Any],
    desired: Dict[str, Any],
    match_on: Tuple[str, ...],
) -> bool:
    """
    Compare selected keys (case-insensitive for strings).
    """
    for k in match_on:
        ev = existing.get(k)
        dv = desired.get(k)
        if isinstance(ev, str) and isinstance(dv, str):
            if ev.strip().lower() != dv.strip().lower():
                return False
        else:
            if ev != dv:
                return False
    return True

async def ensure_webhook(
    apikey: str,
    businessid: str,
    desired: Dict[str, Any],
    *,
    match_on: Tuple[str, ...] = ("url", "method"),
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """
    Idempotently ensure a webhook with matching fields exists.
    - Lists current webhooks
    - If a match is found on `match_on`, returns it (and updates if you pass extra fields)
    - Else creates via create_or_update_webhook
    """
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=KIXIE_TIMEOUT)

    try:
        current = await list_webhooks(apikey, businessid, client=client)
        for w in _iter_webhooks(current):
            if _matches(w, desired, match_on):
                # Optional: push an update if you want to enforce new fields (e.g., events/secret)
                maybe_update = {**desired, "webhookid": w.get("webhookid") or w.get("id")}
                return {
                    "action": "exists",
                    "webhook": w,
                    "maybe_update": await create_or_update_webhook(apikey, businessid, maybe_update, client=client)
                }
        # No match → create
        created = await create_or_update_webhook(apikey, businessid, desired, client=client)
        return {"action": "created", "webhook": created}
    finally:
        if owns_client:
            await client.aclose()
