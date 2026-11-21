"""Bitcoin address activity from an Esplora-compatible API.

Also serves mempool.space (base_url https://mempool.space/api, pagination after_txid): its published
address and transaction field names are the same ones. Written against Esplora's published API.md (GET /address/:address, /address/:address/txs,
/address/:address/txs/chain/:last_seen_txid, GET /tx format). That document gives field names but
no complete examples and does not define the /txs response beyond "transactions", so this assumes
those are objects in the documented transaction format. Verified against recorded fixtures only,
NOT against a live instance.

What it does not do, on purpose. It does not say who controls an address. Several inputs to one
transaction do not prove a single owner, and an output is not necessarily a payment to a person
(change goes back to the sender), so counterparties are recorded as plain values on the payment,
never as linked wallets. Amounts are satoshis, kept as such. The address checksum is not verified.
"""

import re
from datetime import datetime, timezone

from astral import sdk

ADDRESS = re.compile(r"^[A-Za-z0-9]{14,90}$")
PAGE = 25


def _sats(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _flow(tx, addr):
    """(received, sent, other addresses) for this address in this transaction."""
    recv = sum(_sats(o.get("value")) or 0 for o in tx.get("vout") or [] if o.get("scriptpubkey_address") == addr)
    sent = sum(_sats((i.get("prevout") or {}).get("value")) or 0 for i in tx.get("vin") or []
               if (i.get("prevout") or {}).get("scriptpubkey_address") == addr)
    others = []
    for side in [(i.get("prevout") or {} for i in tx.get("vin") or []), (o for o in tx.get("vout") or [])]:
        for x in side:
            a = x.get("scriptpubkey_address")
            if a and a != addr and a not in others:
                others.append(a)
    return recv, sent, others


def invoke(request):
    inp, cfg = request["input"], request["config"]
    ns = request["context"]["entity_namespace"]
    addr = str(inp["address"]).strip()
    if not ADDRESS.match(addr):
        yield sdk.result(status="error", error="not a plausible address (letters and digits, 14-90 characters)")
        return
    base = cfg["base_url"].rstrip("/")
    try:
        res, stats = yield from sdk.fetch_json(f"{base}/address/{addr}")
        chain = stats.get("chain_stats") or {}
        if "tx_count" not in chain:
            raise sdk.CapabilityFailed("the address response has no chain_stats.tx_count: not an Esplora answer")
        evidence = [res["sha256"]]
        txs, seen = [], set()
        _, first = yield from sdk.fetch_json(f"{base}/address/{addr}/txs")
        confirmed = [t for t in first if (t.get("status") or {}).get("confirmed")]
        for t in first:
            if t["txid"] not in seen:
                seen.add(t["txid"])
                txs.append(t)
        page = 1
        while page < cfg["pages"] and len(confirmed) >= PAGE * page:
            last = confirmed[-1]["txid"]
            if cfg["pagination"] == "after_txid":                      # mempool.space documents ?after_txid= on /txs
                _, more = yield from sdk.fetch_json(f"{base}/address/{addr}/txs", params={"after_txid": last})
            else:                                                     # Esplora's /txs/chain/:last_seen_txid
                _, more = yield from sdk.fetch_json(f"{base}/address/{addr}/txs/chain/{last}")
            if not more:
                break
            for t in more:
                if t["txid"] not in seen:
                    seen.add(t["txid"])
                    txs.append(t)
            confirmed += more
            page += 1
    except sdk.CapabilityFailed as e:
        yield sdk.result(coverage=[sdk.coverage("Esplora", unknown=str(e))])
        return

    wallet = sdk.entity_id(ns, "btc-address", addr)
    out = [sdk.a_value(wallet, "schema", "CryptoWallet", evidence, 2), sdk.a_value(wallet, "publicKey", addr, evidence, 2),
           sdk.a_value(wallet, "currency", "BTC", evidence, 2), sdk.a_value(wallet, "name", addr, evidence, 2)]
    for key, pred in (("tx_count", "chainTransactionCount"), ("funded_txo_sum", "chainReceivedSats"), ("spent_txo_sum", "chainSpentSats")):
        if _sats(chain.get(key)) is not None:
            out.append(sdk.a_value(wallet, pred, chain[key], evidence, 2))
    mem = stats.get("mempool_stats") or {}
    if mem.get("tx_count"):
        out.append(sdk.a_value(wallet, "unconfirmedTransactionCount", mem["tx_count"], evidence, 2))

    for t in txs:
        recv, sent, others = _flow(t, addr)
        pay = sdk.entity_id(ns, "btc-tx-flow", f"{t['txid']}:{addr}")
        net = recv - sent
        out += [sdk.a_value(pay, "schema", "Payment", evidence, 2), sdk.a_value(pay, "transactionId", t["txid"], evidence, 2),
                sdk.a_value(pay, "currency", "BTC", evidence, 2), sdk.a_value(pay, "amount", str(abs(net)), evidence, 2),
                sdk.a_value(pay, "amountUnit", "satoshis", evidence, 2),
                sdk.a_edge(pay, "beneficiary" if net >= 0 else "payer", wallet, evidence, 2)]
        bt = (t.get("status") or {}).get("block_time")
        if isinstance(bt, int):
            out.append(sdk.a_value(pay, "date", datetime.fromtimestamp(bt, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), evidence, 2))
        else:
            out.append(sdk.a_value(pay, "status", "unconfirmed", evidence, 2))
        for o in others[: cfg["max_counterparties"]]:
            out.append(sdk.a_value(pay, "counterpartyAddress", o, evidence, 2))
    yield sdk.emit(out)

    total = chain["tx_count"] + (mem.get("tx_count") or 0)
    cov = [sdk.coverage("Esplora address", hits=1 if total else 0,
                        note="activity on this address; says nothing about who controls it" if total else
                        "no recorded transactions on this chain; not evidence about other chains or addresses")]
    if len(txs) < total:
        cov.append(sdk.coverage("Esplora history", unknown=f"only {len(txs)} of {total} transactions fetched (pages={cfg['pages']}); older activity not examined"))
    yield sdk.result(stats={"transactions": len(txs), "total_reported": total}, coverage=cov)


def selfcheck():
    recv, sent, others = _flow({"vin": [{"prevout": {"scriptpubkey_address": "a" * 20, "value": 5}}],
                                "vout": [{"scriptpubkey_address": "b" * 20, "value": 3}, {"scriptpubkey_address": "a" * 20, "value": 2}]}, "a" * 20)
    return {"ok": (recv, sent, others) == (2, 5, ["b" * 20])}
