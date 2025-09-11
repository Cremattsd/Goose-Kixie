from fastapi import APIRouter, Query, HTTPException
import os, httpx
from typing import List, Dict, Any

from ..services.realnex_api import (
    probe_endpoints, get_rn_token, BASES,
    list_timezones, attach_recording_from_url,
    search_by_phone, search_contact_by_phone_wide,
    get_contact, get_contact_full,
)

router = APIRouter()

def _headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

def _join_base_path(base: str, path: str) -> str:
    base = base.rstrip("/")
    path = path.lstrip("/")
    if not path:
        return base
    btail = base.rsplit("/", 1)[-1].lower()
    phead = path.split("/", 1)[0].lower()
    if btail == phead:
        path = path.split("/", 1)[1] if "/" in path else ""
    return f"{base}/{path}" if path else base

@router.get("/debug/realnex/env")
async def debug_env():
    return {
        "has_token": bool(get_rn_token()),
        "REALNEX_API_BASE": os.getenv("REALNEX_API_BASE", "https://sync.realnex.com/api/v1/Crm"),
        "bases_resolved": BASES,
    }

@router.get("/debug/realnex/probe")
async def debug_probe():
    token = get_rn_token()
    if not token:
        return {"status": "dry-run", "reason": "REALNEX_TOKEN/REALNEX_JWT not set"}
    return await probe_endpoints(token)

@router.get("/debug/realnex/paths")
async def debug_try_paths(
    paths: List[str] = Query(..., description="Relative paths, e.g. 'Users?$top=1'"),
    method: str = Query("GET", description="HTTP method: GET|OPTIONS"),
):
    token = get_rn_token()
    if not token:
        return {"status": "dry-run", "reason": "REALNEX_TOKEN/REALNEX_JWT not set", "paths": paths, "method": method}

    method = method.upper()
    if method not in {"OPTIONS", "GET"}:
        method = "GET"

    out: Dict[str, Any] = {"bases": BASES, "method": method, "paths": paths, "attempts": []}
    async with httpx.AsyncClient(timeout=20) as client:
        for base in BASES:
            for p in paths:
                url = _join_base_path(base, p)
                try:
                    r = await client.request(method, url, headers=_headers(token))
                    try:
                        body = r.json()
                        if isinstance(body, dict):
                            body = {k: body[k] for k in list(body.keys())[:5]}
                    except Exception:
                        body = r.text[:500]
                    out["attempts"].append({"url": url, "status": r.status_code, "body": body})
                except Exception as e:
                    out["attempts"].append({"url": url, "error": str(e)})
    return out

@router.get("/debug/realnex/timezones")
async def debug_timezones():
    token = get_rn_token()
    if not token:
        return {"status": "dry-run", "reason": "REALNEX_TOKEN/REALNEX_JWT not set"}
    return await list_timezones(token)

@router.post("/debug/realnex/attachment_test")
async def debug_attachment_test(
    objectKey: str = Query(..., description="GUID of existing object (e.g., contactKey)"),
    url: str = Query(..., description="Publicly fetchable file URL to attach"),
):
    token = get_rn_token()
    if not token:
        raise HTTPException(status_code=500, detail="REALNEX_TOKEN/REALNEX_JWT not set")
    return await attach_recording_from_url(token, objectKey, url)

@router.get("/debug/realnex/search_phone")
async def debug_search_phone(phone: str = Query(..., description="Phone to search")):
    token = get_rn_token()
    if not token:
        return {"status": "dry-run", "reason": "REALNEX_TOKEN/REALNEX_JWT not set"}
    std = await search_by_phone(token, phone)
    wide = await search_contact_by_phone_wide(token, phone)
    return {"standard": std, "wide": wide}

# >>> NEW: get contact by key (basic)
@router.get("/debug/realnex/contact")
async def debug_contact(contactKey: str = Query(..., description="Contact GUID")):
    token = get_rn_token()
    if not token:
        return {"status": "dry-run", "reason": "REALNEX_TOKEN/REALNEX_JWT not set"}
    return await get_contact(token, contactKey)

# >>> NEW: get contact by key (full)
@router.get("/debug/realnex/contact_full")
async def debug_contact_full(contactKey: str = Query(..., description="Contact GUID")):
    token = get_rn_token()
    if not token:
        return {"status": "dry-run", "reason": "REALNEX_TOKEN/REALNEX_JWT not set"}
    return await get_contact_full(token, contactKey)
