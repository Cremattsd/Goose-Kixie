import os, re, base64, asyncio, httpx, json
from typing import Any, Dict, Optional, List, Tuple
from urllib.parse import urlparse, unquote

# ─────────────────── Token & Bases ───────────────────

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
            variants += [root + "/CrmOData", root + "/CRM", root + "/crmodata", root + "/odata"]
        parts = variants
    return parts

BASES = _bases_from_env()

def _headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=True)

async def _format_resp(resp: httpx.Response) -> Dict[str, Any]:
    try:
        data = resp.json() if resp.content else {}
    except Exception:
        data = {"raw": (await resp.aread()).decode("utf-8", "ignore")}
    # Always return a dict so callers can safely resp.get(...)
    if not isinstance(data, dict):
        data = {"data": data}
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

async def _try_paths(method: str, paths: List[str], token: str, **kw) -> Dict[str, Any]:
    async with _client() as client:
        last: Optional[httpx.Response] = None
        for base in BASES:
            for path in paths:
                url = f"{base.rstrip('/')}/{path.lstrip('/')}"
                try:
                    r = await _send(client, method, url, token, **kw)
                    # return first non-404 (allows 400 to bubble so caller can react)
                    if r.status_code != 404:
                        return await _format_resp(r)
                    last = r
                except httpx.HTTPError as e:
                    return {"status": 599, "error": str(e), "attempted": url}
        return await _format_resp(last) if last else {"status": 404, "error": "Not Found"}

# ─────────────────── Phone utils ───────────────────

