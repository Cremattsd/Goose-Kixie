import os, secrets
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..services.db import get_db
from ..models.tenant import Tenant
from ..services.crypto import encrypt
from ..services.kixie_api import create_or_update_webhook, list_webhooks

router = APIRouter()

class InstallBody(BaseModel):
    name: str | None = None
    kixie_api_key: str
    kixie_business_id: str
    realnex_jwt: str

@router.post("")
async def install(body: InstallBody, db: Session = Depends(get_db)):
    secret = secrets.token_hex(16)

    tenant = Tenant(
        name=body.name,
        kixie_business_id=body.kixie_business_id,
        kixie_api_key_enc=encrypt(body.kixie_api_key),
        rn_jwt_enc=encrypt(body.realnex_jwt),
        webhook_secret=secret,
        active=True
    )
    db.add(tenant); db.commit(); db.refresh(tenant)

    base_url = os.getenv("BASE_URL", "").rstrip("/")
    if not base_url:
        # You can still install, but Kixie won't be able to hit you until you set BASE_URL and re-install.
        pass
    location = f"{base_url}/kixie/webhooks" if base_url else "/kixie/webhooks"
    headers = f"[{{\\\"name\\\":\\\"X-Goose-Secret\\\",\\\"value\\\":\\\"{secret}\\\"}}]"

    # Create minimal set of webhooks (you can add startcall/answeredcall later)
    for event, name in [("endcall","goose-endcall"),("disposition","goose-disposition"),("sms","goose-sms")]:
        payload = {
            "call": "postWebhook",
            "eventname": event,
            "direction": "all",
            "callresult": "all",
            "disposition": "all",
            "runtime": "realtime",
            "name": name,
            "location": location,
            "headers": headers
        }
        try:
            await create_or_update_webhook(body.kixie_api_key, body.kixie_business_id, payload)
        except Exception as e:
            # Leave tenant installed even if webhook creation fails; you can retry from an admin tool later
            raise HTTPException(502, f"Kixie webhook create failed: {e}")

    return {"tenant_id": tenant.id, "webhook_secret": secret, "ok": True}
