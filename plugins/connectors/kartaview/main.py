"""Street-level photo METADATA near a point, from KartaView (formerly OpenStreetCam).

READ THIS FIRST. A LIVE CHECK ON 2026-11-08 FOUND NEITHER GENERATION REACHABLE FROM HERE: POST kartaview.org/1.0/
list/nearby-photos/ answered HTTP 404, and api.openstreetcam.org failed TLS verification (its certificate names only
*.apolloservice.openstreetcam.org; verification is never switched off). Both cases are reported as UNKNOWN, as they
should be, so this connector currently returns nothing useful against the public service. It is kept, tested against
fixtures, so that a repaired or relocated endpoint only needs `base_url` changed.

KartaView's official API documentation could not be read when this was written (doc.kartaview.org redirects to a
page that needs JavaScript), so this is written from CODE that uses the API, not from a reference. Two generations
are in use and both are supported, chosen by config `api`:

  1.0-bbox  POST {base}/1.0/list/nearby-photos/, form fields ipp, page, bbTopLeft="north,west",
            bbBottomRight="south,east"; answer {"currentPageItems": [{id, lat, lng, heading, shot_date, date_added,
            username, name (an image path), sequence_id, sequence_index}]}. Source: the iD editor's KartaView service
            (openstreetmap/iD, modules/services/kartaview.ts), which sends no key. Default.
  2.0-nearby GET {base}/2.0/photo/ with lat, lng, zoomLevel, radius, limit, orderBy, orderDirection; answer
            {"status": {apiCode, httpCode, ...}, "result": {"data": [{id, lat, lng, heading, shotDate, fileurlProc,
            fileurlTh, sequence{id}}]}}. Source: the kartaview GitHub organisation's own client (mcp-karta-view).
            Its default host, api.openstreetcam.org, answered a TLS certificate-name mismatch to the research request
            that was made; TLS verification is never switched off here, so that case reports UNKNOWN.

Unconfirmed and therefore handled defensively: whether either generation is still served; rate limits (a secondary
source says 100 requests an hour without an account); whether lat/lng/heading arrive as numbers or strings (both are
accepted); the exact success code of the 2.0 envelope (600 and 200 are accepted, anything else is UNKNOWN, never an
empty result). A service that has gone quiet or changed shape therefore produces "unknown", not "no photos".

Only metadata is recorded. Image paths and thumbnail URLs are recorded as the service gave them and are never fetched
or rewritten (a secondary report says some storage URLs now return 404): pulling an image into the case is a separate,
deliberate capture step, because photos of streets contain people and number plates. The contributor's username is a
third party's public handle, recorded because provenance needs it. Imagery is CC BY-SA. A photo's absence says
nothing about a place.
"""

import re
import urllib.parse

from astral import sdk

OK_CODES = (600, 200)
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _num(v, lo, hi):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if lo <= x <= hi else None


def _day(v):
    return v[:10] if isinstance(v, str) and DAY.match(v) else None


def _photos(api, body):
    """(items, problem). items are dicts with the fields this connector records, or a problem string."""
    if api == "1.0-bbox":
        raw = body.get("currentPageItems") if isinstance(body, dict) else None
        if not isinstance(raw, list):
            return None, "not a KartaView 1.0 answer (no currentPageItems list)"
        return [{"id": r.get("id"), "lat": r.get("lat"), "lng": r.get("lng"), "heading": r.get("heading"), "when": r.get("shot_date"),
                 "user": r.get("username"), "path": r.get("name"), "sequence": r.get("sequence_id")} for r in raw if isinstance(r, dict)], None
    st = body.get("status") if isinstance(body, dict) else None
    if not isinstance(st, dict):
        return None, "not a KartaView 2.0 answer (no status envelope)"
    if st.get("apiCode") not in OK_CODES:
        return None, f"KartaView answered apiCode {st.get('apiCode')!r} ({str(st.get('apiMessage'))[:100]}); no photo list was returned"
    raw = (body.get("result") or {}).get("data") if isinstance(body.get("result"), dict) else None
    if not isinstance(raw, list):
        return None, "not a KartaView 2.0 answer (no result.data list)"
    return [{"id": r.get("id"), "lat": r.get("lat"), "lng": r.get("lng"), "heading": r.get("heading"), "when": r.get("shotDate"),
             "user": None, "path": r.get("fileurlProc") or r.get("fileurlTh"), "sequence": (r.get("sequence") or {}).get("id")}
            for r in raw if isinstance(r, dict)], None


