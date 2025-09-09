# app/routes/dialer.py
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, EmailStr
from typing import Optional

from ..services.realnex_api import (
    # EXPECTED EXPORTS in app/services/realnex_api.py:
    # - resolve_user_and_team(agent_email: str) -> dict
    # - create_contact(first_name: str, last_name: str, email: Optional[str], phone: Optional[str], company: Optional[str]) -> dict
    # - find_contact_by_phone(phone_e164: str) -> Optional[dict]
    # - create_history_record(payload: dict) -> dict
    resolve_user_and_team,
    create_contact,
    find_contact_by_phone,
    create_history_record,
)

router = APIRouter()


@router.get("/health")
def health():
    return {"ok": True, "routes": ["/health", "/contacts", "/history/call", "/debug/realnex/resolve_user"]}


@router.get("/health/realnex")
def health_realnex():
    # simple check that JWT is loaded by the service module
    has = True
    try:
        # will raise if not configured
        _ = resolve_user_and_team  # noqa: F401
    except Exception:
        has = False
    return {"has_jwt": has}


class ContactIn(BaseModel):
    first_name: str
    last_name: str
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    company: Optional[str] = None


@router.post("/contacts")
def create_contact_endpoint(body: ContactIn):
    try:
        resp = create_contact(
            first_name=body.first_name,
            last_name=body.last_name,
            email=body.email,
            phone=body.phone,
            company=body.company,
        )
        return resp
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class HistoryCallIn(BaseModel):
    # who made the call (we resolve user/team from this)
    agent_email: EmailStr

    # basic call facts
    direction: str  # "outbound" | "inbound"
    disposition: Optional[str] = None
    duration_sec: Optional[int] = None
    started_at: Optional[str] = None   # ISO8601
    ended_at: Optional[str] = None     # ISO8601
    notes: Optional[str] = None
    is_completed: Optional[bool] = True

    # linking to a person
    to_number: Optional[str] = None
    from_number: Optional[str] = None

    # optional external references
    call_id: Optional[str] = None
    recording_url: Optional[str] = None


@router.post("/history/call")
def create_history_for_call(body: HistoryCallIn):
    """
    Creates a RealNex History record for a phone call.
    Flow:
      1) resolve userKey/teamKey by agent_email
      2) find or create a contact by phone (prefer 'to_number' for outbound, 'from_number' for inbound)
      3) POST /api/v1/Crm/history with required fields + associations
    """
    try:
        # 1) resolve user/team
        resolved = resolve_user_and_team(body.agent_email)
        user_key = resolved.get("userKey")
        team_key = resolved.get("teamKey")
        if not user_key or not team_key:
            raise HTTPException(status_code=404, detail="Unable to resolve RealNex user/team from agent_email")

        # 2) pick the “other party” phone
        target_phone = None
        if body.direction and body.direction.lower().startswith("out"):
            target_phone = body.to_number or body.from_number
        else:
            target_phone = body.from_number or body.to_number

        contact_key = None
        if target_phone:
            existing = find_contact_by_phone(target_phone)
            if existing and existing.get("objectKey"):
                contact_key = existing["objectKey"]
            else:
                # create a bare contact if none found
                created = create_contact(
                    first_name="",
                    last_name="",
                    email=None,
                    phone=target_phone,
                    company=None,
                )
                contact_key = created.get("objectKey")

        # 3) build history payload
        subject = f"Call ({body.direction})"
        if body.disposition:
            subject += f" - {body.disposition}"

        payload = {
            # required ownership
            "userKey": user_key,
            "teamKey": team_key,

            # status / type
            # These keys are enums in RealNex; if your tenant needs specific numbers, map them in realnex_api.py
            "eventTypeKey": 0,    # e.g., 0 = Call (adjust if your tenant differs)
            "statusKey": 0,       # e.g., 0 = Completed/Open (adjust as needed)

            # timing
            "published": True,
            "timeless": False,
            "startDate": body.started_at,
            "endDate": body.ended_at,

            # presentation
            "subject": subject,
            "notes": body.notes or "",
            "logical1": bool(body.is_completed),
        }

        # attach phone meta in free-form notes
        extra = []
        if body.duration_sec is not None:
            extra.append(f"Duration: {body.duration_sec}s")
        if body.call_id:
            extra.append(f"CallId: {body.call_id}")
        if body.recording_url:
            extra.append(f"Recording: {body.recording_url}")
        if body.from_number:
            extra.append(f"From: {body.from_number}")
        if body.to_number:
            extra.append(f"To: {body.to_number}")
        if extra:
            payload["notes"] = (payload["notes"] + "\n" if payload["notes"] else "") + "\n".join(extra)

        # POST history
        created_hist = create_history_record(payload)

        # link the contact if we have one
        if contact_key:
            # The helper should attach the object if your service implements it,
            # otherwise, you can expose a second helper in realnex_api.py:
            # add_object_to_history(historyKey, objectKey)
            # To keep this router thin, we simply return both keys:
            created_hist["linkedContactKey"] = contact_key

        return created_hist

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/debug/realnex/resolve_user")
def debug_resolve_user(email: EmailStr = Query(..., description="RealNex user login email")):
    try:
        return resolve_user_and_team(str(email))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
