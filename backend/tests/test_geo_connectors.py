"""Geospatial connectors: Overpass, adsb.lol, NASA FIRMS, Copernicus Data Space, Mapillary.
HONEST SCOPE: hand-written responses in the shapes each service's published reference gives; nothing
live was contacted. They prove the mapping, input validation, secrets handling and the rule that a
failed or malformed answer is UNKNOWN rather than an empty result."""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from astral import sdk
from astral.api import create_app
from astral.config import Settings

from conftest import REPO

CONN = REPO / "plugins" / "connectors"
LAT, LON = 51.5007, -0.1246
NAMES = ("overpass", "adsb-lol", "nasa-firms", "copernicus-cdse", "mapillary", "digitraffic-ais", "aisstream", "overture", "kartaview")


@pytest.fixture()
def rig(db, dsn, tmp_path, server, actor, monkeypatch):
    monkeypatch.setenv("FIRMS_MAP_KEY", "firms-secret-key-1")
    monkeypatch.setenv("MAPILLARY_ACCESS_TOKEN", "mly-secret-token-1")
    app = create_app(Settings(dsn=dsn, data_dir=tmp_path), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        for n in NAMES:
            r = c.post("/plugins", json={"path": str(CONN / n)})
            assert r.status_code == 201, r.text
            c.post(f"/plugins/{r.json()['id']}/{r.json()['version']}/enabled")
        case = uuid.UUID(c.post("/cases", json={"name": "geo"}).json()["id"])

        def run(plugin, inp=None, **cfg):
            app.state.host._buckets.clear()
            with db.connection() as conn:
                return app.state.host.invoke(conn, actor, case, plugin, inp or {"lat": LAT, "lon": LON},
                                             config={"base_url": server.url, **cfg}, approved=False)

        def flat():
            c.post(f"/cases/{case}/rebuild")
            return json.dumps([c.get(f"/cases/{case}/entities/{e['cluster_id']}").json() for e in c.get(f"/cases/{case}/entities").json()])
        yield c, server, run, flat, case


# ------------------------------------------------------------------ sdk helpers
def test_geo_helpers_validate_and_clamp():
    assert sdk.lat_lon({"lat": "51.5", "lon": 0}) == (51.5, 0.0)
    for bad in ({}, {"lat": "x", "lon": 1}, {"lat": 91, "lon": 0}, {"lat": 0, "lon": 181}, {"lat": float("nan"), "lon": 0}):
        with pytest.raises(ValueError):
            sdk.lat_lon(bad)
    w, s, e, n = sdk.bbox(60.0, 10.0, 111.32)
    assert abs((n - s) - 2.0) < 1e-6 and (e - w) > 3.9                            # a degree of longitude is shorter at 60 N
    assert sdk.bbox(89.9, 179.9, 500)[2] == 180.0 and sdk.bbox(-89.9, 0, 500)[1] == -90.0
    assert sdk.iso_utc(86400) == "1970-01-02T00:00:00Z"


# ------------------------------------------------------------------ Overpass
def test_overpass_places_nodes_and_centres_counts_the_unplaceable_and_refuses_injection(rig):
    c, server, run, flat, case = rig
    server.add("/api/interpreter", {"version": 0.6, "generator": "Overpass API", "osm3s": {}, "elements": [
        {"type": "node", "id": 1, "lat": 51.5, "lon": -0.12, "tags": {"name": "Cafe Uno", "amenity": "cafe"}},
        {"type": "way", "id": 2, "center": {"lat": 51.501, "lon": -0.125}, "tags": {"name": "Big House", "building": "yes"}},
        {"type": "relation", "id": 3, "tags": {"name": "no position given"}}]})
    r = run("connector.overpass", base_url=server.url + "/api/interpreter")
    assert r.status == "partial" and r.stats == {"features": 2}                    # one element could not be placed: not a clean "ok"
    cov = {x["source"]: x for x in r.coverage}
    assert cov["OpenStreetMap (Overpass)"]["outcome"] == "hits" and cov["OpenStreetMap positions"]["outcome"] == "unknown"
    req = server.requests[-1]
    from urllib.parse import parse_qs
    ql = parse_qs(req["body"].decode())["data"][0]
    assert req["method"] == "POST" and "around:250,51.500700,-0.124600" in ql and "out center tags 50;" in ql
    d = flat()
    assert "Cafe Uno" in d and "amenity=cafe" in d and "Big House" in d and "no position given" not in d
    before = len(server.requests)
    assert run("connector.overpass", selector='a"];out;', base_url=server.url).status == "error" and len(server.requests) == before
    assert run("connector.overpass", {"lat": "x", "lon": 1}).status == "error"


def test_overpass_busy_remarks_and_junk_are_unknown_never_empty(rig):
    c, server, run, flat, case = rig
    server.add("/api/interpreter", "busy", status=429, ctype="text/plain")
    r = run("connector.overpass", base_url=server.url + "/api/interpreter")
    assert r.coverage[0]["outcome"] == "unknown" and "30 seconds" in r.coverage[0]["note"]
    server.routes.clear()
    server.add("/api/interpreter", {"elements": [], "remark": "runtime error: Query timed out in \"query\" at line 1"})
    assert run("connector.overpass", base_url=server.url + "/api/interpreter").coverage[0]["outcome"] == "unknown"
    server.routes.clear()
    server.add("/api/interpreter", {"surprise": 1})
    assert "not an Overpass" in run("connector.overpass", base_url=server.url + "/api/interpreter").coverage[0]["note"]
    server.routes.clear()
    server.add("/api/interpreter", {"elements": []})
    assert [x["outcome"] for x in run("connector.overpass", base_url=server.url + "/api/interpreter").coverage] == ["no_hits"]


# ------------------------------------------------------------------ adsb.lol
def test_adsb_lol_maps_aircraft_and_timestamped_sightings(rig):
    c, server, run, flat, case = rig
    server.add("/v2/point/", {"ac": [{"hex": "406b9f", "flight": "BAW123 ", "r": "G-EUUU", "t": "A320", "lat": 51.6, "lon": -0.2, "alt_baro": 12000, "gs": 310, "track": 90},
                                      {"hex": "~abc123", "seen": 3}], "now": 1700000000000, "total": 2, "ctime": 1, "ptime": 2, "msg": "No error"})
    r = run("connector.adsb-lol", radius_nm=10)
    assert r.status == "ok" and r.stats["aircraft"] == 2
    assert server.requests[-1]["path"] == "/v2/point/51.50070/-0.12460/10"
    d = flat()
    assert "G-EUUU" in d and "BAW123" in d and "2023-11-14T22:13:20Z" in d and "groundSpeedKnots" in d and "abc123" in d
    assert "snapshot" not in d.lower() and "absence is not evidence" in r.coverage[0]["note"]


def test_adsb_lol_failure_and_wrong_shape_are_unknown(rig):
    c, server, run, flat, case = rig
    server.add("/v2/point/", "down", status=503, ctype="text/plain")
    assert run("connector.adsb-lol").coverage[0]["outcome"] == "unknown"
    server.routes.clear()
    server.add("/v2/point/", {"ac": "nope"})
    assert "not an adsb.lol" in run("connector.adsb-lol").coverage[0]["note"]
    assert run("connector.adsb-lol", {"lat": 95, "lon": 0}).status == "error"


# ------------------------------------------------------------------ FIRMS
CSV = ("latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,confidence,version,bright_ti5,frp,daynight\n"
       "51.51,-0.13,330.1,0.4,0.4,2026-11-05,0215,N,n,2.0NRT,290.2,1.5,N\n"
       "51.52,-0.11,340.1,0.4,0.4,2026-11-05,0215,N,h,2.0NRT,291.2,4.5,N\n")


def test_firms_reads_the_header_keeps_the_key_secret_and_never_calls_a_bad_key_empty(rig):
    c, server, run, flat, case = rig
    server.add("/api/area/csv/", CSV, ctype="text/csv")
    r = run("connector.nasa-firms")
    assert r.status == "ok" and r.stats == {"detections": 2} and "not confirmed fires" in r.coverage[0]["note"]
    assert "/firms-secret-key-1/VIIRS_SNPP_NRT/" in server.requests[-1]["path"]
    d = flat()
    assert "2026-11-05T02:15:00Z" in d and "frp" in d and "firms-secret-key-1" not in d
    for a in c.get(f"/cases/{case}/artifacts").json():
        assert "firms-secret-key-1" not in json.dumps(a)
    server.routes.clear()
    server.add("/api/area/csv/", "Invalid MAP_KEY.", ctype="text/plain")
    bad = run("connector.nasa-firms")
    assert bad.coverage[0]["outcome"] == "unknown" and "did not return a detections table" in bad.coverage[0]["note"]
    server.routes.clear()
    server.add("/api/area/csv/", "latitude,longitude,acq_date\n", ctype="text/csv")                # a real table, no rows
    assert [x["outcome"] for x in run("connector.nasa-firms").coverage] == ["no_hits"]


def test_firms_without_a_key_does_not_touch_the_network(rig, monkeypatch):
    c, server, run, flat, case = rig
    monkeypatch.delenv("FIRMS_MAP_KEY")
    r = run("connector.nasa-firms")
    assert r.coverage[0]["outcome"] == "unknown" and "FIRMS_MAP_KEY" in r.coverage[0]["note"] and not server.requests


# ------------------------------------------------------------------ Copernicus
def test_copernicus_builds_a_safe_filter_and_records_scene_metadata(rig):
    c, server, run, flat, case = rig
    server.add("/odata/v1/Products", {"value": [{"Id": "abc-1", "Name": "S2A_MSIL2A_20261101", "S3Path": "/eodata/Sentinel-2/x", "GeoFootprint": {"type": "Polygon"},
                                                 "ContentDate": {"Start": "2026-11-01T10:00:00.000Z", "End": "2026-11-01T10:00:30.000Z"}, "ContentLength": 123}]})
    r = run("connector.copernicus-cdse")
    assert r.status == "ok" and "metadata only" in r.coverage[0]["note"]
    flt = server.requests[-1]["query"]["$filter"]
    assert "Collection/Name eq 'SENTINEL-2'" in flt and "POINT(-0.12460 51.50070)" in flt and "ContentDate/Start gt" in flt
    d = flat()
    assert "S2A_MSIL2A_20261101" in d and "acquisitionStart" in d and "footprintGeoJson" in d
    assert run("connector.copernicus-cdse", collection="x' or '1'='1").status == "error"


def test_copernicus_failures_are_unknown(rig):
    c, server, run, flat, case = rig
    server.add("/odata/v1/Products", "nope", status=401, ctype="text/plain")
    assert run("connector.copernicus-cdse").coverage[0]["outcome"] == "unknown"
    server.routes.clear()
    server.add("/odata/v1/Products", {"value": []})
    assert [x["outcome"] for x in run("connector.copernicus-cdse").coverage] == ["no_hits"]


# ------------------------------------------------------------------ Mapillary
def test_mapillary_records_metadata_only_stays_under_the_box_limit_and_hides_the_token(rig):
    c, server, run, flat, case = rig
    server.add("/images", {"data": [{"id": "111", "captured_at": 1700000000000, "geometry": {"type": "Point", "coordinates": [-0.1245, 51.5008]},
                                     "compass_angle": 270.5, "creator": {"username": "mapper1", "id": "9"}, "thumb_1024_url": "https://cdn.example/t.jpg", "is_pano": False},
                                    {"id": "222"}]})
    from astral.plugins.manifest import ConfigInvalid

    with pytest.raises(ConfigInvalid, match="maximum of 450"):                                     # the manifest caps the box
        run("connector.mapillary", radius_m=9999)
    r = run("connector.mapillary", radius_m=450)
    assert r.status == "ok" and r.stats == {"images": 1} and "were not fetched" in r.coverage[0]["note"]
    req = server.requests[-1]
    w, s, e, n = (float(x) for x in req["query"]["bbox"].split(","))
    assert (e - w) < 0.01 and (n - s) < 0.01 and req["headers"]["authorization"] == "OAuth mly-secret-token-1"
    assert req["query"]["fields"].startswith("id,captured_at")
    d = flat()
    assert "thumbnailUrl" in d and "mapper1" in d and "mly-secret-token-1" not in d
    assert not any(r["path"].endswith("t.jpg") for r in server.requests)                              # the image itself was never fetched


def test_mapillary_missing_token_and_errors_are_unknown(rig, monkeypatch):
    c, server, run, flat, case = rig
    server.add("/images", {"error": {"message": "Application request limit reached"}}, status=429)
    assert run("connector.mapillary").coverage[0]["outcome"] == "unknown"
    monkeypatch.delenv("MAPILLARY_ACCESS_TOKEN")
    before = len(server.requests)
    assert "MAPILLARY_ACCESS_TOKEN" in run("connector.mapillary").coverage[0]["note"] and len(server.requests) == before


# ------------------------------------------------------------------ Digitraffic Marine AIS (plain HTTP, gzip mandatory)
def _gz(obj):
    import gzip

    return gzip.compress(json.dumps(obj).encode())


def ais_feature(mmsi, lon, lat, **p):
    props = {"mmsi": mmsi, "sog": 12.3, "cog": 90.0, "navStat": 0, "rot": 0, "posAcc": True, "raim": False, "timestamp": 30,
             "timestampExternal": 1700000000000, "heading": 91}
    props.update(p)
    return {"type": "Feature", "mmsi": mmsi, "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": props}


def test_digitraffic_decodes_gzip_orders_lon_lat_correctly_and_drops_not_available_values(rig):
    c, server, run, flat, case = rig
    fc = {"type": "FeatureCollection", "dataUpdatedTime": "2023-11-14T22:13:20Z", "features": [
        ais_feature(230123450, 24.95, 60.17), ais_feature(230123451, 25.0, 60.2, sog=102.3, cog=360, heading=511)]}
    server.add("/api/ais/v1/locations", _gz(fc), headers={"Content-Encoding": "gzip"})
    r = run("connector.digitraffic-ais", {"lat": 60.17, "lon": 24.95}, base_url=server.url, radius_km=10)
    req = server.requests[-1]
    assert r.status == "ok" and r.stats == {"vessels": 2}
    assert req["headers"]["accept-encoding"] == "gzip" and req["headers"]["digitraffic-user"] == "AstralLens/connector"
    assert req["query"]["radius"] == "10" and req["query"]["latitude"] == "60.17000" and req["query"]["longitude"] == "24.95000"
    d = flat()
    assert "230123450" in d and "60.17" in d and "24.95" in d and "2023-11-14T22:13:20Z" in d and "speedOverGroundKnots" in d
    assert d.count("headingDegrees") == 1 and d.count("courseOverGroundDegrees") == 1            # 511 and 360 are 'not available', not data
    assert "not transmitting" in r.coverage[0]["note"]


def test_digitraffic_mmsi_mode_adds_particulars_and_reports_what_it_could_not_fetch(rig):
    c, server, run, flat, case = rig
    server.add("/api/ais/v1/locations", _gz({"type": "FeatureCollection", "features": [ais_feature(230123450, 24.95, 60.17)]}), headers={"Content-Encoding": "gzip"})
    server.add("/api/ais/v1/vessels/230123450", _gz({"mmsi": 230123450, "name": "MV EXAMPLE", "imo": 9876543, "callSign": "OHAB", "shipType": 70, "destination": "TALLINN"}),
               headers={"Content-Encoding": "gzip"})
    r = run("connector.digitraffic-ais", {"mmsi": "230123450"}, base_url=server.url)
    assert r.status == "ok" and server.requests[-2]["query"] == {"mmsi": "230123450"}
    d = flat()
    assert "MV EXAMPLE" in d and "9876543" in d and "OHAB" in d and "TALLINN" in d
    server.routes.clear()
    server.add("/api/ais/v1/locations", _gz({"type": "FeatureCollection", "features": [ais_feature(230123450, 24.95, 60.17)]}), headers={"Content-Encoding": "gzip"})
    r = run("connector.digitraffic-ais", {"mmsi": "230123450"}, base_url=server.url)                 # particulars 404: positions still recorded
    assert r.status == "partial" and any(x["source"] == "Digitraffic vessel particulars" and x["outcome"] == "unknown" for x in r.coverage)
    assert run("connector.digitraffic-ais", {"mmsi": "123"}, base_url=server.url).status == "error"


def test_digitraffic_failures_malformed_features_and_corrupt_gzip_are_unknown(rig):
    c, server, run, flat, case = rig
    server.add("/api/ais/v1/locations", "nope", status=406, ctype="text/plain")
    assert run("connector.digitraffic-ais", base_url=server.url).coverage[0]["outcome"] == "unknown"
    server.routes.clear()
    server.add("/api/ais/v1/locations", _gz({"surprise": True}), headers={"Content-Encoding": "gzip"})
    assert "not a Digitraffic" in run("connector.digitraffic-ais", base_url=server.url).coverage[0]["note"]
    server.routes.clear()
    server.add("/api/ais/v1/locations", _gz({"type": "FeatureCollection", "features": [ais_feature(1, 24.9, 60.1), {"type": "Feature", "properties": {}}, ais_feature(2, 400.0, 60.1)]}),
               headers={"Content-Encoding": "gzip"})
    r = run("connector.digitraffic-ais", base_url=server.url, max_vessels=1)
    notes = {x["source"]: x["outcome"] for x in r.coverage}
    assert notes["Digitraffic AIS records"] == "unknown" and r.stats == {"vessels": 1}
    server.routes.clear()
    server.add("/api/ais/v1/locations", b"\x1f\x8b garbage not gzip", headers={"Content-Encoding": "gzip"})
    assert run("connector.digitraffic-ais", base_url=server.url).coverage[0]["outcome"] == "unknown"


def test_gzip_is_only_decoded_within_the_size_cap():
    from astral import egress
    from conftest import FixtureServer

    big = _gz({"pad": "x" * 5_000_000})                                                                # compresses tiny, expands past the cap
    srv = FixtureServer()
    srv.add("/z", big, ctype="application/json", headers={"Content-Encoding": "gzip"})
    res = egress.Fetcher().fetch(egress.EgressPolicy("local-only"), srv.url + "/z", headers={"Accept-Encoding": "gzip"}, max_bytes=100_000)
    assert res.truncated and len(res.body) == 100_000 and res.headers["x-astral-decoded"] == "gzip"


# ------------------------------------------------------------------ AISStream over the bounded ws.collect capability
def ws_server(frames, *, binary=True):
    """A local WebSocket server standing in for AISStream: reads the one subscription frame, sends `frames`, then closes."""
    import threading

    from websockets.sync.server import serve

    got = []

    def handler(ws):
        got.append(ws.recv(timeout=5))
        for f in frames:
            data = f if isinstance(f, (str, bytes)) else json.dumps(f)
            ws.send(data.encode() if binary and isinstance(data, str) else data)

    srv = serve(handler, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"ws://127.0.0.1:{srv.socket.getsockname()[1]}", got, srv


def pos(mmsi, lat, lon, name="SHIP", **m):
    body = {"UserID": mmsi, "Latitude": lat, "Longitude": lon, "Sog": 11.5, "Cog": 180.0, "TrueHeading": 181, "NavigationalStatus": 0}
    body.update(m)
    return {"MessageType": "PositionReport", "MetaData": {"MMSI": mmsi, "ShipName": f"{name} ", "Latitude": lat, "Longitude": lon}, "Message": {"PositionReport": body}}


def test_aisstream_keeps_the_last_position_per_vessel_sends_the_key_only_in_the_frame_and_drops_markers(rig, monkeypatch):
    c, server, run, flat, case = rig
    monkeypatch.setenv("AISSTREAM_API_KEY", "ais-secret-key-9")
    url, got, srv = ws_server([{"MessageType": "SubscriptionConfirmation", "Message": {"CompressionEnabled": True}},
                               pos(111111111, 60.0, 24.0, "ALPHA"), pos(111111111, 60.1, 24.1, "ALPHA"), pos(222222222, 59.9, 24.2, "BRAVO", Sog=102.3, TrueHeading=511),
                               pos(333333333, 91.0, 181.0, "NOPOS"), {"MessageType": "ShipStaticData", "Message": {}}])
    r = run("connector.aisstream", {"lat": 60.0, "lon": 24.0}, url=url, radius_km=50, listen_seconds=3)
    sent = json.loads(got[0])
    assert sent["APIKey"] == "ais-secret-key-9" and sent["FilterMessageTypes"] == ["PositionReport"]
    (s, w), (n, e) = sent["BoundingBoxes"][0]                                                    # [[lat, lon], [lat, lon]], lat first
    assert s < 60.0 < n and w < 24.0 < e and abs(n - s - 0.9) < 0.1
    assert r.stats["vessels"] == 2 and any(x["outcome"] == "unknown" and x["source"] == "AISStream messages" for x in r.coverage)
    d = flat()
    assert "ALPHA" in d and "60.1" in d and "BRAVO" in d and "NOPOS" not in d                    # latest position wins; 91/181 is 'not available'
    assert d.count("headingReported") == 1 and "ais-secret-key-9" not in d                       # 511 dropped
    for a in c.get(f"/cases/{case}/artifacts").json():
        assert "ais-secret-key-9" not in json.dumps(a)
    srv.shutdown()


def test_aisstream_without_a_key_or_with_a_refusal_or_a_dead_server_is_unknown(rig, monkeypatch):
    c, server, run, flat, case = rig
    monkeypatch.delenv("AISSTREAM_API_KEY", raising=False)
    r = run("connector.aisstream", {"lat": 60.0, "lon": 24.0}, url="ws://127.0.0.1:9")
    assert r.coverage[0]["outcome"] == "unknown" and "AISSTREAM_API_KEY" in r.coverage[0]["note"]
    monkeypatch.setenv("AISSTREAM_API_KEY", "bad-key")
    url, got, srv = ws_server([{"error": "Api Key Is Not Valid"}])
    r = run("connector.aisstream", {"lat": 60.0, "lon": 24.0}, url=url, listen_seconds=3)
    assert r.coverage[0]["outcome"] == "unknown" and "Api Key Is Not Valid" in r.coverage[0]["note"] and r.assertion_ids == []
    srv.shutdown()
    r = run("connector.aisstream", {"lat": 60.0, "lon": 24.0}, url="ws://127.0.0.1:9", listen_seconds=3)
    assert r.coverage[0]["outcome"] == "unknown" and "WebSocket failed" in r.coverage[0]["note"]
    assert run("connector.aisstream", {"lat": "x", "lon": 1}).status == "error"


def test_ws_collect_is_bounded_by_message_count_and_refuses_non_local_hosts_under_local_only(rig, monkeypatch):
    c, server, run, flat, case = rig
    monkeypatch.setenv("AISSTREAM_API_KEY", "k")
    url, got, srv = ws_server([pos(100000000 + i, 60.0, 24.0) for i in range(30)])
    from astral import capabilities

    monkeypatch.setattr(capabilities, "MAX_WS_MESSAGES", 5)
    r = run("connector.aisstream", {"lat": 60.0, "lon": 24.0}, url=url, listen_seconds=3)
    assert r.stats["messages"] == 5 and any(x["source"] == "AISStream window" and x["outcome"] == "unknown" for x in r.coverage)
    srv.shutdown()


def test_ws_collect_obeys_the_egress_policy_and_refuses_what_it_cannot_police():
    from types import SimpleNamespace

    from astral import capabilities, egress

    def ctx(mode, footprint="third_party"):
        return SimpleNamespace(manifest=SimpleNamespace(footprint=footprint), egress=egress.EgressPolicy(mode, proxy="http://127.0.0.1:1" if mode == "proxy" else None))

    for mode, url, expect in (("local-only", "wss://stream.example.invalid/v0/stream", "local-only"),
                              ("direct", "https://example.invalid/", "ws:// or wss://"),
                              ("proxy", "wss://stream.example.invalid/v0/stream", "not supported through a proxy"),
                              ("direct", "ftp://x", "ws:// or wss://")):
        with pytest.raises(capabilities.CapabilityError, match=expect):
            capabilities._ws_collect(ctx(mode), {"url": url})
    with pytest.raises(capabilities.CapabilityError, match="footprint 'none'"):
        capabilities._ws_collect(ctx("direct", "none"), {"url": "wss://x.example.invalid/"})


# ------------------------------------------------------------------ Overture Maps over the bounded parquet.query capability
def _overture(server, **kw):
    import parquet_fixtures as fx

    log = []
    server.add("/catalog.json", {"type": "Catalog", "latest": "2026-10-21.0"})
    server.add_fn("/2026-10-21.0/collections.parquet", fx.ranged(fx.stac_index(server.url + "/data/places.parquet"), log=log))
    server.add_fn("/data/places.parquet", fx.ranged(fx.places(), log=log))
    return log


def test_overture_finds_the_right_file_in_the_index_reads_only_matching_row_groups_and_records_the_release(rig):
    pytest.importorskip("pyarrow")
    c, server, run, flat, case = rig
    _overture(server)
    r = run("connector.overture", {"lat": 10.005, "lon": 7.35}, stac_base=server.url, radius_m=2000)
    assert r.status == "ok" and r.stats == {"features": 2, "files_read": 1}
    cov = {x["source"]: x for x in r.coverage}
    assert cov["Overture Maps"]["outcome"] == "hits" and "release 2026-10-21.0" in cov["Overture Maps"]["note"]
    paths = {q["path"] for q in server.requests}
    assert paths == {"/catalog.json", "/2026-10-21.0/collections.parquet", "/data/places.parquet"}      # the far, building and collection rows were never followed
    d = flat()
    assert "Cafe Uno" in d and "overtureRelease" in d and "2026-10-21.0" in d and "cafe" in d and "20 Test Street" in d


def test_overture_rejects_a_bad_release_and_reports_a_malformed_catalogue_as_unknown(rig):
    pytest.importorskip("pyarrow")
    c, server, run, flat, case = rig
    before = len(server.requests)
    assert run("connector.overture", stac_base=server.url, release="../../etc").status == "error" and len(server.requests) == before
    server.add("/catalog.json", {"type": "Catalog", "latest": "tomorrow"})
    r = run("connector.overture", stac_base=server.url)
    assert r.coverage[0]["outcome"] == "unknown" and "latest release" in json.dumps(r.coverage)
    assert "Cafe" not in flat()


def test_overture_that_hits_the_read_budget_says_the_box_is_not_fully_read_instead_of_returning_nothing(rig, monkeypatch):
    pytest.importorskip("pyarrow")
    from astral import parquet_scan

    c, server, run, flat, case = rig
    _overture(server)
    monkeypatch.setitem(parquet_scan.query.__kwdefaults__, "max_bytes", 100_000)       # smaller than one footer read
    r = run("connector.overture", {"lat": 10.005, "lon": 7.35}, stac_base=server.url, radius_m=2000)
    assert r.status == "partial"
    assert any(x["outcome"] == "unknown" and "budget" in json.dumps(x) for x in r.coverage), r.coverage


# ------------------------------------------------------------------ KartaView (written from client code; never run live)
def test_kartaview_1_0_records_metadata_only_accepts_string_numbers_and_counts_the_unusable(rig):
    c, server, run, flat, case = rig
    server.add("/1.0/list/nearby-photos", {"currentPageItems": [
        {"id": "11", "lat": "51.5003", "lng": "-0.1201", "heading": "90.5", "shot_date": "2020-05-01 10:00:00", "username": "mapper-one",
         "name": "storage13/files/photo/a.jpg", "sequence_id": 5},
        {"id": "12", "lat": "not a number", "lng": "0"}]})
    r = run("connector.kartaview", base_url=server.url)
    assert r.status == "partial" and r.stats == {"photos": 1}
    assert {x["source"]: x["outcome"] for x in r.coverage} == {"KartaView": "hits", "KartaView records": "unknown"}
    req = server.requests[0]
    from urllib.parse import parse_qs
    form = {k: v[0] for k, v in parse_qs(req["body"].decode()).items()}
    assert req["method"] == "POST" and form["bbTopLeft"].count(",") == 1 and form["ipp"] == "100" and float(form["bbTopLeft"].split(",")[0]) > LAT
    assert len(server.requests) == 1                                                  # the image path was recorded, never fetched
    d = flat()
    assert "mapper-one" in d and "storage13/files/photo/a.jpg" in d and "2020-05-01" in d and "KartaView photo 11" in d and "KartaView photo 12" not in d


def test_kartaview_2_0_envelope_and_every_failure_shape_is_unknown_never_empty(rig):
    c, server, run, flat, case = rig
    ok = {"status": {"apiCode": 600, "httpCode": 200}, "result": {"data": [
        {"id": 77, "lat": "51.5", "lng": "-0.12", "heading": "10", "shotDate": "2021-03-04T01:02:03Z", "fileurlProc": "https://cdn.example.test/p.jpg", "sequence": {"id": 9}}]}}
    server.add("/2.0/photo", ok)
    r = run("connector.kartaview", api="2.0-nearby", base_url=server.url)
    assert r.status == "ok" and r.stats == {"photos": 1} and server.requests[-1]["query"]["lat"] == f"{LAT:.6f}"
    assert "cdn.example.test/p.jpg" in flat()
    for answer, why in (({"status": {"apiCode": 401, "apiMessage": "login required"}, "result": {"data": []}}, "401"),
                        ({"status": {"apiCode": 600}, "result": {}}, "result.data"), ({"unexpected": True}, "status envelope")):
        server.routes.clear()
        server.add("/2.0/photo", answer)
        r = run("connector.kartaview", api="2.0-nearby", base_url=server.url)
        assert r.coverage[0]["outcome"] == "unknown" and why in json.dumps(r.coverage) and r.stats in ({}, None)
    server.routes.clear()
    server.add("/2.0/photo", "<html>maintenance</html>", ctype="text/html")
    assert run("connector.kartaview", api="2.0-nearby", base_url=server.url).coverage[0]["outcome"] == "unknown"
    server.routes.clear()
    server.add("/2.0/photo", "boom", status=500, ctype="text/plain")
    assert "HTTP 500" in json.dumps(run("connector.kartaview", api="2.0-nearby", base_url=server.url).coverage)
