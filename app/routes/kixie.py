# app/routes/kixie.py
import json
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..services.db import get_db
from ..models.tenant import Tenant
from ..models.eventlog import EventLog
from ..services.crypto import decrypt
from ..services.realnex_api import (
    search_any,
    get_contacts,
    create_contact_by_number,
    create_history,                 # legacy REST fallback (creates + link)
    search_contact_by_phone_odata,  # OData contact search
    list_odata_entitysets,          # OData service doc
    create_history_odata,           # OData history create
)

router = APIRouter()

# ---------- helpers ----------
def _normalize_phone(num: str | None) -> Optional[str]:
    if not num:
        return None
    digits = "".join(ch for ch in str(num) if ch.isdigit())
    if not digits:
        return None
    if digits.startswith("1") and len(digits) == 11:
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    return f"+{digits}"

def _get_ci(d: dict, key: str):
    if not isinstance(d, dict):
        return None
    lower_map = {k.lower(): k for k in d.keys()}
    real = lower_map.get(key.lower())
    return d.get(real) if real else None

def _first_list(obj: Dict[str, Any]) -> list:
    if not isinstance(obj, dict):
        return []
    for k in ("value", "Value", "data", "Data", "results", "Results"):
        v = obj.get(k)
        if isinstance(v, list):
            return v
    return []

def _contact_key(c: Dict[str, Any]) -> Optional[str]:
    for k in ("Key", "key", "objectKey", "contactKey", "id", "Id", "ID"):
        if c.get(k):
            return str(c[k])
    return None

def _extract_name_company_email(d: dict) -> Tuple[Tuple[Optional[str], Optional[str], Optional[str]], Optional[str]]:
    def gi(*keys):
        for k in keys:
            if k in d and d[k]:
                return str(d[k]).strip()
            lk = {kk.lower(): kk for kk in d.keys()}
            real = lk.get(k.lower())
            if real and d.get(real):
                return str(d[real]).strip()
        return None

    full = gi("customername","customer_name","contactname","contact_name","name","full_name","fullname","displayname")
    fn   = gi("firstname","first_name","first")
    ln   = gi("lastname","last_name","last")
    email= gi("email","customeremail","customer_email")

    if full and not (fn and ln):
        parts = [p for p in full.replace(",", " ").split() if p]
        if len(parts) == 1:
            fn = fn or parts[0]
        elif len(parts) >= 2:
            fn = fn or parts[0]
            ln = ln or " ".join(parts[1:])

    company = gi("company","companyname","organization","account","accountname")
    return (fn, ln, email or None), company

def _tenant_by_business(db: Session, business_id: str) -> Optional[Tenant]:
    return (
        db.query(Tenant)
        .filter(Tenant.kixie_business_id == business_id, Tenant.active == True)
        .first()
    )

def _idem_key(tenant_id: int, callid: str | None, event: str) -> str:
    return hashlib.sha256(f"{tenant_id}|{callid or ''}|{event}".encode()).hexdigest()

async def _find_or_create_contact(
    token: str,
    number_e164: str,
    name_email_hint: Tuple[Optional[str], Optional[str], Optional[str]] | None = None,
    company_hint: Optional[str] = None,
) -> Dict[str, Any]:
    # 1) OData phone search
    try:
        od = await search_contact_by_phone_odata(token, number_e164)
        hits = _first_list(od)
    except Exception:
        hits = []
    # 2) REST /Crm/Contact?q=
    if not hits:
        try:
            rc = await get_contacts(token, {"q": number_e164})
            hits = _first_list(rc)
        except Exception:
            hits = []
    # 3) Global search (filter to contacts)
    if not hits:
        try:
            anyr = await search_any(token, number_e164)
            hits = [x for x in _first_list(anyr) if str(_get_ci(x, "entityType") or "").lower().startswith("contact")]
        except Exception:
            hits = []
    # 4) Create minimal contact if still none
    if not hits:
        fn, ln, em = name_email_hint or (None, None, None)
        created = await create_contact_by_number(
            token, number_e164, first_name=fn or "", last_name=ln or "", email=em, company=company_hint
        )
        return created if isinstance(created, dict) else {}
    return hits[0]

def resolve_event_type_key(event: str) -> Optional[str]:
    import os
    ev = (event or "").lower()
    key = None
    if ev == "endcall":
        key = os.getenv("RN_EVENTTYPEKEY_CALL")
    elif ev == "disposition":
        key = os.getenv("RN_EVENTTYPEKEY_DISPOSITION") or os.getenv("RN_EVENTTYPEKEY_CALL")
    elif ev == "sms":
        key = os.getenv("RN_EVENTTYPEKEY_SMS")
    key = key or os.getenv("RN_EVENTTYPEKEY_DEFAULT")
    return str(key) if key else None

# ---------- models ----------
class WebhookBody(BaseModel):
    businessid: str
    hookevent: str
    data: Dict[str, Any] = {}

