# app/services/realnex_api.py
import os
from typing import Any, Dict, Optional, AsyncIterator
from urllib.parse import urlencode, urlparse, parse_qs

import httpx

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
    # Merge status into payload for easy branching by caller
    if isinstance(data, dict):
        data.setdefault("status", resp.status_code)
        return data
    # wrap non-dict JSON
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
    Returns JSON including list of contacts; keys often PascalCase (Key, FirstName, Mobile, ...)
    """
    url = f"{BASE}/Contact"
    return await _get_json(url, token, params=query_params)

async def search_any(token: str, q: str) -> Dict[str, Any]:
    """
    GET /Crm/Search/Any?q=...
    Returns .value[] with entries like { entityType, objectKey, title, ... }
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

# ---------- REST: History (object-scoped preferred) ----------

async def create_history_record(token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fallback: POST /Crm/history (unlinked); returns history row (Key).
    """
    url = f"{BASE}/history"
    return await _post_json(url, token, payload)

async def add_object_to_history(token: str, history_key: str, object_key: str) -> Dict[str, Any]:
    """
    POST /Crm/history/{historyKey}/object  (also try title-case path)
    """
    # try both casing variants that RealNex may expose
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
    Preferred flow:
      1) POST /Crm/object/{objectKey}/history (or /Object/{...}/History) with varying date fields
      2) If all variants fail, POST /Crm/history then POST /Crm/history/{historyKey}/object to link.

    We cycle through common date fields RealNex uses across tenants: Date, ActivityDate, EventDate
    """
    base_paths = [
        f"{BASE}/object/{object_key}/history",
        f"{BASE}/Object/{object_key}/History",
    ]
    date_fields = ["Date", "ActivityDate", "EventDate"]

    def _payload(dfield: str) -> Dict[str, Any]:
        p = {
            "Subject": subject,
            "Title": subject,  # some tenants require Title instead of Subject
            dfield: date_iso,
            "Note": note,
        }
        if event_type_key:
            p["EventTypeKey"] = event_type_key
        return p

    last: Dict[str, Any] = {}
    # Try object-scoped creation first
    for url in base_paths:
        for df in date_fields:
            res = await _post_json(url, token, _payload(df))
            last = {"attempt": url, "date_field": df, **res}
            if res.get("status", 500) < 400:
                return res

    # Fallback: create unscoped history then link to object
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

# ---------- (optional) legacy create_history used by older code paths ----------
async def create_history(token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Legacy plain-history writer. Prefer create_history_for_object().
    """
    return await create_history_record(token, payload)

# ---------- OData helpers for dialer sync ----------

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
    # Preserve commas/parentheses in $filter
    url = f"{ODATA_BASE}/Contacts?{urlencode(params, safe='(),= ')}"
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
