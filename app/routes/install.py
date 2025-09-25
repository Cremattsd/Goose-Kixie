from __future__ import annotations

import os, secrets, json
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..services.db import get_db
from ..models.tenant import Tenant
from ..services.crypto import encrypt, decrypt
from ..services.kixie_api import create_or_update_webhook, list_webhooks, delete_webhook

router = APIRouter()

class InstallBody(BaseModel):
    kixie_api_key: str | None = None
    kixie_business_id: str | None = None
    realnex_jwt: str | None = None

class ReinstallBody(BaseModel):
    tenant_id: Optional[int] = Field(None, description="Defaults to most-recent tenant")
    events: List[str] = Field(default_factory=lambda: ["endcall", "disposition", "SMS"])
    webhook_url: Optional[str] = None
    names: Dict[str, str] = Field(default_factory=dict)

class RemoveBody(BaseModel):
    tenant_id: Optional[int] = None
    names: List[str] = Field(default_factory=lambda: ["goose-endcall", "goose-disposition", "goose-sms"])

def _resolve_defaults(body: InstallBody):
    apikey = body.kixie_api_key or os.getenv("KIXIE_API_KEY")
    bizid  = body.kixie_business_id or os.getenv("KIXIE_BUSINESS_ID")
    rn_jwt = body.realnex_jwt or os.getenv("REALNEX_JWT")
    missing = [k for k, v in {
        "KIXIE_API_KEY": apikey,
        "KIXIE_BUSINESS_ID": bizid,
        "REALNEX_JWT": rn_jwt
    }.items() if not v]
    if missing:
        raise HTTPException(400, f"Missing creds: {', '.join(missing)}. Provide in JSON body or set them in .env")
    return apikey, bizid, rn_jwt

def _tenant_or_latest(db: Session, tenant_id: Optional[int]) -> Tenant:
    q = db.query(Tenant)
    if tenant_id is not None:
        t = q.filter(Tenant.id == tenant_id).first()
        if not t: raise HTTPException(404, f"Tenant {tenant_id} not found")
        return t
    t = q.order_by(Tenant.id.desc()).first()
    if not t: raise HTTPException(404, "No tenant installed")
    return t

def _kixie_creds_for(t: Tenant) -> tuple[str, str]:
    return decrypt(t.kixie_api_key_enc), (t.kixie_business_id or "")

def _default_webhook_url() -> str:
    base_url = (os.getenv("BASE_URL", "").rstrip("/"))
    return f"{base_url}/webhooks/kixie" if base_url else "/webhooks/kixie"

def _payload_for(event: str, name: str, url: str, secret: str) -> Dict[str, Any]:
    headers = json.dumps([{"name": "X-Goose-Secret", "value": secret}])
    evt = "SMS" if event.lower() == "sms" else event
    return {
        "call": "postWebhook",
        "eventname": evt,
        "direction": "all",
        "callresult": "all",
        "disposition": "all",
        "runtime": "realtime",
        "name": name,
        "location": url,
        "headers": headers,
    }

@router.get("/health", summary="Installer health & defaults")
def install_health():
    return {
        "BASE_URL": os.getenv("BASE_URL"),
        "has_KIXIE_API_KEY": bool(os.getenv("KIXIE_API_KEY")),
        "has_KIXIE_BUSINESS_ID": bool(os.getenv("KIXIE_BUSINESS_ID")),
        "has_REALNEX_JWT": bool(os.getenv("REALNEX_JWT")),
        "webhook_target_default": _default_webhook_url(),
        "kixie_apig_base": os.getenv("KIXIE_APIG_BASE_URL", "https://apig.kixie.com/app/v1/api"),
    }

