# app/routes/install.py
from __future__ import annotations

import os
import secrets
import json
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..services.db import get_db
from ..models.tenant import Tenant
from ..services.crypto import encrypt, decrypt
from ..services.kixie_api import create_or_update_webhook, list_webhooks, delete_webhook

router = APIRouter()

# ───────────────────────── Models ─────────────────────────

class InstallBody(BaseModel):
    # Optional: falls back to .env when omitted
    name: Optional[str] = None
    kixie_api_key: Optional[str] = None
    kixie_business_id: Optional[str] = None
    realnex_jwt: Optional[str] = None


class ReinstallBody(BaseModel):
    tenant_id: Optional[int] = Field(None, description="Defaults to most-recent tenant")
    events: List[str] = Field(default_factory=lambda: ["endcall", "disposition", "sms"])
    # Override the webhook URL if needed; otherwise BASE_URL + /kixie/webhooks
    webhook_url: Optional[str] = None
    # Optional custom names; defaults use goose-<event>
    names: Dict[str, str] = Field(default_factory=dict)


class RemoveBody(BaseModel):
    tenant_id: Optional[int] = None
    # Names to remove; default removes the three Goose webhooks if present
    names: List[str] = Field(default_factory=lambda: ["goose-endcall", "goose-disposition", "goose-sms"])


# ───────────────────────── Helpers ─────────────────────────

def _resolve_defaults(body: InstallBody) -> tuple[str, str, str, str]:
    name = body.name or os.getenv("DEFAULT_TENANT_NAME", "Dev Tenant")
    apikey = body.kixie_api_key or os.getenv("KIXIE_API_KEY")
    bizid = body.kixie_business_id or os.getenv("KIXIE_BUSINESS_ID")
    rn_jwt = body.realnex_jwt or os.getenv("REALNEX_JWT")

    missing = [k for k, v in {
        "KIXIE_API_KEY": apikey,
        "KIXIE_BUSINESS_ID": bizid,
        "REALNEX_JWT": rn_jwt,
    }.items() if not v]
    if missing:
        raise HTTPException(
            400,
            f"Missing creds: {', '.join(missing)}. Provide in JSON body or set them in .env",
        )
    return name, apikey, bizid, rn_jwt


def _tenant_or_latest(db: Session, tenant_id: Optional[int]) -> Tenant:
    if tenant_id is not None:
        t = db.get(Tenant, tenant_id)
        if not t:
            raise HTTPException(404, f"Tenant {tenant_id} not found")
        return t
    t = db.query(Tenant).order_by(Tenant.id.desc()).first()
    if not t:
        raise HTTPException(404, "No tenants installed")
    return t


def _kixie_creds_for(t: Tenant) -> tuple[str, str]:
    return decrypt(t.kixie_api_key_enc), (t.kixie_business_id or "")


def _default_webhook_url() -> str:
    base_url = (os.getenv("BASE_URL", "").rstrip("/"))
    return f"{base_url}/kixie/webhooks" if base_url else "/kixie/webhooks"


def _payload_for(event: str, name: str, url: str, secret: str) -> Dict[str, Any]:
    # Kixie expects "headers" as a JSON-stringified array
    headers = json.dumps([{"name": "X-Goose-Secret", "value": secret}])
    return {
        "call": "postWebhook",
        "eventname": event,          # e.g. endcall | disposition | sms
        "direction": "all",
        "callresult": "all",
        "disposition": "all",
        "runtime": "realtime",
        "name": name,
        "location": url,
        "headers": headers,
    }


# ───────────────────────── Endpoints ─────────────────────────
# NOTE: main.py mounts this router with prefix="/install"
# Paths here are therefore: /install/health, /install, /install/webhooks, etc.

