# app/routes/debug_realnex.py
from fastapi import APIRouter, Query
import os, httpx
from typing import List, Dict, Any

from ..services.realnex_api import probe_endpoints, get_rn_token, BASES

router = APIRouter()

def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

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
    """Safe env flags (no secrets)."""
    return {
        "has_token": bool(get_rn_token()),
        "REALNEX_API_BASE": os.getenv("REALNEX_API_BASE", "https://sync.realnex.com/api/v1/Crm"),
        "bases_resolved": BASES,
    }

@router.get("/debug/realnex/probe")
async def debug_probe():
    """OPTIONS probe of common endpoints across all bases."""
    token = get_rn_token()
    if not token:
        return {"status": "dry-run", "reason": "REALNEX_TOKEN/REALNEX_JWT not set"}
    return await probe_endpoints(token)

@router.get("/debug/realnex/paths")
async def debug_try_paths(
    paths: List[str] = Query(..., description="Relative paths to try. Example: 'Users?$top=1' or 'CrmOData/Users?$top=1'"),
    method: str = Query("GET", description="HTTP method to use (GET|OPTIONS)"),
):
    """Try arbitrary relative paths across all bases. Helpful for odd tenant shapes."""
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
                            # keep response tiny
                            body = {k: body[k] for k in list(body.keys())[:5]}
                    except Exception:
                        body = r.text[:500]
                    out["attempts"].append({
                        "url": url,
                        "status": r.status_code,
                        "body": body,
                    })
                except Exception as e:
                    out["attempts"].append({"url": url, "error": str(e)})
    return out