def normalize_phone_e164ish(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    digits = re.sub(r"\D+", "", raw)
    if not digits:
        return None
    if len(digits) == 10:
        return f"+1{digits}"
    if digits.startswith("1") and len(digits) == 11:
        return f"+{digits}"
    if raw.startswith("+"):
        return f"+{digits}"
    return f"+{digits}"

def digits_only(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    d = re.sub(r"\D+", "", raw)
    return d or None

# ─────────────────── CRM: Contacts & History ───────────────────

async def search_by_phone(token: str, phone_e164: str) -> Dict[str, Any]:
    """Standard CRM endpoint; only return success on 2xx so caller can fallback to OData."""
    resp = await _try_paths(
        "GET",
        ["Contacts/search", "Contact/search", "contacts/search", "contact/search"],
        token,
        params={"phone": phone_e164},
    )
    if int(resp.get("status", 0)) // 100 == 2:
        return resp
    return {"status": resp.get("status", 400), "error": "crm_search_failed", "raw": resp}

async def create_contact(token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return await _try_paths("POST", ["contact", "Contact", "contacts", "Contacts"], token, json=payload)

async def create_history(token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return await _try_paths("POST", ["history", "History", "histories", "Histories"], token, json=payload)

async def get_contact(token: str, contact_key: str) -> Dict[str, Any]:
    k = contact_key
    paths = [f"contact/{k}", f"Contact/{k}", f"contacts/{k}", f"Contacts/{k}", f"CRM/contact/{k}", f"CRM/Contact/{k}"]
    return await _try_paths("GET", paths, token)

async def get_contact_full(token: str, contact_key: str) -> Dict[str, Any]:
    k = contact_key
    paths = [
        f"contact/{k}/full",
        f"Contact/{k}/full",
        f"contacts/{k}/full",
        f"Contacts/{k}/full",
        f"CRM/contact/{k}/full",
        f"CRM/Contact/{k}/full",
    ]
    return await _try_paths("GET", paths, token)

# ─────────────────── CRM: Definitions & Timezones ───────────────────

async def get_table_definition(token: str, table: str) -> Dict[str, Any]:
    return await _try_paths("GET", [f"definitions/{table}", f"CRM/definitions/{table}"], token)

async def list_timezones(token: str) -> Dict[str, Any]:
    return await _try_paths("GET", ["timezones", "Timezones", "CRM/timezones", "CRM/Timezones"], token)

async def is_valid_timezone(token: str, tz: Optional[str]) -> bool:
    if not tz:
        return False
    data = await list_timezones(token)
    vals = data.get("data") or data.get("value") or data
    try:
        tzs = {
            (x.get("Key") or x.get("Id") or x.get("name") or x.get("Name") or x).strip()
            for x in (vals if isinstance(vals, list) else [])
        }
    except Exception:
        tzs = set()
    return tz in tzs if tzs else True

# ─────────────────── OData: Contacts (probe fields, then filter) ───────────────────

# IMPORTANT: BASES already includes variants like /CrmOData, /crmodata, /odata.
# Keep collection relative to avoid /CrmOData/CrmOData/Contacts duplication.
_ODATA_CONTACT_COLLECTIONS = ["Contacts"]

# static list used as a last-ditch fallback for legacy callers
_STATIC_PHONE_FIELDS = [
    "PrimaryPhone","MobilePhone","BusinessPhone","HomePhone","WorkPhone",
    "CellPhone","Phone","Phone1","Phone2","Phone3","OtherPhone","AssistantPhone","Fax"
]

async def _odata_field_is_selectable(token: str, field: str) -> bool:
    """Try $select=field&$top=1 across bases; accept <400 as valid."""
    async with _client() as client:
        for base in BASES:
            for coll in _ODATA_CONTACT_COLLECTIONS:
                url = f"{base.rstrip('/')}/{coll}?$select={field}&$top=1"
                try:
                    r = await _send(client, "GET", url, token)
                    if r.status_code < 400:
                        return True
                    # 400 with "Could not find a property named" => invalid; keep trying next
                except httpx.HTTPError:
                    pass
    return False

def _like_phone_name(name: str) -> bool:
    n = name.lower()
    return any(kw in n for kw in ["phone","mobile","cell","workphone","homephone","assistant","fax","telephone"])

def _safe_field_name(x: Any) -> Optional[str]:
    if isinstance(x, dict):
        for k in ("Name","name","Field","field","ApiName","apiName"):
            v = x.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    if isinstance(x, str) and x.strip():
        return x.strip()
    return None

async def probe_odata_phone_fields(token: str) -> List[str]:
    """
    1) Pull CRM definitions for Contacts.
    2) Keep names that look phone-ish.
    3) Validate each name via OData $select.
    """
    defs = await get_table_definition(token, "Contacts")
    items = defs.get("data") or defs.get("fields") or defs.get("value") or defs
    candidates: List[str] = []
    if isinstance(items, list):
        for it in items:
            name = _safe_field_name(it)
            if name and _like_phone_name(name):
                candidates.append(name)
    validated: List[str] = []
    for f in candidates:
        if await _odata_field_is_selectable(token, f):
            validated.append(f)
    # opportunistic adds
    for f in ["PrimaryPhone", "MobilePhone"]:
        if f not in validated and await _odata_field_is_selectable(token, f):
            validated.append(f)
    return validated

async def odata_contacts_filter_by_digits(token: str, digits: str, fields: List[str], top: int = 5) -> Dict[str, Any]:
    if not fields:
        return {"status": 404, "error": "no_valid_phone_fields"}
    flt = " or ".join([f"contains({f},'{digits}')" for f in fields])
    params = {"$filter": flt, "$top": str(top)}
    return await _try_paths("GET", _ODATA_CONTACT_COLLECTIONS, token, params=params)

# ── Legacy wide search kept for compatibility (routes calling it won’t break)

async def search_contact_by_phone_wide(token: str, phone_raw: str) -> Dict[str, Any]:
    """Legacy: build contains() filter using probed fields; fallback to static if probe empty."""
    digits = digits_only(phone_raw) or ""
    if not digits:
        return {"status": 400, "error": "no_digits"}
    fields = await probe_odata_phone_fields(token)
    if not fields:
        fields = list(_STATIC_PHONE_FIELDS)
    return await odata_contacts_filter_by_digits(token, digits, fields, top=1)

# ── Two-stage: OData probe → candidate keys → verify with CRM → return confirmed key

async def search_contact_keys_by_phone_two_stage(token: str, phone_raw: str) -> Dict[str, Any]:
    digits = digits_only(phone_raw) or ""
    if not digits:
        return {"status": 400, "error": "no_digits"}
    fields = await probe_odata_phone_fields(token)
    wide = await odata_contacts_filter_by_digits(token, digits, fields, top=10)
    if int(wide.get("status", 0)) // 100 != 2:
        return {"status": 404, "error": "odata_no_match", "probe_fields": fields, "wide": wide}
    values = wide.get("value") or wide.get("data") or []
    if not isinstance(values, list) or not values:
        return {"status": 404, "error": "odata_empty", "probe_fields": fields}

    cand_keys: List[str] = []
    for v in values:
        if isinstance(v, dict):
            k = v.get("Key") or v.get("key") or v.get("Id") or v.get("id") or v.get("ContactKey") or v.get("contactKey")
            if k:
                cand_keys.append(str(k))

    def _has_digits(d: Dict[str, Any], digits: str) -> bool:
        for n, val in d.items():
            if "phone" in n.lower() or n.lower() in {"fax"}:
                s = str(val or "")
                if digits in re.sub(r"\D+", "", s):
                    return True
        return False

    for ck in cand_keys:
        detail = await get_contact(token, ck)
        if int(detail.get("status", 0)) // 100 == 2:
            body = detail.get("data") or detail
            if isinstance(body, dict):
                flat: Dict[str, Any] = {}
                for k, v in body.items():
                    if isinstance(v, (str, int)):
                        flat[k] = v
                    elif isinstance(v, dict):
                        for kk, vv in v.items():
                            if isinstance(vv, (str, int)):
                                flat[f"{k}.{kk}"] = vv
                if _has_digits(flat, digits):
                    return {
                        "status": 200,
                        "contactKey": ck,
                        "probe_fields": fields,
                        "confirmed_by": "CRM contact basic",
                    }

    return {"status": 404, "error": "no_candidate_verified", "probe_fields": fields, "candidates": cand_keys}

async def search_contact_key_by_phone_auto(token: str, phone_raw: str) -> Tuple[Optional[str], Dict[str, Any]]:
    """
    Convenience: try CRM /search, then two-stage OData, then legacy wide.
    Returns (contactKey or None, debug dict).
    """
    e164 = normalize_phone_e164ish(phone_raw) or phone_raw
    std = await search_by_phone(token, e164)
    # try to extract key
    for container in ("data","value","items"):
        items = std.get(container)
        if isinstance(items, list) and items:
            first = items[0] if isinstance(items[0], dict) else None
            if first:
                for k in ("Key","key","Id","id","ContactKey","contactKey"):
                    if k in first and first[k]:
                        return str(first[k]), {"stage": "crm_search", "resp": std}
    # two-stage
    ts = await search_contact_keys_by_phone_two_stage(token, e164)
    if int(ts.get("status", 0)) // 100 == 2 and ts.get("contactKey"):
        return str(ts["contactKey"]), {"stage": "odata_two_stage", "resp": ts}
    # legacy wide
    wide = await search_contact_by_phone_wide(token, e164)
    vals = wide.get("value") or wide.get("data") or []
    if isinstance(vals, list) and vals:
        first = vals[0]
        for k in ("Key","key","Id","id","ContactKey","contactKey"):
            if k in first and first[k]:
                return str(first[k]), {"stage": "odata_wide", "resp": wide}
    return None, {"stage": "not_found", "resp": {"std": std, "two_stage": ts, "wide": wide}}

# ─────────────────── Attachments (JSON body per Swagger) ───────────────────

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
    if ext in ("m4a", "mp4"): return "audio/mp4"
    if ext == "wav": return "audio/wav"
    if ext == "ogg": return "audio/ogg"
    if fallback: return fallback.split(";")[0].strip()
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

async def upload_attachment_json(
    token: str,
    object_key: str,
    filename: str,
    content_type: str,
    data: bytes,
    user_key: Optional[str] = None,
    team_key: Optional[str] = None,
    description: Optional[str] = None,
    attachment_type_key: Optional[int] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "file": {
            "fileName": filename,
            "fileContent": base64.b64encode(data).decode("ascii"),
            "mediaType": content_type,
        },
        "description": description or filename,
        "picture": False,
    }
    if user_key: body["userKey"] = user_key
    if team_key: body["teamKey"] = team_key
    if attachment_type_key is not None: body["attachmentTypeKey"] = attachment_type_key
    return await _try_paths(
        "POST",
        [f"attachment/{object_key}", f"Attachment/{object_key}"],
        token,
        json=body,
    )

async def attach_recording_from_url(
    token: str,
    object_key: str,
    recording_url: str,
    user_key: Optional[str] = None,
    team_key: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        fetched = await fetch_url_bytes(recording_url)
        if fetched.get("status", 0) >= 400:
            return {"status": fetched.get("status", 500), "error": "fetch_failed", "fetch": fetched}
        return await upload_attachment_json(
            token,
            object_key,
            fetched["filename"],
            fetched["content_type"],
            fetched["bytes"],
            user_key=user_key,
            team_key=team_key,
            description="Kixie Recording"
        )
    except Exception as e:
        return {"status": 500, "error": str(e)}

# ─────────────────── Probe (debug) ───────────────────

async def probe_endpoints(token: str) -> Dict[str, Any]:
    shapes = [
        "contact","history","users","teams",
        "Contact","History","Users","Teams",
        "CrmOData/Users","CrmOData/Teams","crmodata/Users","crmodata/Teams",
        "OData/Users","OData/Teams","CRM/Users","CRM/Teams",
        "timezones","Timezones","attachment/test-key",
        "CrmOData/Contacts","crmodata/Contacts","OData/Contacts","odata/Contacts",
    ]
    out = {"bases": BASES, "checks": []}
    async with _client() as client:
        for base in BASES:
            for path in shapes:
                url = f"{base.rstrip('/')}/{path.lstrip('/')}"
                try:
                    # Use the token passed into the function (don’t ignore it)
                    r = await _send(client, "OPTIONS", url, token)
                    out["checks"].append({"url": url, "status": r.status_code})
                except Exception as e:
                    out["checks"].append({"url": url, "error": str(e)})
    return out
