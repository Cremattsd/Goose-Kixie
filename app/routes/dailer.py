import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import and_

from ..services.db import get_db
from ..models.tenant import Tenant
from ..models.dialer_queue import DialerQueue
from ..services.crypto import decrypt
from ..services import realnex_api as rn

router = APIRouter()

# ---------- helpers ----------
def find_tenant(db: Session, business_id: Optional[str]) -> Tenant:
    q = db.query(Tenant).filter(Tenant.active == True)
    if business_id:
        q = q.filter(Tenant.kixie_business_id == business_id)
    t = q.order_by(Tenant.id.desc()).first()
    if not t:
        raise HTTPException(404, "No active tenant")
    return t

def first_phone(contact: Dict[str, Any]) -> Optional[str]:
    # Accept both PascalCase and camelCase keys
    def g(k: str):
        return contact.get(k) or contact.get(k[:1].lower() + k[1:]) or contact.get(k.upper()) or contact.get(k.lower())
    for k in ("Mobile", "Work", "Home"):
        v = g(k)
        if v:
            return str(v).strip()
    return None

_e164 = re.compile(r"^\+?\d[\d\-\.\s\(\)]*$")
def normalize_e164(num: str) -> Optional[str]:
    if not num: return None
    num = num.strip()
    if not _e164.match(num):
        return None
    digits = re.sub(r"[^\d]", "", num)
    if not digits:
        return None
    if digits.startswith("1") and len(digits) == 11:
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if num.startswith("+"):
        return f"+{digits}"
    # fallback: assume already E.164 digits with country
    return f"+{digits}" if digits else None

def name_parts(contact: Dict[str, Any]) -> tuple[str, str]:
    def g(k: str):
        return contact.get(k) or contact.get(k[:1].lower() + k[1:]) or contact.get(k.upper()) or contact.get(k.lower())
    return (g("FirstName") or "", g("LastName") or "")

def get_company(contact: Dict[str, Any]) -> str:
    for k in ("Company", "company", "Employer", "employer"):
        if contact.get(k):
            return str(contact[k])
    return ""

def get_email(contact: Dict[str, Any]) -> str:
    for k in ("Email","email"):
        if contact.get(k):
            return str(contact[k])
    return ""

def do_not_call(contact: Dict[str, Any]) -> bool:
    for k in ("DoNotCall","doNotCall","donotcall"):
        if contact.get(k) is True:
            return True
    return False

def contact_key(contact: Dict[str, Any]) -> Optional[str]:
    for k in ("Key","key","objectKey","contactKey","id","Id","ID"):
        if contact.get(k):
            return str(contact[k])
    return None

def crm_url_from_key(key: str) -> str:
    return f"https://crm.realnex.com/Contact/{key}"

# ---------- schemas ----------
class SyncBody(BaseModel):
    campaign: Optional[str] = Field(None, description="Campaign label (optional).")
    max_rows: int = Field(500, ge=1, le=5000, description="Max rows to pull from OData.")
    # Optional: custom OData filter if you want to override defaults
    odata_filter: Optional[str] = Field(None, description="Raw $filter to use instead of default.")

class BulkItem(BaseModel):
    object_key: str
    phone_e164: str
    name_first: Optional[str] = ""
    name_last: Optional[str] = ""
    company: Optional[str] = ""
    email: Optional[str] = ""

class BulkBody(BaseModel):
    campaign: Optional[str] = None
    items: List[BulkItem]

