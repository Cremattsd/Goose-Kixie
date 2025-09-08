from dotenv import load_dotenv; load_dotenv()

from fastapi import FastAPI
from .routes.dialer import router as dialer_router

app = FastAPI(title="Goose-Kixie (RealNex wired)")

@app.get("/")
def root():
    return {
        "ok": True,
        "routes": [
            "/health",
            "/webhooks/kixie",
            "/activities/call",
            "/contacts/search",
            "/contacts",
            "/kixie/lists/push",
        ],
    }

@app.get("/health")
def health():
    return {"ok": True}

app.include_router(dialer_router, tags=["dialer"])
