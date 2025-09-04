import os
import httpx
from typing import Dict, Any

REALNEX_API_BASE = os.getenv("REALNEX_API_BASE", "https://sync.realnex.com/api/v1/Crm")

def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

async def search_any(token: str, q: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{REALNEX_API_BASE}/search", params={"q": q}, headers=_headers(token))
        r.raise_for_status()
        return r.json()

async def get_contacts(token: str, params: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{REALNEX_API_BASE}/contact", params=params, headers=_headers(token))
        r.raise_for_status()
        return r.json()

async def create_contact(token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{REALNEX_API_BASE}/contact", json=payload, headers=_headers(token))
        r.raise_for_status()
        return r.json()

async def create_history(token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{REALNEX_API_BASE}/history", json=payload, headers=_headers(token))
        r.raise_for_status()
        return r.json()
