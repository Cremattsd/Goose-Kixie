# app/routes/powerlist.py
from __future__ import annotations

import os
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Header, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..services.db import get_db
from ..models.tenant import Tenant
from ..services.kixie_api import add_many_to_powerlist
from ..services.realnex_api import (
    odata_contacts_iter,
    normalize_phone_e164ish,
    get_rn_token,
)

router = APIRouter(prefix="/kixie/powerlist", tags=["kixie"])

# ───────────────────────── Security (shared secret) ─────────────────────────

def _verify_goose_shared_secret(db: Session, request: Request) -> None:
    rows = db.query(Tenant.id, Tenant.webhook_secret).all()
    if not rows:
        return
    provided = request.headers.get("X-Goose-Secret")
    if not provided:
        raise HTTPException(status_code=401, detail="X-Goose-Secret required")
    if provided not in {r.webhook_secret for r in rows}:
        raise HTTPException(status_code=401, detail="Invalid X-Goose-Secret")

# ───────────────────────── Schemas ───────────────────────────────────────────

class PowerListEntry(BaseModel):
    phone: str = Field(..., description="Phone number (any format)")
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None
    email: Optional[str] = None
    extra_data: Optional[Dict[str, Any]] = None

class PowerListUpsert(BaseModel):
    contacts: List[PowerListEntry]

class FromODataBody(BaseModel):
    filter: Optional[str] = Field(
        default=None,
        description="OData $filter (e.g. \"DoNotCall eq false and Email ne null\")"
    )
    max_rows: int = Field(default=500, ge=1, le=5000, description="Max contacts to scan")
    # Field map (CRM → Kixie)
    phone_priority: List[str] = Field(
        default_factory=lambda: [
            "Mobile","PrimaryPhone","Phone","Phone1","Phone2","Phone3",
            "WorkPhone","HomePhone","AssistantPhone"
        ]
    )
    first_name_field: str = "FirstName"
    last_name_field: str = "LastName"
    company_field: str = "Company"
    email_field: str = "Email"
    include_key_in_extra: bool = True

# ───────────────────────── Endpoints ─────────────────────────────────────────

@router.post("/{powerlist_id}/add")
async def powerlist_add(
    powerlist_id: str,
    body: PowerListUpsert,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Push an explicit list of contacts into a Kixie PowerList.
    Requires X-Goose-Secret if tenants are installed.
    """
    _verify_goose_shared_secret(db, request)

    contacts = []
    for c in body.contacts:
        norm = normalize_phone_e164ish(c.phone)
        if not norm:
            continue
        contacts.append({
            "phone": norm,
            "first_name": c.first_name,
            "last_name": c.last_name,
            "company": c.company,
            "email": c.email,
            "extra_data": c.extra_data or {},
        })

    if not contacts:
        return {"status": 400, "error": "no_valid_contacts"}

    result = await add_many_to_powerlist(powerlist_id, contacts)
    return {"status": 200, "result": result, "count": len(contacts)}

@router.post("/{powerlist_id}/from-odata")
async def powerlist_from_odata(
    powerlist_id: str,
    body: FromODataBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Pull contacts from RealNex OData (with optional $filter) and push to a Kixie PowerList.
    Skips rows without a usable phone or duplicates within the request.
    """
    _verify_goose_shared_secret(db, request)

    token = get_rn_token()
    if not token:
        raise HTTPException(401, "REALNEX_JWT/REALNEX_TOKEN not configured")

    select = ",".join(set(
        ["Key", body.first_name_field, body.last_name_field, body.company_field, body.email_field] + body.phone_priority
    ))

    # Harvest candidates
    candidates: List[Dict[str, Any]] = []
    async for page in odata_contacts_iter(
        token=token, select=select, filter=body.filter, top=200, max_rows=body.max_rows
    ):
        for row in page:
            # choose first non-empty phone from priority list
            phone_val = None
            for fname in body.phone_priority:
                v = row.get(fname)
                if isinstance(v, str) and v.strip():
                    phone_val = v.strip()
                    break
            norm = normalize_phone_e164ish(phone_val) if phone_val else None
            if not norm:
                continue

            first = (row.get(body.first_name_field) or "") or None
            last  = (row.get(body.last_name_field) or "") or None
            comp  = (row.get(body.company_field) or "") or None
            email = (row.get(body.email_field) or "") or None

            extra = {}
            if body.include_key_in_extra and "Key" in row:
                extra["RealNexKey"] = row["Key"]

            candidates.append({
                "phone": norm,
                "first_name": first,
                "last_name": last,
                "company": comp,
                "email": email,
                "extra_data": extra or {},
            })

    if not candidates:
        return {"status": 204, "powerlistId": powerlist_id, "pushed": 0, "result": {"ok": 0, "skipped": 0, "fail": 0}}

    result = await add_many_to_powerlist(powerlist_id, candidates)
    return {"status": 200, "powerlistId": powerlist_id, "pushed": len(candidates), "result": result}

@router.get("/preview-from-odata")
async def preview_from_odata(
    filter: Optional[str] = Query(None, description="OData $filter to preview"),
    max_rows: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """
    Preview contacts that *would* be sent to a PowerList (no push).
    Handy to craft your $filter before hitting the 'from-odata' endpoint.
    """
    token = get_rn_token()
    if not token:
        raise HTTPException(401, "REALNEX_JWT/REALNEX_TOKEN not configured")

    select = "Key,FirstName,LastName,Company,Email,Mobile,PrimaryPhone,Phone,Phone1,Phone2,Phone3,WorkPhone,HomePhone,AssistantPhone"
    preview: List[Dict[str, Any]] = []

    async for page in odata_contacts_iter(
        token=token, select=select, filter=filter, top=100, max_rows=max_rows
    ):
        for row in page:
            # pick a phone for preview
            phone = (
                row.get("Mobile") or row.get("PrimaryPhone") or row.get("Phone") or
                row.get("Phone1") or row.get("Phone2") or row.get("Phone3") or
                row.get("WorkPhone") or row.get("HomePhone") or row.get("AssistantPhone")
            )
            preview.append({
                "Key": row.get("Key"),
                "FirstName": row.get("FirstName"),
                "LastName": row.get("LastName"),
                "Company": row.get("Company"),
                "Email": row.get("Email"),
                "PhoneCandidate": phone
            })

    return {"status": 200, "count": len(preview), "items": preview}
