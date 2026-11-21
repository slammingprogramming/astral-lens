"""Helpers for plugin authors. Stdlib only; safe to import in any runtime.

A plugin is a generator. These helpers wrap the `call` round trip so a
connector reads top to bottom:

    def invoke(request):
        page = yield from sdk.fetch_json(url, params={"q": name})
        ...
        yield sdk.emit([...])
        yield sdk.result(coverage=[sdk.coverage("gleif", hits=len(rows))])
"""

from __future__ import annotations

import itertools
import json
import uuid
from typing import Any, Generator

_ids = itertools.count(1)


class CapabilityFailed(Exception):
    """Core refused or failed a capability call. A connector turns this into a
    coverage entry with outcome 'unknown', never into 'no results'."""


def call(capability: str, **args: Any) -> Generator[dict, dict, dict]:
    reply = yield {"op": "call", "id": f"c{next(_ids)}", "capability": capability, "args": args}
    if not reply or not reply.get("ok"):
        raise CapabilityFailed((reply or {}).get("error") or "no reply from host")
    return reply["result"]


def fetch(url: str, **kw: Any) -> Generator[dict, dict, dict]:
    return (yield from call("http.fetch", url=url, **kw))


def fetch_json(url: str, **kw: Any) -> Generator[dict, dict, tuple[dict, Any]]:
    """GET and parse. Returns (fetch_result, parsed_json). A non-2xx status or
    unparseable body raises CapabilityFailed so the caller reports UNKNOWN."""
    res = yield from fetch(url, **kw)
    if not 200 <= res["status"] < 300:
        raise CapabilityFailed(f"HTTP {res['status']} from {_host(url)}")
    try:
        return res, json.loads(res.get("text") or "")
    except ValueError as e:
        raise CapabilityFailed(f"{_host(url)} returned a body that is not JSON") from e


def parquet_query(url: str, **kw: Any) -> Generator[dict, dict, dict]:
    """Rows from a remote Parquet file by bounded range requests (columns, bbox, where, limit)."""
    return (yield from call("parquet.query", url=url, **kw))


def ws_collect(url: str, **kw: Any) -> Generator[dict, dict, dict]:
    """Listen on a WebSocket for a bounded time (send_text, max_seconds, max_messages, max_bytes)."""
    return (yield from call("ws.collect", url=url, **kw))


def put_artifact(text: str | None = None, *, data_b64: str | None = None,
                 media_type: str | None = None, acquired_from: str | None = None,
                 parent_sha256: str | None = None, derivation: str | None = None,
                 ctx: dict | None = None) -> Generator[dict, dict, dict]:
    return (yield from call("artifact.put", text=text, data_b64=data_b64,
                            media_type=media_type, acquired_from=acquired_from,
                            parent_sha256=parent_sha256, derivation=derivation, ctx=ctx or {}))


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).hostname or url


# ------------------------------------------------------------------- messages
def emit(assertions: list[dict], call_id: str | None = None) -> dict:
    return {"op": "emit", "call_id": call_id, "assertions": assertions}


def log(msg: str, level: str = "info") -> dict:
    return {"op": "log", "level": level, "msg": msg}


def result(*, status: str = "ok", stats: dict | None = None,
           coverage: list[dict] | None = None, error: str | None = None) -> dict:
    msg: dict[str, Any] = {"op": "result", "status": status, "stats": stats or {}}
    if coverage:
        msg["coverage"] = coverage
    if error:
        msg["error"] = error
    return msg


def coverage(source: str, *, hits: int | None = None, unknown: str | None = None,
             note: str | None = None) -> dict:
    """What a source said. `unknown` (with a reason) means the source could not
    be consulted; zero `hits` means it answered 'nothing found', which is
    reported as such and is not evidence of absence."""
    if unknown:
        return {"source": source, "outcome": "unknown", "note": unknown}
    return {"source": source, "outcome": "hits" if hits else "no_hits", "note": note}


