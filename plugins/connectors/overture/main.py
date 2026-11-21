"""Overture Maps features (places, buildings, addresses) near a point, read from Overture's public Parquet files.

Overture publishes no query API, only very large Parquet files on S3/Azure plus a static STAC catalogue
(docs.overturemaps.org/getting-data, checked 2026-11-08). This connector therefore (1) reads the catalogue's
`latest` release, (2) reads that release's `collections.parquet` index, which lists each file with a bbox, and
picks the files that intersect the search box (the same method the `overturemaps` package uses), then (3) asks the
host's bounded `parquet.query` capability for rows inside the box. The host reads only the footer and the row
groups whose bbox statistics can match; hard byte/request/row-group budgets apply and are reported.

Written from Overture's published documentation and STAC JSON, and from reading the `overturemaps` package
source for the index columns (collection, type, bbox{xmin,xmax,ymin,ymax}, assets.aws.href / alternate.s3.href).
Fixture-verified unless a live run is recorded in docs/11-roadmap.md. Not confirmed against current files: whether
row groups are spatially ordered tightly enough for statistics to prune most of a file (if they are not, the byte
budget is reached and the area is reported as NOT fully read, never as empty).

A feature absent from Overture says nothing about what exists on the ground. Licences differ by theme and source:
places carry per-source licences (CDLA-Permissive-2.0, Apache-2.0, CC0), buildings are ODbL.
"""

import re
import struct

from astral import sdk

RELEASE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")
S3_HTTPS = "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/"
COLUMNS = {
    "place": ["id", "names", "basic_category", "confidence", "addresses", "websites", "phones", "operating_status", "geometry", "bbox"],
    "building": ["id", "names", "class", "subtype", "height", "num_floors", "geometry", "bbox"],
    "address": ["id", "number", "street", "postcode", "country", "address_levels", "geometry", "bbox"],
}


def point(hexwkb):
    """(lon, lat) of a WKB Point, or None for anything else (polygons are placed at their bbox centre instead)."""
    try:
        b = bytes.fromhex(hexwkb)
        endian = "<" if b[0] == 1 else ">"
        kind = struct.unpack(endian + "I", b[1:5])[0]
        if kind != 1 or len(b) < 21:
            return None
        x, y = struct.unpack(endian + "dd", b[5:21])
        return (x, y) if -180 <= x <= 180 and -90 <= y <= 90 else None
    except (ValueError, IndexError, struct.error, TypeError):
        return None


def where(row):
    p = point(row["geometry"]) if isinstance(row.get("geometry"), str) else None
    if p:
        return p[1], p[0], "point"
    bb = row.get("bbox")
    if isinstance(bb, dict) and all(isinstance(bb.get(k), (int, float)) for k in ("xmin", "xmax", "ymin", "ymax")):
        return (bb["ymin"] + bb["ymax"]) / 2, (bb["xmin"] + bb["xmax"]) / 2, "bbox centre"
    return None


def s3_to_https(href):
    return S3_HTTPS + href[len("s3://overturemaps-us-west-2/"):] if isinstance(href, str) and href.startswith("s3://overturemaps-us-west-2/") else None


def file_url(assets):
    aws = (assets or {}).get("aws") if isinstance(assets, dict) else None
    if not isinstance(aws, dict):
        return None
    href = aws.get("href")
    if isinstance(href, str) and href.startswith(("https://", "http://")):
        return href
    return s3_to_https(((aws.get("alternate") or {}).get("s3") or {}).get("href"))


