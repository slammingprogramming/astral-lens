"""Satellite products (Sentinel scenes) covering a point, from the Copernicus Data Space OData catalogue.

Written against the catalogue documentation: base https://catalogue.dataspace.copernicus.eu/odata/v1/
Products, $filter with `Collection/Name eq '...'`, `OData.CSC.Intersects(area=geography'SRID=4326;
POINT(lon lat)')` and `ContentDate/Start gt|lt <ISO 8601>`, plus $top and $orderby; results in `value[]`
with Id, Name, S3Path and GeoFootprint. The part of the page read here does not show ContentDate, so it
is recorded only when present; the page's examples are unauthenticated and its authentication and
rate-limit text was not read. Fixture-verified only.

This lists what exists in the archive, which is METADATA: it does not download imagery. A scene
covering a point says only that the satellite passed over; cloud cover, resolution and acquisition
time decide whether it shows anything at all.
"""

import re
from datetime import datetime, timedelta, timezone

from astral import sdk

COLLECTION = re.compile(r"^[A-Z0-9][A-Z0-9-]{2,30}$")


def invoke(request):
    inp, cfg = request["input"], request["config"]
    ns = request["context"]["entity_namespace"]
    try:
        lat, lon = sdk.lat_lon(inp)
    except ValueError as e:
        yield sdk.result(status="error", error=str(e))
        return
    if not COLLECTION.match(cfg["collection"]):
        yield sdk.result(status="error", error="collection must look like SENTINEL-2")
        return
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=cfg["days_back"])).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    flt = (f"Collection/Name eq '{cfg['collection']}' and OData.CSC.Intersects(area=geography'SRID=4326;POINT({lon:.5f} {lat:.5f})')"
           f" and ContentDate/Start gt {start}")
    try:
        res, body = yield from sdk.fetch_json(cfg["base_url"].rstrip("/") + "/odata/v1/Products",
                                              params={"$filter": flt, "$top": cfg["top"], "$orderby": "ContentDate/Start desc"})
        if not isinstance(body, dict) or not isinstance(body.get("value"), list):
            raise sdk.CapabilityFailed("not an OData answer (no value array)")
    except sdk.CapabilityFailed as e:
        yield sdk.result(coverage=[sdk.coverage("Copernicus Data Space", unknown=str(e))])
        return
    ev, out = [res["sha256"]], []
    for p in body["value"]:
        if not p.get("Id") or not p.get("Name"):
            continue
        sc = sdk.entity_id(ns, "cdse-product", str(p["Id"]))
        out += [sdk.a_value(sc, "schema", "SatelliteProduct", ev, 2), sdk.a_value(sc, "name", p["Name"], ev, 2),
                sdk.a_value(sc, "productId", str(p["Id"]), ev, 2), sdk.a_value(sc, "collection", cfg["collection"], ev, 2)]
        cd = p.get("ContentDate") or {}
        for k, pred in (("Start", "acquisitionStart"), ("End", "acquisitionEnd")):
            if cd.get(k):
                out.append(sdk.a_value(sc, pred, cd[k], ev, 2))
        if p.get("S3Path"):
            out.append(sdk.a_value(sc, "archivePath", p["S3Path"], ev, 2))
        if p.get("GeoFootprint"):
            out.append(sdk.a_value(sc, "footprintGeoJson", str(p["GeoFootprint"])[:2000], ev, 2))
        if isinstance(p.get("ContentLength"), int):
            out.append(sdk.a_value(sc, "contentLengthBytes", p["ContentLength"], ev, 2))
    if out:
        yield sdk.emit(out)
    n = len([p for p in body["value"] if p.get("Id") and p.get("Name")])
    cov = [sdk.coverage("Copernicus Data Space", hits=n, note=f"{cfg['collection']} scenes over the point in the last {cfg['days_back']} days (metadata only; "
                                                              "coverage is not proof the scene shows anything)")]
    if len(body["value"]) >= cfg["top"]:
        cov.append(sdk.coverage("Copernicus limit", unknown=f"the {cfg['top']}-product limit was reached; older scenes may exist"))
    yield sdk.result(stats={"products": n}, coverage=cov)


def selfcheck():
    return {"ok": bool(COLLECTION.match("SENTINEL-2")) and not COLLECTION.match("x' or '1'='1")}
