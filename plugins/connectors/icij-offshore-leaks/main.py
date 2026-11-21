"""ICIJ Offshore Leaks Database from the published bulk CSV files.

Input: `nodes` = {"entities": sha, "officers": sha, "intermediaries": sha,
"addresses": sha, "others": sha} (any subset, as ingested artifacts) and
`relationships` = sha of relationships.csv. Names are matched in the node files;
for each hit its relationships are followed one hop. Being in the database is
not an accusation: it records an appearance in a leak, which has many innocent
explanations. The mapping keeps the leak name (`sourceID`) on every record.
"""

import csv

from astral import names, sdk

KIND_SCHEMA = {"entities": "Company", "officers": "LegalEntity", "intermediaries": "LegalEntity",
               "addresses": "Address", "others": "LegalEntity"}
ROLE_SCHEMA = (("shareholder", "Ownership"), ("beneficiary", "Ownership"), ("owner", "Ownership"),
               ("director", "Directorship"), ("secretary", "Directorship"), ("president", "Directorship"),
               ("officer", "Directorship"))


def _rows(path):
    with open(path, encoding="utf-8", newline="") as f:
        yield from csv.DictReader(f)


def invoke(request):
    inp, cfg = request["input"], request["config"]
    ns = request["context"]["entity_namespace"]
    paths = {}
    try:
        for kind, sha in (inp.get("nodes") or {}).items():
            paths[kind] = (sha, (yield from sdk.call("artifact.path", sha256=sha, verify=True))["path"])
        rel_path = None
        if inp.get("relationships"):
            rel_path = (yield from sdk.call("artifact.path", sha256=inp["relationships"], verify=True))["path"]
    except sdk.CapabilityFailed as e:
        yield sdk.result(coverage=[sdk.coverage("ICIJ Offshore Leaks", unknown=str(e))])
        return

    query = inp["name"]
    hits, scanned = [], 0
    for kind, (sha, path) in paths.items():
        if kind == "addresses":
            continue
        for r in _rows(path):
            scanned += 1
            n = r.get("name") or ""
            s = max(names.score(query, n, org=kind == "entities").score,
                    names.score(query, r.get("original_name") or "", org=True).score if r.get("original_name") else 0)
            if s >= cfg["min_match"]:
                hits.append((s, kind, sha, r))
    hits.sort(key=lambda t: -t[0])
    hits = hits[: cfg["limit"]]
    hit_ids = {r["node_id"] for _, _, _, r in hits}

    # one-hop relationships touching a hit
    rels = []
    if rel_path and hit_ids:
        for r in _rows(rel_path):
            if r["node_id_start"] in hit_ids or r["node_id_end"] in hit_ids:
                rels.append(r)
    need = {r["node_id_start"] for r in rels} | {r["node_id_end"] for r in rels}
    nodes = {r["node_id"]: (kind, sha, r) for _, kind, sha, r in hits}
    for kind, (sha, path) in paths.items():
        for r in _rows(path):
            if r["node_id"] in need and r["node_id"] not in nodes:
                nodes[r["node_id"]] = (kind, sha, r)

    out = []
    for nid, (kind, sha, r) in nodes.items():
        eid, ev = sdk.entity_id(ns, "icij", nid), [sha]
        out.append(sdk.a_value(eid, "schema", KIND_SCHEMA[kind], ev, 2))
        for pred, col in (("name", "name"), ("alias", "former_name"), ("jurisdiction", "country_codes"),
                          ("status", "status"), ("address", "address"), ("sourceDataset", "sourceID")):
            if r.get(col):
                out.append(sdk.a_value(eid, pred, r[col], ev, 2))
        if kind == "addresses" and r.get("address"):
            out.append(sdk.a_value(eid, "name", r["address"], ev, 2))
        for col, pred in (("incorporation_date", "incorporationDate"), ("inactivation_date", "dissolutionDate")):
            iso = sdk.iso_date(r[col]) if r.get(col) else None
            if iso:
                out.append(sdk.a_value(eid, pred, iso, ev, 2))
    for r in rels:
        if r["node_id_start"] not in nodes or r["node_id_end"] not in nodes:
            continue
        link = (r.get("link") or r.get("rel_type") or "").lower()
        schema = next((s for key, s in ROLE_SCHEMA if key in link), "Associate")
        rel = sdk.entity_id(ns, "icij-rel", f"{r['node_id_start']}>{r['node_id_end']}:{link}")
        a, b = (sdk.entity_id(ns, "icij", r["node_id_start"]), sdk.entity_id(ns, "icij", r["node_id_end"]))
        ev = [inp["relationships"]]
        pa, pb = {"Ownership": ("owner", "asset"), "Directorship": ("director", "organization"),
                  "Associate": ("person", "associate")}[schema]
        out += [sdk.a_value(rel, "schema", schema, ev, 2), sdk.a_value(rel, "role", link or "related", ev, 2),
                sdk.a_edge(rel, pa, a, ev, 2), sdk.a_edge(rel, pb, b, ev, 2)]
        for col, pred in (("start_date", "startDate"), ("end_date", "endDate")):
            iso = sdk.iso_date(r[col]) if r.get(col) else None
            if iso:
                out.append(sdk.a_value(rel, pred, iso, ev, 2))
    yield sdk.emit(out)
    yield sdk.result(
        stats={"nodes": len(hits), "relationships": len(rels), "scanned": scanned},
        coverage=[sdk.coverage("ICIJ Offshore Leaks files", hits=len(hits),
                               note=f"scanned {scanned} node rows in local files; an appearance is not an accusation")])


def selfcheck():
    return {"ok": sdk.iso_date("12-JAN-2001") == "2001-01-12"}
