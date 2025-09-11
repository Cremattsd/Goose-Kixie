# app/services/kixie_api.py
import os, httpx
from typing import Dict, Any, List, Optional
from .realnex_api import normalize_phone_e164ish

KIXIE_MGMT_BASE = os.getenv("KIXIE_API_BASE", "https://apig.kixie.com/app/v1/api")
KIXIE_EVENT_BASE = "https://apig.kixie.com/app/event"  # for make-a-call & powerlist

def _api_key() -> str:
    return os.getenv("KIXIE_API_KEY", "")

def _biz_id() -> str:
    return os.getenv("KIXIE_BUSINESS_ID", "")

async def _post_json(url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, json=payload)
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text}
        data.setdefault("status", r.status_code)
        data.setdefault("url", str(r.request.url))
        return data

# ── Existing webhook mgmt helpers (kept) ───────────────────────────────────────
async def create_or_update_webhook(apikey: str, businessid: str, payload: Dict[str, Any]) -> dict:
    return await _post_json(f"{KIXIE_MGMT_BASE}/postwebhook",
                            {"apikey": apikey, "businessid": businessid, **payload})

async def list_webhooks(apikey: str, businessid: str) -> dict:
    return await _post_json(f"{KIXIE_MGMT_BASE}/getWebhooks",
                            {"apikey": apikey, "businessid": businessid, "call": "getWebhooks"})

async def delete_webhook(apikey: str, businessid: str, webhookid: str) -> dict:
    return await _post_json(f"{KIXIE_MGMT_BASE}/deleteWebhooks",
                            {"apikey": apikey, "businessid": businessid, "call": "removeWebhook", "webhookid": webhookid})

# ── Make-a-call (FYI) ─────────────────────────────────────────────────────────
async def make_call(email: str, target_e164: str, displayname: Optional[str] = None) -> Dict[str, Any]:
    """
    Uses Kixie's 'Make a Call' API (POST to /app/event?apikey=...)
    """
    apikey, biz = _api_key(), _biz_id()
    payload = {
        "businessid": biz,
        "email": email,
        "target": target_e164,
        "displayname": displayname or target_e164,
        "eventname": "call",
        "apikey": apikey,
    }
    return await _post_json(f"{KIXIE_EVENT_BASE}?apikey={apikey}", payload)

# ── PowerList helpers (NEW) ───────────────────────────────────────────────────
async def add_to_powerlist_one(powerlist_id: str,
                               phone_raw: str,
                               first_name: str | None = None,
                               last_name: str | None = None,
                               company: str | None = None,
                               email: str | None = None,
                               extra_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Add ONE number to a PowerList (Kixie API only supports one-at-a-time).
    Docs: https://support.kixie.com/hc/en-us/articles/19135310564635-Powerlist-API
    """
    apikey, biz = _api_key(), _biz_id()
    target = normalize_phone_e164ish(phone_raw) or phone_raw
    payload = {
        "businessid": biz,
        "powerlistId": powerlist_id,
        "apikey": apikey,
        "target": target,
        "eventname": "updatepowerlist",
        "firstName": first_name or "",
        "lastName": last_name or "",
        "companyName": company or "",
        "email": email or "",
    }
    if extra_data:
        payload["extraData"] = extra_data
    return await _post_json(f"{KIXIE_EVENT_BASE}?apikey={apikey}", payload)

async def add_many_to_powerlist(powerlist_id: str, contacts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    contacts: [{phone, first_name?, last_name?, company?, email?, extra_data?}, ...]
    """
    out = {"powerlistId": powerlist_id, "ok": 0, "skipped": 0, "fail": 0, "results": []}
    seen: set[str] = set()
    for c in contacts:
        raw = c.get("phone") or c.get("target") or ""
        norm = normalize_phone_e164ish(raw) or raw
        if not norm:
            out["skipped"] += 1
            out["results"].append({"phone": raw, "status": "skipped_no_phone"})
            continue
        if norm in seen:
            out["skipped"] += 1
            out["results"].append({"phone": norm, "status": "skipped_duplicate"})
            continue
        seen.add(norm)
        resp = await add_to_powerlist_one(
            powerlist_id=powerlist_id,
            phone_raw=norm,
            first_name=c.get("first_name"),
            last_name=c.get("last_name"),
            company=c.get("company"),
            email=c.get("email"),
            extra_data=c.get("extra_data"),
        )
        ok = 200 <= resp.get("status", 0) < 300
        out["ok" if ok else "fail"] += 1
        out["results"].append({"phone": norm, "resp": resp, "ok": ok})
    return out
