import os, httpx, re, asyncio, base64, json
from typing import Any, Dict, Optional, List, Tuple

# ========= Tokens / Bases =========

def get_rn_token() -> str:
    return os.getenv("REALNEX_JWT") or os.getenv("REALNEX_TOKEN") or ""

def _bases_from_env() -> List[str]:
    """
    REALNEX_API_BASE can be a comma-separated list, or a single base like:
      https://sync.realnex.com/api/v1/Crm
    We derive sensible variants for CrmOData/CRM/crmodata automatically.
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

# ========= HTTP helpers =========

def _headers(token: str) -> Dict[str,str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(30.0))

async def _format_resp(resp: httpx.Response) -> Any:
    try:
        data = resp.json() if resp.content else {}
    except Exception:
        try:
            raw = await resp.aread()
            data = {"raw": raw.decode("utf-8", "ignore")}
        except Exception:
            data = {"raw": "<unreadable>"}
    if isinstance(data, dict):
        data.setdefault("status", resp.status_code)
        if resp.request is not None:
            data.setdefault("url", str(resp.request.url))
            data.setdefault("method", resp.request.method)
    return data

async def _send(client: httpx.AsyncClient, method: str, url: str, token: str, **kw) -> httpx.Response:
    req = client.build_request(method, url, headers=_headers(token), **kw)
    return await client.send(req)

async def _try_paths(method: str, paths: List[str], token: str, **kw) -> Any:
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

# ========= URL join helper for OData =========

def _join_base_path(base: str, path: str) -> str:
    base = base.rstrip("/")
    path = path.lstrip("/")
    if not path:
        return base
    btail = base.rsplit("/", 1)[-1].lower()
    phead = path.split("/", 1)[0].lower()
    if btail == phead:
        path = path.split("/", 1)[1] if "/" in path else ""
    return f"{base}/{path}" if path else base

# ========= Phone utils =========

def normalize_phone_e164ish(raw: Optional[str]) -> Optional[str]:
    if not raw: return None
    digits = re.sub(r"\D+", "", raw)
    if not digits: return None
    if len(digits) == 10: return f"+1{digits}"
    if digits.startswith("1") and len(digits) == 11: return f"+{digits}"
    if raw.startswith("+"): return f"+{digits}"
    return f"+{digits}"

def _phone_formats(raw: Optional[str]) -> Dict[str, Optional[str]]:
    e164 = normalize_phone_e164ish(raw)
    digits = re.sub(r"\D+", "", raw or "")
    last10 = digits[-10:] if len(digits) >= 10 else digits or None
    return {"e164": e164, "last10": last10, "digits": digits or None}

# ========= Core endpoints (generic wrappers) =========

async def search_by_phone(token: str, phone_e164: str) -> Any:
    return await _try_paths(
        "GET",
        ["Contacts/search","Contact/search","contacts/search","contact/search"],
        token,
        params={"phone": phone_e164}
    )

async def create_contact(token: str, payload: Dict[str, Any]) -> Any:
    return await _try_paths(
        "POST",
        ["contact","Contact","contacts","Contacts"],
        token,
        json=payload
    )

async def create_history(token: str, payload: Dict[str, Any]) -> Any:
    return await _try_paths(
        "POST",
        ["history","History","histories","Histories"],
        token,
        json=payload
    )

# ========= OData helpers =========

_ODATA_CONTACT_COLLECTIONS = [
    "CrmOData/Contacts", "crmodata/Contacts",
    "OData/Contacts", "odata/Contacts",
]
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

async def _odata_first(client: httpx.AsyncClient, base: str, path: str, token: str, params: Dict[str,str]):
    url = _join_base_path(base, path)
    try:
        r = await _send(client, "GET", url, token, params=params)
        if r.status_code >= 400:
            return None
        data = await _format_resp(r)
        if isinstance(data, dict) and isinstance(data.get("value"), list) and data["value"]:
            return data["value"][0]
        if isinstance(data, dict):
            if "error" in data:
                return None
            body_keys = {"status","url","method"}
            d = {k:v for k,v in data.items() if k not in body_keys}
            return d or None
        if isinstance(data, list) and data:
            return data[0]
        return None
    except httpx.HTTPError:
        return None

def _odata_filter(email: str) -> Dict[str,str]:
    conds = [f"{f} eq '{email}'" for f in _EMAIL_FIELDS]
    return {"$filter": " or ".join(conds), "$top": "1"}

# ========= Parsing helpers =========

def _as_list_from_search_result(resp: Any) -> List[Dict[str,Any]]:
    if isinstance(resp, list):
        return resp
    if not isinstance(resp, dict):
        return []
    if isinstance(resp.get("items"), list):
        return resp["items"]
    if isinstance(resp.get("value"), list):
        return resp["value"]
    for k in ("Key","key","Id","id","ContactKey","contactKey","PartyKey","partyKey"):
        if k in resp:
            return [resp]
    return []

def extract_contact_key(contact: Dict[str,Any]) -> Optional[str]:
    for k in ("Key","key","ContactKey","contactKey","PartyKey","partyKey","Id","id"):
        if k in contact and contact[k]:
            return str(contact[k])
    return None

def _pluck_any(d: Dict[str,Any], names: List[str]) -> Optional[str]:
    for n in names:
        if n in d and d[n]:
            return str(d[n])
    return None

# ========= Non-OData fallbacks (CRM endpoints) =========

async def list_users(token: str) -> List[Dict[str, Any]]:
    resp = await _try_paths("GET", ["Users","users","CRM/Users","CRM/users"], token)
    return resp if isinstance(resp, list) else _as_list_from_search_result(resp)

async def list_teams(token: str) -> List[Dict[str, Any]]:
    resp = await _try_paths("GET", ["Teams","teams","CRM/Teams","CRM/teams"], token)
    return resp if isinstance(resp, list) else _as_list_from_search_result(resp)

# ========= High-level helpers =========

async def find_contact_by_phone(token: str, raw_phone: str) -> Optional[Dict[str,Any]]:
    ph = _phone_formats(raw_phone)

    # 1) Tenant search first
    if ph["e164"]:
        rest1 = await search_by_phone(token, ph["e164"])
        items = _as_list_from_search_result(rest1)
        if items:
            return items[0]
    if ph["last10"]:
        rest2 = await search_by_phone(token, ph["last10"])
        items = _as_list_from_search_result(rest2)
        if items:
            return items[0]

    # 2) OData simple fields, no Phones/any unless last resort
    simple_fields = [
        "Phone", "MobilePhone", "PrimaryPhone", "PrimaryPhoneNumber",
        "BusinessPhone", "WorkPhone", "HomePhone"
    ]
    candidates: List[str] = []
    if ph["e164"]:
        candidates += [f"{f} eq '{ph['e164']}'" for f in simple_fields]
    if ph["last10"]:
        candidates += [f"{f} eq '{ph['last10']}'" for f in simple_fields]
    if ph["e164"]:
        candidates.append(f"Phones/any(p: p/Number eq '{ph['e164']}')")
    if ph["last10"]:
        candidates.append(f"Phones/any(p: p/Number eq '{ph['last10']}')")

    async with _client() as client:
        for base in BASES:
            for coll in _ODATA_CONTACT_COLLECTIONS:
                for filt in candidates:
                    params = {"$filter": filt, "$top": "1"}
                    found = await _odata_first(client, base, coll, token, params)
                    if isinstance(found, dict) and "error" in found:
                        continue
                    if found:
                        return found
    return None

async def get_or_create_contact_by_phone(
    token: str,
    raw_phone: str,
    team_key: Optional[str] = None,
    first_name: str = "Kixie",
    last_name: str = "Unknown"
) -> Dict[str,Any]:
    existing = await find_contact_by_phone(token, raw_phone)
    if isinstance(existing, dict):
        key = extract_contact_key(existing)
        if key:
            return {"created": False, "contact": existing, "contactKey": key}

    ph = _phone_formats(raw_phone)
    number = ph["e164"] or ph["last10"]
    if not number:
        return {"created": False, "error": "No phone digits"}

    payload = {
        "FirstName": first_name,
        "LastName": last_name,
        "Published": True,
        "Phones": [{"Number": number, "Type": "Mobile"}],
    }
    if team_key:
        payload["TeamKey"] = team_key

    created = await create_contact(token, payload)
    key = None
    if isinstance(created, dict):
        key = extract_contact_key(created)
        if not key and isinstance(created.get("data"), dict):
            key = extract_contact_key(created["data"])

    return {"created": True, "contact": created, "contactKey": key}

# ========= Post History with dynamic link-field probing =========

def _link_field_order_from_env() -> List[str]:
    """
    Order of fields to try when linking a History to a contact/lead.
    You can override with:
      RN_HISTORY_CONTACT_LINK_FIELDS="leadKey,partyKey,contactKey,linkedTo"
    or the single:
      RN_HISTORY_CONTACT_LINK_FIELD="leadKey"
    """
    multi = os.getenv("RN_HISTORY_CONTACT_LINK_FIELDS")
    if multi:
        return [f.strip() for f in multi.split(",") if f.strip()]
    single = os.getenv("RN_HISTORY_CONTACT_LINK_FIELD")
    if single:
        return [single.strip()]
    # Default order (tenant-agnostic, but biased for your setup):
    # - leadKey (your UI shows field as history_info.lead_key / "Linked to")
    # - partyKey / contactKey common in other tenants
    # - linkedTo variants seen occasionally
    return ["leadKey", "LeadKey", "lead_key", "history_info.lead_key",
            "partyKey", "PartyKey", "contactKey", "ContactKey",
            "linkedTo", "LinkedTo"]

async def post_history_for_contact(
    token: str,
    contact_key: str,
    history_payload: Dict[str,Any]
) -> Any:
    fields = _link_field_order_from_env()
    attempts: List[Dict[str,Any]] = []

    for f in fields:
        body = dict(history_payload)
        body[f] = contact_key
        resp = await create_history(token, body)
        status = resp.get("status", 500) if isinstance(resp, dict) else 500
        attempts.append({"field": f, "status": status})
        if status < 400:
            if isinstance(resp, dict):
                resp["link_field"] = f
                resp["attempts"] = attempts
            return resp

    # If none worked, return the last response and the attempts list
    last = attempts[-1] if attempts else {"field": None, "status": 500}
    return {
        "status": last["status"],
        "error": "Failed to post History with any link field",
        "link_field": None,
        "attempts": attempts,
    }

# ========= Capability probe =========

async def probe_endpoints(token: str) -> Dict[str, Any]:
    shapes = [
        "contact","history","users","teams",
        "Contact","History","Users","Teams",
        "CrmOData/Users","CrmOData/Teams","crmodata/Users","crmodata/Teams",
        "OData/Users","OData/Teams","CRM/Users","CRM/Teams",
        "CrmOData/Contacts","crmodata/Contacts","OData/Contacts"
    ]
    out = {"bases": BASES, "checks": []}
    async with _client() as client:
        for base in BASES:
            for path in shapes:
                url = f"{base}/{path}"
                try:
                    r = await _send(client, "OPTIONS", url, get_rn_token())
                    out["checks"].append({"url": url, "status": r.status_code})
                except Exception as e:
                    out["checks"].append({"url": url, "error": str(e)})
    return out

# ========= JWT helpers + dynamic user/team resolver =========

def _b64url_pad(s: str) -> str:
    return s + "=" * ((4 - len(s) % 4) % 4)

def parse_jwt_noverify(jwt: str) -> Dict[str, Any]:
    try:
        parts = jwt.split(".")
        if len(parts) < 2:
            return {}
        payload = json.loads(base64.urlsafe_b64decode(_b64url_pad(parts[1])).decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}

async def resolve_rn_context(token: str) -> Dict[str, Any]:
    payload = parse_jwt_noverify(token)
    email = payload.get("email") or payload.get("sub") or None
    jwt_user_key = payload.get("user_key") or payload.get("userKey") or None

    result: Dict[str, Any] = {"email": email, "user_key": jwt_user_key, "team_key": None, "sources": []}
    if jwt_user_key:
        result["sources"].append("jwt")

    def _extract_team_from_user(u: Dict[str, Any]) -> Optional[str]:
        for f in list(u.keys()):
            if f.lower().endswith("teamkey") and u.get(f):
                return str(u[f])
        for f in ("Teams", "teams", "UserTeams", "userTeams"):
            val = u.get(f)
            if isinstance(val, list) and val:
                first = val[0]
                if isinstance(first, dict):
                    for k in ("TeamKey","teamKey","Key","key","Id","id"):
                        if k in first and first[k]:
                            return str(first[k])
        return None

    async with _client() as client:
        if email:
            for base in BASES:
                for coll in _ODATA_USER_COLLECTIONS:
                    u = await _odata_first(client, base, coll, token, _odata_filter(email))
                    if u:
                        uk = _pluck_any(u, _USER_KEY_FIELDS)
                        if uk:
                            result["user_key"] = uk
                            if "jwt" not in result["sources"]:
                                result["sources"].append("odata_user")
                        tk = _extract_team_from_user(u)
                        if tk:
                            result["team_key"] = tk
                            result["sources"].append("user_team_field")
                        break

        if result["user_key"] and not result["team_key"]:
            uk = result["user_key"]
            membership_filters = [
                f"Users/any(u: u/Key eq '{uk}')",
                f"Members/any(u: u/Key eq '{uk}')"
            ]
            for base in BASES:
                for coll in _ODATA_TEAM_COLLECTIONS:
                    for mf in membership_filters:
                        params = {"$filter": mf, "$top": "1"}
                        t = await _odata_first(client, base, coll, token, params)
                        if t:
                            tk = _pluck_any(t, _TEAM_KEY_FIELDS)
                            if tk:
                                result["team_key"] = tk
                                result["sources"].append("odata_team_membership")
                                break
                    if result["team_key"]:
                        break
                if result["team_key"]:
                    break

    if result["user_key"] and not result["team_key"]:
        users = await list_users(token)
        teams = await list_teams(token)
        uk = result["user_key"]

        uname = None
        for u in users:
            if str(u.get("key")) == uk:
                uname = u.get("userName") or u.get("loginName") or None
                break

        for t in teams:
            if str(t.get("key")) == uk:
                result["team_key"] = uk
                result["sources"].append("crm_teams_key_equals_userkey")
                break

        if not result["team_key"] and uname:
            private_name = f"Private ({uname})"
            for t in teams:
                if t.get("name") == private_name:
                    result["team_key"] = str(t.get("key"))
                    result["sources"].append("crm_teams_private_name")
                    break

        if not result["team_key"] and uname:
            low = uname.lower()
            for t in teams:
                n = (t.get("name") or "").lower()
                if low in n:
                    result["team_key"] = str(t.get("key"))
                    result["sources"].append("crm_teams_name_contains_user")
                    break

    return result
