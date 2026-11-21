"""Parquet files shaped like Overture's, and a Range-capable handler for the fixture server. Not tests."""

from __future__ import annotations

import io
import os
import struct


def wkb_point(lon: float, lat: float) -> bytes:
    return b"\x01\x01\x00\x00\x00" + struct.pack("<dd", lon, lat)


def places(groups: int = 20, per_group: int = 50, *, statistics: bool = True) -> bytes:
    """Row group g holds longitudes g .. g+0.9, so a bbox query can be answered from statistics alone.
    Row (g=7, i=20) is 'Cafe Uno' at lon 7.36. Latitudes are 10.000 .. 10.009. `payload` is 3 KB of incompressible filler
    that a query which does not ask for it must never read."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    rows = []
    for g in range(groups):
        for i in range(per_group):
            lon, lat = g + 0.9 * i / per_group, 10.0 + 0.001 * (i % 10)
            rows.append({"id": f"p-{g}-{i}", "names": {"primary": "Cafe Uno" if (g, i) == (7, 20) else f"Place {g}-{i}"},
                         "geometry": wkb_point(lon, lat), "bbox": {"xmin": lon, "ymin": lat, "xmax": lon, "ymax": lat},
                         "basic_category": "cafe" if (g, i) == (7, 20) else "shop", "confidence": 0.9,
                         "addresses": [{"freeform": f"{i} Test Street"}], "websites": ["https://example.test/" + str(i)] if i % 5 == 0 else [],
                         "phones": [], "operating_status": "open", "payload": os.urandom(3000)})
    bbox_t = pa.struct([("xmin", pa.float64()), ("ymin", pa.float64()), ("xmax", pa.float64()), ("ymax", pa.float64())])
    table = pa.table({
        "id": pa.array([r["id"] for r in rows]),
        "names": pa.array([r["names"] for r in rows], pa.struct([("primary", pa.string())])),
        "geometry": pa.array([r["geometry"] for r in rows], pa.binary()),
        "bbox": pa.array([r["bbox"] for r in rows], bbox_t),
        "basic_category": pa.array([r["basic_category"] for r in rows]),
        "confidence": pa.array([r["confidence"] for r in rows]),
        "addresses": pa.array([r["addresses"] for r in rows], pa.list_(pa.struct([("freeform", pa.string())]))),
        "websites": pa.array([r["websites"] for r in rows], pa.list_(pa.string())),
        "phones": pa.array([r["phones"] for r in rows], pa.list_(pa.string())),
        "operating_status": pa.array([r["operating_status"] for r in rows]),
        "payload": pa.array([r["payload"] for r in rows], pa.binary()),
    })
    buf = io.BytesIO()
    pq.write_table(table, buf, row_group_size=per_group, write_statistics=statistics, compression="zstd")
    return buf.getvalue()


def stac_index(file_url: str, *, far_url: str = "https://example.invalid/far.parquet") -> bytes:
    """Rows like Overture's collections.parquet: one Feature per file with a bbox and assets.aws.{href, alternate.s3.href}."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    def row(coll, typ, box, href, s3):
        return {"collection": coll, "type": typ, "bbox": dict(zip(("xmin", "ymin", "xmax", "ymax"), box)),
                "assets": {"aws": {"href": href, "alternate": {"s3": {"href": s3}}}}}
    rows = [row("place", "Feature", (-5.0, 9.0, 25.0, 11.0), file_url, "s3://overturemaps-us-west-2/release/x/near.parquet"),
            row("place", "Feature", (100.0, 9.0, 110.0, 11.0), far_url, "s3://overturemaps-us-west-2/release/x/far.parquet"),
            row("building", "Feature", (-5.0, 9.0, 25.0, 11.0), "https://example.invalid/building.parquet", "s3://overturemaps-us-west-2/b.parquet"),
            row("place", "Collection", (-5.0, 9.0, 25.0, 11.0), "https://example.invalid/collection.parquet", "s3://overturemaps-us-west-2/c.parquet")]
    bbox_t = pa.struct([("xmin", pa.float64()), ("ymin", pa.float64()), ("xmax", pa.float64()), ("ymax", pa.float64())])
    href_t = pa.struct([("href", pa.string()), ("alternate", pa.struct([("s3", pa.struct([("href", pa.string())]))]))])
    table = pa.table({"collection": pa.array([r["collection"] for r in rows]), "type": pa.array([r["type"] for r in rows]),
                      "bbox": pa.array([r["bbox"] for r in rows], bbox_t),
                      "assets": pa.array([r["assets"] for r in rows], pa.struct([("aws", href_t)]))})
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


def ranged(data: bytes, *, honour_range: bool = True, log: list | None = None):
    """A FixtureServer handler that serves `data` with HTTP Range support (or ignores Range, like a bad server)."""
    def fn(req):
        rng = req["headers"].get("range")
        if log is not None:
            log.append(rng)
        if not rng or not honour_range:
            return 200, "application/octet-stream", data, {}
        a, _, b = rng.removeprefix("bytes=").partition("-")
        start, end = int(a), min(int(b), len(data) - 1)
        return 206, "application/octet-stream", data[start:end + 1], {"Content-Range": f"bytes {start}-{end}/{len(data)}"}
    return fn


def getter(data: bytes, counter: dict | None = None, *, honour_range: bool = True):
    """The `get(start, end)` callable parquet_scan.query takes, backed by memory."""
    def get(start, end):
        if counter is not None:
            counter["n"] = counter.get("n", 0) + 1
        if not honour_range:
            return 200, {}, data
        end = min(end, len(data) - 1)
        return 206, {"content-range": f"bytes {start}-{end}/{len(data)}"}, data[start:end + 1]
    return get
