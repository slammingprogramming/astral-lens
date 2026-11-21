"""Street-level photo METADATA near a point, from the Mapillary Graph API v4.

Written against Mapillary's API documentation: GET https://graph.mapillary.com/images with
bbox=left,bottom,right,top (the box must be smaller than 0.01 degrees square), limit (default and max
2000), fields requested explicitly (id, captured_at in ms since epoch, geometry as a GeoJSON Point,
compass_angle, creator{username,id}, thumb_1024_url, is_pano), response {"data": [...]}, token as
`Authorization: OAuth <token>`. Verified against fixtures only.

Only metadata is recorded. The thumbnail is a URL, never fetched here: pulling an image into the case
is a separate, deliberate capture step, because photos of streets contain people. The contributor's
username is recorded as the author handle because provenance needs it; it is a third party's public
handle, not a subject of the investigation. A photo's absence says nothing about a place.
"""

from astral import sdk

FIELDS = "id,captured_at,geometry,compass_angle,creator,thumb_1024_url,is_pano"


def invoke(request):
    inp, cfg = request["input"], request["config"]
    ns = request["context"]["entity_namespace"]
    token = request["secrets"].get("MAPILLARY_ACCESS_TOKEN")
    if not token:
        yield sdk.result(coverage=[sdk.coverage("Mapillary", unknown="MAPILLARY_ACCESS_TOKEN is not set")])
        return
    try:
        lat, lon = sdk.lat_lon(inp)
    except ValueError as e:
        yield sdk.result(status="error", error=str(e))
        return
    half = min(cfg["radius_m"], 450) / 111320.0                      # keep the whole box under the documented 0.01 degrees
    w, s, e_, n = lon - half, lat - half, lon + half, lat + half
    try:
        res, body = yield from sdk.fetch_json(cfg["base_url"].rstrip("/") + "/images", headers={"Authorization": f"OAuth {token}"},
                                              params={"bbox": f"{w:.6f},{s:.6f},{e_:.6f},{n:.6f}", "fields": FIELDS, "limit": cfg["limit"]})
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise sdk.CapabilityFailed("not a Mapillary answer (no data array)")
    except sdk.CapabilityFailed as e:
        yield sdk.result(coverage=[sdk.coverage("Mapillary", unknown=str(e))])
        return
    ev, out, kept = [res["sha256"]], [], 0
    for img in body["data"]:
        coords = (img.get("geometry") or {}).get("coordinates")
        if not img.get("id") or not (isinstance(coords, list) and len(coords) == 2):
            continue
        kept += 1
        eid = sdk.entity_id(ns, "mapillary-image", str(img["id"]))
        out += [sdk.a_value(eid, "schema", "Image", ev, 2), sdk.a_value(eid, "name", f"Mapillary image {img['id']}", ev, 2),
                sdk.a_value(eid, "mapillaryId", str(img["id"]), ev, 2), sdk.a_value(eid, "longitude", coords[0], ev, 2),
                sdk.a_value(eid, "latitude", coords[1], ev, 2)]
        if isinstance(img.get("captured_at"), (int, float)):
            out.append(sdk.a_value(eid, "date", sdk.iso_utc(img["captured_at"] / 1000.0), ev, 2))
        if isinstance(img.get("compass_angle"), (int, float)):
            out.append(sdk.a_value(eid, "bearingDegrees", img["compass_angle"], ev, 3))
        if img.get("thumb_1024_url"):
            out.append(sdk.a_value(eid, "thumbnailUrl", img["thumb_1024_url"], ev, 2))
        if (img.get("creator") or {}).get("username"):
            out.append(sdk.a_value(eid, "author", img["creator"]["username"], ev, 2))
    if out:
        yield sdk.emit(out)
    cov = [sdk.coverage("Mapillary", hits=kept, note="photo metadata only; the images were not fetched. No photo does not mean nothing is there")]
    if len(body["data"]) >= cfg["limit"]:
        cov.append(sdk.coverage("Mapillary limit", unknown=f"the {cfg['limit']}-image limit was reached; more may exist"))
    yield sdk.result(stats={"images": kept}, coverage=cov)


def selfcheck():
    return {"ok": FIELDS.startswith("id,")}
