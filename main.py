from fastapi import FastAPI
from .services.db import init_db
from .routes.install import router as install_router
from .routes.kixie import router as kixie_router

app = FastAPI(title="Goose-Kixie")

@app.on_event("startup")
def startup():
    init_db()

@app.get("/health")
def health():
    return {"ok": True}

app.include_router(install_router, prefix="/install", tags=["install"])
app.include_router(kixie_router,   prefix="/kixie",   tags=["kixie"])
