# add these imports at the top if missing
from pydantic import BaseModel
from typing import Any, Dict
from fastapi import Header

class WebhookBody(BaseModel):
    businessid: str
    hookevent: str
    data: Dict[str, Any] = {}

@router.post("/webhooks", summary="Kixie → Goose webhook (validated)")
async def webhooks(
    body: WebhookBody,
    x_goose_secret: str = Header(..., alias="X-Goose-Secret"),
    db: Session = Depends(get_db)
):
    # find tenant
    tenant = find_tenant(db, body.businessid)
    if not tenant:
        raise HTTPException(404, "Unknown businessid")

    # auth header
    if x_goose_secret != tenant.webhook_secret:
        raise HTTPException(401, "Invalid signature")

    # dedupe key
    callid = body.data.get("callid") or body.data.get("id")
    key = idem_key(tenant.id, callid, body.hookevent)
    if db.query(EventLog).filter_by(idem_key=key).first():
        return {"ok": True, "duplicate": True}

    status, error = "ok", None
    try:
        rn_token = decrypt(tenant.rn_jwt_enc)

        # choose a number
        num = (
            body.data.get("fromnumber164") or body.data.get("fromnumber")
            or body.data.get("customernumber")
            or body.data.get("tonumber164") or body.data.get("tonumber") or ""
        )
        if not num:
            raise HTTPException(400, "No phone number in payload")

        # find/create contact
        try:
            res = await get_contacts(rn_token, {"q": num})
            candidates = res.get("value", [])
        except Exception:
            candidates = []
        if not candidates:
            created = await create_contact(rn_token, {"mobile": num, "firstName": "", "lastName": "", "source": "kixie"})
            candidates = [created]
        c = candidates[0]
        cid = c.get("id") or c.get("objectKey") or c.get("contactKey")
        if not cid:
            raise HTTPException(500, "Unable to resolve RealNex contact id")

        # write History on endcall/disposition/sms
        if body.hookevent.lower() in ("endcall", "disposition", "sms"):
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

            await create_history(rn_token, {
                "entityType": "Contact",
                "entityId": cid,
                "note": note,
                "date": datetime.now(timezone.utc).isoformat()
            })

    except Exception as e:
        status, error = "error", str(e)

    # log event
    ev = EventLog(
        tenant_id=tenant.id, event_type=body.hookevent, callid=callid,
        idem_key=key, payload_json=json.dumps(body.model_dump()), status=status, error=error
    )
    db.add(ev); db.commit()

    if error:
        raise HTTPException(500, error)
    return {"ok": True}
