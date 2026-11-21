"""Ethereum / EVM address activity from the Etherscan API V2.

Written against Etherscan's published reference for module=account, action=balance and action=txlist
(request parameters chainid/address/startblock/endblock/page/offset/sort/apikey; response status,
message, result; values in wei as strings). Verified against recorded fixtures only, NOT the live service.

Two things the reference does not pin down, handled conservatively:
  * status "0" means either an error or "no records", and the page does not give the empty-case text.
    Only an empty result list with a message beginning "No transactions" is reported as no hits; every
    other status "0" (including rate limiting) is UNKNOWN with Etherscan's own message.
  * no total transaction count is returned, so a full page means older history may exist; that is
    reported as unknown rather than silently dropped.

It does not say who controls an address, and a contract call is a transaction like any other here.
"""

import re
from datetime import datetime, timezone

from astral import sdk

ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _ok(body):
    if not isinstance(body, dict) or "status" not in body or "result" not in body:
        raise sdk.CapabilityFailed("not an Etherscan answer (no status/result)")
    return body


def _fail(body):
    detail = body["result"] if isinstance(body["result"], str) else ""
    return sdk.CapabilityFailed(f"Etherscan refused: {body.get('message')}" + (f" ({detail})" if detail else ""))


def invoke(request):
    inp, cfg = request["input"], request["config"]
    ns = request["context"]["entity_namespace"]
    key = request["secrets"].get("ETHERSCAN_API_KEY")
    if not key:
        yield sdk.result(coverage=[sdk.coverage("Etherscan", unknown="ETHERSCAN_API_KEY is not set")])
        return
    addr = str(inp["address"]).strip()
    if not ADDRESS.match(addr):
        yield sdk.result(status="error", error="not an EVM address (0x followed by 40 hex digits)")
        return
    chain = str(cfg["chainid"])
    base = {"chainid": chain, "module": "account", "address": addr, "apikey": key}
    offset, pages = cfg["page_size"], cfg["pages"]
    try:
        res, bal = yield from sdk.fetch_json(cfg["base_url"], params={**base, "action": "balance", "tag": "latest"})
        if _ok(bal)["status"] != "1" or not isinstance(bal["result"], str) or not bal["result"].isdigit():
            raise _fail(bal)
        evidence, rows = [res["sha256"]], []
        for page in range(1, pages + 1):
            r, body = yield from sdk.fetch_json(cfg["base_url"], params={**base, "action": "txlist", "startblock": 0, "endblock": 999999999,
                                                                          "page": page, "offset": offset, "sort": "desc"})
            if _ok(body)["status"] != "1":
                if body["result"] == [] and str(body.get("message", "")).startswith("No transactions"):
                    break
                raise _fail(body)
            if not isinstance(body["result"], list):
                raise sdk.CapabilityFailed("unexpected txlist result shape")
            evidence.append(r["sha256"])
            rows += body["result"]
            if len(body["result"]) < offset:
                break
    except sdk.CapabilityFailed as e:
        yield sdk.result(coverage=[sdk.coverage("Etherscan", unknown=str(e))])
        return

    cur = "ETH" if chain == "1" else f"native token of chain {chain}"
    wallet = sdk.entity_id(ns, "evm-address", f"{chain}:{addr.lower()}")
    out = [sdk.a_value(wallet, "schema", "CryptoWallet", evidence, 2), sdk.a_value(wallet, "publicKey", addr, evidence, 2),
           sdk.a_value(wallet, "name", addr, evidence, 2), sdk.a_value(wallet, "currency", cur, evidence, 2),
           sdk.a_value(wallet, "balanceWei", bal["result"], evidence, 2), sdk.a_value(wallet, "chainId", chain, evidence, 2)]
    for t in rows:
        pay = sdk.entity_id(ns, "evm-tx-flow", f"{chain}:{t['hash']}:{addr.lower()}")
        incoming = str(t.get("to", "")).lower() == addr.lower() and str(t.get("from", "")).lower() != addr.lower()
        other = t.get("from") if incoming else t.get("to")
        out += [sdk.a_value(pay, "schema", "Payment", evidence, 2), sdk.a_value(pay, "transactionId", t["hash"], evidence, 2),
                sdk.a_value(pay, "currency", cur, evidence, 2), sdk.a_value(pay, "amount", str(t.get("value", "0")), evidence, 2),
                sdk.a_value(pay, "amountUnit", "wei", evidence, 2),
                sdk.a_edge(pay, "beneficiary" if incoming else "payer", wallet, evidence, 2)]
        if str(t.get("timeStamp", "")).isdigit():
            out.append(sdk.a_value(pay, "date", datetime.fromtimestamp(int(t["timeStamp"]), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), evidence, 2))
        if other:
            out.append(sdk.a_value(pay, "counterpartyAddress", other, evidence, 2))
        elif t.get("contractAddress"):
            out.append(sdk.a_value(pay, "createdContract", t["contractAddress"], evidence, 2))
        if str(t.get("isError", "0")) == "1":
            out.append(sdk.a_value(pay, "status", "failed", evidence, 2))
    yield sdk.emit(out)
    cov = [sdk.coverage("Etherscan address", hits=1 if rows else 0,
                        note="activity on this address; says nothing about who controls it" if rows else
                        "no transactions returned for this chain; not evidence about other chains or addresses")]
    if rows and len(rows) >= offset * pages:
        cov.append(sdk.coverage("Etherscan history", unknown=f"the most recent {len(rows)} transactions were fetched; older activity may exist and was not examined"))
    yield sdk.result(stats={"transactions": len(rows)}, coverage=cov)


def selfcheck():
    return {"ok": bool(ADDRESS.match("0x" + "a" * 40)) and not ADDRESS.match("0x123")}
