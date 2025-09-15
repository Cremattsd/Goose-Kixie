# app/routes/dialer.py
from __future__ import annotations

import os, hmac, hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

from fastapi import APIRouter, Request, HTTPException, Query, Header, Depends
from pydantic import BaseModel, Field
from zoneinfo import ZoneInfo
from sqlalchemy.orm import Session

from ..schemas.kixie import KixieWebhook, SimpleContact
from ..services.db import get_db
from ..models.call_state import CallState
from ..models.tenant import Tenant

from ..services.realnex_api import (
    normalize_phone_e164ish,
    digits_only,
    search_by_phone,                          # CRM-native search (if tenant supports)
    create_contact,
    create_history,
    create_task,
    get_rn_token,
    search_contact_keys_by_phone_two_stage,   # OData probe + CRM verify
    attach_recording_from_url,
    is_valid_timezone,
)

router = APIRouter()

# ─────────────────────────── Health ───────────────────────────

@router.get("/health/realnex")
def health_realnex():
    """Surface that we have a RealNex token configured."""
    return {"has_jwt": bool(get_rn_token())}

# ───────────────────────── Signature / Secrets ──────────────────────────

def _verify_kixie_signature(raw: bytes, header_sig: Optional[str]) -> None:
    """HMAC-SHA256 check if KIXIE_WEBHOOK_SECRET is set; otherwise no-op."""
    secret = os.getenv("KIXIE_WEBHOOK_SECRET")
    if not secret:
        return
    if not header_sig:
        raise HTTPException(status_code=401, detail="Missing signature")
    calc = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, header_sig):
        raise HTTPException(status_code=401, detail="Invalid signature")

def _verify_goose_shared_secret(db: Session, request: Request) -> None:
    """
    Accepts the X-Goose-Secret header (installed via /install) if present.
    If there are tenants installed, require a match. If none installed, skip.
    """
    rows = db.query(Tenant.id, Tenant.webhook_secret).all()
    if not rows:
        return
    provided = request.headers.get("X-Goose-Secret")
    if not provided:
        raise HTTPException(status_code=401, detail="X-Goose-Secret required")
    if provided not in {r.webhook_secret for r in rows}:
        raise HTTPException(status_code=401, detail="Invalid X-Goose-Secret")

# ───────────────────────── Helpers ────────────────────────────

def _subject_from(a: KixieWebhook) -> str:
    dirn = a.direction or "unknown"
    dispo = a.disposition or "unknown"
    return f"Call {dirn} - {dispo}"

def _notes_from(a: KixieWebhook, extra_note: Optional[str] = None) -> str:
    parts = [
        f"Kixie {a.event} • {a.duration_sec or 0}s",
        f"From: {a.from_number or ''} → To: {a.to_number or ''}",
        f"Agent: {a.agent_email or ''}",
    ]
    if a.recording_url:
        parts.append(f"Recording: {a.recording_url}")
    if a.call_id:
        parts.append(f"Call ID: {a.call_id}")
    if getattr(a, "agent_notes", None):
        parts.append(f"Notes: {getattr(a, 'agent_notes')}")
    if (extra_note or "").strip():
        parts.append(f"User Note: {extra_note.strip()}")
    return "\n".join(parts)

async def _find_existing_contact_key(token: str, number_raw: Optional[str]) -> Optional[str]:
    """
    Pipeline:
      1) CRM /Contact(s)/search (strict 2xx only).
      2) OData probe → candidate keys → CRM confirm phones.
    Never auto-creates.
    """
    if not number_raw:
        return None
    e164 = normalize_phone_e164ish(number_raw)
    if not e164:
        return None

    # #1 CRM search (only trust 2xx)
    crm = await search_by_phone(token, e164)
    if int(crm.get("status", 0)) // 100 == 2:
        data = crm.get("data") or crm.get("value") or crm.get("items") or crm
        rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        for row in rows:
            if isinstance(row, dict):
                for k in ("contactKey", "ContactKey", "Key", "key", "Id", "id"):
                    v = row.get(k)
                    if isinstance(v, str) and v:
                        return v

    # #2 OData two-stage (probe phone fields; verify via CRM read)
    od = await search_contact_keys_by_phone_two_stage(token, e164)
    if int(od.get("status", 0)) // 100 == 2 and od.get("contactKey"):
        return str(od["contactKey"])

    return None

