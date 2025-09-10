# app/services/realnex_keys.py
from __future__ import annotations
import os, json, base64, asyncio
from typing import Optional, Tuple, Dict, Any

from .realnex_api import get_rn_token, get_user_by_email, list_teams

# Optional selector: prefer a specific team by name (case-insensitive)
PREFERRED_TEAM_NAME = os.getenv("RN_TEAM_NAME", "").strip().lower()

# Simple in-process cache (refresh on process restart)
_cache: Dict[str, Any] = {}
_cache_lock = asyncio.Lock()

def _b64url_decode(s: str) -> bytes:
    s += "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s.encode("utf-8"))

def _decode_jwt_unverified(token: str) -> Dict[str, Any]:
    """
    Return JWT payload claims without verifying signature (we only need metadata).
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}

def _pick_team_key(teams: list[dict]) -> Optional[str]:
    if not teams:
        return None
    if PREFERRED_TEAM_NAME:
        for t in teams:
            name = str(t.get("Name") or t.get("name") or "").lower()
            if name == PREFERRED_TEAM_NAME:
                for k in ("TeamKey","teamKey","Key","key","Id","id"):
                    if t.get(k):
                        return str(t[k])
    # fallback: first team with a usable key
    for t in teams:
        for k in ("TeamKey","teamKey","Key","key","Id","id"):
            if t.get(k):
                return str(t[k])
    return None

async def _resolve_keys_uncached() -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (user_key, team_key) by inspecting env, JWT claims, then OData.
    """
    # 1) Hard overrides via env (if present)
    env_user = os.getenv("RN_USER_KEY")
    env_team = os.getenv("RN_TEAM_KEY")
    if env_user and env_team:
        return env_user, env_team

    token = get_rn_token()
    if not token:
        # No token → nothing we can do
        return env_user, env_team

    # 2) Try JWT claims
    claims = _decode_jwt_unverified(token)
    email = claims.get("email") or claims.get("Email")
    user_key = claims.get("user_key") or claims.get("UserKey")

    # 3) If user_key missing, look up via OData using email
    if not user_key and email:
        u = await get_user_by_email(token, email)
        if isinstance(u, dict):
            for k in ("UserKey","userKey","Key","key","Id","id"):
                if u.get(k):
                    user_key = str(u[k]); break

    # 4) Resolve teamKey (Teams OData)
    team_key = env_team
    if not team_key:
        teams = await list_teams(token)
        if isinstance(teams, list):
            team_key = _pick_team_key(teams)

    return (user_key, team_key)

async def get_rn_keys() -> Tuple[Optional[str], Optional[str]]:
    """
    Cached wrapper. Returns (user_key, team_key).
    """
    async with _cache_lock:
        if "keys" in _cache:
            return _cache["keys"]
        keys = await _resolve_keys_uncached()
        _cache["keys"] = keys
        return keys
