"""GLEIF Global LEI Index (JSON:API, keyless, CC0).

Written against the documented response shape (data[].attributes.entity /
.registration, relationships.direct-parent). Verified against recorded
fixtures only, NOT against the live service in this repository's tests.
"""

from astral import sdk


def _addr(a):
    if not a:
        return None
    parts = [*(a.get("addressLines") or []), a.get("city"), a.get("region"),
             a.get("postalCode"), a.get("country")]
    return ", ".join(p for p in parts if p) or None


def _company(ns, rec, ev):
    at = rec["attributes"]
    ent, reg = at.get("entity") or {}, at.get("registration") or {}
    lei = at.get("lei") or rec["id"]
    eid = sdk.entity_id(ns, "company-lei", lei)
    out = [sdk.a_value(eid, "schema", "Company", ev, 2),
           sdk.a_value(eid, "leiCode", lei, ev, 2)]
    name = (ent.get("legalName") or {}).get("name")
    if name:
        out.append(sdk.a_value(eid, "name", name, ev, 2))
    for o in ent.get("otherNames") or []:
        out.append(sdk.a_value(eid, "previousName" if o.get("type") == "PREVIOUS_LEGAL_NAME" else "alias",
                               o["name"], ev, 2))
    if ent.get("jurisdiction"):
        out.append(sdk.a_value(eid, "jurisdiction", ent["jurisdiction"], ev, 2))
    if ent.get("registeredAs"):
        out.append(sdk.a_value(eid, "registrationNumber", ent["registeredAs"], ev, 2))
    for key in ("legalAddress", "headquartersAddress"):
        a = _addr(ent.get(key))
        if a:
            out.append(sdk.a_value(eid, "address", a, ev, 2))
    if ent.get("status"):
        out.append(sdk.a_value(eid, "status", ent["status"], ev, 2))
    if reg.get("status"):
        out.append(sdk.a_value(eid, "leiStatus", reg["status"], ev, 2))
    return eid, lei, out


def invoke(request):
    inp, cfg = request["input"], request["config"]
    ns = request["context"]["entity_namespace"]
    base = cfg["base_url"].rstrip("/")
    params = {"filter[fulltext]": inp["name"], "page[size]": cfg["max_results"]}
    if inp.get("country"):
        params["filter[entity.legalAddress.country]"] = inp["country"].upper()
    try:
        res, body = yield from sdk.fetch_json(base + "/lei-records", params=params)
    except sdk.CapabilityFailed as e:
        yield sdk.result(coverage=[sdk.coverage("GLEIF", unknown=str(e))])
        return

    rows = body.get("data") or []
    out, parents = [], []
    for rec in rows:
        eid, lei, a = _company(ns, rec, [res["sha256"]])
        out += a
        link = ((rec.get("relationships") or {}).get("direct-parent") or {}).get("links") or {}
        if cfg["follow_parent"] and link.get("lei-record"):
            parents.append((eid, link["lei-record"]))
    yield sdk.emit(out)

    gaps = []
    for child, url in parents[:10]:
        try:
            pres, pbody = yield from sdk.fetch_json(url)
        except sdk.CapabilityFailed as e:
            gaps.append(f"parent record: {e}")
            continue
        prec = pbody.get("data")
        if not prec:
            continue
        pid, plei, pa = _company(ns, prec, [pres["sha256"]])
        rel = sdk.entity_id(ns, "ownership-lei", f"{plei}>{child}")
        ev = [pres["sha256"]]
        pa += [sdk.a_value(rel, "schema", "Ownership", ev, 2),
               sdk.a_value(rel, "role", "direct accounting-consolidation parent (GLEIF)", ev, 2),
               sdk.a_edge(rel, "owner", pid, ev, 2),
               sdk.a_edge(rel, "asset", child, ev, 2)]
        yield sdk.emit(pa)
    cov = [sdk.coverage("GLEIF", hits=len(rows))]
    if gaps:
        cov.append(sdk.coverage("GLEIF parent records", unknown="; ".join(gaps)))
    yield sdk.result(stats={"records": len(rows)}, coverage=cov)


def selfcheck():
    # Known-present / known-absent fixture for the parser, not a live probe: a
    # live check belongs to the scheduled health sweep with network enabled.
    ns = "00000000-0000-0000-0000-000000000000"
    rec = {"id": "X", "attributes": {"lei": "X", "entity": {"legalName": {"name": "T"}}}}
    _, lei, out = _company(ns, rec, ["a" * 64])
    return {"ok": lei == "X" and any(a["predicate"] == "name" for a in out)}
