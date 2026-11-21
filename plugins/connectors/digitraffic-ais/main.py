"""Vessel AIS positions from Digitraffic Marine (Fintraffic, Finland), a plain-HTTP AIS source.

Written against Digitraffic's OpenAPI document (meri.digitraffic.fi/swagger/openapi.json, checked 2026-11-07):
GET /api/ais/v1/locations (query: mmsi, latitude, longitude, radius in km, from/to in epoch milliseconds) returns a
GeoJSON FeatureCollection whose features carry geometry.coordinates [lon, lat] and properties mmsi, sog (knots),
cog, navStat, rot, posAcc, raim, timestamp, timestampExternal (epoch ms), heading. GET /api/ais/v1/vessels/{mmsi}
returns name, imo, callSign, shipType, destination and so on. Fixture-verified only; the live service was not
contacted.

COVERAGE IS REGIONAL. The feed is the Baltic/Finnish AIS network: a vessel elsewhere, or one that is not
transmitting, is simply absent, and absence is not evidence it was not there. AIS is self-reported: positions can be
wrong, spoofed or switched off. The OpenAPI document marks speed 102.3, course 360, heading 511 and rate of turn -128
as "not available"; those values are dropped, not recorded as real. Gzip is mandatory on this service, so the
request asks for it (the host decodes it). Digitraffic-User carries a generic client name, never a person.
"""

from astral import sdk

HEADERS = {"Accept-Encoding": "gzip", "Digitraffic-User": "AstralLens/connector"}
NOT_AVAILABLE = {"sog": 102.3, "cog": 360, "heading": 511}


def _feature_ok(f):
    g = f.get("geometry") if isinstance(f, dict) else None
    p = f.get("properties") if isinstance(f, dict) else None
    c = g.get("coordinates") if isinstance(g, dict) else None
    return (isinstance(p, dict) and isinstance(p.get("mmsi"), int) and isinstance(c, list) and len(c) >= 2
            and all(isinstance(x, (int, float)) for x in c[:2]) and -90 <= c[1] <= 90 and -180 <= c[0] <= 180)