def entity_id(namespace: str, kind: str, key: str) -> str:
    """Deterministic, case-scoped entity id (the host supplies the namespace)."""
    return str(uuid.uuid5(uuid.UUID(namespace), f"{kind}:{key}"))


# ------------------------------------------------------------------- geography
def lat_lon(inp: dict) -> tuple[float, float]:
    """(lat, lon) from a connector input, or ValueError. A string from an entity property is accepted."""
    try:
        la, lo = float(inp["lat"]), float(inp["lon"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError("lat and lon must be numbers") from e
    if not (-90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0):
        raise ValueError("lat must be within -90..90 and lon within -180..180")
    return la, lo


def bbox(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """(west, south, east, north) of a box about a point; longitude is widened with latitude and everything is clamped."""
    import math

    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * max(0.01, math.cos(math.radians(lat))))
    return (max(-180.0, lon - dlon), max(-90.0, lat - dlat), min(180.0, lon + dlon), min(90.0, lat + dlat))


def iso_utc(epoch_seconds: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch_seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Properties whose values are ids of other FollowTheMoney entities.
FTM_REF_PROPS = frozenset({
    "owner", "asset", "director", "organization", "person", "relative", "associate", "agent",
    "client", "employee", "employer", "member", "entity", "sourceEntity", "targetEntity",
    "holder", "parent", "subsidiary", "payer", "beneficiary", "debtor", "creditor", "account"})
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def iso_date(text: str) -> str | None:
    """'1 Jan 1950', '01-JAN-1950', '1950-01', '1950' -> ISO prefix; else None.
    Partial dates stay partial (a year is not January 1st)."""
    import re

    t = text.strip()
    if re.fullmatch(r"\d{4}(-\d{2}){0,2}", t):
        return t
    m = re.fullmatch(r"(\d{1,2})[ -]([A-Za-z]{3})[a-z]*[ ,.-]*(\d{4})", t)
    if m and m.group(2).lower() in _MONTHS:
        return f"{m.group(3)}-{_MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    m = re.fullmatch(r"([A-Za-z]{3})[a-z]* (\d{4})", t)
    if m and m.group(1).lower() in _MONTHS:
        return f"{m.group(2)}-{_MONTHS[m.group(1).lower()]:02d}"
    return None


def ftm_assertions(ns: str, ent: dict, evidence: list[str], *, prefix: str = "ftm",
                   info_grade: int = 2, skip: frozenset[str] = frozenset()) -> tuple[str, list[dict]]:
    """FollowTheMoney entity JSON ({"id","schema","properties":{...}}) -> assertions.
    OpenSanctions and ICIJ's FtM exports share this shape, so they share this."""
    eid = entity_id(ns, prefix, str(ent["id"]))
    out = [a_value(eid, "schema", ent["schema"], evidence, info_grade)]
    props = ent.get("properties") or {}
    if not props.get("name") and ent.get("caption"):
        out.append(a_value(eid, "name", ent["caption"], evidence, info_grade))
    for prop, values in props.items():
        if prop in skip:
            continue
        for v in values:
            if prop in FTM_REF_PROPS:
                out.append(a_edge(eid, prop, entity_id(ns, prefix, str(v)), evidence, info_grade))
            else:
                out.append(a_value(eid, prop, v, evidence, info_grade))
    return eid, out


def a_value(subject: str, predicate: str, value: Any, evidence: list[str],
            info_grade: int = 3, **extra: Any) -> dict:
    return {"subject": subject, "predicate": predicate, "object_value": value,
            "evidence": evidence, "info_grade": info_grade, **extra}


def a_edge(subject: str, predicate: str, obj: str, evidence: list[str],
           info_grade: int = 3, **extra: Any) -> dict:
    return {"subject": subject, "predicate": predicate, "object_entity": obj,
            "evidence": evidence, "info_grade": info_grade, **extra}
