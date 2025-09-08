# app/services/kixie_api.py
import httpx
from typing import Dict, Any

KIXIE_BASE = "https://apig.kixie.com/app/v1/api"

async def create_or_update_webhook(apikey: str, businessid: str, payload: Dict[str, Any]) -> dict:
    body = {"apikey": apikey, "businessid": businessid, **payload}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{KIXIE_BASE}/postwebhook", json=body)
        r.raise_for_status()
        return r.json()

async def list_webhooks(apikey: str, businessid: str) -> dict:
    body = {"apikey": apikey, "businessid": businessid, "call": "getWebhooks"}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{KIXIE_BASE}/getWebhooks", json=body)
        r.raise_for_status()
        return r.json()

async def delete_webhook(apikey: str, businessid: str, webhookid: str) -> dict:
    body = {"apikey": apikey, "businessid": businessid, "call": "removeWebhook", "webhookid": webhookid}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{KIXIE_BASE}/deleteWebhooks", json=body)
        r.raise_for_status()
        return r.json()
