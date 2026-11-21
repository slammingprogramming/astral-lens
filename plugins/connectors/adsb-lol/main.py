"""Aircraft currently reported near a point, from the adsb.lol v2 API.

Written against adsb.lol's OpenAPI document: GET /v2/point/{lat}/{lon}/{radius} (lat -90..90, lon
-180..180, radius an integer 0..250, nautical miles per the endpoint summary); response ac[], now, total,
ctime, ptime, msg; aircraft hex, flight, r (registration), t (type), lat, lon, alt_baro, gs, track, seen.
The spec defines no authentication (it says a key will be required in future) and states no rate limit;
the author asks production users to get in touch first. Fixture-verified only.

This is a SNAPSHOT of what volunteer receivers heard at one moment. An aircraft that is not listed may
simply be out of range or not broadcasting; military and some other traffic is filtered or absent. A
flight number is a callsign the pilot set, not proof of a route. Data is ODbL (share-alike).
"""

from astral import sdk


def invoke(request):
    inp, cfg = request["input"], request["config"]
    ns = request["context"]["entity_namespace"]
    try:
        lat, lon = sdk.lat_lon(inp)
    except ValueError as e:
        yield sdk.result(status="error", error=str(e))
        return
    url = f"{cfg['base_url'].rstrip('/')}/v2/point/{lat:.5f}/{lon:.5f}/{int(cfg['radius_nm'])}"
    try:
        res, body = yield from sdk.fetch_json(url)
        if not isinstance(body, dict) or not isinstance(body.get("ac"), list) or not isinstance(body.get("now"), (int, float)):
            raise sdk.CapabilityFailed("not an adsb.lol v2 answer (no ac/now)")
    except sdk.CapabilityFailed as e:
        yield sdk.result(coverage=[sdk.coverage("adsb.lol", unknown=str(e))])
        return
    ev, out, seen_at = [res["sha256"]], [], sdk.iso_utc(body["now"] / 1000.0 if body["now"] > 1e11 else body["now"])
    for ac in body["ac"][: cfg["max_aircraft"]]:
        hexid = str(ac.get("hex") or "").lower().lstrip("~")
        if not hexid:
            continue
        plane = sdk.entity_id(ns, "aircraft", hexid)
        out += [sdk.a_value(plane, "schema", "Airplane", ev, 2), sdk.a_value(plane, "name", (ac.get("r") or hexid).strip(), ev, 2),
                sdk.a_value(plane, "icao24", hexid, ev, 2)]
        for k, pred in (("r", "registrationNumber"), ("t", "model")):
            if ac.get(k):
                out.append(sdk.a_value(plane, pred, str(ac[k]).strip(), ev, 3))
        if ac.get("flight"):
            out.append(sdk.a_value(plane, "callsign", str(ac["flight"]).strip(), ev, 3))
        if isinstance(ac.get("lat"), (int, float)) and isinstance(ac.get("lon"), (int, float)):
            sight = sdk.entity_id(ns, "aircraft-sighting", f"{hexid}:{body['now']}")
            out += [sdk.a_value(sight, "schema", "Event", ev, 3), sdk.a_value(sight, "name", f"{ac.get('flight', '').strip() or hexid} sighting", ev, 3),
                    sdk.a_value(sight, "date", seen_at, ev, 3), sdk.a_value(sight, "latitude", ac["lat"], ev, 3),
                    sdk.a_value(sight, "longitude", ac["lon"], ev, 3), sdk.a_edge(sight, "involved", plane, ev, 3)]
            for k, pred in (("alt_baro", "altitudeBaroFt"), ("gs", "groundSpeedKnots"), ("track", "trackDegrees")):
                if isinstance(ac.get(k), (int, float)):
                    out.append(sdk.a_value(sight, pred, ac[k], ev, 3))
    if out:
        yield sdk.emit(out)
    shown = min(len(body["ac"]), cfg["max_aircraft"])
    cov = [sdk.coverage("adsb.lol", hits=shown, note="aircraft heard by volunteer receivers at this moment; absence is not evidence an aircraft was not there")]
    if len(body["ac"]) > shown:
        cov.append(sdk.coverage("adsb.lol limit", unknown=f"{len(body['ac']) - shown} more aircraft not recorded (max_aircraft={cfg['max_aircraft']})"))
    yield sdk.result(stats={"aircraft": shown, "reported_total": body.get("total")}, coverage=cov)


def selfcheck():
    return {"ok": sdk.iso_utc(0) == "1970-01-01T00:00:00Z"}
