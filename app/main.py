# app/main.py
from dotenv import load_dotenv; load_dotenv()

import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from .routes.dialer import router as dialer_router
from .routes.debug_realnex import router as debug_router
from .routes.powerlist import router as powerlist_router

from .services.db import init_db

app = FastAPI(title="Goose-Kixie (RealNex)")

# ── DB init on startup (dev-friendly; disable with DB_CREATE_ALL=0) ───────────
if os.getenv("DB_CREATE_ALL", "1") not in ("0", "false", "False"):
    try:
        init_db()
    except Exception as e:
        print(f"[init_db] warning: {e}")

# ── Middleware: CORS & Trusted Hosts ──────────────────────────────────────────
origins = [o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

allowed_hosts = [h.strip() for h in os.getenv("TRUSTED_HOSTS", "*").split(",") if h.strip()]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

# ── Basic health/root ─────────────────────────────────────────────────────────
@app.get("/")
def root():
    # enumerate routes for quick smoke test
    routes = []
    for r in app.router.routes:
        try:
            routes.append({"path": r.path, "methods": list(r.methods), "name": r.name})
        except Exception:
            pass
    return {"ok": True, "routes": routes}

@app.get("/health")
def health():
    return {"ok": True}

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(dialer_router, tags=["dialer"])
app.include_router(debug_router, tags=["debug"])
app.include_router(powerlist_router, tags=["kixie"])
