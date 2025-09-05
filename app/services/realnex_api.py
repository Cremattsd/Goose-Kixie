# app/services/realnex_api.py
import os, re
import httpx

BASE = os.getenv("REALNEX_API_BASE", "https://sync.realnex.com/api/v1/Crm").rstrip("/")

# ---- OData config ----
ODATA_VERSION = os.getenv("RN_ODATA_VERSION", "1.0")
# If BASE is .../api/v1/Crm we want .../api/v1 for OData
ODATA_ROOT = BASE[:-4] if BASE.lower().endswith("/crm") else BASE

# Optional env pins to make history deterministic
RN_ODATA_HISTORY_SET = os.getenv("RN_ODATA_HISTORY_SET")  # e.g. "/Histories"
RN_ODATA_HISTORY_DATE_FIELD = os.getenv("RN_ODATA_HISTORY_DATE_FIELD")  # e.g. "Date"


def _headers(token: str, odata: bool = False, xml: bool = False) -> dict:
    if xml:
        accept = "application/xml"
    else:
        accept = "application/json;odata.metadata=minimal;odata.streaming=true" if odata else "application/json"
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": accept,
    }


async def _safe_json(r: httpx.Response) -> dict:
    ct = (r.headers.get("content-type") or "").lower()
    body = (r.text or "").strip()
    okish = r.status_code < 400
    if "application/json" in ct and body:
        try:
            js = r.json()
        except Exception:
            js = {"raw": body}
    else:
        js = {"body": body} if body else {}
    js.update({"status": r.status_code, "reason": r.reason_phrase})
    if not okish:
        js["error"] = f"{r.status_code} {r.reason_phrase}"
    return js


