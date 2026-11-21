"""OpenCorporates company search (token required; ~200 calls/month free)."""

from astral import sdk


def invoke(request):
    inp, cfg = request["input"], request["config"]
    ns = request["context"]["entity_namespace"]
    token = request["secrets"].get("OPENCORPORATES_API_TOKEN")
    if not token:
        yield sdk.result(coverage=[sdk.coverage(
            "OpenCorporates", unknown="OPENCORPORATES_API_TOKEN is not configured "
                                      "(the API no longer serves anonymous requests)")])
        return
    params = {"q": inp["name"], "per_page": cfg["max_results"], "api_token": token}
    if inp.get("country"):
        params["country_code"] = inp["country"].lower()
    try:
        res, body = yield from sdk.fetch_json(cfg["base_url"].rstrip("/") + "/companies/search", params=params)
    except sdk.CapabilityFailed as e:
        yield sdk.result(coverage=[sdk.coverage("OpenCorporates", unknown=str(e))])
        return

    rows = [c["company"] for c in (body.get("results") or {}).get("companies") or []]
    out, ev = [], [res["sha256"]]
    for c in rows:
        eid = sdk.entity_id(ns, "company-oc", f"{c['jurisdiction_code']}/{c['company_number']}")
        out += [sdk.a_value(eid, "schema", "Company", ev, 2),
                sdk.a_value(eid, "name", c["name"], ev, 2),
                sdk.a_value(eid, "registrationNumber", c["company_number"], ev, 2),
                sdk.a_value(eid, "jurisdiction", c["jurisdiction_code"], ev, 2)]
        for k, pred in (("incorporation_date", "incorporationDate"), ("dissolution_date", "dissolutionDate"),
                        ("current_status", "status"), ("company_type", "legalForm"),
                        ("registered_address_in_full", "address"),
                        ("opencorporates_url", "opencorporatesUrl")):
            if c.get(k):
                out.append(sdk.a_value(eid, pred, c[k], ev, 2))
        for pn in c.get("previous_names") or []:
            out.append(sdk.a_value(eid, "previousName", pn["company_name"], ev, 2))
    yield sdk.emit(out)
    yield sdk.result(stats={"companies": len(rows)}, coverage=[sdk.coverage("OpenCorporates", hits=len(rows))])


def selfcheck():
    return {"ok": True}
