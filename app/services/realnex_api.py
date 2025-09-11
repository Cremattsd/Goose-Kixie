# app/services/realnex_api.py
import os, httpx, re, asyncio
from typing import Any, Dict, Optional, List, Tuple
from urllib.parse import urlparse, unquote

def get_rn_token() -> str:
    return os.getenv("REALNEX_JWT") or os.getenv("REALNEX_TOKEN") or ""

def _bases_from_env() -> List[str]:
    raw = os.getenv("REALNEX_API_BASE", "https://sync.realnex.com/api/v1/Crm")
    parts = [p.strip().rstrip("/") for p in raw.split(",") if p.strip()]
    if len(parts) == 1:
        b = parts[0]
        variants = [b]
        if b.lower().endswith("/crm"):
            root = b[:-4]
            variants += [root + "/CrmOData", root + "/CRM", root + "/crmodata"]
        parts = variants
    return parts

BASES = _bases_from_env()

def _headers(token: str) -> Dict[str,str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=True)

async def _format_resp(resp: httpx.Response) -> Dict[str, Any]:
    try:
        data = resp.json() if resp.content else {}
    except Exception:
        data = {"raw": (await resp.aread()).decode("utf-8","ignore")}
    if isinstance(data, dict):
        data.setdefault("status", resp.status_code)
        data.setdefault("url", str(resp.request.url))
        data.setdefault("method", resp.request.method)
    return data

async def _send(client: httpx.AsyncClient, method: str, url: str, token: str, **kw) -> httpx.Response:
    base_headers = _headers(token)
    extra_headers = kw.pop("headers", None)
    if "json" in kw and extra_headers is None:
        base_headers["Content-Type"] = "application/json"
    if extra_headers:
        base_headers.update({k: v for k, v in extra_headers.items() if v is not None})
    req = client.build_request(method, url, headers=base_headers, **kw)
    return await client.send(req)

async def _try_paths(method: str, paths: List[str], token: str, **kw) -> Dict[str,Any]:
    async with _client() as client:
        last = None
        for base in BASES:
            for path in paths:
                url = f"{base}/{path}"
                try:
                    r = await _send(client, method, url, token, **kw)
                    if r.status_code != 404:
                        return await _format_resp(r)
                    last = r
                except httpx.HTTPError as e:
                    return {"status": 599, "error": str(e), "attempted": url}
        return await _format_resp(last) if last else {"status": 404, "error": "Not Found"}

# ---------- Phone utils ----------

def normalize_phone_e164ish(raw: Optional[str]) -> Optional[str]:
    if not raw: return None
    digits = re.sub(r"\D+", "", raw)
    if not digits: return None
    if len(digits) == 10: return f"+1{digits}"
    if digits.startswith("1") and len(digits)==11: return f"+{digits}"
    if raw.startswith("+"): return f"+{digits}"
    return f"+{digits}"

def digits_only(raw: Optional[str]) -> Optional[str]:
    if not raw: return None
    d = re.sub(r"\D+", "", raw)
    return d or None

# ---------- Contacts & History ----------

async def search_by_phone(token: str, phone_e164: str) -> Dict[str,Any]:
    return await _try_paths("GET",
        ["Contacts/search","Contact/search","contacts/search","contact/search"],
        token, params={"phone": phone_e164})

# NEW: wide search via OData contains() across common phone fields
_ODATA_CONTACT_PATHS = [
    "CrmOData/Contacts","crmodata/Contacts","OData/Contacts","odata/Contacts"
]
_PHONE_FIELDS = [
    "PrimaryPhone","MobilePhone","BusinessPhone","HomePhone",
    "WorkPhone","CellPhone","Phone","Phone1","Phone2","Phone3",
    "OtherPhone","AssistantPhone","Fax"
]

async def search_contact_by_phone_wide(token: str, phone_raw: str) -> Dict[str, Any]:
    """
    Fallback: if Contacts/search misses, probe OData Contacts with contains()
    on many phone fields using digits-only token (robust to formatting).
    Returns the raw response of the first non-404 hit; may include .value list.
    """
    d = digits_only(phone_raw)
    if not d:
        return {"status": 400, "error": "no_digits"}
    # Build $filter like: contains(PrimaryPhone,'8584581063') or contains(MobilePhone,'8584581063') ...
    flt = " or ".join([f"contains({f},'{d}')" for f in _PHONE_FIELDS])
    params = {"$filter": flt, "$top": "1"}
    return await _try_paths("GET", _ODATA_CONTACT_PATHS, token, params=params)

