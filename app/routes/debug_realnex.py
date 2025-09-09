cat > app/routes/debug_realnex.py <<'PY'
from fastapi import APIRouter
from ..services.realnex_api import get_rn_token, probe_endpoints

router = APIRouter()

@router.get("/debug/realnex/probe")
async def debug_probe():
    token = get_rn_token()
    if not token:
        return {"status": "dry-run", "reason": "REALNEX_JWT/REALNEX_TOKEN not set"}
    return await probe_endpoints(token)
PY
