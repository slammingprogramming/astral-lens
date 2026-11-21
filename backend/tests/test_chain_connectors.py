"""Etherscan, GraphSense and the mempool.space mode of the Esplora connector. HONEST SCOPE: hand-written
responses in the field names each service's published reference gives; no live service was contacted."""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from astral.api import create_app
from astral.config import Settings

from conftest import REPO
from test_esplora import ADDR, stats, tx

CONN = REPO / "plugins" / "connectors"
ETH = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20


@pytest.fixture()
def rig(db, dsn, tmp_path, server, actor, monkeypatch):
    monkeypatch.setenv("ETHERSCAN_API_KEY", "test-key-123")
    app = create_app(Settings(dsn=dsn, data_dir=tmp_path), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        for name in ("etherscan", "graphsense", "esplora"):
            r = c.post("/plugins", json={"path": str(CONN / name)})
            assert r.status_code == 201, r.text
            c.post(f"/plugins/{r.json()['id']}/{r.json()['version']}/enabled")
        case = uuid.UUID(c.post("/cases", json={"name": "chains"}).json()["id"])

        def run(plugin, address, **cfg):
            app.state.host._buckets.clear()                      # these tests make many calls inside one second
            with db.connection() as conn:
                return app.state.host.invoke(conn, actor, case, plugin, {"address": address}, config={"base_url": server.url, **cfg})
        yield c, server, run, case


def etx(h, frm, to, value, ts=1700000000, err="0"):
    return {"blockNumber": "100", "timeStamp": str(ts), "hash": h, "from": frm, "to": to, "value": value, "isError": err, "contractAddress": ""}


def eth_route(server, rows, *, balance="172774397764084972158218"):
    def handler(req):
        q = req["query"]
        if q.get("action") == "balance":
            return (200, "application/json", {"status": "1", "message": "OK", "result": balance}, {})
        page, offset = int(q.get("page", 1)), int(q.get("offset", 100))
        part = rows[(page - 1) * offset: page * offset]
        if not part:
            return (200, "application/json", {"status": "0", "message": "No transactions found", "result": []}, {})
        return (200, "application/json", {"status": "1", "message": "OK", "result": part}, {})
    server.add_fn("/", handler)


# ------------------------------------------------------------------ Etherscan
def test_etherscan_maps_a_wallet_and_payments_and_keeps_the_key_out_of_evidence(rig):
    c, server, run, case = rig
    eth_route(server, [etx("0x01", OTHER, ETH, "1000000000000000000"), etx("0x02", ETH, OTHER, "5", err="1")])
    res = run("connector.etherscan", ETH)
    assert res.status == "ok" and res.stats == {"transactions": 2} and [x["outcome"] for x in res.coverage] == ["hits"]
    assert all(r["query"].get("apikey") == "test-key-123" for r in server.requests)                  # it was sent ...
    c.post(f"/cases/{case}/rebuild")
    flat = json.dumps([c.get(f"/cases/{case}/entities/{e['cluster_id']}").json() for e in c.get(f"/cases/{case}/entities").json()])
    assert "wei" in flat and "failed" in flat and OTHER in flat and "172774397764084972158218" in flat
    assert "test-key-123" not in flat                                                                     # ... and never stored
    for a in c.get(f"/cases/{case}/artifacts").json():
        meta = json.dumps(a)
        assert "test-key-123" not in meta


def test_etherscan_distinguishes_empty_from_error_and_flags_possible_older_history(rig):
    c, server, run, case = rig
    eth_route(server, [])
    r = run("connector.etherscan", ETH)
    assert [x["outcome"] for x in r.coverage] == ["no_hits"] and "not evidence" in r.coverage[0]["note"]
    server.routes.clear()
    server.add("/", {"status": "0", "message": "NOTOK", "result": "Max calls per sec rate limit reached (3/sec)"})
    r = run("connector.etherscan", ETH)
    assert r.coverage[0]["outcome"] == "unknown" and "rate limit" in r.coverage[0]["note"] and r.assertion_ids == []
    server.routes.clear()
    eth_route(server, [etx(f"0x{i:02x}", OTHER, ETH, "1") for i in range(5)])
    r = run("connector.etherscan", ETH, page_size=5)
    assert any(x["source"] == "Etherscan history" and x["outcome"] == "unknown" for x in r.coverage)
    r = run("connector.etherscan", ETH, page_size=5, pages=2)                                         # second page is empty -> done
    assert r.stats["transactions"] == 5


def test_etherscan_without_a_key_or_with_a_bad_address_does_not_touch_the_network(rig, monkeypatch):
    c, server, run, case = rig
    assert run("connector.etherscan", "0x123").status == "error" and not server.requests
    monkeypatch.delenv("ETHERSCAN_API_KEY")
    r = run("connector.etherscan", ETH)
    assert r.coverage[0]["outcome"] == "unknown" and "ETHERSCAN_API_KEY" in r.coverage[0]["note"] and not server.requests


# ------------------------------------------------------------------ GraphSense
def test_graphsense_records_tags_as_attributed_claims_and_clusters_as_doubtful(rig):
    c, server, run, case = rig
    server.add("/btc/addresses/" + ADDR + "/entity", {"entity": 4711, "no_addresses": 120, "root_address": ADDR})
    server.add("/btc/addresses/" + ADDR + "/tags", {"address_tags": [
        {"label": "Example Exchange", "source": "tagpack-x", "category": "exchange", "abuse": None},
        {"label": "scam-report", "source": "forum", "category": "scam", "abuse": "ransomware"}], "next_page": None})
    server.add("/btc/addresses/" + ADDR, {"currency": "btc", "address": ADDR, "entity": 4711, "no_incoming_txs": 3, "no_outgoing_txs": 2,
                                           "balance": {"value": 1000, "fiat_values": []}, "total_received": {"value": 5000}, "total_spent": {"value": 4000}})
    res = run("connector.graphsense", ADDR)
    assert res.status == "ok" and {x["source"]: x["outcome"] for x in res.coverage} == {"GraphSense address": "hits", "GraphSense tags": "hits"}
    c.post(f"/cases/{case}/rebuild")
    wallet = next(e for e in c.get(f"/cases/{case}/entities").json() if e["schema_name"] == "CryptoWallet")
    d = json.dumps(c.get(f"/cases/{case}/entities/{wallet['cluster_id']}").json())
    assert "Example Exchange [exchange] per tagpack-x" in d and "(abuse)" in d and "graphsenseCluster" in d
    assert not any("authorization" in r["headers"] for r in server.requests)                              # the spec defines no auth


def test_graphsense_no_tags_is_not_a_clean_bill_and_failure_is_unknown(rig):
    c, server, run, case = rig
    server.add("/btc/addresses/" + ADDR + "/entity", {"entity": 1, "no_addresses": 1})
    server.add("/btc/addresses/" + ADDR + "/tags", {"address_tags": []})
    server.add("/btc/addresses/" + ADDR, {"address": ADDR, "balance": {"value": 0}})
    cov = {x["source"]: x for x in run("connector.graphsense", ADDR).coverage}
    assert cov["GraphSense tags"]["outcome"] == "no_hits" and "not evidence the address is clean" in cov["GraphSense tags"]["note"]
    server.routes.clear()
    server.add("/btc/addresses/" + ADDR, "boom", status=500, ctype="text/plain")
    r = run("connector.graphsense", ADDR)
    assert r.coverage[0]["outcome"] == "unknown" and "500" in r.coverage[0]["note"]
    server.routes.clear()
    server.add("/btc/addresses/" + ADDR, {"unexpected": True})
    assert "not a GraphSense" in run("connector.graphsense", ADDR).coverage[0]["note"]


# ------------------------------------------------------------------ mempool.space mode of Esplora
def test_after_txid_pagination_for_mempool_space(rig):
    c, server, run, case = rig
    page1 = [tx(f"{i:064x}", recv=1000) for i in range(25)]
    page2 = [tx(f"{i:064x}", recv=1000) for i in range(25, 28)]
    server.add_fn(f"/address/{ADDR}/txs", lambda req: (200, "application/json", page2 if req["query"].get("after_txid") == f"{24:064x}" else page1, {}))
    server.add(f"/address/{ADDR}", stats(28))
    r = run("connector.esplora", ADDR, pages=2, pagination="after_txid")
    assert r.stats["transactions"] == 28 and any(q["query"].get("after_txid") == f"{24:064x}" for q in server.requests)


# ------------------------------------------------------------------ Blockchair (written from the 2021 documentation copy)
BTC = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"


def bc_body(txs, *, count=None, code=200):
    return {"data": {BTC: {"address": {"type": "pubkeyhash", "balance": 6812392291, "received": 6812392291, "spent": 0,
                                       "transaction_count": len(txs) if count is None else count,
                                       "first_seen_receiving": "2009-01-03 18:15:05", "last_seen_receiving": "2019-10-24 18:47:23"},
                           "transactions": txs, "utxo": []}},
            "context": {"code": code, "state": 600897, "results": 1, "limit": "100,0"}}


def enable_blockchair(c):
    r = c.post("/plugins", json={"path": str(CONN / "blockchair")})
    assert r.status_code == 201, r.text
    c.post(f"/plugins/{r.json()['id']}/{r.json()['version']}/enabled")


def test_blockchair_maps_a_wallet_and_net_changes_and_keeps_the_key_out_of_evidence(rig, monkeypatch):
    c, server, run, case = rig
    enable_blockchair(c)
    monkeypatch.setenv("BLOCKCHAIR_API_KEY", "bc-secret-key-77")
    server.add(f"/bitcoin/dashboards/address/{BTC}", bc_body([
        {"block_id": 600890, "hash": "ab" * 32, "time": "2019-10-24 18:47:23", "balance_change": 267582},
        {"block_id": -1, "hash": "cd" * 32, "time": "2019-10-25 01:00:00", "balance_change": -500}]))
    res = run("connector.blockchair", BTC)
    assert res.status == "ok" and res.stats == {"transactions": 2, "total_reported": 2}
    q = server.requests[-1]["query"]
    assert q["transaction_details"] == "true" and q["limit"] == "100,0" and q["key"] == "bc-secret-key-77"
    c.post(f"/cases/{case}/rebuild")
    flat = json.dumps([c.get(f"/cases/{case}/entities/{e['cluster_id']}").json() for e in c.get(f"/cases/{case}/entities").json()])
    assert "267582" in flat and "payer" in flat and "unconfirmed" in flat and "2019-10-24T18:47:23Z" in flat and "not stated by the documentation" in flat
    assert "bc-secret-key-77" not in flat
    for a in c.get(f"/cases/{case}/artifacts").json():
        assert "bc-secret-key-77" not in json.dumps(a)


def test_blockchair_limits_junk_and_shape_changes_are_unknown_never_empty(rig, monkeypatch):
    c, server, run, case = rig
    enable_blockchair(c)
    monkeypatch.delenv("BLOCKCHAIR_API_KEY", raising=False)
    server.add(f"/bitcoin/dashboards/address/{BTC}", "slow down", status=402, ctype="text/plain")
    r = run("connector.blockchair", BTC)
    assert r.coverage[0]["outcome"] == "unknown" and "402" in r.coverage[0]["note"] and "key" not in server.requests[-1]["query"]
    for status in (429, 430, 435):
        server.routes.clear()
        server.add(f"/bitcoin/dashboards/address/{BTC}", "x", status=status, ctype="text/plain")
        assert run("connector.blockchair", BTC).coverage[0]["outcome"] == "unknown"
    server.routes.clear()
    server.add(f"/bitcoin/dashboards/address/{BTC}", {"data": None, "context": {"code": 404, "error": "Address not found"}})
    n = run("connector.blockchair", BTC).coverage[0]
    assert n["outcome"] == "unknown" and "Address not found" in n["note"]
    server.routes.clear()
    server.add(f"/bitcoin/dashboards/address/{BTC}", {"data": {BTC: {"surprise": 1}}, "context": {"code": 200}})
    assert "documented address dashboard shape" in run("connector.blockchair", BTC).coverage[0]["note"]
    server.routes.clear()
    server.add(f"/bitcoin/dashboards/address/{BTC}", bc_body([{"block_id": 1, "hash": "ee" * 32, "time": "2020-01-01 00:00:00", "balance_change": 5}, {"nonsense": 1}], count=40))
    cov = {x["source"]: x["outcome"] for x in run("connector.blockchair", BTC).coverage}
    assert cov == {"Blockchair address": "hits", "Blockchair history": "unknown", "Blockchair transactions": "unknown"}
    assert run("connector.blockchair", "not an address!").status == "error"
