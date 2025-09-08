import os
from typing import Any, Dict, Optional, AsyncIterator, List
from urllib.parse import urlencode, urlparse, parse_qs

import httpx

# Base URLs
BASE = os.getenv("REALNEX_API_BASE", "https://sync.realnex.com/api/v1/Crm").rstrip("/")
ODATA_BASE = BASE.replace("/Crm", "/CrmOData")

# ---------- low-level HTTP helpers ----------

def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(25.0))

async def _format_resp(resp: httpx.Response) -> Dict[str, Any]:
    try:
        data = resp.json() if resp.content else {}
    except Exception:
        data = {"raw": resp.text}
    if isinstance(data, dict):
        data.setdefault("status", resp.status_code)
        return data
    return {"status": resp.status_code, "data": data}

async def _get_json(url: str, token: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    async with _client() as client:
        try:
            r = await client.get(url, params=params, headers=_headers(token))
            return await _format_resp(r)
        except httpx.HTTPError as e:
            status = getattr(getattr(e, "response", None), "status_code", 599)
            return {"status": status, "error": str(e)}

async def _post_json(url: str, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    async with _client() as client:
        try:
            r = await client.post(url, json=payload, headers=_headers(token))
            return await _format_resp(r)
        except httpx.HTTPError as e:
            status = getattr(getattr(e, "response", None), "status_code", 599)
            return {"status": status, "error": str(e)}

# ---------- REST: Contacts, Search, Create ----------

async def get_contacts(token: str, query_params: Dict[str, Any]) -> Dict[str, Any]:
    """
    GET /Crm/Contact?q=...
    """
    url = f"{BASE}/Contact"
    return await _get_json(url, token, params=query_params)

async def search_any(token: str, q: str) -> Dict[str, Any]:
    """
    GET /Crm/Search/Any?q=...
    Returns .value[] with {entityType, objectKey, ...}
    """
    url = f"{BASE}/Search/Any"
    return await _get_json(url, token, params={"q": q})

async def create_contact(token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    POST /Crm/Contact
    Payload example: { "firstName":"", "lastName":"", "mobile":"+1...", "source":"kixie" }
    Returns created contact with Key.
    """
    url = f"{BASE}/Contact"
    return await _post_json(url, token, payload)

async def create_contact_by_number(
    token: str,
    number_e164: str,
    first_name: str = "",
    last_name: str = "",
    email: Optional[str] = None,
    company: Optional[str] = None,
    source: str = "kixie",
) -> Dict[str, Any]:
    """
    Convenience wrapper for minimal contact creation.
    """
    payload: Dict[str, Any] = {
        "mobile": number_e164,
        "firstName": first_name or "",
        "lastName": last_name or "",
        "source": source,
    }
    if email:
        payload["email"] = email
    if company:
        payload["company"] = company
    return await create_contact(token, payload)

# ---------- REST: History (object-scoped) ----------

async def create_history_record(token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fallback: POST /Crm/history (unlinked); returns history row (Key)
    """
    url = f"{BASE}/history"
    return await _post_json(url, token, payload)

async def add_object_to_history(token: str, history_key: str, object_key: str) -> Dict[str, Any]:
    """
    POST /Crm/history/{historyKey}/object (try two casings)
    """
    paths = [
        f"{BASE}/history/{history_key}/object",
        f"{BASE}/History/{history_key}/Object",
    ]
    last = {}
    for path in paths:
        res = await _post_json(path, token, {"objectKey": object_key})
        last = res
        if res.get("status", 500) < 400:
            return res
    return last

async def create_history_for_object(
    token: str,
    object_key: str,
    subject: str,
    note: str,
    date_iso: str,
    event_type_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Preferred REST path:
      1) POST /Crm/object/{objectKey}/history (or /Object/{...}/History) with varying date fields
      2) If all variants fail, POST /Crm/history then link it to object
    """
    base_paths = [
        f"{BASE}/object/{object_key}/history",
        f"{BASE}/Object/{object_key}/History",
    ]
    date_fields = ["Date", "ActivityDate", "EventDate"]

    def _payload(dfield: str) -> Dict[str, Any]:
        p = {
            "Subject": subject,
            "Title": subject,
            dfield: date_iso,
            "Note": note,
        }
        if event_type_key:
            p["EventTypeKey"] = event_type_key
        return p

    last: Dict[str, Any] = {}
    for url in base_paths:
        for df in date_fields:
            res = await _post_json(url, token, _payload(df))
            last = {"attempt": url, "date_field": df, **res}
            if res.get("status", 500) < 400:
                return res

    # Fallback: create then link
    for df in date_fields:
        created = await create_history_record(token, _payload(df))
        last = {"attempt": "create", "date_field": df, **created}
        if created.get("status", 500) < 400:
            hk = created.get("Key") or created.get("key") or created.get("historyKey")
            if hk:
                linked = await add_object_to_history(token, str(hk), object_key)
                if linked.get("status", 500) < 400:
                    return {"status": 201, "linked": True, "historyKey": hk}
                last = {"attempt": "link", "historyKey": hk, **linked}

    return last

# Back-compat name used by your kixie.py (now accepts optional object_key)
async def create_history(token: str, payload: Dict[str, Any], object_key: Optional[str] = None) -> Dict[str, Any]:
    """
    Legacy entrypoint:
      - Creates plain history
      - If object_key is provided, tries to link it
    """
    created = await create_history_record(token, payload)
    if created.get("status", 500) < 400 and object_key:
        hk = created.get("Key") or created.get("key") or created.get("historyKey")
        if hk:
            _ = await add_object_to_history(token, str(hk), object_key)
    return created

# ---------- OData helpers ----------

def _odata_url(path: str) -> str:
    return f"{ODATA_BASE}/{path.lstrip('/')}"

async def list_odata_entitysets(token: str) -> Dict[str, Any]:
    """
    OData service document:
      GET /CrmOData/          -> usually returns {"value":[{"name":"Contacts","url":"Contacts"}, ...]}
    """
    url = ODATA_BASE  # service root
    return await _get_json(url, token)

def _escape_odata_str(val: str) -> str:
    # escape single quotes for OData
    return val.replace("'", "''")

async def search_contact_by_phone_odata(token: str, phone_e164: str) -> Dict[str, Any]:
    """
    GET /CrmOData/Contacts?$select=...&$filter=...
    Try multiple fields (Mobile, Work, Home, Phone, BusinessPhone) and both exact + contains matches.
    """
    # normalize and build variants
    digits = "".join(ch for ch in str(phone_e164) if ch.isdigit())
    last10 = digits[-10:] if len(digits) >= 10 else digits
    variants = [phone_e164, f"+1{last10}", last10]
    fields: List[str] = ["Mobile", "Work", "Home", "Phone", "BusinessPhone"]

    clauses: List[str] = []
    for f in fields:
        for v in variants:
            vq = _escape_odata_str(str(v))
            clauses.append(f"{f} eq '{vq}'")
            clauses.append(f"contains({f}, '{vq}')")

    odata_filter = " or ".join(clauses)
    select = "Key,FirstName,LastName,Work,Mobile,Home,Email"

    params = {
        "$select": select,
        "$filter": odata_filter,
        "$top": "200",
    }
    # Keep parentheses/commas
    url = _odata_url(f"Contacts?{urlencode(params, safe='(),= ')}")
    return await _get_json(url, token)

async def create_history_odata(
    token: str,
    subject: str,
    note: str,
    date_iso: str,
    contact_key: str,
    event_type_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Try to create History via OData entity set(s).
    Because RealNex tenants can differ, try a few combinations:
      - POST /CrmOData/History
      - POST /CrmOData/Histories
    with varying date field names and payload shapes.
    If all OData attempts fail, the caller should fall back to REST.
    """
    endpoints = [
        _odata_url("History"),
        _odata_url("Histories"),
    ]
    date_fields = ["Date", "ActivityDate", "EventDate"]

    def _payload(dfield: str) -> Dict[str, Any]:
        p = {
            "Subject": subject,
            "Title": subject,
            dfield: date_iso,
            "Note": note,
            # try to bind directly to the contact by key
            "ObjectKey": contact_key,
            "EntityType": "Contact",
        }
        if event_type_key:
            p["EventTypeKey"] = event_type_key
        return p

    last = {}
    for ep in endpoints:
        for df in date_fields:
            res = await _post_json(ep, token, _payload(df))
            last = {"attempt": ep, "date_field": df, **res}
            if res.get("status", 500) < 400:
                return res

    return last

# ---------- Extra OData: paging helpers (kept for dialer sync) ----------

async def odata_contacts_page(
    token: str,
    select: str,
    filter: str,
    top: int = 200,
    skiptoken: Optional[str] = None,
) -> Dict[str, Any]:
    params = {"$select": select, "$filter": filter, "$top": str(top)}
    if skiptoken:
        params["$skiptoken"] = skiptoken
    url = _odata_url(f"Contacts?{urlencode(params, safe='(),= ')}")
    return await _get_json(url, token)

async def odata_contacts_iter(
    token: str,
    select: str,
    filter: str,
    top: int = 200,
    max_rows: int = 500,
) -> AsyncIterator[list]:
    pulled = 0
    next_token: Optional[str] = None
    while pulled < max_rows:
        page = await odata_contacts_page(
            token, select=select, filter=filter, top=min(top, max_rows - pulled), skiptoken=next_token
        )
        items = page.get("value") or page.get("Value") or []
        if not items:
            break
        yield items
        pulled += len(items)
        nxt = page.get("@odata.nextLink") or page.get("odata.nextLink")
        if not nxt:
            break
        try:
            qs = parse_qs(urlparse(nxt).query)
            st = qs.get("$skiptoken") or qs.get("%24skiptoken")
            next_token = st[0] if st else None
        except Exception:
            next_token = None
        if not next_token:
            break
