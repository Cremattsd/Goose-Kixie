from fastapi import APIRouter
from schemas.kixie import KixieWebhook

router = APIRouter(tags=["dialer"])

@router.post("/webhooks/kixie", summary="Kixie Webhook")
async def kixie_webhook(payload: KixieWebhook):
    print("Received webhook:", payload.dict())
    return {"status": "received", "data": payload.dict()}