# ---------- routes ----------
@router.post("/queue/sync")
async def sync_queue(
    body: SyncBody,
    businessid: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    tenant = find_tenant(db, businessid)
    token = decrypt(tenant.rn_jwt_enc)

    # Build OData query
    select = "Key,FirstName,LastName,Company,Mobile,Work,Home,Email,DoNotCall"
    if body.odata_filter:
        filt = body.odata_filter
    else:
        # default: not DNC and has any phone
        filt = "(DoNotCall eq false) and ((Mobile ne null) or (Work ne null) or (Home ne null))"

    pulled = 0
    upserted = 0
    skipped = 0

    async for page in rn.odata_contacts_iter(token, select=select, filter=filt, top=200, max_rows=body.max_rows):
        for c in page:
            if do_not_call(c):
                skipped += 1
                continue
            num = first_phone(c)
            phone = normalize_e164(num or "")
            if not phone:
                skipped += 1
                continue
            key = contact_key(c)
            if not key:
                skipped += 1
                continue
            first, last = name_parts(c)
            company = get_company(c)
            email = get_email(c)

            # upsert
            item = (
                db.query(DialerQueue)
                .filter(
                    DialerQueue.tenant_id == tenant.id,
                    DialerQueue.object_key == key,
                    DialerQueue.campaign.is_(body.campaign) if body.campaign is None
                    else DialerQueue.campaign == body.campaign
                ).first()
            )
            if item:
                item.name_first = first or item.name_first
                item.name_last  = last  or item.name_last
                item.company    = company or item.company
                item.email      = email or item.email
                item.phone_e164 = phone or item.phone_e164
            else:
                item = DialerQueue(
                    tenant_id = tenant.id,
                    campaign  = body.campaign,
                    object_key = key,
                    name_first = first,
                    name_last  = last,
                    company    = company,
                    phone_e164 = phone,
                    email      = email,
                    status     = "pending"
                )
                db.add(item)
                upserted += 1
            pulled += 1
        db.commit()

    return {"ok": True, "pulled": pulled, "added_or_updated": upserted, "skipped": skipped, "campaign": body.campaign}

@router.post("/queue/bulk")
def bulk_seed(
    body: BulkBody,
    businessid: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    tenant = find_tenant(db, businessid)
    added = 0
    updated = 0
    for it in body.items:
        phone = normalize_e164(it.phone_e164)
        if not phone:
            continue
        item = (
            db.query(DialerQueue)
            .filter(
                DialerQueue.tenant_id == tenant.id,
                DialerQueue.object_key == it.object_key,
                DialerQueue.campaign.is_(body.campaign) if body.campaign is None
                else DialerQueue.campaign == body.campaign
            ).first()
        )
        if item:
            item.name_first = it.name_first or item.name_first
            item.name_last  = it.name_last  or item.name_last
            item.company    = it.company    or item.company
            item.email      = it.email      or item.email
            item.phone_e164 = phone         or item.phone_e164
            updated += 1
        else:
            db.add(DialerQueue(
                tenant_id = tenant.id,
                campaign  = body.campaign,
                object_key = it.object_key,
                name_first = it.name_first,
                name_last  = it.name_last,
                company    = it.company,
                phone_e164 = phone,
                email      = it.email,
                status     = "pending"
            ))
            added += 1
    db.commit()
    return {"ok": True, "added": added, "updated": updated}

class NextOut(BaseModel):
    id: int
    contact_key: str
    name: str
    company: str | None = None
    phone: str
    email: str | None = None
    crm_url: str

@router.get("/next", response_model=NextOut)
def next_contact(
    businessid: Optional[str] = Query(None),
    agent: str = Query(..., description="Agent identifier (email)"),
    campaign: Optional[str] = Query(None),
    lock_ttl_sec: int = Query(120, ge=30, le=600),
    db: Session = Depends(get_db)
):
    tenant = find_tenant(db, businessid)

    # release expired locks
    now = datetime.now(timezone.utc)
    ttl_cutoff = now - timedelta(seconds=lock_ttl_sec)
    expired = (
        db.query(DialerQueue)
        .filter(
            DialerQueue.tenant_id == tenant.id,
            DialerQueue.status == "locked",
            DialerQueue.locked_at < ttl_cutoff
        ).all()
    )
    for r in expired:
        r.status = "pending"
        r.locked_by = None
        r.locked_at = None
    if expired:
        db.commit()

    # pick next pending
    q = (
        db.query(DialerQueue)
        .filter(
            DialerQueue.tenant_id == tenant.id,
            DialerQueue.status == "pending"
        )
    )
    if campaign is None:
        q = q.filter(DialerQueue.campaign.is_(None))
    else:
        q = q.filter(DialerQueue.campaign == campaign)

    item = q.order_by(DialerQueue.attempts.asc(), DialerQueue.created_at.asc()).first()
    if not item:
        raise HTTPException(404, "No pending records")

    item.status = "locked"
    item.locked_by = agent
    item.locked_at = now
    db.commit()
    db.refresh(item)

    full_name = " ".join([p for p in [item.name_first or "", item.name_last or ""] if p]).strip() or "Unknown"
    return NextOut(
        id=item.id,
        contact_key=item.object_key,
        name=full_name,
        company=item.company,
        phone=item.phone_e164,
        email=item.email,
        crm_url=crm_url_from_key(item.object_key)
    )