def _auto_tasks_enabled(flag_qs: Optional[bool]) -> bool:
    if flag_qs is not None:
        return bool(flag_qs)
    env = os.getenv("AUTO_TASKS_DEFAULT", "0").strip().lower()
    return env in ("1","true","yes","y","on")

def _auto_task_dispo_set() -> set[str]:
    raw = os.getenv("AUTO_TASK_DISPOSITIONS", "Left VM,Voicemail,No Answer,Call Back,Follow Up")
    return {s.strip().lower() for s in raw.split(",") if s.strip()}

def _due_iso(end_iso: Optional[str]) -> str:
    try:
        if end_iso:
            dt = datetime.fromisoformat(end_iso.replace("Z","+00:00"))
        else:
            dt = datetime.now(timezone.utc)
    except Exception:
        dt = datetime.now(timezone.utc)
    return (dt + timedelta(days=1)).astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")

async def _post_history_and_optional_task(
    payload: KixieWebhook,
    tz_assume: Optional[ZoneInfo],
    extra_note: Optional[str],
    auto_tasks: bool,
    token: str,
    contact_key: str,
) -> Dict[str, Any]:
    # Build History payload (use schema’s UTC helpers)
    start_iso = payload.start_utc_ms(tz_assume)
    end_iso   = payload.end_utc_ms(tz_assume)

    event_type_key = int(os.getenv("RN_EVENT_TYPE_PHONE", os.getenv("RN_EVENTTYPEKEY_CALL", "1")))
    status_key     = int(os.getenv("RN_STATUS_COMPLETED", "0"))
    link_field     = os.getenv("RN_HISTORY_CONTACT_LINK_FIELD", "contactKey")

    hist: Dict[str, Any] = {
        "published": True,
        "timeless": False,
        "startDate": start_iso,
        "endDate": end_iso,
        "eventTypeKey": event_type_key,
        "statusKey": status_key,
        "subject": _subject_from(payload),
        "notes": _notes_from(payload, extra_note=extra_note),
        "user1": "Kixie",
        "user2": payload.event or "",
        "user3": payload.disposition or "",
        "user4": payload.direction or "",
        "logical1": True,
        link_field: contact_key,
    }
    if os.getenv("RN_USER_KEY"):
        hist["userKey"] = os.getenv("RN_USER_KEY")
    if os.getenv("RN_TEAM_KEY"):
        hist["teamKey"] = os.getenv("RN_TEAM_KEY")

    rn_hist = await create_history(token, {k: v for k, v in hist.items() if v is not None})
    out: Dict[str, Any] = {
        "status": rn_hist.get("status", 200),
        "contactKey": contact_key,
        "history_post_body": hist,
        "realnex_history": rn_hist,
    }

    # Optional recording attachment (to Contact)
    if os.getenv("ATTACH_RECORDING_TO_CONTACT", "0") == "1" and payload.recording_url:
        out["attachment"] = await attach_recording_from_url(token, contact_key, payload.recording_url)

    # Optional auto-task
    if auto_tasks:
        trigger_set = _auto_task_dispo_set()
        if (payload.disposition or "").strip().lower() in trigger_set:
            subj = f"Follow up: {payload.disposition or 'Call'}"
            notes = f"Auto-task from call ({payload.direction or 'n/a'}). ContactKey: {contact_key}\n" \
                    f"Number: {payload.to_number or payload.from_number or ''}\n" \
                    f"Call ID: {payload.call_id or ''}"
            due = _due_iso(end_iso)
            task = {
                "subject": subj,
                "notes": notes,
                "dueDate": due,
                "DueDate": due,  # be generous with field name
                "contactKey": contact_key,
            }
            if os.getenv("RN_USER_KEY"):
                task["userKey"] = os.getenv("RN_USER_KEY")
            if os.getenv("RN_TEAM_KEY"):
                task["teamKey"] = os.getenv("RN_TEAM_KEY")
            rn_task = await create_task(token, task)
            out["auto_task"] = {"post_body": task, "realnex_task": rn_task}
    return out

# ───────────────────────── Routes: Contact lookup ─────────────────────────────

