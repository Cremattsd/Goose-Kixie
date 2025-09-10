# app/main.py
from dotenv import load_dotenv; load_dotenv()

import os
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.routing import APIRoute

# Routers
from .routes.dialer import router as dialer_router
try:
    from .routes.debug_realnex import router as debug_router
except Exception:  # optional debug router
    from fastapi import APIRouter
    debug_router = APIRouter()

# -----------------------------------------------------------------------------
# App setup
# -----------------------------------------------------------------------------
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

# CORS (optional, helpful for local tools / Swagger in browsers)
allow_origins = [o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Trusted hosts (optional, tighten in prod)
trusted_hosts = [h.strip() for h in os.getenv("TRUSTED_HOSTS", "*").split(",")]
app.add_middleware(TrustedHostMiddleware, allowed_ho
