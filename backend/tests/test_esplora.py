"""M6: Esplora-style Bitcoin connector. HONEST SCOPE: a local server returns hand-written responses
in the field names Esplora's API.md documents. This proves the mapping, pagination and the
unknown/partial handling; it does not prove a live Esplora instance answers this way."""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from astral.api import create_app
from astral.config import Settings

from conftest import REPO

ADDR, OTHER, CHANGE = "bc1qexampleaddress000000000000000000000", "bc1qcounterparty0000000000000000000000", "bc1qchange00000000000000000000000000000"


def tx(txid, *, recv=0, sent=0, height=800000, time=1700000000):
    vin = [{"txid": "f" * 64, "vout": 0, "prevout": {"scriptpubkey_address": ADDR if sent else OTHER, "value": sent or 9000}}]
    vout = [{"scriptpubkey_address": ADDR if recv else OTHER, "value": recv or sent - 100}]
    if recv and not sent:
        vout.append({"scriptpubkey_address": CHANGE, "value": 100})
    return {"txid": txid, "fee": 100, "vin": vin, "vout": vout,
            "status": {"confirmed": height is not None, "block_height": height, "block_time": time if height else None}}


@pytest.fixture()
def rig(db, dsn, tmp_path, server, actor):
    app = create_app(Settings(dsn=dsn, data_dir=tmp_path), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        r = c.post("/plugins", json={"path": str(REPO / "plugins" / "connectors" / "esplora")})
        assert r.status_code == 201, r.text
        c.post(f"/plugins/{r.json()['id']}/{r.json()['version']}/enabled")
        case = uuid.UUID(c.post("/cases", json={"name": "btc"}).json()["id"])

        def run(address=ADDR, **cfg):
            with db.connection() as conn:
                return app.state.host.invoke(conn, actor, case, "connector.esplora", {"address": address},
                                             config={"base_url": server.url, **cfg})
        yield c, server, run, case


def stats(total, mem=0):
    return {"address": ADDR, "chain_stats": {"tx_count": total, "funded_txo_count": 2, "funded_txo_sum": 12000, "spent_txo_count": 1, "spent_txo_sum": 4000},
            "mempool_stats": {"tx_count": mem, "funded_txo_count": 0, "funded_txo_sum": 0, "spent_txo_count": 0, "spent_txo_sum": 0}}


def test_address_history_maps_to_a_wallet_and_payments_without_claiming_ownership(rig):
    c, server, run, case = rig
    server.add(f"/address/{ADDR}/txs", [tx("a" * 64, recv=12000), tx("b" * 64, sent=4100), tx("c" * 64, recv=500, height=None)])
    server.add(f"/address/{ADDR}", stats(2, mem=1))
    res = run()
    assert res.status == "ok" and res.stats == {"transactions": 3, "total_reported": 3}
    assert [x["outcome"] for x in res.coverage] == ["hits"] and "who controls" in res.coverage[0]["note"]
    c.post(f"/cases/{case}/rebuild")
    ents = c.get(f"/cases/{case}/entities").json()
    assert sorted(e["schema_name"] for e in ents) == ["CryptoWallet", "Payment", "Payment", "Payment"]
    wallet = next(e for e in ents if e["schema_name"] == "CryptoWallet")
    flat = json.dumps(c.get(f"/cases/{case}/entities/{wallet['cluster_id']}").json())
    assert ADDR in flat and "12000" in flat
    pays = " ".join(json.dumps(c.get(f"/cases/{case}/entities/{e['cluster_id']}").json()) for e in ents if e["schema_name"] == "Payment")
    assert OTHER in pays and CHANGE in pays and "satoshis" in pays and "unconfirmed" in pays
    assert not any(e["schema_name"] == "CryptoWallet" and e["cluster_id"] != wallet["cluster_id"] for e in ents)   # counterparties are values, not wallets


def test_pagination_follows_last_seen_txid_and_truncation_is_reported_as_unknown(rig):
    c, server, run, case = rig
    page1 = [tx(f"{i:064x}", recv=1000) for i in range(25)]
    page2 = [tx(f"{i:064x}", recv=1000) for i in range(25, 30)]
    server.add(f"/address/{ADDR}/txs/chain/{24:064x}", page2)
    server.add(f"/address/{ADDR}/txs", page1)
    server.add(f"/address/{ADDR}", stats(30))
    one = run()
    assert one.stats["transactions"] == 25
    assert any(x["source"] == "Esplora history" and x["outcome"] == "unknown" and "25 of 30" in x["note"] for x in one.coverage)
    two = run(pages=2)
    assert two.stats["transactions"] == 30 and not any(x["outcome"] == "unknown" for x in two.coverage)


def test_failures_and_non_esplora_answers_are_unknown_never_empty(rig):
    c, server, run, case = rig
    server.add(f"/address/{ADDR}", "overloaded", status=503, ctype="text/plain")
    r = run()
    assert r.coverage[0]["outcome"] == "unknown" and "503" in r.coverage[0]["note"] and r.assertion_ids == []
    server.routes.clear()
    server.add(f"/address/{ADDR}", {"surprise": True})                              # valid JSON, wrong shape
    r = run()
    assert r.coverage[0]["outcome"] == "unknown" and "not an Esplora answer" in r.coverage[0]["note"]


def test_a_quiet_address_is_a_no_hit_that_is_not_evidence_about_anything_else(rig):
    c, server, run, case = rig
    server.add(f"/address/{ADDR}/txs", [])
    server.add(f"/address/{ADDR}", {**stats(0), "chain_stats": {"tx_count": 0, "funded_txo_count": 0, "funded_txo_sum": 0, "spent_txo_count": 0, "spent_txo_sum": 0}})
    r = run()
    assert [x["outcome"] for x in r.coverage] == ["no_hits"] and "not evidence" in r.coverage[0]["note"]


def test_implausible_addresses_never_reach_the_network(rig):
    c, server, run, case = rig
    before = len(server.requests)
    for bad in ("short", "../../etc/passwd", "bc1q x"):
        assert run(bad).status == "error"
    assert len(server.requests) == before
