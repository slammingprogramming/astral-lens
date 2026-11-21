"""Satellite fire detections in a box around a point, from NASA FIRMS.

Written against the FIRMS area API page: /api/area/csv/[MAP_KEY]/[SOURCE]/[west,south,east,north]/
[DAY_RANGE (1-5)], sources such as VIIRS_SNPP_NRT and MODIS_NRT, a free MAP_KEY, 5000 transactions per
10 minutes. That page links to attribute tables instead of listing the CSV columns, so this reads the
header row and REQUIRES latitude, longitude and acq_date; a body without them (FIRMS reports a bad key
in plain text) is UNKNOWN, never "no fires". Other columns are kept only if present. Fixture-verified.

A detection is a thermal anomaly seen by a satellite at a time, with a confidence value; it is not a
confirmed fire, and no detection in the window is not evidence nothing burned (cloud cover, overpass
timing and sensor resolution all hide fires).
"""

import csv
import io

from astral import sdk

OPTIONAL = ("confidence", "frp", "satellite", "instrument", "daynight", "bright_ti4", "bright_t31", "acq_time")


def invoke(request):
    inp, cfg = request["input"], request["config"]
    ns = request["context"]["entity_namespace"]
    key = request["secrets"].get("FIRMS_MAP_KEY")
    if not key:
        yield sdk.result(coverage=[sdk.coverage("NASA FIRMS", unknown="FIRMS_MAP_KEY is not set (a free key is available from FIRMS)")])
        return
    try:
        lat, lon = sdk.lat_lon(inp)
    except ValueError as e:
        yield sdk.result(status="error", error=str(e))
        return
    w, s, e_, n = sdk.bbox(lat, lon, cfg["radius_km"])
    area = f"{w:.4f},{s:.4f},{e_:.4f},{n:.4f}"
    url = f"{cfg['base_url'].rstrip('/')}/api/area/csv/{key}/{cfg['source']}/{area}/{cfg['days']}"
    try:
        res = yield from sdk.fetch(url)
        if not 200 <= res["status"] < 300:
            raise sdk.CapabilityFailed(f"HTTP {res['status']} from FIRMS")
        text = res.get("text") or ""
        rows = list(csv.DictReader(io.StringIO(text)))
        head = set(rows[0]) if rows else set((text.splitlines() or [""])[0].split(","))
        if not {"latitude", "longitude", "acq_date"} <= head:
            raise sdk.CapabilityFailed("FIRMS did not return a detections table: " + text[:120].replace("\n", " "))
    except sdk.CapabilityFailed as e:
        yield sdk.result(coverage=[sdk.coverage("NASA FIRMS", unknown=str(e))])
        return
    ev, out = [res["sha256"]], []
    for r in rows[: cfg["max_detections"]]:
        try:
            la, lo = float(r["latitude"]), float(r["longitude"])
        except ValueError:
            continue
        ident = f"{cfg['source']}:{r['latitude']}:{r['longitude']}:{r['acq_date']}:{r.get('acq_time', '')}"
        det = sdk.entity_id(ns, "firms", ident)
        when = r["acq_date"] + (f"T{r['acq_time'][:2]}:{r['acq_time'][2:4]}:00Z" if len(r.get("acq_time", "")) == 4 and r["acq_time"].isdigit() else "")
        out += [sdk.a_value(det, "schema", "Event", ev, 3), sdk.a_value(det, "name", f"Thermal anomaly ({cfg['source']})", ev, 3),
                sdk.a_value(det, "date", when, ev, 3), sdk.a_value(det, "latitude", la, ev, 3), sdk.a_value(det, "longitude", lo, ev, 3),
                sdk.a_value(det, "detectionSource", cfg["source"], ev, 3)]
        for k in OPTIONAL:
            if r.get(k) not in (None, "") and k != "acq_time":
                out.append(sdk.a_value(det, k, r[k], ev, 3))
    if out:
        yield sdk.emit(out)
    shown = min(len(rows), cfg["max_detections"])
    cov = [sdk.coverage("NASA FIRMS", hits=shown, note=f"thermal anomalies in a ~{cfg['radius_km']} km box over {cfg['days']} day(s); not confirmed fires, and none "
                                                       "does not mean nothing burned")]
    if len(rows) > shown:
        cov.append(sdk.coverage("NASA FIRMS limit", unknown=f"{len(rows) - shown} more detection(s) not recorded"))
    yield sdk.result(stats={"detections": shown}, coverage=cov)


def selfcheck():
    w, s, e, n = sdk.bbox(0.0, 0.0, 111.32)
    return {"ok": abs((n - s) - 2.0) < 1e-6 and w < e}
