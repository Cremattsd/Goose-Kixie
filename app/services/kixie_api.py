import httpx
from typing import Dict, Any

KIXIE_BASE = "https://apig.kixie.com/app/v1/api"

async def create_or_update_webhook(apikey: str, businessid: str, payload: Dict[str, Any]) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{KIXIE_BASE}/postwebhook", json={ "apikey": apikey, "businessid": businessid, **payload })
        r.raise_for_status()
        return r.json()

async def list_webhooks(apikey: str, businessid: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{KIXIE_BASE}/getWebhooks", json={ "apikey": apikey, "businessid": businessid, "call": "getWebhooks" })
        r.raise_for_status()
        return r.json()

async def delete_webhook(apikey: str, businessid: str, webhookid: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{KIXIE_BASE}/deleteWebhooks", json={ "apikey": apikey, "businessid": businessid, "call": "removeWebhook", "webhookid": webhookid })
        r.raise_for_status()
        return r.json()
