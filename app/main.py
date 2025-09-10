# app/main.py
from dotenv import load_dotenv; load_dotenv()

import os
import logging
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse

# Routers
from .routes.dialer import router as dialer_router
try:
    from .routes.debug_realnex import router as debug_router
except Exception:
    from fastapi import APIRouter
    debug_router = APIRouter()

# ── App setup ────────────────────────────────────────────────────────────────
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("goose-kixie")

TAGS = [
    {"name": "dialer", "description": "Kixie webhook + contact/history utils"},
    {"name": "debug", "description": "RealNex endpoint probes & utilities"},
]

app = FastAPI(
    title="Goose-Kixie (RealNex)",
    version=os.getenv("APP_VERSION", "1.0.0"),
    openapi_tags=TAGS,
)

# CORS (dev-friendly; tighten in prod)
allow_origins = [o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Trusted hosts (tighten in prod)
trusted_hosts = [h.strip() for h in os.getenv("TRUSTED_HOSTS", "*").split(",")]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)

# ── Root + health ───────────────────────────────────────────────────────────
@app.get("/", tags=["debug"])
def root():
    routes = []
    for r in app.routes:
        if isinstance(r, APIRoute):
            methods = sorted(m for m in r.methods if m not in {"HEAD", "OPTIONS"})
            routes.append({"path": r.path, "methods": methods, "name": r.name})
    routes.sort(key=lambda x: x["path"])
    return {"ok": True, "routes": routes}

@app.get("/health", tags=["debug"])
def health():
    return {"ok": True}

# ── Global JSON error handler (prevents Codespaces 502 with empty body) ─────
@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "status": 500,
            "error": str(exc),
            "path": request.url.path,
        },
    )

# ── Wire routers ────────────────────────────────────────────────────────────
app.include_router(dialer_router, tags=["dialer"])
app.include_router(debug_router, tags=["debug"])

# ── Startup logs ────────────────────────────────────────────────────────────
@app.on_event("startup")
async def _startup_log():
    rn_token_present = bool(os.getenv("REALNEX_JWT") or os.getenv("REALNEX_TOKEN"))
    logger.info("Goose-Kixie starting…")
    logger.info("REALNEX token present: %s", rn_token_present)
    logger.info("REALNEX_API_BASE: %s", os.getenv("REALNEX_API_BASE", "https://sync.realnex.com/api/v1/Crm"))
