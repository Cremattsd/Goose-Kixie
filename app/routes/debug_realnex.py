from fastapi import APIRouter
from ..services.realnex_api import probe_endpoints, get_rn_token

router = APIRouter()

@router.get("/debug/realnex/probe")
async def debug_probe():
    token = get_rn_token()
    return await probe_endpoints(token)
