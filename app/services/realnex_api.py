import os, httpx, re, asyncio
from typing import Any, Dict, Optional, List, Tuple

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
    return {"Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json"}

def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(30.0))

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

def normalize_phone_e164ish(raw: Optional[str]) -> Optional[str]:
    if not raw: return None
    digits = re.sub(r"\D+", "", raw)
    if not digits: return None
    if len(digits) == 10: return f"+1{digits}"
    if digits.startswith("1") and len(digits)==11: return f"+{digits}"
    if raw.startswith("+"): return f"+{digits}"
    return f"+{digits}"

async def _send(client: httpx.AsyncClient, method: str, url: str, token: str, **kw) -> httpx.Response:
    req = client.build_request(method, url, headers=_headers(token), **kw)
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

# ---------- Contacts & History (only) ----------

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
        "OData/Users","OData/Teams","CRM/Users","CRM/Teams"
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
