# app/main.py
from dotenv import load_dotenv; load_dotenv()

from fastapi import FastAPI
from .services.db import init_db
from .routes.install import router as install_router
from .routes.kixie import router as kixie_router
from .routes.dialer import router as dialer_router  # ensure this file exists

app = FastAPI(title="Goose-Kixie")

@app.on_event("startup")
def startup():
    init_db()

@app.get("/")
def root():
    return {
        "ok": True,
        "routes": [
            "/health",
            "/docs",
            "/install",
            "/install/tenants",
            "/kixie/webhooks",
            "/kixie/lookup",
            "/kixie/odata/sets",
            "/dialer/queue/sync",
            "/dialer/queue/bulk",
            "/dialer/next",
        ],
    }

@app.get("/health")
def health():
    return {"ok": True}

app.include_router(install_router, prefix="/install", tags=["install"])
app.include_router(kixie_router,   prefix="/kixie",   tags=["kixie"])
app.include_router(dialer_router,  prefix="/dialer",  tags=["dialer"])