async def _get_json(url: str, token: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.get(url, headers=_headers(token), params=params)
        return await _safe_json(r)


async def _post_json(url: str, token: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.post(url, headers=_headers(token), json=payload)
        return await _safe_json(r)


async def _odata_get(path: str, token: str, params: dict | None = None) -> dict:
    q = {"api-version": ODATA_VERSION}
    if params:
        q.update(params)
    url = f"{ODATA_ROOT}/CrmOData{path}"
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.get(url, headers=_headers(token, odata=True), params=q)
        return await _safe_json(r)


async def _odata_post(path: str, token: str, payload: dict) -> dict:
    q = {"api-version": ODATA_VERSION}
    url = f"{ODATA_ROOT}/CrmOData{path}"
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.post(url, headers=_headers(token, odata=True), params=q, json=payload)
        return await _safe_json(r)


# --------- OData helpers ---------

def _odata_params(filter_expr: str, top: int = 5, select: str | None = None) -> dict:
    p = {"$filter": filter_expr, "$top": top}
    if select:
        p["$select"] = select
    return p


def _normalize_phone_variants(e164: str) -> list[str]:
    digits = "".join(ch for ch in e164 if ch.isdigit())
    last10 = digits[-10:] if len(digits) >= 10 else digits
    out: list[str] = []
    for v in (e164, digits, last10):
        if v and v not in out:
            out.append(v)
    return out


async def search_contact_by_phone_odata(token: str, e164: str) -> dict:
    variants = _normalize_phone_variants(e164)
    fields = ["Mobile", "Home", "Work", "Fax"]
    select_cols = "Key,FirstName,LastName,Mobile,Home,Work,Email"

    eq_parts: list[str] = []
    for fld in fields:
        for v in variants:
            eq_parts.append(f"{fld} eq '{v}'")
    if eq_parts:
        fexpr = " or ".join(eq_parts)
        res = await _odata_get("/Contacts", token, _odata_params(fexpr, top=5, select=select_cols))
        if res.get("status", 500) < 400 and res.get("value"):
            return res

    tails = [v for v in variants if len(v) >= 7][-2:] or variants[-1:]
    contains_parts: list[str] = []
    for fld in fields:
        for v in tails:
            contains_parts.append(f"contains({fld},'{v}')")
    if contains_parts:
        fexpr = " or ".join(contains_parts)
        res = await _odata_get("/Contacts", token, _odata_params(fexpr, top=5, select=select_cols))
        if res.get("status", 500) < 400 and res.get("value"):
            return res

    return {"status": 404, "value": []}


# --- OData metadata (for debugging) ---
async def list_odata_entitysets(token: str) -> dict:
    url = f"{ODATA_ROOT}/CrmOData/$metadata?api-version={ODATA_VERSION}"
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.get(url, headers=_headers(token, odata=True, xml=True))
        status = r.status_code
        text = r.text or ""
        names = re.findall(r'EntitySet\s+Name="([^"]+)"', text) or re.findall(r'EntitySet Name="([^"]+)"', text)
        return {"status": status, "sets": names, "raw": text[:2000]}


# --------- REST searches (fallbacks) ---------

async def get_contacts(token: str, params: dict) -> dict:
    for path in ("/Contact", "/contact"):
        res = await _get_json(f"{BASE}{path}", token, params)
        if res.get("status", 500) < 400:
            return res
    return res


async def search_any(token: str, query: str) -> dict:
    for path in ("/Search/Any", "/search/any"):
        res = await _get_json(f"{BASE}{path}", token, {"q": query})
        if res.get("status", 500) < 400:
            return res
    return res


# --------- Creates / History ---------

async def create_contact(token: str, contact: dict) -> dict:
    for path in ("/Contact", "/contact"):
        res = await _post_json(f"{BASE}{path}", token, contact)
        if res.get("status", 500) < 400:
            return res
    return res


async def create_contact_by_number(
    token: str,
    number_e164: str,
    first_name: str | None = None,
    last_name: str | None = None,
    email: str | None = None,
    company: str | None = None,
) -> dict:
    """
    Create a contact with best-available detail.
    Sends both camelCase and PascalCase variants; tries 'phones' array shape too.
    """
    number_tail = "".join(ch for ch in number_e164 if ch.isdigit())[-4:] or number_e164[-4:]
    fn = (first_name or "").strip() or "Unknown"
    ln = (last_name  or "").strip() or f"Caller {number_tail}"
    em = (email or "").strip() or None
    co = (company or "").strip() or None

    def _clean(d: dict) -> dict:
        return {k: v for k, v in d.items() if v is not None and v != ""}

    payloads = [
        _clean({
            "firstName": fn,
            "lastName": ln,
            "mobile": number_e164,
            "email": em,
            "company": co,
            "source": "kixie",
        }),
        _clean({
            "FirstName": fn,
            "LastName": ln,
            "Mobile": number_e164,
            "Email": em,
            "Company": co,
            "Source": "kixie",
        }),
        _clean({
            "firstName": fn,
            "lastName": ln,
            "phones": [{"type": "Mobile", "phoneNumber": number_e164}],
            "email": em,
            "source": "kixie",
            "company": co,
        }),
    ]

    last = {}
    for p in payloads:
        res = await create_contact(token, p)
        if res.get("status", 500) < 400:
            return res
        last = res
    return last


async def create_history_rest(token: str, history: dict, contact_id: str | None = None) -> dict:
    roots = [BASE]
    if BASE.lower().endswith("/crm"):
        roots.append(BASE[:-4])
    else:
        roots.append(f"{BASE}/Crm")

    path_variants = ["/History", "/history", "/CrmHistory", "/crmhistory"]
    if contact_id:
        path_variants = [
            f"/Contact/{contact_id}/History",
            f"/contact/{contact_id}/history",
        ] + path_variants

    last = {}
    for root in roots:
        for path in path_variants:
            url = f"{root.rstrip('/')}{path}"
            res = await _post_json(url, token, history)
            if res.get("status", 500) < 400:
                return res
            last = {"attempt": url, **res}
    return last


async def create_history_odata(token: str, subject: str, note: str, date_iso: str, contact_id: str, event_type_key: str | None) -> dict:
    """
    Create History via OData entity sets. Try env-pinned set/field first, else common fallbacks.
    """
    common = {"Subject": subject, "Title": subject, "Description": note, "Note": note, "ContactKey": contact_id}
    if event_type_key:
        common["EventTypeKey"] = event_type_key
        common["eventTypeKey"] = event_type_key

    # First: try pinned env set/field if provided
    if RN_ODATA_HISTORY_SET and RN_ODATA_HISTORY_DATE_FIELD:
        payload = dict(common)
        payload[RN_ODATA_HISTORY_DATE_FIELD] = date_iso
        res = await _odata_post(RN_ODATA_HISTORY_SET, token, payload)
        if res.get("status", 500) < 400:
            return res

    # Otherwise: try common sets and date fields
    sets = ["/Histories", "/ContactHistories", "/Activities", "/Notes", "/ActivityHistories", "/ContactNotes"]
    date_fields = ["Date", "ActivityDate", "EventDate", "CreatedOn"]

    last = {}
    for s in sets:
        for df in date_fields:
            payload = dict(common)
            payload[df] = date_iso
            res = await _odata_post(s, token, payload)
            if res.get("status", 500) < 400:
                return res
            last = {"attempt": f"{ODATA_ROOT}/CrmOData{s}", "payload_keys": list(payload.keys()), **res}
    return last


async def create_history(token: str, history: dict, contact_id: str | None = None) -> dict:
    # Legacy fallback used by router if OData fails
    return await create_history_rest(token, history, contact_id)
