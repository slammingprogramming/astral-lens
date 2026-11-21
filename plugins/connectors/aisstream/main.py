"""A short listen to AISStream's WebSocket for vessel position reports inside a box around a point.

Written against https://aisstream.io/documentation (checked 2026-11-07): connect to wss://stream.aisstream.io/v0/stream
and send one JSON frame {"APIKey", "BoundingBoxes": [[[lat1, lon1], [lat2, lon2]]], "FilterMessageTypes": [...]}
within 3 seconds. Messages are {"MessageType", "MetaData", "Message": {<type>: {...}}}; PositionReport carries
UserID (the MMSI), Latitude, Longitude, Sog, Cog, TrueHeading, NavigationalStatus. Fixture-verified only; the live
service was not contacted and no API key was available.

What this is. A SAMPLE of what the network reported during a few seconds, not a census: a vessel that did not
transmit in that window, is out of coverage, or has AIS switched off is simply absent. The service states it offers
no SLA and does not replay events. AIS is self-reported and can be wrong or spoofed. The documentation does not
say what units the position-report fields use (speed, course) and gives no timestamp in the metadata sample, so
those are recorded as reported, labelled as such, with the time the listen ended. The API key is sent in the
subscription frame and is never stored.
"""

import json
import time

from astral import sdk

NOT_AVAILABLE = {"Sog": 102.3, "Cog": 360, "TrueHeading": 511}


def _report(env):
    if not isinstance(env, dict) or env.get("MessageType") != "PositionReport":
        return None
    m = (env.get("Message") or {}).get("PositionReport")
    if not isinstance(m, dict) or not isinstance(m.get("UserID"), int):
        return None
    lat, lon = m.get("Latitude"), m.get("Longitude")
    if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and -90 <= lat <= 90 and -180 <= lon <= 180):
        return None                                                  # 91/181 are AIS 'not available' markers, not positions
    return m, (env.get("MetaData") if isinstance(env.get("MetaData"), dict) else {})


def invoke(request):
    inp, cfg = request["input"], request["config"]
    ns = request["context"]["entity_namespace"]
    key = request["secrets"].get("AISSTREAM_API_KEY")
    if not key:
        yield sdk.result(coverage=[sdk.coverage("AISStream", unknown="AISSTREAM_API_KEY is not set")])
        return
    try:
        lat, lon = sdk.lat_lon(inp)
    except ValueError as e:
        yield sdk.result(status="error", error=str(e))
        return
    west, south, east, north = sdk.bbox(lat, lon, cfg["radius_km"])
    sub = json.dumps({"APIKey": key, "BoundingBoxes": [[[south, west], [north, east]]], "FilterMessageTypes": ["PositionReport"]})
    try:
        res = yield from sdk.ws_collect(cfg["url"], send_text=sub, max_seconds=cfg["listen_seconds"], max_messages=2000)
    except sdk.CapabilityFailed as e:
        yield sdk.result(coverage=[sdk.coverage("AISStream", unknown=str(e))])
        return
    seen_at = sdk.iso_utc(time.time())
    latest, junk, other = {}, 0, 0
    for line in (res.get("text") or "").splitlines():
        try:
            env = json.loads(line)
        except ValueError:
            junk += 1
            continue
        if isinstance(env, dict) and env.get("MessageType") == "SubscriptionConfirmation":
            continue
        if isinstance(env, dict) and isinstance(env.get("error"), str):
            yield sdk.result(coverage=[sdk.coverage("AISStream", unknown=f"AISStream refused the subscription: {env['error'][:160]}")])
            return
        rep = _report(env)
        if rep is None:
            other += 1
            continue
        latest[rep[0]["UserID"]] = rep                                 # the last position heard for each vessel in the window
    ev, out = [res["sha256"]], []
    for mmsi, (m, meta) in list(latest.items())[: cfg["max_vessels"]]:
        ship = sdk.entity_id(ns, "vessel", str(mmsi))
        name = meta.get("ShipName").strip() if isinstance(meta.get("ShipName"), str) and meta["ShipName"].strip() else f"MMSI {mmsi}"
        out += [sdk.a_value(ship, "schema", "Vessel", ev, 2), sdk.a_value(ship, "mmsi", str(mmsi), ev, 2), sdk.a_value(ship, "name", name, ev, 3)]
        sight = sdk.entity_id(ns, "vessel-position", f"{mmsi}:{seen_at}")
        out += [sdk.a_value(sight, "schema", "Event", ev, 3), sdk.a_value(sight, "name", f"AIS position of MMSI {mmsi}", ev, 3),
                sdk.a_value(sight, "latitude", m["Latitude"], ev, 3), sdk.a_value(sight, "longitude", m["Longitude"], ev, 3),
                sdk.a_value(sight, "date", seen_at, ev, 4), sdk.a_edge(sight, "involved", ship, ev, 3)]
        for key_, pred in (("Sog", "speedOverGroundReported"), ("Cog", "courseOverGroundReported"), ("TrueHeading", "headingReported")):
            v = m.get(key_)
            if isinstance(v, (int, float)) and v != NOT_AVAILABLE[key_]:
                out.append(sdk.a_value(sight, pred, v, ev, 3))
        if isinstance(m.get("NavigationalStatus"), int):
            out.append(sdk.a_value(sight, "navigationStatusCode", m["NavigationalStatus"], ev, 3))
    if out:
        yield sdk.emit(out)
    shown = min(len(latest), cfg["max_vessels"])
    cov = [sdk.coverage("AISStream", hits=shown,
                        note=f"vessels heard in a {cfg['listen_seconds']}-second window; a vessel not listed may not have transmitted, may be out of "
                             "coverage or have AIS off; the service has no uptime guarantee; AIS is self-reported")]
    if len(latest) > shown:
        cov.append(sdk.coverage("AISStream limit", unknown=f"{len(latest) - shown} more vessels heard and not recorded (max_vessels={cfg['max_vessels']})"))
    if res.get("truncated"):
        cov.append(sdk.coverage("AISStream window", unknown=f"listening stopped early at the {res.get('stopped_by')} limit; more traffic existed"))
    if junk or other:
        cov.append(sdk.coverage("AISStream messages", unknown=f"{junk} unreadable and {other} unusable message(s) were not recorded"))
    yield sdk.result(stats={"vessels": shown, "messages": res.get("count")}, coverage=cov)


def selfcheck():
    return {"ok": _report({"MessageType": "PositionReport", "Message": {"PositionReport": {"UserID": 1, "Latitude": 60.0, "Longitude": 24.0}}}) is not None
            and _report({"MessageType": "PositionReport", "Message": {"PositionReport": {"UserID": 1, "Latitude": 91, "Longitude": 181}}}) is None}
