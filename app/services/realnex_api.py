import os
import httpx

BASE = os.getenv("REALNEX_API_BASE", "https://sync.realnex.com/api/v1/Crm").rstrip("/")

def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

async def _get_json(url: str, token: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, headers=_headers(token), params=params)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} {r.reason_phrase}: {r.text}")
        return r.json()

async def _post_json(url: str, token: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, headers=_headers(token), json=payload)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} {r.reason_phrase}: {r.text}")
        return r.json()

# --------- Searches ---------

async def get_contacts(token: str, params: dict) -> dict:
    # preferred endpoint
    try:
        return await _get_json(f"{BASE}/Contact", token, params)
    except Exception:
        # fallback (some tenants accept lowercase)
        return await _get_json(f"{BASE}/contact", token, params)

async def search_any(token: str, query: str) -> dict:
    try:
        return await _get_json(f"{BASE}/Search/Any", token, {"q": query})
    except Exception:
        return await _get_json(f"{BASE}/search/any", token, {"q": query})

# --------- Creates ---------

async def create_contact(token: str, contact: dict) -> dict:
    try:
        return await _post_json(f"{BASE}/Contact", token, contact)
    except Exception:
        return await _post_json(f"{BASE}/contact", token, contact)

async def create_contact_by_number(token: str, number_e164: str) -> dict:
    """
    Minimal, tenant-compatible create:
      A) first/last + mobile
      B) if 400, retry with phones[] array
    """
    attempt_a = {
        "firstName": "Kixie",
        "lastName": "Lead",
        "mobile": number_e164,
        "source": "kixie"
    }
    try:
        return await create_contact(token, attempt_a)
    except Exception as e_a:
        attempt_b = {
            "firstName": "Kixie",
            "lastName": "Lead",
            "source": "kixie",
            "phones": [
                {"type": "Mobile", "phoneNumber": number_e164}
            ]
        }
        try:
            return await create_contact(token, attempt_b)
        except Exception:
            # bubble the more informative first error
            raise e_a

async def create_history(token: str, history: dict) -> dict:
    try:
        return await _post_json(f"{BASE}/History", token, history)
    except Exception:
        return await _post_json(f"{BASE}/history", token, history)