async def create_contact(token: str, payload: Dict[str, Any]) -> Dict[str,Any]:
    return await _try_paths("POST",
        ["contact","Contact","contacts","Contacts"],
        token, json=payload)

async def create_history(token: str, payload: Dict[str, Any]) -> Dict[str,Any]:
    return await _try_paths("POST",
        ["history","History","histories","Histories"],
        token, json=payload)

# ---------- Timezones ----------

async def list_timezones(token: str) -> Dict[str, Any]:
    return await _try_paths("GET",
        ["timezones","Timezones","CRM/timezones","CRM/Timezones"],
        token)

async def is_valid_timezone(token: str, tz: Optional[str]) -> bool:
    if not tz: return False
    data = await list_timezones(token)
    vals = data.get("data") or data.get("value") or data
    try:
        tzs = { (x.get("Key") or x.get("Id") or x.get("name") or x.get("Name") or x).strip()
               for x in (vals if isinstance(vals, list) else []) }
    except Exception:
        tzs = set()
    return tz in tzs if tzs else True

# ---------- Attachments ----------

def _basename_from_url(u: str) -> str:
    try:
        path = urlparse(u).path
        name = unquote(path.split("/")[-1]) if path else ""
        return name or "recording"
    except Exception:
        return "recording"

def _guess_content_type(name: str, fallback: Optional[str]) -> str:
    ext = (name.rsplit(".", 1)[-1].lower() if "." in name else "")
    if ext == "mp3": return "audio/mpeg"
    if ext in ("m4a","mp4"): return "audio/mp4"
    if ext == "wav": return "audio/wav"
    if ext == "ogg": return "audio/ogg"
    if fallback:
        return fallback.split(";")[0].strip()
    return "application/octet-stream"

async def fetch_url_bytes(url: str) -> Dict[str, Any]:
    async with _client() as client:
        r = await client.get(url)
        r.raise_for_status()
        content = await r.aread()
        ct = r.headers.get("content-type", "")
        name = _basename_from_url(url)
        if "." not in name:
            if "mpeg" in ct: name += ".mp3"
            elif "wav" in ct: name += ".wav"
        return {"bytes": content, "content_type": _guess_content_type(name, ct), "filename": name, "status": r.status_code}

async def upload_attachment(token: str, object_key: str, filename: str, content_type: str, data: bytes) -> Dict[str, Any]:
    files = {"file": (filename, data, content_type)}
    return await _try_paths("POST",
        [f"attachment/{object_key}", f"Attachment/{object_key}", f"attachments/{object_key}", f"Attachments/{object_key}"],
        token, files=files)

async def attach_recording_from_url(token: str, object_key: str, recording_url: str) -> Dict[str, Any]:
    try:
        fetched = await fetch_url_bytes(recording_url)
        if fetched.get("status", 0) >= 400:
            return {"status": fetched.get("status", 500), "error": "fetch_failed", "fetch": fetched}
        return await upload_attachment(token, object_key, fetched["filename"], fetched["content_type"], fetched["bytes"])
    except Exception as e:
        return {"status": 500, "error": str(e)}

# ---------- Probe ----------

async def probe_endpoints(token: str) -> Dict[str, Any]:
    shapes = [
        "contact","history","users","teams",
        "Contact","History","Users","Teams",
        "CrmOData/Users","CrmOData/Teams","crmodata/Users","crmodata/Teams",
        "OData/Users","OData/Teams","CRM/Users","CRM/Teams",
        "timezones","Timezones","attachment/test-key","CrmOData/Contacts"
    ]
    out = {"bases": BASES, "checks": []}
    async with _client() as client:
        for base in BASES:
            for path in shapes:
                url = f"{base}/{path}"
                try:
                    r = await _send(client, "OPTIONS", url, token)
                    out["checks"].append({"url": url, "status": r.status_code})
                except Exception as e:
                    out["checks"].append({"url": url, "error": str(e)})
    return out
