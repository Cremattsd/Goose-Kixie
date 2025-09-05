import os, json, hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Optional

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
    create_history,
    search_contact_by_phone_odata,
)

router = APIRouter()


# ---------- helpers ----------
def find_tenant(db: Session, business_id: str):
    return (
        db.query(Tenant)
        .filter(Tenant.kixie_business_id == business_id, Tenant.active == True)
        .first()
    )


def idem_key(tenant_id: int, callid: str | None, event: str) -> str:
    return hashlib.sha256(f"{tenant_id}|{callid or ''}|{event}".encode()).hexdigest()


def _get_case_insensitive(d: dict, key: str):
    """Return d[key] with case-insensitive lookup, else None."""
    if not isinstance(d, dict):
        return None
    lower_map = {k.lower(): k for k in d.keys()}
    real = lower_map.get(key.lower())
    return d.get(real) if real else None


def extract_contact_id(obj: Any) -> Optional[str]:
    if isinstance(obj, dict):
        # case-insensitive search across common id keys
        for k in ("contactId", "id", "key", "objectKey", "contactKey", "entityId", "entityKey"):
            v = _get_case_insensitive(obj, k)
            if v:
                return str(v)
        # nested "value" arrays
        val = _get_case_insensitive(obj, "value")
        if isinstance(val, list) and val:
            for item in val:
                et = str(_get_case_insensitive(item, "entityType") or "").lower()
                if "contact" in et:
                    cid = extract_contact_id(item)
                    if cid:
                        return cid
            return extract_contact_id(val[0])
    return None


async def _find_or_create_contact(token: str, number_e164: str) -> dict:
    # 0) OData phone search (preferred)
    try:
        res = await search_contact_by_phone_odata(token, number_e164)
        candidates = res.get("value", []) if isinstance(res, dict) else []
    except Exception:
        candidates = []

    # 1) legacy direct search (belt & suspenders)
    if not candidates:
        try:
            res = await get_contacts(token, {"q": number_e164})
            candidates = res.get("value", []) if isinstance(res, dict) else []
        except Exception:
            candidates = []

    # 2) global search fallback (filters to contacts)
    if not candidates:
        try:
            res_any = await search_any(token, number_e164)
            if isinstance(res_any, dict):
                candidates = [x for x in res_any.get("value", []) if "contact" in str(_get_case_insensitive(x, "entityType") or "").lower()]
        except Exception:
            candidates = []

    # 3) create if still none
    if not candidates:
        created = await create_contact_by_number(token, number_e164)
        candidates = [created] if isinstance(created, dict) else []

    return candidates[0] if candidates else {}


def resolve_event_type_key(event: str) -> str:
    ev = (event or "").lower()
    key = None
    if ev == "endcall":
        key = os.getenv("RN_EVENTTYPEKEY_CALL")
    elif ev == "disposition":
        key = os.getenv("RN_EVENTTYPEKEY_DISPOSITION") or os.getenv("RN_EVENTTYPEKEY_CALL")
    elif ev == "sms":
        key = os.getenv("RN_EVENTTYPEKEY_SMS")
    key = key or os.getenv("RN_EVENTTYPEKEY_DEFAULT")
    if not key:
        raise HTTPException(500, "Missing RN_EVENTTYPEKEY_* in environment (.env)")
    return str(key)


# ---------- models ----------
class WebhookBody(BaseModel):
    businessid: str
    hookevent: str
    data: Dict[str, Any] = {}


# ---------- routes ----------
@router.get("/lookup")
async def lookup(
    number: str = Query(..., alias="number"),
    businessid: str = Query(None),
    db: Session = Depends(get_db),
):
    tenant = db.query(Tenant).first() if not businessid else find_tenant(db, businessid)
    if not tenant:
        raise HTTPException(404, "No tenant configured")
    rn_token = decrypt(tenant.rn_jwt_enc)
    contact = await _find_or_create_contact(rn_token, number)

    cid = extract_contact_id(contact)
    if not cid:
        raise HTTPException(
            500,
            f"Unable to resolve RealNex contact id (keys={list(contact.keys()) if isinstance(contact, dict) else type(contact).__name__})",
        )

    url = f"https://crm.realnex.com/Contact/{cid}"
    return {
        "found": True,
        "contact": {
            "first_name": str(_get_case_insensitive(contact, "firstName") or _get_case_insensitive(contact, "FirstName") or ""),
            "last_name":  str(_get_case_insensitive(contact, "lastName")  or _get_case_insensitive(contact, "LastName")  or ""),
            "email":      str(_get_case_insensitive(contact, "email")     or _get_case_insensitive(contact, "Email")     or ""),
            "phone_number": number,
            "contact_id": cid,
            "url": url,
        },
    }


