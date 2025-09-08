# app/services/realnex_api.py
import os, httpx
from typing import Any, Dict

BASE = os.getenv("REALNEX_API_BASE", "https://sync.realnex.com/api/v1/Crm").rstrip("/")
ODATA_BASE = BASE.replace("/Crm", "/CrmOData")

def _headers(token: str) -> Dict[str,str]:
    return {"Authorization": f"Bearer {token}", "Accept":"application/json", "Content-Type":"application/json"}

def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(25.0))

async def _format_resp(resp: httpx.Response) -> Dict[str, Any]:
    try:
        data = resp.json() if resp.content else {}
    except Exception:
        data = {"raw": await resp.aread()}
    if isinstance(data, dict):
        data.setdefault("status", resp.status_code)
    return data

# ---------- Examples ----------
async def search_by_phone(token: str, phone_e164: str) -> Dict[str,Any]:
    url = f"{BASE}/Contacts/search"
    params = {"phone": phone_e164}
    async with _client() as client:
        try:
            r = await client.get(url, params=params, headers=_headers(token))
            return await _format_resp(r)
        except httpx.HTTPError as e:
            return {"status": getattr(getattr(e, "response", None), "status_code", 599), "error": str(e)}

async def create_activity(token: str, payload: Dict[str, Any]) -> Dict[str,Any]:
    url = f"{BASE}/Activities"
    async with _client() as client:
        try:
            r = await client.post(url, json=payload, headers=_headers(token))
            return await _format_resp(r)
        except httpx.HTTPError as e:
            return {"status": getattr(getattr(e, "response", None), "status_code", 599), "error": str(e)}
