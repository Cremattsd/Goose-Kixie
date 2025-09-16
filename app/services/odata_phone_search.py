# app/services/odata_phone_search.py
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple, Set
import httpx

# OData param your tenant(s) require
_ODATA_DEFAULT_PARAMS = {"api-version": "1.0"}

def _merge_params(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if a: out.update(a)
    if b: out.update(b)
    return out

def _like_phone_name(name: str) -> bool:
    n = name.lower()
    return any(kw in n for kw in ["phone","mobile","cell","workphone","homephone","assistant","fax","telephone","tel"])

def _is_fax_field(name: str) -> bool:
    return "fax" in (name or "").lower()

def _is_mobile_field(name: str) -> bool:
    n = (name or "").lower()
    return any(k in n for k in ["mobile","cell"])

def _truthy(v: Any) -> bool:
    if isinstance(v, bool): return v
    if isinstance(v, (int, float)): return v != 0
    if isinstance(v, str): return v.strip().lower() in {"1","true","yes","y","t"}
    return False

# ---- Host app plumbing (import from realnex_api) ----
from .realnex_api import BASES, _client, _send, _format_resp  # noqa: E402

# ---------- Seeds ----------
DEFAULT_FIELD_SEEDS = {
    "Contacts": ["Mobile","Fax","Phone","Phone1","Phone2","Phone3","AssistantPhone","HomePhone","WorkPhone"],
    "Companies": ["Phone","MainPhone","Fax","Mobile","WorkPhone","OfficePhone"],
    "Projects":  ["Phone","MainPhone","Fax","Mobile"],
}

DEFAULT_DNC_FIELD_SEEDS = {
    "Contacts": {
        "call": ["DoNotCall","Dnc","DNC","DoNotPhone","NoCall","NoPhone","DoNotContactPhone"],
        "fax":  ["DoNotFax","NoFax"],
        "text": ["DoNotText","DoNotSMS","NoSMS","NoText"],
    },
    "Companies": {
        "call": ["DoNotCall","NoCall","NoPhone"],
        "fax":  ["DoNotFax","NoFax"],
        "text": ["DoNotText","NoText","NoSMS","DoNotSMS"],
    },
    "Projects": {
        "call": ["DoNotCall","NoCall"],
        "fax":  ["DoNotFax","NoFax"],
        "text": ["DoNotText","NoText"],
    },
}

# ---------- OData capability probes ----------
async def _odata_field_is_selectable(token: str, entity: str, field: str) -> bool:
    async with _client() as client:
        for base in BASES:
            if not base.lower().endswith(("crmodata","odata")):
                continue
            url = f"{base.rstrip('/')}/{entity}"
            try:
                r = await _send(
                    client, "GET", url, token,
                    params=_merge_params({"$select": field, "$top": "1"}, _ODATA_DEFAULT_PARAMS),
                )
                if r.status_code < 400:
                    return True
            except httpx.HTTPError:
                pass
    return False

async def _odata_guess_fields_from_sample(token: str, entity: str) -> List[str]:
    async with _client() as client:
        for base in BASES:
            if not base.lower().endswith(("crmodata","odata")):
                continue
            url = f"{base.rstrip('/')}/{entity}"
            try:
                r = await _send(client, "GET", url, token, params=_merge_params({"$top": "1"}, _ODATA_DEFAULT_PARAMS))
                if r.status_code >= 400:
                    continue
                payload = await _format_resp(r)
                values = payload.get("value") or payload.get("data") or []
                if isinstance(values, list) and values and isinstance(values[0], dict):
                    keys = [k for k in values[0].keys() if isinstance(k, str) and _like_phone_name(k)]
                    valid = []
                    for k in keys:
                        if await _odata_field_is_selectable(token, entity, k):
                            valid.append(k)
                    return valid
            except httpx.HTTPError:
                continue
    return []

async def _field_supports_contains(token: str, entity: str, field: str) -> Tuple[bool, bool]:
    """Returns (works_without_cast, works_with_cast)."""
    async with _client() as client:
        for base in BASES:
            if not base.lower().endswith(("crmodata","odata")):
                continue
            path = f"{base.rstrip('/')}/{entity}"
            # raw contains
            try:
                r1 = await _send(
                    client, "GET", path, token,
                    params=_merge_params({"$filter": f"contains({field},'0')", "$top": "0"}, _ODATA_DEFAULT_PARAMS),
                )
                if r1.status_code < 400:
                    return True, False
            except httpx.HTTPError:
                pass
            # cast contains
            try:
                r2 = await _send(
                    client, "GET", path, token,
                    params=_merge_params({"$filter": f"contains(cast({field},'Edm.String'),'0')", "$top": "0"}, _ODATA_DEFAULT_PARAMS),
                )
                if r2.status_code < 400:
                    return False, True
            except httpx.HTTPError:
                pass
    return False, False

# ---------- Probing API ----------
async def probe_phone_fields(token: str, entity: str, extra_candidates: Optional[List[str]] = None) -> List[str]:
    seeds = list(DEFAULT_FIELD_SEEDS.get(entity, []))
    if extra_candidates:
        for s in extra_candidates:
            if s not in seeds:
                seeds.append(s)
    validated: List[str] = []
    for f in seeds:
        if await _odata_field_is_selectable(token, entity, f):
            validated.append(f)
    if not validated:
        inferred = await _odata_guess_fields_from_sample(token, entity)
        for x in inferred:
            if x not in validated:
                validated.append(x)
    return validated

async def probe_dnc_fields(token: str, entity: str) -> Dict[str, List[str]]:
    seeds = DEFAULT_DNC_FIELD_SEEDS.get(entity, {})
    out = {"call": [], "fax": [], "text": []}
    for kind, names in seeds.items():
        for f in names:
            if await _odata_field_is_selectable(token, entity, f):
                out[kind].append(f)
    # Best-effort inference from sample if nothing validated
    if not any(out.values()):
        sample = await _odata_guess_fields_from_sample(token, entity)
        for k in sample:
            lk = k.lower()
            if "donot" in lk or lk in {"dnc","nocall","nophone","nofax","nosms","notext"}:
                if "fax" in lk:
                    out["fax"].append(k)
                elif "text" in lk or "sms" in lk:
                    out["text"].append(k)
                else:
                    out["call"].append(k)
    return out

# ---------- Matching & DNC filtering ----------
def _row_matched_fields(row: Dict[str, Any], digits: str, fields: List[str]) -> Set[str]:
    hits: Set[str] = set()
    for f in fields:
        if f in row and row[f] is not None:
            val = re.sub(r"\D+", "", str(row[f]))
            if digits and digits in val:
                hits.add(f)
    return hits

def _passes_dnc(row: Dict[str, Any], matched_fields: Set[str], dnc_fields: Dict[str, List[str]], exclude_fax: bool) -> bool:
    # Global do-not-call?
    for f in dnc_fields.get("call", []):
        if f in row and _truthy(row[f]):
            return False
    # If we only matched fax-y fields, and fax is excluded or explicitly do-not-fax, drop it
    if matched_fields:
        only_fax = all(_is_fax_field(f) for f in matched_fields)
        if only_fax and exclude_fax:
            return False
        if any(_is_fax_field(f) for f in matched_fields):
            for f in dnc_fields.get("fax", []):
                if f in row and _truthy(row[f]):
                    return False
    return True

# ---------- Search paths ----------
async def _try_contains(token: str, entity: str, digits: str, fields: List[str], dnc_fields: Dict[str, List[str]], top: int, exclude_fax: bool) -> Dict[str, Any]:
    usable: List[str] = []
    cast_map: Dict[str, bool] = {}
    for f in fields:
        raw_ok, cast_ok = await _field_supports_contains(token, entity, f)
        if raw_ok or cast_ok:
            usable.append(f)
            cast_map[f] = cast_ok and not raw_ok
    if not usable:
        return {"status": 404, "error": "no_filterable_fields", "fields_tried": fields}

    parts = []
    for f in usable:
        parts.append(f"contains({f},'{digits}')" if not cast_map.get(f) else f"contains(cast({f},'Edm.String'),'{digits}')")
    flt = " or ".join(parts)

    async with _client() as client:
        for base in BASES:
            if not base.lower().endswith(("crmodata","odata")):
                continue
            url = f"{base.rstrip('/')}/{entity}"
            try:
                r = await _send(
                    client, "GET", url, token,
                    params=_merge_params({"$filter": flt, "$top": str(top)}, _ODATA_DEFAULT_PARAMS),
                )
                payload = await _format_resp(r)
                if r.status_code >= 400:
                    return payload
                rows = payload.get("value") or payload.get("data") or []
                if not isinstance(rows, list) or not rows:
                    return {"status": 204, "value": [], "method": "filter", "url": payload.get("url")}
                # DNC filter + fax handling
                kept: List[Dict[str, Any]] = []
                filtered = 0
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    hits = _row_matched_fields(row, digits, usable)
                    if not _passes_dnc(row, hits, dnc_fields, exclude_fax):
                        filtered += 1
                        continue
                    kept.append(row)
                if kept:
                    return {"status": 200, "value": kept, "method": "filter", "filtered_out": filtered, "url": payload.get("url")}
                return {"status": 404, "error": "filtered_all_by_dnc_or_fax", "filtered_out": filtered, "method": "filter"}
            except httpx.HTTPError as e:
                return {"status": 599, "error": str(e)}
    return {"status": 404, "error": "no_odata_base"}

async def _scan_pages(token: str, entity: str, digits: str, fields: List[str], dnc_fields: Dict[str, List[str]], page_top: int = 100, max_pages: int = 10, exclude_fax: bool = True) -> Dict[str, Any]:
    async with _client() as client:
        for base in BASES:
            if not base.lower().endswith(("crmodata","odata")):
                continue
            url = f"{base.rstrip('/')}/{entity}"
            skip = 0
            for _ in range(max_pages):
                try:
                    r = await _send(
                        client, "GET", url, token,
                        params=_merge_params({"$top": str(page_top), "$skip": str(skip)}, _ODATA_DEFAULT_PARAMS),
                    )
                    if r.status_code >= 400:
                        break
                    payload = await _format_resp(r)
                    rows = payload.get("value") or payload.get("data") or []
                    if not isinstance(rows, list) or not rows:
                        break
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        hits = _row_matched_fields(row, digits, fields)
                        if not hits:
                            continue
                        if not _passes_dnc(row, hits, dnc_fields, exclude_fax):
                            continue
                        # Found a viable row
                        return {"status": 200, "value": [row], "method": "scan", "url": payload.get("url")}
                    if len(rows) < page_top:
                        break
                    skip += page_top
                except httpx.HTTPError:
                    break
    return {"status": 404, "error": "scan_no_match"}

# ---------- PUBLIC API ----------
async def search_digits(
    token: str,
    entity: str,
    phone_raw: str,
    extra_candidates: Optional[List[str]] = None,
    top: int = 10,
    exclude_fax: bool = True,
) -> Dict[str, Any]:
    """
    Tenant-safe phone search with DNC awareness.
    """
    digits = re.sub(r"\D+", "", phone_raw or "")
    if not digits:
        return {"status": 400, "error": "no_digits"}

    fields = await probe_phone_fields(token, entity, extra_candidates=extra_candidates)
    if not fields:
        return {"status": 404, "error": "no_valid_phone_fields"}

    dnc_fields = await probe_dnc_fields(token, entity)

    tried = await _try_contains(token, entity, digits, fields, dnc_fields, top, exclude_fax=exclude_fax)
    if int(tried.get("status", 0)) // 100 == 2 and (tried.get("value") or tried.get("data")):
        return tried
    if tried.get("status") in (200, 204, 404, 400, 599):
        scanned = await _scan_pages(token, entity, digits, fields, dnc_fields, page_top=100, max_pages=10, exclude_fax=exclude_fax)
        if int(scanned.get("status", 0)) // 100 == 2:
            return scanned
        if tried.get("status") in (200, 204):
            return {"status": 404, "error": "odata_empty_or_filtered_by_dnc", "probe_fields": fields, "dnc_fields": dnc_fields, "tried": tried, "scan": scanned}
        return {"status": 404, "error": "odata_no_match", "probe_fields": fields, "dnc_fields": dnc_fields, "tried": tried, "scan": scanned}
    return {"status": 404, "error": "unknown_state", "probe_fields": fields, "dnc_fields": dnc_fields}

# ---------- Back-compat exports for existing imports ----------
def digits_only(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    d = re.sub(r"\D+", "", raw)
    return d or None

async def probe_odata_phone_fields(token: str) -> List[str]:
    return await probe_phone_fields(token, "Contacts")

async def odata_contacts_filter_by_digits(token: str, digits: str, fields: List[str], top: int = 5) -> Dict[str, Any]:
    dnc_fields = await probe_dnc_fields(token, "Contacts")
    tried = await _try_contains(token, "Contacts", digits, fields, dnc_fields, top, exclude_fax=True)
    if int(tried.get("status", 0)) // 100 == 2:
        vals = tried.get("value") or tried.get("data") or []
        if isinstance(vals, list) and vals:
            return tried
        scanned = await _scan_pages(token, "Contacts", digits, fields, dnc_fields, page_top=100, max_pages=10, exclude_fax=True)
        if int(scanned.get("status", 0)) // 100 == 2:
            return scanned
        return {"status": 404, "error": "odata_empty_or_filtered_by_dnc", "dnc_fields": dnc_fields, "tried": tried, "scan": scanned}
    scanned = await _scan_pages(token, "Contacts", digits, fields, dnc_fields, page_top=100, max_pages=10, exclude_fax=True)
    if int(scanned.get("status", 0)) // 100 == 2:
        return scanned
    return {"status": tried.get("status", 400), "error": "odata_no_match", "dnc_fields": dnc_fields, "tried": tried, "scan": scanned}