@router.post("", summary="Install tenant and register Kixie webhooks (uses .env defaults)")
async def install(body: InstallBody, db: Session = Depends(get_db)):
    apikey, bizid, rn_jwt = _resolve_defaults(body)

    secret = secrets.token_hex(16)
    tenant = Tenant(
        webhook_secret=secret,
        base_url=os.getenv("BASE_URL"),
        kixie_business_id=bizid,
        kixie_api_key_enc=encrypt(apikey),
        rn_jwt_enc=encrypt(rn_jwt),
        active=True
    )
    db.add(tenant); db.commit(); db.refresh(tenant)

    location = _default_webhook_url()

    webhook_errors: list[str] = []
    for event, wname in [("endcall", "goose-endcall"), ("disposition", "goose-disposition"), ("SMS", "goose-sms")]:
        try:
            resp = await create_or_update_webhook(apikey, bizid, _payload_for(event, wname, location, secret))
            status = int(resp.get("status", 200)) if isinstance(resp, dict) else 200
            if status >= 300:
                webhook_errors.append(f"{event}: status {status} {resp}")
        except Exception as e:
            webhook_errors.append(f"{event}: {e}")

    return {
        "tenant_id": tenant.id,
        "webhook_secret": secret,
        "webhook_location": location,
        "ok": len(webhook_errors) == 0,
        "webhook_errors": webhook_errors
    }

@router.post("/webhooks", summary="(Re)install Kixie webhooks for an existing tenant")
async def reinstall_webhooks(body: ReinstallBody, db: Session = Depends(get_db)):
    t = _tenant_or_latest(db, body.tenant_id)
    apikey, bizid = _kixie_creds_for(t)
    location = body.webhook_url or _default_webhook_url()

    results = []
    for evt in body.events:
        evt_norm = "SMS" if evt.lower() == "sms" else evt
        name = body.names.get(evt) if body.names else None
        name = name or f"goose-{evt_norm.lower()}"
        try:
            resp = await create_or_update_webhook(apikey, bizid, _payload_for(evt_norm, name, location, t.webhook_secret))
            status = int(resp.get("status", 200)) if isinstance(resp, dict) else 200
            results.append({"event": evt_norm, "name": name, "status": status, "resp": resp})
        except Exception as e:
            results.append({"event": evt_norm, "name": name, "error": str(e)})

    ok = all((r.get("status", 500) < 300) for r in results if "status" in r)
    return {"tenant_id": t.id, "location": location, "ok": ok, "results": results}

@router.post("/webhooks/remove", summary="Remove Goose webhooks by name for an existing tenant")
async def remove_webhooks(body: RemoveBody, db: Session = Depends(get_db)):
    t = _tenant_or_latest(db, body.tenant_id)
    apikey, bizid = _kixie_creds_for(t)

    listing = await list_webhooks(apikey, bizid)
    items = (listing.get("response") or {})
    arr: list = []
    if isinstance(items, list):
        arr = items
    elif isinstance(items, dict):
        for k in ("data", "webhooks", "items", "value"):
            v = items.get(k)
            if isinstance(v, list):
                arr = v; break

    removed, errors = [], []
    name_set = set(body.names)

    for it in arr:
        wid = it.get("webhookid") or it.get("id")
        nm  = it.get("name") or it.get("webhookname") or ""
        if nm in name_set and wid:
            try:
                resp = await delete_webhook(apikey, bizid, str(wid))
                status = int(resp.get("status", 200)) if isinstance(resp, dict) else 200
                removed.append({"name": nm, "id": wid, "status": status})
            except Exception as e:
                errors.append({"name": nm, "id": wid, "error": str(e)})

    found_names = {r["name"] for r in removed}
    not_found = [n for n in name_set - found_names]

    ok = (len(errors) == 0)
    return {"tenant_id": t.id, "removed": removed, "not_found": not_found, "errors": errors, "ok": ok}

@router.get("/tenants", summary="List installed tenants")
def list_tenants(db: Session = Depends(get_db)):
    rows = db.query(Tenant).order_by(Tenant.id.desc()).all()
    return [{"id": t.id, "businessid": t.kixie_business_id, "base_url": t.base_url, "webhook_secret": t.webhook_secret} for t in rows]