def invoke(request):
    inp, cfg = request["input"], request["config"]
    ns = request["context"]["entity_namespace"]
    base = cfg["base_url"].rstrip("/")
    mmsi = None
    if inp.get("mmsi") not in (None, ""):
        if not str(inp["mmsi"]).strip().isdigit() or len(str(inp["mmsi"]).strip()) != 9:
            yield sdk.result(status="error", error="an MMSI is nine digits")
            return
        mmsi = int(str(inp["mmsi"]).strip())
        params = {"mmsi": mmsi}
    else:
        try:
            lat, lon = sdk.lat_lon(inp)
        except ValueError as e:
            yield sdk.result(status="error", error=str(e))
            return
        params = {"latitude": f"{lat:.5f}", "longitude": f"{lon:.5f}", "radius": cfg["radius_km"]}
    try:
        res, body = yield from sdk.fetch_json(f"{base}/api/ais/v1/locations", params=params, headers=HEADERS)
        if res.get("truncated"):
            raise sdk.CapabilityFailed("the answer was larger than the fetch limit and was cut off")
        if not isinstance(body, dict) or body.get("type") != "FeatureCollection" or not isinstance(body.get("features"), list):
            raise sdk.CapabilityFailed("not a Digitraffic locations answer (no GeoJSON FeatureCollection)")
    except sdk.CapabilityFailed as e:
        yield sdk.result(coverage=[sdk.coverage("Digitraffic AIS", unknown=str(e))])
        return
    feats = [f for f in body["features"] if _feature_ok(f)]
    bad = len(body["features"]) - len(feats)
    shown = feats[: cfg["max_vessels"]]
    ev, out = [res["sha256"]], []
    meta_note = None
    for f in shown:
        p, (x, y) = f["properties"], f["geometry"]["coordinates"][:2]
        ship = sdk.entity_id(ns, "vessel", str(p["mmsi"]))
        out += [sdk.a_value(ship, "schema", "Vessel", ev, 2), sdk.a_value(ship, "mmsi", str(p["mmsi"]), ev, 2),
                sdk.a_value(ship, "name", f"MMSI {p['mmsi']}", ev, 3)]
        when = sdk.iso_utc(p["timestampExternal"] / 1000.0) if isinstance(p.get("timestampExternal"), (int, float)) else None
        sight = sdk.entity_id(ns, "vessel-position", f"{p['mmsi']}:{p.get('timestampExternal')}")
        out += [sdk.a_value(sight, "schema", "Event", ev, 3), sdk.a_value(sight, "name", f"AIS position of MMSI {p['mmsi']}", ev, 3),
                sdk.a_value(sight, "latitude", y, ev, 3), sdk.a_value(sight, "longitude", x, ev, 3), sdk.a_edge(sight, "involved", ship, ev, 3)]
        if when:
            out.append(sdk.a_value(sight, "date", when, ev, 3))
        for key, pred in (("sog", "speedOverGroundKnots"), ("cog", "courseOverGroundDegrees"), ("heading", "headingDegrees")):
            v = p.get(key)
            if isinstance(v, (int, float)) and v != NOT_AVAILABLE[key]:
                out.append(sdk.a_value(sight, pred, v, ev, 3))
        if isinstance(p.get("navStat"), int):
            out.append(sdk.a_value(sight, "navigationStatusCode", p["navStat"], ev, 3))
    if mmsi is not None and shown:                         # one vessel asked for: its registered particulars too
        try:
            mres, meta = yield from sdk.fetch_json(f"{base}/api/ais/v1/vessels/{mmsi}", headers=HEADERS)
            if isinstance(meta, dict) and meta.get("mmsi") == mmsi:
                ship = sdk.entity_id(ns, "vessel", str(mmsi))
                mev = [mres["sha256"]]
                for key, pred in (("name", "name"), ("callSign", "callSign"), ("destination", "destination")):
                    if isinstance(meta.get(key), str) and meta[key].strip():
                        out.append(sdk.a_value(ship, pred, meta[key].strip(), mev, 3))
                if isinstance(meta.get("imo"), int) and meta["imo"] > 0:
                    out.append(sdk.a_value(ship, "imoNumber", str(meta["imo"]), mev, 3))
                if isinstance(meta.get("shipType"), int):
                    out.append(sdk.a_value(ship, "shipTypeCode", meta["shipType"], mev, 3))
            else:
                meta_note = "vessel particulars were not in the documented shape"
        except sdk.CapabilityFailed as e:
            meta_note = f"vessel particulars not fetched: {e}"
    if out:
        yield sdk.emit(out)
    cov = [sdk.coverage("Digitraffic AIS", hits=len(shown),
                        note="positions reported by the Finnish/Baltic AIS network and cached by the service; a vessel not listed may be out of "
                             "coverage or not transmitting; AIS is self-reported and can be wrong or spoofed")]
    if len(feats) > len(shown):
        cov.append(sdk.coverage("Digitraffic AIS limit", unknown=f"{len(feats) - len(shown)} more vessels not recorded (max_vessels={cfg['max_vessels']})"))
    if bad:
        cov.append(sdk.coverage("Digitraffic AIS records", unknown=f"{bad} feature(s) did not have the documented shape and were not recorded"))
    if meta_note:
        cov.append(sdk.coverage("Digitraffic vessel particulars", unknown=meta_note))
    yield sdk.result(stats={"vessels": len(shown)}, coverage=cov)


def selfcheck():
    return {"ok": _feature_ok({"geometry": {"coordinates": [24.9, 60.1]}, "properties": {"mmsi": 230000000}})
            and not _feature_ok({"geometry": {"coordinates": [300, 60]}, "properties": {"mmsi": 1}})}
