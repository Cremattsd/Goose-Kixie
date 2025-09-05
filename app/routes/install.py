import os, secrets
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..services.db import get_db
from ..models.tenant import Tenant
from ..services.crypto import encrypt
from ..services.kixie_api import create_or_update_webhook

router = APIRouter()

class InstallBody(BaseModel):
    # Optional: fall back to .env if omitted
    name: str | None = None
    kixie_api_key: str | None = None
    kixie_business_id: str | None = None
    realnex_jwt: str | None = None

def _resolve_defaults(body: InstallBody):
    name  = body.name or os.getenv("DEFAULT_TENANT_NAME", "Dev Tenant")
    apikey = body.kixie_api_key or os.getenv("KIXIE_API_KEY")
    bizid  = body.kixie_business_id or os.getenv("KIXIE_BUSINESS_ID")
    rn_jwt = body.realnex_jwt or os.getenv("REALNEX_JWT")
    missing = [k for k, v in {
        "KIXIE_API_KEY": apikey,
        "KIXIE_BUSINESS_ID": bizid,
        "REALNEX_JWT": rn_jwt
    }.items() if not v]
    if missing:
        raise HTTPException(
            400,
            f"Missing creds: {', '.join(missing)}. Provide in JSON body or set them in .env"
        )
    return name, apikey, bizid, rn_jwt

@router.post("", summary="Install tenant and register Kixie webhooks (uses .env defaults)")
async def install(body: InstallBody, db: Session = Depends(get_db)):
    name, apikey, bizid, rn_jwt = _resolve_defaults(body)

    secret = secrets.token_hex(16)
    tenant = Tenant(
        name=name,
        kixie_business_id=bizid,
        kixie_api_key_enc=encrypt(apikey),
        rn_jwt_enc=encrypt(rn_jwt),
        webhook_secret=secret,
        active=True
    )
    db.add(tenant); db.commit(); db.refresh(tenant)

    base_url = (os.getenv("BASE_URL", "").rstrip("/"))
    location = f"{base_url}/kixie/webhooks" if base_url else "/kixie/webhooks"
    headers  = f"[{{\\\"name\\\":\\\"X-Goose-Secret\\\",\\\"value\\\":\\\"{secret}\\\"}}]"

    webhook_errors: list[str] = []
    for event, wname in [
        ("endcall", "goose-endcall"),
        ("disposition", "goose-disposition"),
        ("sms", "goose-sms"),
    ]:
        payload = {
            "call": "postWebhook",
            "eventname": event,
            "direction": "all",
            "callresult": "all",
            "disposition": "all",
            "runtime": "realtime",
            "name": wname,
            "location": location,
            "headers": headers
        }
        try:
            await create_or_update_webhook(apikey, bizid, payload)
        except Exception as e:
            webhook_errors.append(f"{event}: {e}")

    return {
        "tenant_id": tenant.id,
        "webhook_secret": secret,
        "webhook_location": location,
        "ok": len(webhook_errors) == 0,
        "webhook_errors": webhook_errors
    }

@router.get("/tenants", summary="List installed tenants")
def list_tenants(db: Session = Depends(get_db)):
    rows = db.query(Tenant).order_by(Tenant.id.desc()).all()
    return [
        {
            "id": t.id,
            "name": t.name,
            "businessid": t.kixie_business_id,
            "webhook_secret": t.webhook_secret,
            "active": t.active,
        }
        for t in rows
    ]
