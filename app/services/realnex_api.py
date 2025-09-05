import os
import httpx

BASE = os.getenv("REALNEX_API_BASE", "https://sync.realnex.com/api/v1/Crm").rstrip("/")

# ---- OData config ----
ODATA_VERSION = os.getenv("RN_ODATA_VERSION", "1.0")
# If BASE is .../api/v1/Crm we want .../api/v1 for OData
ODATA_ROOT = BASE[:-4] if BASE.lower().endswith("/crm") else BASE


def _headers(token: str, odata: bool = False) -> dict:
    accept = "application/json;odata.metadata=minimal;odata.streaming=true" if odata else "application/json"
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": accept,
    }


async def _safe_json(r: httpx.Response) -> dict:
    """
    RN sometimes 200/204 with no JSON; or 500 with empty text.
    Return structured info without crashing.
    """
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
    """
    OData search across likely contact phone fields on /CrmOData/Contacts.
    Tries exact eq first, then contains() with last digits.
    Returns {"status": <int>, "value": [...]}
    """
    variants = _normalize_phone_variants(e164)
    # RN OData schema uses PascalCase
    fields = ["Mobile", "Home", "Work", "Fax"]
    select_cols = "Key,FirstName,LastName,Mobile,Home,Work,Email"

    # 1) equals (strongest)
    eq_parts: list[str] = []
    for fld in fields:
        for v in variants:
            eq_parts.append(f"{fld} eq '{v}'")
    if eq_parts:
        fexpr = " or ".join(eq_parts)
        res = await _odata_get(
            "/Contacts",
            token,
            _odata_params(fexpr, top=5, select=select_cols),
        )
        if res.get("status", 500) < 400 and res.get("value"):
            return res

    # 2) contains() (use last 7–10 digits)
    tails = [v for v in variants if len(v) >= 7][-2:] or variants[-1:]
    contains_parts: list[str] = []
    for fld in fields:
        for v in tails:
            contains_parts.append(f"contains({fld},'{v}')")
    if contains_parts:
        fexpr = " or ".join(contains_parts)
        res = await _odata_get(
            "/Contacts",
            token,
            _odata_params(fexpr, top=5, select=select_cols),
        )
        if res.get("status", 500) < 400 and res.get("value"):
            return res

    return {"status": 404, "value": []}


# --------- REST searches (fallbacks) ---------

async def get_contacts(token: str, params: dict) -> dict:
    for path in ("/Contact", "/contact"):
        res = await _get_json(f"{BASE}{path}", token, params)
        if res.get("status", 500) < 400:
            return res
    return res  # last result (error)


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


async def create_contact_by_number(token: str, number_e164: str) -> dict:
    """Try two common shapes."""
    attempt_a = {
        "firstName": "Kixie",
        "lastName": "Lead",
        "mobile": number_e164,
        "source": "kixie",
    }
    res = await create_contact(token, attempt_a)
    if res.get("status", 500) < 400:
        return res
    attempt_b = {
        "firstName": "Kixie",
        "lastName": "Lead",
        "source": "kixie",
        "phones": [{"type": "Mobile", "phoneNumber": number_e164}],
    }
    return await create_contact(token, attempt_b)


async def create_history(token: str, history: dict, contact_id: str | None = None) -> dict:
    """
    Try across:
      Bases: BASE, BASE(without /Crm)  e.g. …/api/v1/Crm and …/api/v1
      Paths: nested & top-level (case variants)
    """
    roots = [BASE]
    if BASE.lower().endswith("/crm"):
        roots.append(BASE[:-4])  # strip '/Crm'
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
            last = res
    return last  # error result