# ---------- routes ----------
@router.get("/lookup")
async def lookup(
    number: str = Query(..., alias="number"),
    businessid: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    tenant = db.query(Tenant).first() if not businessid else _tenant_by_business(db, businessid)
    if not tenant:
        raise HTTPException(404, "No tenant configured")

    rn_token = decrypt(tenant.rn_jwt_enc)
    phone = _normalize_phone(number)
    if not phone:
        raise HTTPException(400, "Invalid phone")

    contact = await _find_or_create_contact(rn_token, phone)
    cid = _contact_key(contact)
    if not cid:
        raise HTTPException(500, f"Unable to resolve RealNex contact id (keys={list(contact.keys()) if isinstance(contact, dict) else type(contact).__name__})")

    url = f"https://crm.realnex.com/Contact/{cid}"
    return {
        "found": True,
        "contact": {
            "first_name": contact.get("FirstName") or contact.get("firstName") or "",
            "last_name":  contact.get("LastName")  or contact.get("lastName")  or "",
            "email":      contact.get("Email")     or contact.get("email")     or "",
            "phone_number": phone,
            "contact_id": cid,
            "url": url,
        },
    }

@router.get("/odata/sets", summary="List RN OData entity sets for this tenant")
async def odata_sets(
    businessid: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    tenant = db.query(Tenant).first() if not businessid else _tenant_by_business(db, businessid)
    if not tenant:
        raise HTTPException(404, "No tenant configured")
    rn_token = decrypt(tenant.rn_jwt_enc)
    return await list_odata_entitysets(rn_token)

@router.post("/webhooks", summary="Kixie → Goose webhook (validated)")
async def webhooks(
    body: WebhookBody,
    x_goose_secret: str = Header(..., alias="X-Goose-Secret"),
    db: Session = Depends(get_db),
):
    tenant = _tenant_by_business(db, body.businessid)
    if not tenant:
        raise HTTPException(404, "Unknown businessid")
    if x_goose_secret != tenant.webhook_secret:
        raise HTTPException(401, "Invalid signature")

    callid = body.data.get("callid") or body.data.get("id")
    idem = _idem_key(tenant.id, callid, body.hookevent)
    if db.query(EventLog).filter_by(idem_key=idem).first():
        return {"ok": True, "duplicate": True}

    status, error = "ok", None
    try:
        rn_token = decrypt(tenant.rn_jwt_enc)
        num = (
            body.data.get("fromnumber164")
            or body.data.get("fromnumber")
            or body.data.get("customernumber")
            or body.data.get("internalnumber")
            or body.data.get("tonumber164")
            or body.data.get("tonumber")
        )
        phone = _normalize_phone(num)
        if not phone:
            raise HTTPException(400, "No phone number in payload")

        name_email_hint, company_hint = _extract_name_company_email(body.data)
        contact = await _find_or_create_contact(rn_token, phone, name_email_hint, company_hint)
        cid = _contact_key(contact)
        if not cid:
            raise HTTPException(500, f"Unable to resolve RealNex contact id (keys={list(contact.keys())})")

        # Build note/subject
        parts = []
        d = body.data.get("calltype") or body.data.get("direction")
        if d: parts.append(f"Direction: {d}")
        dur = body.data.get("duration")
        if dur is not None: parts.append(f"Duration: {dur}s")
        dispo = body.data.get("disposition")
        if dispo: parts.append(f"Disposition: {dispo}")
        rec = body.data.get("recordingurl")
        if rec: parts.append(f"Recording: {rec}")
        agent = body.data.get("userid") or body.data.get("agent")
        if agent: parts.append(f"Agent: {agent}")
        note = " | ".join(parts) if parts else body.hookevent

        fn, ln, _em = name_email_hint
        friendly = " ".join([x for x in [fn, ln] if x])
        if friendly:
            note = f"{friendly} | {note}"

        subject = f"Kixie {body.hookevent}"
        now = datetime.now(timezone.utc).isoformat()
        et_key = resolve_event_type_key(body.hookevent)

        # OData-first
        odata_res = await create_history_odata(rn_token, subject, note, now, cid, et_key)
        if odata_res.get("status", 500) >= 400:
            # Fallback: create generic history, then link to contact object
            common = {
                "note": note,
                "description": note,
                "subject": subject,
                "title": subject,
                "date": now,
            }
            if et_key:
                common["EventTypeKey"] = et_key
                common["eventTypeKey"] = et_key
            rest_res = await create_history(rn_token, common, object_key=cid)
            if rest_res.get("status", 500) >= 400:
                raise HTTPException(502, f"RealNex history failed: {rest_res.get('error') or rest_res}")

    except HTTPException as he:
        status, error = "error", f"{he.status_code}: {he.detail}"
    except Exception as e:
        status, error = "error", str(e)

    ev = EventLog(
        tenant_id=tenant.id,
        event_type=body.hookevent,
        callid=callid,
        idem_key=idem,
        payload_json=json.dumps(body.model_dump()),
        status=status,
        error=error,
    )
    db.add(ev); db.commit()

    if error:
        raise HTTPException(500, error)
    return {"ok": True}