@router.get("/contacts/search")
async def contacts_search(phone: str = Query(..., description="Phone number to search (any format)")):
    """
    Search a contact by phone with tenant-safe fallback.
    1) Normalize to E.164.
    2) Try CRM /Contact(s)/search (strict 2xx only).
    3) Fallback to OData two-stage (probe phone fields, then CRM verify).
    """
    token = get_rn_token()
    if not token:
        raise HTTPException(status_code=401, detail="REALNEX_JWT/REALNEX_TOKEN not configured")

    normalized = normalize_phone_e164ish(phone) or ""
    if len(digits_only(normalized) or "") < 11:  # Require full E.164-like length
        return {
            "status": 400,
            "error": "phone_too_short",
            "hint": "Use full E.164 (e.g. +18584581063)",
            "normalized": normalized,
        }

    # 1) CRM-native search
    crm = await search_by_phone(token, normalized)
    if int(crm.get("status", 0)) // 100 == 2:
        # Extract key if possible
        key = None
        data = crm.get("data") or crm.get("value") or crm.get("items") or crm
        rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        for row in rows:
            if isinstance(row, dict):
                for k in ("contactKey", "ContactKey", "Key", "key", "Id", "id"):
                    v = row.get(k)
                    if isinstance(v, str) and v:
                        key = v
                        break
            if key:
                break
        return {"status": 200, "source": "crm_search", "normalized": normalized, "contactKey": key, "raw": crm}

    # 2) OData two-stage fallback
    wide = await search_contact_keys_by_phone_two_stage(token, phone)
    if int(wide.get("status", 0)) // 100 == 2 and wide.get("contactKey"):
        return {
            "status": 200,
            "source": "odata_two_stage",
            "normalized": normalized,
            "contactKey": wide["contactKey"],
            "probe_fields": wide.get("probe_fields", []),
        }

    return {
        "status": 404,
        "error": "no_contact_match",
        "normalized": normalized,
        "crm": crm,
        "fallback": wide,
    }

# ───────────────────────── Routes: In-flight call state ───────────────────────

class StartCallBody(BaseModel):
    call_id: str = Field(..., min_length=2, max_length=64)
    agent_email: Optional[str] = None
    phone: Optional[str] = None
    started_at: Optional[datetime] = None  # allow UI to send precise start

class UpdateCallBody(BaseModel):
    call_id: str = Field(..., min_length=2, max_length=64)
    disposition: Optional[str] = None
    note: Optional[str] = None

@router.post("/dialer/call/start")
def call_start(body: StartCallBody, db: Session = Depends(get_db)):
    """
    Start/ensure a call state row (for live note-taking & timer).
    Idempotent on call_id.
    """
    row = db.get(CallState, body.call_id)
    if row is None:
        row = CallState(call_id=body.call_id)
        db.add(row)
    row.agent_email = body.agent_email or row.agent_email
    row.phone_e164  = normalize_phone_e164ish(body.phone) if body.phone else row.phone_e164
    row.started_at  = (body.started_at if (body.started_at and body.started_at.tzinfo) else
                       (body.started_at.replace(tzinfo=timezone.utc) if body.started_at else datetime.now(timezone.utc)))
    row.updated_at  = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True, "call_id": row.call_id, "started_at": row.started_at}

@router.post("/dialer/call/update")
def call_update(body: UpdateCallBody, db: Session = Depends(get_db)):
    """
    Update live disposition and/or note during the call.
    """
    row = db.get(CallState, body.call_id)
    if row is None:
        row = CallState(call_id=body.call_id)
        db.add(row)
    if body.disposition is not None:
        row.disposition = body.disposition
    if body.note is not None:
        row.note = body.note
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True, "call_id": row.call_id, "disposition": row.disposition, "note": row.note}

@router.post("/dialer/call/end")
async def call_end(
    payload: KixieWebhook,
    request: Request,
    auto_tasks: Optional[bool] = Query(None, description="Override AUTO_TASKS_DEFAULT (true/false)"),
    x_user_tz: Optional[str] = Header(None, convert_underscores=False),
    db: Session = Depends(get_db),
):
    """
    Finish a call without waiting for Kixie webhook (manual path).
    Uses the same History-posting logic as the webhook and merges any
    saved live note/disposition from CallState.
    """
    # (Optional) honor X-Goose-Secret if tenants exist
    _verify_goose_shared_secret(db, request)

    token = get_rn_token()
    if not token:
        raise HTTPException(status_code=401, detail="REALNEX_JWT/REALNEX_TOKEN not configured")

    # Resolve timezone for naive timestamps
    tz_assume = None
    if x_user_tz:
        try:
            if await is_valid_timezone(token, x_user_tz):
                tz_assume = ZoneInfo(x_user_tz)
            else:
                tz_assume = ZoneInfo(x_user_tz)
        except Exception:
            tz_assume = None

    # Merge any live state
    cs = db.get(CallState, payload.call_id) if payload.call_id else None
    merged_note = cs.note if cs and cs.note else None
    if cs and not payload.disposition and cs.disposition:
        payload.disposition = cs.disposition

    # Determine target number and contact
    target_number = payload.to_number if (payload.direction or "outbound") == "outbound" else payload.from_number
    contact_key = await _find_existing_contact_key(token, target_number)
    if not contact_key:
        # Clean up state anyway
        if cs:
            db.delete(cs); db.commit()
        return {
            "status": 202,
            "skipped": True,
            "reason": "No contact match; not creating contacts",
            "normalized_number": normalize_phone_e164ish(target_number or ""),
            "tz_used": x_user_tz or "UTC",
        }

    out = await _post_history_and_optional_task(
        payload=payload,
        tz_assume=tz_assume,
        extra_note=merged_note,
        auto_tasks=_auto_tasks_enabled(auto_tasks),
        token=token,
        contact_key=contact_key,
    )

    # Clean up state
    if cs:
        db.delete(cs); db.commit()

    return out

