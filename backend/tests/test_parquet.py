"""parquet.query: bounded range reads of a remote Parquet file. HONEST SCOPE: files are generated here in the
shape of Overture's places (struct bbox, WKB geometry, one spatial range per row group) and served by a local
Range-capable server. This proves pruning, budgets, failure handling and the egress policy; it does not prove
that Overture's real files are ordered tightly enough for statistics to prune them."""

from __future__ import annotations

import types
import uuid

import pytest

pytest.importorskip("pyarrow")

from astral import capabilities, egress, parquet_scan  # noqa: E402

import parquet_fixtures as fx  # noqa: E402

BOX = {"xmin": 7.3, "xmax": 7.4, "ymin": 9.99, "ymax": 10.02}
DATA = fx.places()


def q(data=DATA, **kw):
    kw.setdefault("columns", ["id", "names", "bbox"])
    return parquet_scan.query(fx.getter(data), "http://x.test/f.parquet", **kw)


def test_a_bbox_query_reads_only_the_row_groups_the_statistics_cannot_rule_out_and_never_the_unrequested_column():
    r = q(bbox=BOX)
    assert {x["id"] for x in r["rows"]} == {f"p-7-{i}" for i in range(17, 23)} and r["complete"] and r["stopped_by"] == "end_of_file"
    assert r["row_groups_total"] == 20 and r["row_groups_read"] == 1 and r["row_groups_skipped_by_statistics"] == 19
    assert r["bytes_read"] < r["file_size"] / 5                                     # the `payload` column was never fetched
    assert any(x["names"]["primary"] == "Cafe Uno" for x in r["rows"])


def test_without_statistics_nothing_is_skipped_and_the_answer_is_still_correct():
    r = q(fx.places(statistics=False), bbox=BOX)
    assert r["row_groups_skipped_by_statistics"] == 0 and r["row_groups_read"] == 20
    assert {x["id"] for x in r["rows"]} == {f"p-7-{i}" for i in range(17, 23)}


def test_where_filters_columns_and_bytes_and_binary_values_are_returned_as_hex():
    r = q(columns=["id", "geometry"], where={"basic_category": "cafe"})
    assert [x["id"] for x in r["rows"]] == ["p-7-20"] and r["rows"][0]["geometry"] == fx.wkb_point(7.36, 10.0).hex()


def test_a_row_limit_a_row_group_cap_and_a_byte_budget_stop_the_read_and_say_so():
    r = q(bbox={"xmin": 0, "xmax": 20, "ymin": 9, "ymax": 11}, limit=7)
    assert len(r["rows"]) == 7 and r["stopped_by"] == "limit" and r["truncated"] and not r["complete"]
    r = q(bbox={"xmin": 0, "xmax": 20, "ymin": 9, "ymax": 11}, max_row_groups=3, limit=500)
    assert r["row_groups_read"] == 3 and r["stopped_by"] == "row_groups" and r["truncated"] and len(r["rows"]) == 150
    r = q(bbox={"xmin": 0, "xmax": 20, "ymin": 9, "ymax": 11}, limit=500, max_bytes=270_000)
    assert r["stopped_by"] in ("bytes", "requests") and r["truncated"] and not r["complete"]


def test_a_server_that_ignores_range_a_missing_column_and_a_non_parquet_file_are_refused_not_guessed():
    with pytest.raises(parquet_scan.ParquetError, match="range"):
        parquet_scan.query(fx.getter(DATA, honour_range=False), "http://x.test/f", columns=["id"])
    with pytest.raises(parquet_scan.ParquetError, match="no column 'nope'"):
        q(columns=["nope"])
    with pytest.raises(parquet_scan.ParquetError):
        parquet_scan.query(fx.getter(b"this is not a parquet file at all" * 100), "http://x.test/f", columns=["id"])


def test_the_request_count_is_bounded():
    r = q(bbox={"xmin": 0, "xmax": 20, "ymin": 9, "ymax": 11}, limit=500, max_requests=4)
    assert r["requests"] <= 4 and r["stopped_by"] in ("requests", "row_groups", "bytes", "limit")


def test_missing_pyarrow_is_reported(monkeypatch):
    import builtins

    real = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", lambda n, *a, **k: (_ for _ in ()).throw(ImportError("no")) if n.startswith("pyarrow") else real(n, *a, **k))
    with pytest.raises(parquet_scan.ParquetError, match="pyarrow"):
        q(bbox=BOX)


# ------------------------------------------------------------------ the capability: policy, storage, provenance
def ctx(host, mode="direct", footprint="third_party", conn=None, store=None):
    from astral.secrets import Redactor

    class Store:
        def ingest(self, conn, actor, case, body, **kw):
            Store.last = (body, kw)
            return types.SimpleNamespace(hex="ab" * 32)

    return types.SimpleNamespace(manifest=types.SimpleNamespace(footprint=footprint, rate_limit=None, id="t", version="1"),
                                 egress=egress.EgressPolicy(mode), host=types.SimpleNamespace(fetcher=egress.Fetcher()),
                                 hosts_contacted=__import__("collections").Counter(), redactor=Redactor(), conn=None, actor=None,
                                 case_id=uuid.uuid4(), store=Store(), acquired_by="t@1"), Store


def test_the_capability_reads_through_the_egress_policy_stores_the_rows_and_records_what_was_read(server):
    log: list = []
    server.add("/places.parquet", fx.ranged(DATA, log=log)) if False else server.add_fn("/places.parquet", fx.ranged(DATA, log=log))
    c, Store = ctx(None)
    out = capabilities._parquet_query(c, {"url": server.url + "/places.parquet", "columns": ["id", "names"], "bbox": BOX})
    assert len(out["rows"]) == 6 and out["row_groups_read"] == 1 and out["sha256"] == "ab" * 32
    body, kw = Store.last
    assert body.count(b"\n") == 5 and kw["media_type"] == "application/x-ndjson"
    assert kw["acquisition_ctx"]["row_groups_skipped_by_statistics"] == 19 and kw["acquisition_ctx"]["bbox"] == BOX
    assert all(x.startswith("bytes=") for x in log) and c.hosts_contacted["127.0.0.1"] == len(log)


def test_the_capability_refuses_local_only_for_a_remote_host_a_footprint_none_plugin_and_a_server_without_ranges(server):
    c, _ = ctx(None, "local-only")
    with pytest.raises(capabilities.CapabilityError, match="local-only"):
        capabilities._parquet_query(c, {"url": "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/x.parquet"})
    with pytest.raises(capabilities.CapabilityError, match="footprint"):
        capabilities._parquet_query(ctx(None, footprint="none")[0], {"url": server.url + "/x"})
    server.add_fn("/nr.parquet", fx.ranged(DATA, honour_range=False))
    with pytest.raises(capabilities.CapabilityError, match="range"):
        capabilities._parquet_query(ctx(None)[0], {"url": server.url + "/nr.parquet", "columns": ["id"]})
