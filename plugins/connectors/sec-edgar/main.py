"""SEC EDGAR: company_tickers.json for name lookup, data.sec.gov submissions.

LIMIT, stated plainly: company_tickers.json lists only registrants that have a
ticker. A registrant without one (many filers of Forms 3/4/13F/D) is invisible
to this name search; pass `cik` to look one up directly. A miss here is
therefore "not among ticker-listed registrants", never "not an SEC registrant".
"""

from astral import names, sdk


def invoke(request):
    inp, cfg = request["input"], request["config"]
    ns = request["context"]["entity_namespace"]
    hdr = {"User-Agent": f"AstralLens/0.1 {cfg['contact']}"}      # SEC requires contact details
    hits = []
    try:
        if inp.get("cik"):
            hits = [{"cik_str": int(inp["cik"]), "title": inp.get("name") or ""}]
        else:
            lres, listing = yield from sdk.fetch_json(cfg["www_url"].rstrip("/") + "/files/company_tickers.json",
                                                      headers=hdr)
            scored = sorted(((names.score(inp["name"], r["title"], org=True).score, r)
                             for r in listing.values()), key=lambda t: -t[0])
            hits = [r for s, r in scored if s >= cfg["min_match"]][: cfg["max_results"]]
    except sdk.CapabilityFailed as e:
        yield sdk.result(coverage=[sdk.coverage("SEC EDGAR", unknown=str(e))])
        return

    gaps, found = [], 0
    for h in hits:
        cik = f"{int(h['cik_str']):010d}"
        try:
            res, sub = yield from sdk.fetch_json(f"{cfg['data_url'].rstrip('/')}/submissions/CIK{cik}.json",
                                                 headers=hdr)
        except sdk.CapabilityFailed as e:
            gaps.append(f"CIK {cik}: {e}")
            continue
        ev = [res["sha256"]]
        eid = sdk.entity_id(ns, "company-sec", cik)
        out = [sdk.a_value(eid, "schema", "Company", ev, 2),
               sdk.a_value(eid, "name", sub["name"], ev, 2),
               sdk.a_value(eid, "cikNumber", cik, ev, 2),
               sdk.a_value(eid, "jurisdiction", "us", ev, 2)]
        if sub.get("ein"):
            out.append(sdk.a_value(eid, "taxNumber", sub["ein"], ev, 2))
        if sub.get("stateOfIncorporation"):
            out.append(sdk.a_value(eid, "incorporationState", sub["stateOfIncorporation"], ev, 2))
        if sub.get("sicDescription"):
            out.append(sdk.a_value(eid, "sector", sub["sicDescription"], ev, 2))
        for t in sub.get("tickers") or []:
            out.append(sdk.a_value(eid, "ticker", t, ev, 2))
        for f in sub.get("formerNames") or []:
            out.append(sdk.a_value(eid, "previousName", f["name"], ev, 2))
        for kind in ("business", "mailing"):
            a = (sub.get("addresses") or {}).get(kind) or {}
            line = ", ".join(str(a[k]) for k in ("street1", "street2", "city", "stateOrCountry", "zipCode")
                             if a.get(k))
            if line:
                out.append(sdk.a_value(eid, "address", line, ev, 2))
        yield sdk.emit(out)
        found += 1

    cov = [sdk.coverage("SEC EDGAR", hits=found,
                        note=None if found else "searched ticker-listed registrants only; not proof the entity is not a filer")]
    if gaps:
        cov.append(sdk.coverage("SEC EDGAR (details)", unknown="; ".join(gaps)))
    yield sdk.result(stats={"companies": found}, coverage=cov)


def selfcheck():
    return {"ok": names.score("Apple Inc.", "APPLE INC", org=True).score == 1.0}
