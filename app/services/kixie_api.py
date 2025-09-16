# from repo root
mkdir -p app/services

cat > app/services/kixie_api.py <<'EOF'
from __future__ import annotations

import os
from typing import Optional, Dict, Any

import httpx

# Minimal Kixie client with a safe fallback.
# If KIXIE_API_KEY / KIXIE_BUSINESS_ID are not set, we return a 202 "skipped"
# so your /dialer/call/make endpoint still works for demos/tests.

def _kx_headers(api_key: str) -> Dict[str, str]:
    # Not sure which scheme your Kixie tenant prefers; include both common patterns.
    return {
        "Authorization": f"Bearer {api_key}",
        "X-API-KEY": api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

def _kx_base() -> str:
    # Allow override; default guessed public base.
    return os.getenv("KIXIE_BASE_URL", "https://api.kixie.com")

async def make_call(
    email: str,
    target_e164: str,
    displayname: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Trigger a Kixie click-to-dial. If Kixie creds aren't configured, returns a harmless stub.

    Env:
      KIXIE_API_KEY       -> your API key
      KIXIE_BUSINESS_ID   -> your business/account id
      KIXIE_BASE_URL      -> optional, defaults to https://api.kixie.com
      KIXIE_CALL_PATH     -> optional, defaults to /v1/calls (POST)

    Returns a dict with status + payload; never raises.
    """
    api_key = os.getenv("KIXIE_API_KEY", "").strip()
    biz_id  = os.getenv("KIXIE_BUSINESS_ID", "").strip()

    # No credentials? Return a stub so demos don’t break.
    if not api_key or not biz_id:
        return {
            "status": 202,
            "skipped": True,
            "reason": "Kixie credentials not configured",
            "echo": {"email": email, "target": target_e164, "displayname": displayname or target_e164},
        }

    base = _kx_base().rstrip("/")
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
EOF
