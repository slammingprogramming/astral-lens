"""Bitcoin-like address activity from Blockchair's address dashboard.

Written from the copy of Blockchair's documentation in github.com/Blockchair/Blockchair.Support
(API v2.0.80, July 2021): GET {base}/{chain}/dashboards/address/{address} returns {"data": {address:
{"address": {...}, "transactions": [...], "utxo": [...]}}, "context": {"code", "error", ...}}. With
transaction_details=true each transaction is {"block_id", "hash", "time", "balance_change"}. That documentation
is five years old and Blockchair's own docs page could not be read, so this connector is DEFENSIVE: any
answer that does not have the documented shape, or whose context.code is not 200, is UNKNOWN with the reason,
never "no hits". Verified against hand-written fixtures only, NOT the live service.

What it records: the address's totals and, per transaction, the net balance change for THIS address. The
dashboard gives no counterparties, so none are recorded (use the Esplora connector for those). Amounts are
documented without a unit; the magnitudes match satoshi-scale integers, so they are recorded as the raw
number with the unit "smallest unit of the chain", not converted.
"""

import re
from datetime import datetime, timezone

from astral import sdk

ADDRESS = re.compile(r"^[A-Za-z0-9:]{20,100}$")
CHAIN_CURRENCY = {"bitcoin": "BTC", "bitcoin-cash": "BCH", "litecoin": "LTC", "bitcoin-sv": "BSV", "dogecoin": "DOGE",
                  "dash": "DASH", "groestlcoin": "GRS", "zcash": "ZEC", "ecash": "XEC"}
LIMIT_CODES = {402: "daily request limit exceeded", 429: "rate limited (repeated 402s turn into an hour-long IP block)",
               430: "IP temporarily blocked", 434: "IP temporarily blocked", 435: "parallel-request limit exceeded",
               436: "request limit exceeded", 437: "request limit exceeded", 503: "service unavailable"}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _time(s):
    try:
        return datetime.strptime(str(s), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def invoke(request):
    inp, cfg = request["input"], request["config"]
    ns = request["context"]["entity_namespace"]
    addr = str(inp["address"]).strip()
    if not ADDRESS.match(addr):
        yield sdk.result(status="error", error="not a plausible address for a Bitcoin-like chain")
        return
    chain = cfg["chain"]
    params = {"limit": f"{cfg['limit']},0", "transaction_details": "true"}      # transactions only: no UTXO list
    key = request["secrets"].get("BLOCKCHAIR_API_KEY")
    if key:
        params["key"] = key
    url = f"{cfg['base_url'].rstrip('/')}/{chain}/dashboards/address/{addr}"
    try:
        res = yield from sdk.fetch(url, params=params)
        status = res["status"]
        if status in LIMIT_CODES:
            raise sdk.CapabilityFailed(f"Blockchair answered HTTP {status}: {LIMIT_CODES[status]}")
        if not 200 <= status < 300:
            raise sdk.CapabilityFailed(f"HTTP {status} from Blockchair")
        import json

        try:
            body = json.loads(res.get("text") or "")
        except ValueError as e:
            raise sdk.CapabilityFailed("Blockchair returned a body that is not JSON") from e
    except sdk.CapabilityFailed as e:
        yield sdk.result(coverage=[sdk.coverage("Blockchair", unknown=str(e))])
        return
    ctx = body.get("context") if isinstance(body, dict) else None
    if not isinstance(ctx, dict) or ctx.get("code") != 200:
        why = (ctx or {}).get("error") if isinstance(ctx, dict) else None
        yield sdk.result(coverage=[sdk.coverage("Blockchair", unknown=f"not a successful Blockchair answer (code {(ctx or {}).get('code')}"
                                                                        + (f": {why}" if why else "") + ")")])
        return
    data = body.get("data")
    entry = data.get(addr) if isinstance(data, dict) else None
    info = entry.get("address") if isinstance(entry, dict) else None
    if not isinstance(info, dict) or not _num(info.get("transaction_count")) or not isinstance(entry.get("transactions"), list):
        yield sdk.result(coverage=[sdk.coverage("Blockchair", unknown="the answer does not have the documented address dashboard shape; "
                                                                        "the API may have changed since its 2021 documentation")])
        return
    txs = [t for t in entry["transactions"] if isinstance(t, dict) and isinstance(t.get("hash"), str) and _num(t.get("balance_change"))]
    skipped = len(entry["transactions"]) - len(txs)
    evidence = [res["sha256"]]
    cur = CHAIN_CURRENCY[chain]
    wallet = sdk.entity_id(ns, "btc-like-address", f"{chain}:{addr}")
    out = [sdk.a_value(wallet, "schema", "CryptoWallet", evidence, 2), sdk.a_value(wallet, "publicKey", addr, evidence, 2),
           sdk.a_value(wallet, "name", addr, evidence, 2), sdk.a_value(wallet, "currency", cur, evidence, 2),
           sdk.a_value(wallet, "chain", chain, evidence, 2), sdk.a_value(wallet, "chainTransactionCount", info["transaction_count"], evidence, 2)]
    for field, pred in (("received", "chainReceivedSats"), ("spent", "chainSpentSats")):
        if _num(info.get(field)):
            out.append(sdk.a_value(wallet, pred, info[field], evidence, 2))
    for field, pred in (("first_seen_receiving", "firstSeenReceiving"), ("last_seen_receiving", "lastSeenReceiving")):
        if _time(info.get(field)):
            out.append(sdk.a_value(wallet, pred, _time(info[field]), evidence, 2))
    for t in txs:
        pay = sdk.entity_id(ns, "btc-like-tx-flow", f"{chain}:{t['hash']}:{addr}")
        change = t["balance_change"]
        out += [sdk.a_value(pay, "schema", "Payment", evidence, 2), sdk.a_value(pay, "transactionId", t["hash"], evidence, 2),
                sdk.a_value(pay, "currency", cur, evidence, 2), sdk.a_value(pay, "amount", str(abs(change)), evidence, 2),
                sdk.a_value(pay, "amountUnit", "smallest unit of the chain (as reported; unit not stated by the documentation)", evidence, 2),
                sdk.a_edge(pay, "beneficiary" if change >= 0 else "payer", wallet, evidence, 2)]
        if _time(t.get("time")):
            out.append(sdk.a_value(pay, "date", _time(t["time"]), evidence, 2))
        if t.get("block_id") == -1:
            out.append(sdk.a_value(pay, "status", "unconfirmed", evidence, 2))
    yield sdk.emit(out)
    total = int(info["transaction_count"])
    cov = [sdk.coverage("Blockchair address", hits=1 if total else 0,
                        note="activity on this address; says nothing about who controls it" if total else
                        "no recorded transactions on this chain; not evidence about other chains or addresses")]
    if len(txs) < total:
        cov.append(sdk.coverage("Blockchair history", unknown=f"only {len(txs)} of {total} transactions were returned; older activity was not examined"))
    if skipped:
        cov.append(sdk.coverage("Blockchair transactions", unknown=f"{skipped} transaction(s) did not have the documented shape and were not recorded"))
    yield sdk.result(stats={"transactions": len(txs), "total_reported": total}, coverage=cov)


def selfcheck():
    return {"ok": bool(ADDRESS.match("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")) and not ADDRESS.match("0x1") and _time("2019-10-24 18:47:23") == "2019-10-24T18:47:23Z"}
