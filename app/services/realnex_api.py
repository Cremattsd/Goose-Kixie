# app/services/realnex_api.py
import os, httpx, re, asyncio
from typing import Any, Dict, Optional, List, Tuple
from urllib.parse import urlparse, unquote
from datetime import datetime

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
    # No default Content-Type; set in _send based on payload type.
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

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
    # Merge headers and set proper Content-Type when using json
    base_headers = _headers(token)
    extra_headers = kw.pop("headers", None)
    if "json" in kw and extra_headers is None:
        base_headers["Content-Type"] = "application/json"
    if extra_headers:
        base_headers.update({k: v for k, v in extra_headers.items() if v is not None})
        # if caller explicitly passes Content-Type, we honor it
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

# ---------- Utilities ----------

def normalize_phone_e164ish(raw: Optional[str]) -> Optional[str]:
    if not raw: return None
    digits = re.sub(r"\D+", "", raw)
    if not digits: return None
    if len(digits) == 10: return f"+1{digits}"
    if digits.startswith("1") and len(digits)==11: return f"+{digits}"
    if raw.startswith("+"): return f"+{digits}"
    return f"+{digits}"

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
        # strip params like "; charset=..."
        return fallback.split(";")[0].strip()
    return "application/octet-stream"

# ---------- Contacts & History ----------

async def search_by_phone(token: str, phone_e164: str) -> Dict[str,Any]:
    return await _try_paths("GET",
        ["Contacts/search","Contact/search","contacts/search","contact/search"],
        token, params={"phone": phone_e164})

async def create_contact(token: str, payload: Dict[str, Any]) -> Dict[str,Any]:
    return await _try_paths("POST",
        ["contact","Contact","contacts","Contacts"],
        token, json=payload)

async def create_history(token: str, payload: Dict[str, Any]) -> Dict[str,Any]:
    return await _try_paths("POST",
        ["history","History","histories","Histories"],
        token, json=payload)

# ---------- Timezones (list/validate) ----------

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
    return tz in tzs if tzs else True  # if API doesn’t list, assume ok

# ---------- Attachments ----------

async def fetch_url_bytes(url: str) -> Dict[str, Any]:
    async with _client() as client:
        r = await client.get(url)
        r.raise_for_status()
        content = await r.aread()
        ct = r.headers.get("content-type", "")
        name = _basename_from_url(url)
        if "." not in name:
            # add default ext when obvious
            if "mpeg" in ct: name += ".mp3"
            elif "wav" in ct: name += ".wav"
        return {"bytes": content, "content_type": _guess_content_type(name, ct), "filename": name, "status": r.status_code}

async def upload_attachment(token: str, object_key: str, filename: str, content_type: str, data: bytes) -> Dict[str, Any]:
    files = {"file": (filename, data, content_type)}
    # Do not set Content-Type header (httpx will set multipart)
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

# ---------- Resolve user/team by email (optional helpers) ----------

_USER_LIST_PATHS = ["users","Users","CRM/users","CRM/Users"]
_TEAM_LIST_PATHS = ["teams","Teams","CRM/teams","CRM/Teams"]

_ODATA_USER_COLLECTIONS = [
    "CrmOData/Users", "CrmOData/users", "crmodata/Users", "crmodata/users",
    "OData/Users", "odata/Users",
]
_ODATA_TEAM_COLLECTIONS = [
    "CrmOData/Teams", "CrmOData/teams", "crmodata/Teams", "crmodata/teams",
    "OData/Teams", "odata/Teams",
]

_USER_KEY_FIELDS = ["Key","Id","id","UserKey","userKey","UserId","userId"]
_TEAM_KEY_FIELDS = ["TeamKey","teamKey","Key","Id","id"]
_EMAIL_FIELDS = ["Email","email","UserEmail","Username","username","Login","login"]

_user_cache: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
_user_lock = asyncio.Lock()

def _cache_key(email: str) -> str:
    return email.lower().strip()

def _pluck_any(d: Dict[str,Any], names: List[str]) -> Optional[str]:
    for n in names:
        if n in d and d[n]:
            return str(d[n])
    return None

async def _first_ok(method: str, rel_paths: List[str], token: str):
    async with _client() as client:
        for base in BASES:
            for p in rel_paths:
                url = f"{base}/{p}"
                try:
                    r = await _send(client, method, url, token)
                    if r.status_code < 400:
                        try:
                            return (url, r.json())
                        except Exception:
                            return (url, None)
                except httpx.HTTPError:
                    continue
    return (None, None)

async def _odata_first(client: httpx.AsyncClient, base: str, path: str, token: str, params: Dict[str,str]):
    url = f"{base}/{path}"
    try:
        r = await _send(client, "GET", url, token, params=params)
        if r.status_code == 404:
            return None
        data = await _format_resp(r)
        if isinstance(data.get("value"), list) and data["value"]:
            return data["value"][0]
        d = {k:v for k,v in data.items() if k not in ("status","url","method")}
        return d or None
    except httpx.HTTPError:
        return None

def _odata_filter(email: str) -> Dict[str,str]:
    conds = [f"{f} eq '{email}'" for f in _EMAIL_FIELDS]
    return {"$filter": " or ".join(conds), "$top": "1"}

async def probe_endpoints(token: str) -> Dict[str, Any]:
    shapes = [
        "contact","history","users","teams",
        "Contact","History","Users","Teams",
        "CrmOData/Users","CrmOData/Teams","crmodata/Users","crmodata/Teams",
        "OData/Users","OData/Teams","CRM/Users","CRM/Teams",
        "timezones","Timezones","attachment/test-key"
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