# ───────────────────────── Kixie Webhook (merge state + auto-task) ────────────

@router.post("/webhooks/kixie")
async def kixie_webhook(
    payload: KixieWebhook,
    request: Request,
    x_user_tz: Optional[str] = Header(None, convert_underscores=False),  # pass IANA tz like "America/Chicago"
    db: Session = Depends(get_db),
):
    """
    Receives Kixie webhook, finds an existing contact by phone, and logs a History
    linked to that contact. We do NOT create contacts. If no match → 202 skipped.
    Merges any live CallState note/dispo and (optionally) creates a follow-up Task.
    """
    raw = await request.body()
    _verify_kixie_signature(raw, request.headers.get(os.getenv("KIXIE_SIGNATURE_HEADER", "X-Kixie-Signature")))
    # Also accept X-Goose-Secret if installed via /install
    try:
        _verify_goose_shared_secret(db, request)
    except HTTPException:
        # If tenant-secret check fails but KIXIE HMAC passed (or not configured), continue.
        # This keeps compatibility with existing Kixie-only configs.
        pass

    token = get_rn_token()
    if not token:
        raise HTTPException(status_code=401, detail="REALNEX_JWT/REALNEX_TOKEN not configured")

    # Timezone for naive timestamps
    tz_assume = None
    if x_user_tz:
        try:
            if await is_valid_timezone(token, x_user_tz):
                tz_assume = ZoneInfo(x_user_tz)
            else:
                tz_assume = ZoneInfo(x_user_tz)
        except Exception:
            tz_assume = None

    # Target number (customer)
    target_number = payload.to_number if (payload.direction or "outbound") == "outbound" else payload.from_number

    # Merge any live CallState
    cs = db.get(CallState, payload.call_id) if payload.call_id else None
    merged_note = cs.note if cs and cs.note else None
    if cs and not payload.disposition and cs.disposition:
        payload.disposition = cs.disposition

    # Find existing contact (no auto-creates)
    contact_key = await _find_existing_contact_key(token, target_number)
    if not contact_key:
        if cs:
            db.delete(cs); db.commit()
        return {
            "status": 202,
            "skipped": True,
            "reason": "No contact match; not creating contacts",
            "normalized_number": normalize_phone_e164ish(target_number or ""),
            "tz_used": x_user_tz or "UTC",
            "search": {"wide_two_stage": True},
        }

    out = await _post_history_and_optional_task(
        payload=payload,
        tz_assume=tz_assume,
        extra_note=merged_note,
        auto_tasks=_auto_tasks_enabled(None),  # webhook uses env default unless you add qs flag at Kixie
        token=token,
        contact_key=contact_key,
    )

    # Clean up CallState after merge
    if cs:
        db.delete(cs); db.commit()

    return out

# ─────────────────────── Contact create (manual) ───────────────────────

@router.post("/contacts")
async def contacts_create(body: SimpleContact):
    """
    Manual contact create (dev/testing). Webhook path never auto-creates.
    """
    token = get_rn_token()
    if not token:
        raise HTTPException(status_code=401, detail="REALNEX_JWT/REALNEX_TOKEN not configured")

    payload = {
        "FirstName": body.first_name or "",
        "LastName": body.last_name or "",
        "Email": body.email or None,
        "PrimaryPhone": normalize_phone_e164ish(body.phone) if body.phone else None,
        "Company": body.company or None,
        "Source": "Goose",
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    return await create_contact(token, payload)
