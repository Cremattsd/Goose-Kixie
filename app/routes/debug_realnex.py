from fastapi import APIRouter, Query
from ..services.realnex_api import get_rn_token, probe_endpoints, resolve_user_team_by_email

router = APIRouter()

@router.get("/debug/realnex/probe")
async def debug_probe():
    token = get_rn_token()
    if not token:
        return {"ok": False, "reason": "No REALNEX_JWT or REALNEX_TOKEN set"}
    return await probe_endpoints(token)

@router.get("/debug/realnex/resolve_user")
async def debug_resolve_user(email: str = Query(...)):
    token = get_rn_token()
    if not token:
        return {"ok": False, "reason": "No REALNEX_JWT or REALNEX_TOKEN set"}
    uk, tk, ctx = await resolve_user_team_by_email(token, email)
    return {"ok": True, "userKey": uk, "teamKey": tk, "ctx": ctx}
