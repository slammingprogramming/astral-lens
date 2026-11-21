"""OpenStreetMap features around a point, from an Overpass API server.

Written against the OSM wiki's Overpass API and Overpass QL pages: POST with a form field `data`,
`[out:json]`, `nwr(around:radius,lat,lon)`, `out center tags N;`, top-level keys version/generator/
osm3s/elements, each element with type/id/tags. The pages do NOT document where coordinates appear
in the JSON; this reads `lat`/`lon` on nodes and `center.lat`/`center.lon` on ways and relations (the
layout `out center` produces) and counts any element it cannot place rather than guessing a position.
Fixture-verified only.

Overpass answers 200 with a "remark" when a query runs out of time or memory, so a remark that names an
error is UNKNOWN, never an empty result. 429/406 mean the server is busy: the wiki asks for a 30 s wait.
Features are what mappers recorded: absence of a feature says nothing about what exists on the ground.
Data is ODbL; derived databases are share-alike.
"""

import re
import urllib.parse

from astral import sdk

SELECTOR = re.compile(r'^[A-Za-z_:]{1,40}(=[A-Za-z0-9_ :.\-]{1,60})?$')
KEEP_TAGS = ("name", "amenity", "shop", "building", "highway", "tourism", "office", "operator", "brand", "addr:street", "addr:housenumber", "website")


def _query(lat, lon, radius, selector, limit):
    if selector:
        key, _, val = selector.partition("=")
        flt = f'["{key}"="{val}"]' if val else f'["{key}"]'
    else:
        flt = "[name]"
    return f"[out:json][timeout:25];nwr(around:{radius},{lat:.6f},{lon:.6f}){flt};out center tags {limit};"


def invoke(request):
    inp, cfg = request["input"], request["config"]
    ns = request["context"]["entity_namespace"]
    try:
        lat, lon = sdk.lat_lon(inp)
    except ValueError as e:
        yield sdk.result(status="error", error=str(e))
        return
    sel = cfg["selector"].strip()
    if sel and not SELECTOR.match(sel):
        yield sdk.result(status="error", error="selector must be key or key=value")
        return
    q = _query(lat, lon, cfg["radius_m"], sel, cfg["limit"])
    try:
        res = yield from sdk.fetch(cfg["base_url"], method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"},
                                   body_text=urllib.parse.urlencode({"data": q}))
        if res["status"] in (429, 406):
            raise sdk.CapabilityFailed(f"Overpass is busy (HTTP {res['status']}); wait 30 seconds before retrying")
        if not 200 <= res["status"] < 300:
            raise sdk.CapabilityFailed(f"HTTP {res['status']} from Overpass")
        import json

        body = json.loads(res.get("text") or "")
        if not isinstance(body, dict) or "elements" not in body:
            raise sdk.CapabilityFailed("not an Overpass JSON answer")
        if "error" in str(body.get("remark", "")).lower():
            raise sdk.CapabilityFailed(f"Overpass reported: {body['remark']}")
    except (sdk.CapabilityFailed, ValueError) as e:
        yield sdk.result(coverage=[sdk.coverage("OpenStreetMap (Overpass)", unknown=str(e))])
        return
    ev, out, unplaced = [res["sha256"]], [], 0
    for el in body["elements"]:
        pos = (el.get("lat"), el.get("lon")) if el.get("type") == "node" else ((el.get("center") or {}).get("lat"), (el.get("center") or {}).get("lon"))
        if not all(isinstance(x, (int, float)) for x in pos):
            unplaced += 1
            continue
        tags = el.get("tags") or {}
        eid = sdk.entity_id(ns, "osm", f"{el['type']}/{el['id']}")
        out += [sdk.a_value(eid, "schema", "Address", ev, 3), sdk.a_value(eid, "name", tags.get("name") or f"{el['type']} {el['id']}", ev, 3),
                sdk.a_value(eid, "latitude", pos[0], ev, 3), sdk.a_value(eid, "longitude", pos[1], ev, 3),
                sdk.a_value(eid, "osmId", f"{el['type']}/{el['id']}", ev, 3)]
        for k in KEEP_TAGS:
            if k != "name" and tags.get(k):
                out.append(sdk.a_value(eid, "osmTag", f"{k}={str(tags[k])[:120]}", ev, 3))
    if out:
        yield sdk.emit(out)
    n = len(body["elements"]) - unplaced
    cov = [sdk.coverage("OpenStreetMap (Overpass)", hits=n, note="features mappers recorded near this point; no feature does not mean nothing is there")]
    if unplaced:
        cov.append(sdk.coverage("OpenStreetMap positions", unknown=f"{unplaced} element(s) had no usable position in the answer and were not recorded"))
    if len(body["elements"]) >= cfg["limit"]:
        cov.append(sdk.coverage("OpenStreetMap limit", unknown=f"the {cfg['limit']}-feature limit was reached; more may exist"))
    yield sdk.result(stats={"features": n}, coverage=cov)


def selfcheck():
    return {"ok": "around:250,10.000000,20.000000" in _query(10.0, 20.0, 250, "", 5) and bool(SELECTOR.match("amenity=cafe")) and not SELECTOR.match('a"];out;')}
