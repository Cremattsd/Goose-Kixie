import os, httpx, re, asyncio
from typing import Any, Dict, Optional, List
from urllib.parse import urlparse, unquote

# ---------- Auth / Bases ----------

def get_rn_token() -> str:
    return os.getenv("REALNEX_JWT") or os.getenv("REALNEX_TOKEN") or ""

def _bases_from_env() -> List[str]:
    """
    REALNEX_API_BASE can be a single base or comma-separated bases.
    If it ends with /Crm, we auto-add common siblings so the app can
    adapt to different tenant shapes.
    """
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

def _odata_bases() -> List[str]:
    """Return only bases that look like OData collections."""
    return [b for b in BASES if b.lower().endswith("/crmoodata") or b.lower().endswith("/crmodata")]

# ---------- HTTP helpers ----------

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

def _join_base_path(base: str, path: str) -> str:
    """
    Safe join so we never double the segment (e.g., CrmOData/CrmOData/...).
    """
    base = base.rstrip("/")
    path = (path or "").lstrip("/")
    if not path:
        return base
    btail = base.rsplit("/", 1)[-1].lower()
    phead = path.split("/", 1)[0].lower()
    if btail == phead:
        path = path.split("/", 1)[1] if "/" in path else ""
    return f"{base}/{path}" if path else base

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
    """
    Legacy behavior: return first response that is NOT 404.
    Use this when probing variable tenant shapes, BUT NOT for validation-sensitive lookups.
    """
    async with _client() as client:
        last = None
        for base in BASES:
            for path in paths:
                url = _join_base_path(base, path)
                try:
                    r = await _send(client, method, url, token, **kw)
                    if r.status_code != 404:
                        return await _format_resp(r)
                    last = r
                except httpx.HTTPError as e:
                    return {"status": 599, "error": str(e), "attempted": url}
        return await _format_resp(last) if last else {"status": 404, "error": "Not Found"}

async def _try_paths_status_ok(method: str, paths: List[str], token: str, **kw) -> Dict[str,Any]:
    """
    Strict success: return first **2xx** response. This avoids treating 400s as 'hits'.
    Use for endpoints like Contact/search where some tenants 400 on bad shapes.
    """
    async with _client() as client:
        last = None
        for base in BASES:
            for path in paths:
                url = _join_base_path(base, path)
                try:
                    r = await _send(client, method, url, token, **kw)
                    last = r
                    if 200 <= r.status_code < 300:
                        return await _format_resp(r)
                except httpx.HTTPError as e:
                    return {"status": 599, "error": str(e), "attempted": url}
        return await _format_resp(last) if last else {"status": 404, "error": "Not Found"}

async def _try_paths_over_bases_status_ok(method: str, paths: List[str], bases: List[str], token: str, **kw) -> Dict[str,Any]:
    """Same strict 2xx rule, but only over the given bases (used for OData)."""
    async with _client() as client:
        last = None
        for base in bases:
            for path in paths:
                url = _join_base_path(base, path)
                try:
                    r = await _send(client, method, url, token, **kw)
                    last = r
                    if 200 <= r.status_code < 300:
                        return await _format_resp(r)
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
    """
    Strict 2xx success so a tenant that returns 400 on /Contact/search won't short-circuit.
    """
    return await _try_paths_status_ok(
        "GET",
        ["Contacts/search","contacts/search","Contact/search","contact/search"],
        token,
        params={"phone": phone_e164},
    )

_PHONE_FIELDS = [
    "PrimaryPhone","MobilePhone","BusinessPhone","HomePhone",
    "WorkPhone","CellPhone","Phone","Phone1","Phone2","Phone3",
    "OtherPhone","AssistantPhone","Fax"
]

async def search_contact_by_phone_wide(token: str, phone_raw: str) -> Dict[str, Any]:
    """
    OData fallback: search Contacts across common phone fields with contains(digits).
    Only runs against OData bases to avoid malformed joins.
    """
    d = digits_only(phone_raw)
    if not d:
        return {"status": 400, "error": "no_digits"}
    flt = " or ".join([f"contains({f},'{d}')" for f in _PHONE_FIELDS])
    params = {"$filter": flt, "$top": "1"}
    odata_bases = _odata_bases()
    if not odata_bases:
        return {"status": 404, "error": "no_odata_bases", "bases": BASES}
    # Path is just 'Contacts'; _join_base_path will do the right thing for CrmOData/crmodata.
    return await _try_paths_over_bases_status_ok("GET", ["Contacts"], odata_bases, token, params=params)

async def create_contact(token: str, payload: Dict[str, Any]) -> Dict[str,Any]:
    return await _try_paths("POST",
        ["contact","Contact","contacts","Contacts"],
        token, json=payload)

async def create_history(token: str, payload: Dict[str, Any]) -> Dict[str,Any]:
    return await _try_paths("POST",
        ["history","History","histories","Histories"],
        token, json=payload)

# Read a contact (basic & full)
async def get_contact(token: str, contact_key: str) -> Dict[str, Any]:
    k = contact_key
    paths = [
        f"contact/{k}", f"Contact/{k}", f"contacts/{k}", f"Contacts/{k}",
        f"CRM/contact/{k}", f"CRM/Contact/{k}",
    ]
    return await _try_paths("GET", paths, token)

async def get_contact_full(token: str, contact_key: str) -> Dict[str, Any]:
    k = contact_key
    paths = [
        f"contact/{k}/full", f"Contact/{k}/full", f"contacts/{k}/full", f"Contacts/{k}/full",
        f"CRM/contact/{k}/full", f"CRM/Contact/{k}/full",
    ]
    return await _try_paths("GET", paths, token)

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
    return tz in tzs if tzs else True  # permissive if API doesn't enumerate

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
        "timezones","Timezones","attachment/test-key",
        "CrmOData/Contacts","crmodata/Contacts"
    ]
    out = {"bases": BASES, "checks": []}
    async with _client() as client:
        for base in BASES:
            for path in shapes:
                url = _join_base_path(base, path)
                try:
                    r = await _send(client, "OPTIONS", url, token)
                    out["checks"].append({"url": url, "status": r.status_code})
                except Exception as e:
                    out["checks"].append({"url": url, "error": str(e)})
    return out
