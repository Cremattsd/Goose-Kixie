# app/routes/powerlist.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional
import os

from ..services.kixie_api import add_many_to_powerlist

router = APIRouter()

class PowerContact(BaseModel):
    phone: str = Field(..., description="Phone number (any format; will be normalized)")
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    company: Optional[str] = None
    extra_data: Optional[Dict[str, Any]] = None  # becomes Kixie extraData columns

class PowerListIn(BaseModel):
    powerlist_id: str = Field(..., description="Kixie PowerList ID")
    contacts: List[PowerContact]

def _require_env(name: str) -> str:
    v = os.getenv(name, "")
    if not v:
        raise HTTPException(status_code=400, detail=f"Missing env var: {name}")
    return v

@router.post("/kixie/powerlist/add")
async def kixie_powerlist_add(body: PowerListIn):
    # sanity: ensure KIXIE envs exist
    _require_env("KIXIE_API_KEY")
    _require_env("KIXIE_BUSINESS_ID")
    result = await add_many_to_powerlist(body.powerlist_id, [c.model_dump() for c in body.contacts])
    return result