def invoke(request):
    inp, cfg = request["input"], request["config"]
    ns = request["context"]["entity_namespace"]
    kind = cfg["feature_type"]
    try:
        lat, lon = sdk.lat_lon(inp)
    except ValueError as e:
        yield sdk.result(status="error", error=str(e))
        return
    release = cfg["release"].strip()
    if release and not RELEASE.match(release):
        yield sdk.result(status="error", error="release must look like 2026-10-21.0, or be empty for the latest")
        return
    base = cfg["stac_base"].rstrip("/")
    w, s, e, n = sdk.bbox(lat, lon, cfg["radius_m"] / 1000.0)
    box = {"xmin": w, "ymin": s, "xmax": e, "ymax": n}
    ev, unknown, files_read = [], [], 0
    try:
        if not release:
            res, cat = yield from sdk.fetch_json(f"{base}/catalog.json")
            ev.append(res["sha256"])
            release = cat.get("latest") if isinstance(cat, dict) else None
            if not isinstance(release, str) or not RELEASE.match(release):
                raise sdk.CapabilityFailed("the Overture STAC catalogue did not name a latest release in the documented form")
        idx = yield from sdk.parquet_query(f"{base}/{release}/collections.parquet", columns=["collection", "type", "assets"], bbox=box,
                                           where={"collection": kind, "type": "Feature"}, limit=cfg["max_files"] * 4)
        ev.append(idx["sha256"])
        if idx["stopped_by"] in ("bytes", "requests", "row_groups"):
            raise sdk.CapabilityFailed(f"the release index could not be read completely ({idx['stopped_by']} budget reached)")
        urls = []
        for r in idx["rows"]:
            u = file_url(r.get("assets"))
            if u and u not in urls:
                urls.append(u)
    except sdk.CapabilityFailed as err:
        yield sdk.result(coverage=[sdk.coverage("Overture Maps", unknown=str(err))])
        return
    if not urls:
        yield sdk.result(stats={"features": 0}, coverage=[sdk.coverage(
            "Overture Maps", hits=0, note=f"release {release} lists no {kind} file covering this box; that says nothing about what is on the ground")])
        return
    rows, partial = [], False
    for u in urls[: cfg["max_files"]]:
        left = cfg["limit"] - len(rows)
        if left <= 0:
            break
        try:
            q = yield from sdk.parquet_query(u, columns=COLUMNS[kind], bbox=box, limit=left)
        except sdk.CapabilityFailed as err:
            unknown.append(f"{u.rsplit('/', 1)[-1][:40]}: {err}")
            continue
        files_read += 1
        ev.append(q["sha256"])
        rows += q["rows"]
        if q["stopped_by"] in ("bytes", "requests", "row_groups"):
            partial = True
            unknown.append(f"{u.rsplit('/', 1)[-1][:40]}: stopped at the {q['stopped_by']} budget after {q['row_groups_read']} of "
                           f"{q['row_groups_total']} row groups ({q['row_groups_skipped_by_statistics']} skipped by statistics); the box is not fully covered")
    if len(urls) > cfg["max_files"]:
        unknown.append(f"{len(urls) - cfg['max_files']} more file(s) intersect the box and were not read (max_files={cfg['max_files']})")
    out, placed, unplaced = [], 0, 0
    for r in rows:
        pos = where(r)
        if not pos or not isinstance(r.get("id"), str):
            unplaced += 1
            continue
        placed += 1
        nm = (r.get("names") or {}).get("primary") if isinstance(r.get("names"), dict) else None
        eid = sdk.entity_id(ns, "overture", r["id"])
        label = nm or (" ".join(str(x) for x in (r.get("number"), r.get("street")) if x) or None) or f"Overture {kind} {r['id'][:8]}"
        out += [sdk.a_value(eid, "schema", "Address", ev, 3), sdk.a_value(eid, "name", str(label)[:200], ev, 3),
                sdk.a_value(eid, "latitude", pos[0], ev, 3), sdk.a_value(eid, "longitude", pos[1], ev, 3),
                sdk.a_value(eid, "overtureId", r["id"], ev, 3), sdk.a_value(eid, "overtureRelease", release, ev, 3)]
        if pos[2] == "bbox centre":
            out.append(sdk.a_value(eid, "positionNote", "centre of the feature's bounding box", ev, 3))
        for key, pred in (("basic_category", "overtureCategory"), ("operating_status", "operatingStatus"), ("class", "buildingClass"),
                          ("subtype", "buildingSubtype"), ("postcode", "postalCode"), ("country", "country")):
            if isinstance(r.get(key), str) and r[key]:
                out.append(sdk.a_value(eid, pred, r[key][:120], ev, 3))
        if isinstance(r.get("confidence"), (int, float)):
            out.append(sdk.a_value(eid, "sourceConfidence", round(float(r["confidence"]), 3), ev, 3))
        for key, pred in (("websites", "website"), ("phones", "phone")):
            for v in (r.get(key) or [])[:3]:
                if isinstance(v, str) and v:
                    out.append(sdk.a_value(eid, pred, v[:200], ev, 3))
        for a in (r.get("addresses") or [])[:1]:
            if isinstance(a, dict) and a.get("freeform"):
                out.append(sdk.a_value(eid, "streetAddress", str(a["freeform"])[:200], ev, 3))
    if out:
        yield sdk.emit(out)
    cov = [sdk.coverage("Overture Maps", hits=placed,
                        note=f"release {release}, {kind} features inside the box; Overture is a conflated open dataset: a feature may be missing, "
                             "stale or wrong, and absence says nothing about what exists")]
    if len(rows) >= cfg["limit"]:
        cov.append(sdk.coverage("Overture limit", unknown=f"the {cfg['limit']}-feature limit was reached; more may exist"))
    if unplaced:
        cov.append(sdk.coverage("Overture positions", unknown=f"{unplaced} row(s) had no usable position or id and were not recorded"))
    for u in unknown:
        cov.append(sdk.coverage("Overture read", unknown=u))
    yield sdk.result(status="partial" if (partial or unknown) else "ok", stats={"features": placed, "files_read": files_read}, coverage=cov)


def selfcheck():
    wkb = "0101000000" + struct.pack("<dd", 10.5, 20.25).hex()
    return {"ok": point(wkb) == (10.5, 20.25) and point("zz") is None and RELEASE.match("2026-10-21.0") is not None
            and file_url({"aws": {"alternate": {"s3": {"href": "s3://overturemaps-us-west-2/release/x.parquet"}}}}) == S3_HTTPS + "release/x.parquet"}