@router.post("/webhooks", summary="Kixie → Goose webhook (validated)")
async def webhooks(
    body: WebhookBody,
    x_goose_secret: str = Header(..., alias="X-Goose-Secret"),
    db: Session = Depends(get_db),
):
    tenant = find_tenant(db, body.businessid)
    if not tenant:
        raise HTTPException(404, "Unknown businessid")
    if x_goose_secret != tenant.webhook_secret:
        raise HTTPException(401, "Invalid signature")

    callid = body.data.get("callid") or body.data.get("id")
    key = idem_key(tenant.id, callid, body.hookevent)
    if db.query(EventLog).filter_by(idem_key=key).first():
        return {"ok": True, "duplicate": True}

    status, error = "ok", None
    try:
        rn_token = decrypt(tenant.rn_jwt_enc)
        num = (
            body.data.get("fromnumber164")
            or body.data.get("fromnumber")
            or body.data.get("customernumber")
            or body.data.get("tonumber164")
            or body.data.get("tonumber")
            or ""
        )
        if not num:
            raise HTTPException(400, "No phone number in payload")

        contact = await _find_or_create_contact(rn_token, num)
        cid = extract_contact_id(contact)
        if not cid:
            raise HTTPException(
                500,
                f"Unable to resolve RealNex contact id (keys={list(contact.keys()) if isinstance(contact, dict) else type(contact).__name__})",
            )

        # ---- Build a rich note ----
        parts = []
        d = body.data.get("calltype") or body.data.get("direction")
        if d:
            parts.append(f"Direction: {d}")
        dur = body.data.get("duration")
        if dur is not None:
            parts.append(f"Duration: {dur}s")
        dispo = body.data.get("disposition")
        if dispo:
            parts.append(f"Disposition: {dispo}")
        rec = body.data.get("recordingurl")
        if rec:
            parts.append(f"Recording: {rec}")
        agent = body.data.get("userid") or body.data.get("agent")
        if agent:
            parts.append(f"Agent: {agent}")
        note = " | ".join(parts) if parts else body.hookevent

        et_key = resolve_event_type_key(body.hookevent)
        now = datetime.now(timezone.utc).isoformat()

        # Try multiple payload shapes & entityType casings (some tenants are picky)
        candidates: list[dict] = []
        common = {
            "note": note,
            "description": note,
            "subject": f"Kixie {body.hookevent}",
            "title": f"Kixie {body.hookevent}",
            "date": now,
            "EventTypeKey": et_key,
            "eventTypeKey": et_key,
        }
        for et in ("Contact", "crm.contact", "CrmContact"):
            candidates.append(dict(common, entityType=et, entityId=cid))
            candidates.append(dict(common, entityType=et, entityKey=cid))

        errors: list[str] = []
        success = None
        for payload in candidates:
            res = await create_history(rn_token, payload, cid)
            if res.get("status", 500) < 400:
                success = res
                break
            else:
                errors.append(
                    f"{payload.get('entityType')}|{('entityId' if 'entityId' in payload else 'entityKey')} -> {res.get('error')}"
                )

        if not success:
            # last resort: strip subject/title and try again quickly
            for payload in candidates[:2]:
                payload2 = {k: v for k, v in payload.items() if k not in ("subject", "title")}
                res = await create_history(rn_token, payload2, cid)
                if res.get("status", 500) < 400:
                    success = res
                    break
                else:
                    errors.append(f"no-subject/title -> {res.get('error')}")

        if not success:
            raise HTTPException(502, "RealNex History rejected all payloads: " + "; ".join(errors))

    except Exception as e:
        status, error = "error", str(e)

    ev = EventLog(
        tenant_id=tenant.id,
        event_type=body.hookevent,
        callid=callid,
        idem_key=key,
        payload_json=json.dumps(body.model_dump()),
        status=status,
        error=error,
    )
    db.add(ev)
    db.commit()

    if error:
        raise HTTPException(500, error)
    return {"ok": True}
