"""Address attribution context from a SELF-HOSTED GraphSense REST API.

Written against the OpenAPI spec of graphsense-REST: GET /{currency}/addresses/{address} (balance,
total_received, total_spent, first_tx, last_tx, no_incoming_txs, no_outgoing_txs, entity),
/{currency}/addresses/{address}/entity (entity, no_addresses, best_address_tag) and
/{currency}/addresses/{address}/tags (address_tags[].label/source/category/abuse). The spec defines
no authentication, so none is sent; point base_url at your own instance. The upstream repository is
archived ("retired", superseded by graphsense-lib), and nothing here has been run against a live
GraphSense, only recorded fixtures.

What it must not be mistaken for. An ENTITY is GraphSense's heuristic clustering of addresses
(chiefly the multiple-input heuristic); it is a guess about common control, not a finding, and is
recorded with the weakest credibility grade. A TAG is somebody else's label for the address, with its
own source and category; it is recorded as that claim, attributed, never as a fact about the holder.
No tag is not evidence the address is clean.
"""

import re
import urllib.parse

from astral import sdk

CURRENCY = re.compile(r"^[a-z]{2,6}$")
ADDRESS = re.compile(r"^[A-Za-z0-9]{14,90}$")


def _val(v):
    if isinstance(v, dict) and isinstance(v.get("value"), (int, str)) and str(v["value"]).lstrip("-").isdigit():
        return str(v["value"])
    return None


def invoke(request):
    inp, cfg = request["input"], request["config"]
    ns = request["context"]["entity_namespace"]
    addr, cur = str(inp["address"]).strip(), str(cfg["currency"]).lower()
    if not ADDRESS.match(addr) or not CURRENCY.match(cur):
        yield sdk.result(status="error", error="implausible address or currency")
        return
    base = cfg["base_url"].rstrip("/") + "/" + urllib.parse.quote(cur) + "/addresses/" + urllib.parse.quote(addr)
    try:
        res, a = yield from sdk.fetch_json(base)
        if not isinstance(a, dict) or "address" not in a:
            raise sdk.CapabilityFailed("not a GraphSense address answer")
        evidence = [res["sha256"]]
        ent = None
        if cfg["include_entity"]:
            r2, ent = yield from sdk.fetch_json(base + "/entity")
            evidence.append(r2["sha256"])
        r3, tags = yield from sdk.fetch_json(base + "/tags", params={"pagesize": cfg["max_tags"]})
        evidence.append(r3["sha256"])
    except sdk.CapabilityFailed as e:
        yield sdk.result(coverage=[sdk.coverage("GraphSense", unknown=str(e))])
        return

    wallet = sdk.entity_id(ns, "chain-address", f"{cur}:{addr}")
    out = [sdk.a_value(wallet, "schema", "CryptoWallet", evidence, 2), sdk.a_value(wallet, "publicKey", addr, evidence, 2),
           sdk.a_value(wallet, "name", addr, evidence, 2), sdk.a_value(wallet, "currency", cur.upper(), evidence, 2)]
    for field, pred in (("balance", "balanceBaseUnits"), ("total_received", "chainReceivedBaseUnits"), ("total_spent", "chainSpentBaseUnits")):
        v = _val(a.get(field))
        if v is not None:
            out.append(sdk.a_value(wallet, pred, v, evidence, 2))
    for field, pred in (("no_incoming_txs", "incomingTransactionCount"), ("no_outgoing_txs", "outgoingTransactionCount")):
        if isinstance(a.get(field), int):
            out.append(sdk.a_value(wallet, pred, a[field], evidence, 2))
    if isinstance(ent, dict) and ent.get("entity") is not None:
        out.append(sdk.a_value(wallet, "graphsenseCluster", str(ent["entity"]), evidence, 4))           # heuristic: doubtful
        if isinstance(ent.get("no_addresses"), int):
            out.append(sdk.a_value(wallet, "graphsenseClusterSize", ent["no_addresses"], evidence, 4))
    n = 0
    for t in (tags.get("address_tags") if isinstance(tags, dict) else None) or []:
        label = str(t.get("label") or "").strip()
        if not label:
            continue
        n += 1
        claim = label + (f" [{t['category']}]" if t.get("category") else "") + (" (abuse)" if t.get("abuse") else "") + \
            (f" per {t['source']}" if t.get("source") else "")
        out.append(sdk.a_value(wallet, "thirdPartyTag", claim[:300], evidence, 4))                      # someone else's claim
    yield sdk.emit(out)
    cov = [sdk.coverage("GraphSense address", hits=1, note="address known to this GraphSense instance")]
    cov.append(sdk.coverage("GraphSense tags", hits=n, note=("third-party labels, each with its own source" if n else
                                                              "no tag recorded; not evidence the address is clean")))
    yield sdk.result(stats={"tags": n}, coverage=cov)


def selfcheck():
    return {"ok": _val({"value": 5}) == "5" and _val({"value": "x"}) is None}
