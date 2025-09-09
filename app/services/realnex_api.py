import os, httpx, re, functools, asyncio
from typing import Any, Dict, Optional, List, Tuple

# ------------------ env / base handling ------------------

def get_rn_token() -> str:
    return os.getenv("REALNEX_JWT") or os.getenv("REALNEX_TOKEN") or ""

def _bases_from_env() -> List[str]:
    raw = os.getenv("REALNEX_API_BASE", "https://sync.realnex.com/api/v1/Crm")
    parts = [p.strip().rstrip("/") for p in raw.split(",") if p.strip()]
    if len(parts) == 1:
        b = parts[0]
        variants = [b]
        # add common siblings
        if b.lower().endswith("/crm"):
            root = b[:-4]
            variants += [root + "/CrmOData", root + "/CRM", root + "/crmodata"]
        parts = variants
    return parts

BASES = _bases_from_env()

def _headers(token: str) -> Dict[str,str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

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

# ------------------ helpers ------------------

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

# ------------------ Contacts / Activities / History ------------------

async def search_by_phone(token: str, phone_e164: str) -> Dict[str,Any]:
    return await _try_paths("GET",
        ["Contacts/search","Contact/search","contacts/search","contact/search"],
        token, params={"phone": phone_e164})

async def create_contact(token: str, payload: Dict[str, Any]) -> Dict[str,Any]:
    return await _try_paths("POST",
        ["Contacts","Contact","contacts","contact"],
        token, json=payload)

async def create_activity(token: str, payload: Dict[str, Any]) -> Dict[str,Any]:
    return await _try_paths("POST",
        ["Activities","Activity","activities","activity"],
        token, json=payload)

async def create_history(token: str, payload: Dict[str, Any]) -> Dict[str,Any]:
    return await _try_paths("POST",
        ["history","History","histories","Histories"],
        token, json=payload)

# ------------------ OData: resolve user/team by email ------------------

# Collections to probe (order matters)
_ODATA_USER_COLLECTIONS = [
    "CrmOData/Users", "CrmOData/users", "crmodata/Users", "crmodata/users",
    "OData/Users", "odata/Users", "CRM/Users", "CRM/users",
]
_ODATA_TEAM_COLLECTIONS = [
    "CrmOData/Teams", "CrmOData/teams", "crmodata/Teams", "crmodata/teams",
    "OData/Teams", "odata/Teams", "CRM/Teams", "CRM/teams",
]

# possible field names for filtering/keys
_EMAIL_FIELDS = ["Email","email","UserEmail","Username","username","login","Login"]
_USER_KEY_FIELDS = ["Key","Id","id","UserKey","userKey","UserId","userId"]
_TEAM_KEY_FIELDS = ["TeamKey","teamKey","Key","Id","id"]

def _build_odata_filter(email: str) -> Dict[str, str]:
    # Try several filter syntaxes
    conds = [
        f"Email eq '{email}'",
        f"email eq '{email}'",
        f"UserEmail eq '{email}'",
        f"Username eq '{email}'",
        f"username eq '{email}'",
        f"Login eq '{email}'",
        f"login eq '{email}'",
    ]
    return {"$filter": " or ".join(conds), "$top": "1"}

@functools.lru_cache(maxsize=512)
def _cache_key(email: str) -> str:
    return email.lower().strip()

_user_cache: Dict[str, Tuple[Optional[str], Optional[str]]] = {}  # email -> (userKey, teamKey)
_user_lock = asyncio.Lock()

async def _odata_first(client: httpx.AsyncClient, base: str, path: str, token: str, params: Dict[str,str]) -> Optional[Dict[str,Any]]:
    url = f"{base}/{path}"
    try:
        r = await _send(client, "GET", url, token, params=params)
        if r.status_code == 404:
            return None
        data = await _format_resp(r)
        # OData may wrap in 'value' list; otherwise return dict
        if isinstance(data, dict):
            # prefer a list in 'value'
            if isinstance(data.get("value"), list) and data["value"]:
                return data["value"][0]
            # or maybe it's a single entity
            # strip helper fields we added
            d = {k:v for k,v in data.items() if k not in ("status","url","method")}
            if d:
                return d
        return None
    except httpx.HTTPError:
        return None

async def resolve_user_team_by_email(token: str, email: str) -> Tuple[Optional[str], Optional[str], Dict[str, Any]]:
    """
    Returns (userKey, teamKey, context) for debugging.
    Caches results per email.
    """
    key = _cache_key(email)
    async with _user_lock:
        if key in _user_cache:
            uk, tk = _user_cache[key]
            return uk, tk, {"cached": True}

    params = _build_odata_filter(email)
    ctx: Dict[str, Any] = {"tried": []}
    async with _client() as client:
        # Find USER
        user_obj: Optional[Dict[str,Any]] = None
        hit_user_url: Optional[str] = None
        for base in BASES:
            for coll in _ODATA_USER_COLLECTIONS:
                u = await _odata_first(client, base, coll, token, params)
                ctx["tried"].append(f"{base}/{coll}")
                if u:
                    user_obj = u
                    hit_user_url = f"{base}/{coll}"
                    break
            if user_obj:
                break

        user_key: Optional[str] = None
        team_key: Optional[str] = None

        if user_obj:
            # pull userKey
            for f in _USER_KEY_FIELDS:
                if f in user_obj and user_obj[f]:
                    user_key = str(user_obj[f]); break
            # find team directly on user if present
            for f in ["TeamKey","teamKey","DefaultTeamKey","defaultTeamKey"]:
                if f in user_obj and user_obj[f]:
                    team_key = str(user_obj[f]); break

        # If still no team, query TEAMS with any relationship hints
        if not team_key:
            team_obj: Optional[Dict[str,Any]] = None
            # try by "OwnerEmail == email" style filters too
            team_params = {
                "$top": "1",
                "$filter": " or ".join([
                    f"OwnerEmail eq '{email}'",
                    f"ownerEmail eq '{email}'",
                    f"Email eq '{email}'",
                    f"email eq '{email}'",
                ])
            }
            for base in BASES:
                for coll in _ODATA_TEAM_COLLECTIONS:
                    t = await _odata_first(client, base, coll, token, team_params)
                    ctx["tried"].append(f"{base}/{coll}")
                    if t:
                        team_obj = t
                        break
                if team_obj:
                    break
            if team_obj:
                for f in _TEAM_KEY_FIELDS:
                    if f in team_obj and team_obj[f]:
                        team_key = str(team_obj[f]); break

    async with _user_lock:
        _user_cache[key] = (user_key, team_key)
    ctx.update({"userKey": user_key, "teamKey": team_key, "userHit": hit_user_url})
    return user_key, team_key, ctx

# ------------------ debug probe ------------------

async def probe_endpoints(token: str) -> Dict[str, Any]:
    shapes = ["Contacts","Contact","contacts","contact",
              "Activities","Activity","activities","activity",
              "history","History","histories","Histories",
              # odata collections (HEAD/GET for existence)
              "CrmOData/Users","CrmOData/Teams","crmodata/Users","crmodata/Teams",
              "OData/Users","OData/Teams","CRM/Users","CRM/Teams"]
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