@router.get("/health", summary="Installer health & defaults")
def install_health():
    return {
        "BASE_URL": os.getenv("BASE_URL"),
        "has_KIXIE_API_KEY": bool(os.getenv("KIXIE_API_KEY")),
        "has_KIXIE_BUSINESS_ID": bool(os.getenv("KIXIE_BUSINESS_ID")),
        "has_REALNEX_JWT": bool(os.getenv("REALNEX_JWT")),
        "webhook_target_default": _default_webhook_url(),
    }


@router.post("", summary="Install tenant and register Kixie webhooks (uses .env defaults)")
async def install(body: InstallBody, db: Session = Depends(get_db)):
    name, apikey, bizid, rn_jwt = _resolve_defaults(body)

    secret = secrets.token_hex(16)
    tenant = Tenant(
        webhook_secret=secret,
        base_url=os.getenv("BASE_URL"),
        kixie_business_id=bizid,
        kixie_api_key_enc=encrypt(apikey),
        realnex_jwt_enc=encrypt(rn_jwt),
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)

    location = _default_webhook_url()
    errors: list[str] = []
    for event, wname in [("endcall", "goose-endcall"),
                         ("disposition", "goose-disposition"),
                         ("sms", "goose-sms")]:
        try:
            await create_or_update_webhook(apikey, bizid, _payload_for(event, wname, location, secret))
        except Exception as e:
            errors.append(f"{event}: {e}")

    return {
        "tenant_id": tenant.id,
        "tenant_label": name,
        "webhook_secret": secret,
        "webhook_location": location,
        "ok": len(errors) == 0,
        "webhook_errors": errors,
    }


@router.post("/webhooks", summary="(Re)install Kixie webhooks for an existing tenant")
async def reinstall_webhooks(body: ReinstallBody, db: Session = Depends(get_db)):
    t = _tenant_or_latest(db, body.tenant_id)
    apikey, bizid = _kixie_creds_for(t)
    location = body.webhook_url or _default_webhook_url()

    results: list[dict] = []
    for evt in body.events:
        name = (body.names.get(evt) if body.names else None) or f"goose-{evt}"
        try:
            resp = await create_or_update_webhook(apikey, bizid, _payload_for(evt, name, location, t.webhook_secret))
            results.append({"event": evt, "name": name, "status": resp.get("status"), "resp": resp})
        except Exception as e:
            results.append({"event": evt, "name": name, "error": str(e)})

    ok = all((r.get("status", 500) < 300) for r in results if "status" in r)
    return {"tenant_id": t.id, "location": location, "ok": ok, "results": results}


@router.post("/webhooks/remove", summary="Remove Goose webhooks by name for an existing tenant")
async def remove_webhooks(body: RemoveBody, db: Session = Depends(get_db)):
    t = _tenant_or_latest(db, body.tenant_id)
    apikey, bizid = _kixie_creds_for(t)

    listing = await list_webhooks(apikey, bizid)
    items = (listing.get("data") or listing.get("webhooks") or listing.get("items") or [])
    removed: list[dict] = []
    errors: list[dict] = []
    name_set = set(body.names)

    for it in (items if isinstance(items, list) else []):
        wid = it.get("webhookid") or it.get("id")
        nm = it.get("name") or ""
        if nm in name_set and wid:
            try:
                resp = await delete_webhook(apikey, bizid, str(wid))
                removed.append({"name": nm, "id": wid, "status": resp.get("status")})
            except Exception as e:
                errors.append({"name": nm, "id": wid, "error": str(e)})

    found_names = {r["name"] for r in removed}
    misses = [n for n in name_set - found_names]

    ok = (len(errors) == 0)
    return {"tenant_id": t.id, "removed": removed, "not_found": misses, "errors": errors, "ok": ok}


@router.get("/tenants", summary="List installed tenants")
def list_tenants(db: Session = Depends(get_db)):
    rows = db.query(Tenant).order_by(Tenant.id.desc()).all()
    return [
        {
            "id": t.id,
            "businessid": t.kixie_business_id,
            "base_url": t.base_url,
            "webhook_secret": t.webhook_secret,
        }
        for t in rows
    ]