def invoke(request):
    inp, cfg = request["input"], request["config"]
    ns = request["context"]["entity_namespace"]
    api = cfg["api"]
    try:
        lat, lon = sdk.lat_lon(inp)
    except ValueError as e:
        yield sdk.result(status="error", error=str(e))
        return
    base = cfg["base_url"].rstrip("/")
    w, s, e_, n = sdk.bbox(lat, lon, cfg["radius_m"] / 1000.0)
    try:
        if api == "1.0-bbox":
            form = {"ipp": cfg["limit"], "page": 1, "bbTopLeft": f"{n:.6f},{w:.6f}", "bbBottomRight": f"{s:.6f},{e_:.6f}"}
            res = yield from sdk.fetch(f"{base}/1.0/list/nearby-photos/", method="POST",
                                       headers={"Content-Type": "application/x-www-form-urlencoded"}, body_text=urllib.parse.urlencode(form))
        else:
            res = yield from sdk.fetch(f"{base}/2.0/photo/", params={"lat": f"{lat:.6f}", "lng": f"{lon:.6f}", "zoomLevel": 16, "radius": cfg["radius_m"],
                                                                      "limit": cfg["limit"], "orderBy": "id", "orderDirection": "desc"})
        if not 200 <= res["status"] < 300:
            raise sdk.CapabilityFailed(f"HTTP {res['status']} from KartaView")
        import json

        try:
            body = json.loads(res.get("text") or "")
        except ValueError as err:
            raise sdk.CapabilityFailed("KartaView returned a body that is not JSON") from err
        items, problem = _photos(api, body)
        if problem:
            raise sdk.CapabilityFailed(problem)
    except sdk.CapabilityFailed as err:
        yield sdk.result(coverage=[sdk.coverage("KartaView", unknown=str(err))])
        return
    ev, out, kept, bad = [res["sha256"]], [], 0, 0
    for p in items:
        la, lo, pid = _num(p["lat"], -90, 90), _num(p["lng"], -180, 180), p["id"]
        if la is None or lo is None or pid in (None, ""):
            bad += 1
            continue
        kept += 1
        eid = sdk.entity_id(ns, "kartaview-photo", str(pid))
        out += [sdk.a_value(eid, "schema", "Image", ev, 2), sdk.a_value(eid, "name", f"KartaView photo {pid}", ev, 2),
                sdk.a_value(eid, "kartaviewId", str(pid), ev, 2), sdk.a_value(eid, "latitude", la, ev, 2), sdk.a_value(eid, "longitude", lo, ev, 2)]
        if _day(p["when"]):
            out.append(sdk.a_value(eid, "date", _day(p["when"]), ev, 2))
        hd = _num(p["heading"], 0, 360)
        if hd is not None:
            out.append(sdk.a_value(eid, "bearingDegrees", hd, ev, 3))
        if isinstance(p["user"], str) and p["user"]:
            out.append(sdk.a_value(eid, "author", p["user"][:120], ev, 2))
        if isinstance(p["path"], str) and p["path"]:
            out.append(sdk.a_value(eid, "imagePath", p["path"][:300], ev, 2))
        if p["sequence"] not in (None, ""):
            out.append(sdk.a_value(eid, "kartaviewSequence", str(p["sequence"]), ev, 2))
    if out:
        yield sdk.emit(out)
    cov = [sdk.coverage("KartaView", hits=kept, note="photo metadata only; no image was fetched. No photo does not mean nothing is there. "
                                                     "Not run against the live service when this connector was written")]
    if len(items) >= cfg["limit"]:
        cov.append(sdk.coverage("KartaView limit", unknown=f"the {cfg['limit']}-photo limit was reached; more may exist"))
    if bad:
        cov.append(sdk.coverage("KartaView records", unknown=f"{bad} record(s) had no usable id or position and were not recorded"))
    yield sdk.result(status="partial" if bad else "ok", stats={"photos": kept}, coverage=cov)


def selfcheck():
    one, _ = _photos("1.0-bbox", {"currentPageItems": [{"id": 1}]})
    two, why = _photos("2.0-nearby", {"status": {"apiCode": 401}})
    return {"ok": len(one) == 1 and two is None and "401" in why and _day("2020-05-01 10:00:00") == "2020-05-01" and _num("51.5", -90, 90) == 51.5}
