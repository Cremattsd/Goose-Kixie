mkdir -p app/routes app/services app/schemas
cat > app/main.py <<'PY'
from dotenv import load_dotenv; load_dotenv()

from fastapi import FastAPI

from .routes.dialer import router as dialer_router
from .routes.debug_realnex import router as debug_router

app = FastAPI(title="Goose-Kixie (RealNex)")

@app.get("/")
def root():
    return {
        "ok": True,
        "routes": [
            "/health",
            "/health/realnex",
            "/debug/realnex/probe",
            "/contacts",
            "/contacts/search",
            "/activities/call",
            "/webhooks/kixie",
        ],
    }

@app.get("/health")
def health():
    return {"ok": True}

app.include_router(dialer_router, tags=["dialer"])
app.include_router(debug_router, tags=["debug"])
PY
