"""UK Companies House public data API (free key; HTTP Basic, key as username).

Search, company profile, officers, persons with significant control. Written to
the documented response shapes; verified against recorded fixtures only.
"""

import base64
import re

from astral import sdk


def _natural(name):
    """'DOE, Jane Q' -> 'Jane Q Doe'. The original form is kept as an alias."""
    if "," in name:
        sur, _, rest = name.partition(",")
        return f"{rest.strip()} {sur.strip().title()}".strip()
    return name


def _partial_dob(d):
    if not d or not d.get("year"):
        return None
    return f"{d['year']}-{int(d['month']):02d}" if d.get("month") else str(d["year"])


def _addr(a):
    if not a:
        return None
    keys = ("premises", "address_line_1", "address_line_2", "locality", "region",
            "postal_code", "country")
    return ", ".join(str(a[k]) for k in keys if a.get(k)) or None


def invoke(request):
    inp, cfg = request["input"], request["config"]
    ns = request["context"]["entity_namespace"]
    key = request["secrets"].get("COMPANIES_HOUSE_API_KEY")
    if not key:
        yield sdk.result(coverage=[sdk.coverage(
            "Companies House", unknown="COMPANIES_HOUSE_API_KEY is not configured")])
        return
    hdr = {"Authorization": "Basic " + base64.b64encode(f"{key}:".encode()).decode()}
    base = cfg["base_url"].rstrip("/")
    try:
        sres, search = yield from sdk.fetch_json(
            base + "/search/companies", headers=hdr,
            params={"q": inp["name"], "items_per_page": cfg["max_companies"]})
    except sdk.CapabilityFailed as e:
        yield sdk.result(coverage=[sdk.coverage("Companies House", unknown=str(e))])
        return

    items = (search.get("items") or [])[: cfg["max_companies"]]
    gaps, total = [], 0
    for it in items:
        num = it["company_number"]
        try:
            pres, prof = yield from sdk.fetch_json(f"{base}/company/{num}", headers=hdr)
        except sdk.CapabilityFailed as e:
            gaps.append(f"{num}: {e}")
            continue
        ev = [pres["sha256"]]
        co = sdk.entity_id(ns, "company-ch", num)
        out = [sdk.a_value(co, "schema", "Company", ev, 2),
               sdk.a_value(co, "name", prof["company_name"], ev, 2),
               sdk.a_value(co, "registrationNumber", num, ev, 2),
               sdk.a_value(co, "jurisdiction", "gb", ev, 2)]
        for k, pred in (("date_of_creation", "incorporationDate"), ("date_of_cessation", "dissolutionDate"),
                        ("company_status", "status"), ("type", "legalForm")):
            if prof.get(k):
                out.append(sdk.a_value(co, pred, prof[k], ev, 2))
        if _addr(prof.get("registered_office_address")):
            out.append(sdk.a_value(co, "address", _addr(prof["registered_office_address"]), ev, 2))
        for pn in prof.get("previous_company_names") or []:
            out.append(sdk.a_value(co, "previousName", pn["name"], ev, 2))
        yield sdk.emit(out)
        total += 1

        if cfg["include_officers"]:
            try:
                ores, offs = yield from sdk.fetch_json(f"{base}/company/{num}/officers", headers=hdr)
            except sdk.CapabilityFailed as e:
                gaps.append(f"{num} officers: {e}")
            else:
                yield sdk.emit(_officers(ns, co, num, offs, ores["sha256"]))
        if cfg["include_psc"]:
            try:
                cres, psc = yield from sdk.fetch_json(
                    f"{base}/company/{num}/persons-with-significant-control", headers=hdr)
            except sdk.CapabilityFailed as e:
                gaps.append(f"{num} PSC: {e}")
            else:
                yield sdk.emit(_psc(ns, co, num, psc, cres["sha256"]))

    cov = [sdk.coverage("Companies House", hits=total)]
    if gaps:
        cov.append(sdk.coverage("Companies House (details)", unknown="; ".join(gaps)))
    yield sdk.result(stats={"companies": total}, coverage=cov)


def _person(ns, key, name, ev, out, **props):
    pid = sdk.entity_id(ns, "person-ch", key)
    out += [sdk.a_value(pid, "schema", "Person", ev, 2),
            sdk.a_value(pid, "name", _natural(name), ev, 2)]
    if _natural(name) != name:
        out.append(sdk.a_value(pid, "alias", name, ev, 2))
    for k, v in props.items():
        if v:
            out.append(sdk.a_value(pid, k, v, ev, 2))
    return pid


def _officers(ns, co, num, body, sha):
    ev, out = [sha], []
    for o in body.get("items") or []:
        link = ((o.get("links") or {}).get("officer") or {}).get("appointments", "")
        m = re.search(r"/officers/([^/]+)/appointments", link)
        key = m.group(1) if m else f"{num}:{o['name']}:{_partial_dob(o.get('date_of_birth'))}"
        pid = _person(ns, key, o["name"], ev, out, birthDate=_partial_dob(o.get("date_of_birth")),
                      nationality=o.get("nationality"), country=o.get("country_of_residence"))
        rel = sdk.entity_id(ns, "directorship-ch", f"{num}:{key}:{o.get('appointed_on')}:{o.get('officer_role')}")
        out += [sdk.a_value(rel, "schema", "Directorship", ev, 2),
                sdk.a_value(rel, "role", o.get("officer_role", "officer"), ev, 2),
                sdk.a_edge(rel, "director", pid, ev, 2), sdk.a_edge(rel, "organization", co, ev, 2)]
        if o.get("appointed_on"):
            out.append(sdk.a_value(rel, "startDate", o["appointed_on"], ev, 2))
        if o.get("resigned_on"):
            out.append(sdk.a_value(rel, "endDate", o["resigned_on"], ev, 2))
    return out


def _psc(ns, co, num, body, sha):
    ev, out = [sha], []
    for p in body.get("items") or []:
        kind = p.get("kind", "")
        name = p["name"]
        if "individual" in kind:
            owner = _person(ns, f"psc:{num}:{name}:{_partial_dob(p.get('date_of_birth'))}", name, ev, out,
                            birthDate=_partial_dob(p.get("date_of_birth")),
                            nationality=p.get("nationality"), country=p.get("country_of_residence"))
        else:
            owner = sdk.entity_id(ns, "legalentity-ch", f"psc:{num}:{name}")
            out += [sdk.a_value(owner, "schema", "LegalEntity", ev, 2), sdk.a_value(owner, "name", name, ev, 2)]
            ident = p.get("identification") or {}
            if ident.get("registration_number"):
                out.append(sdk.a_value(owner, "registrationNumber", ident["registration_number"], ev, 2))
        rel = sdk.entity_id(ns, "ownership-ch", f"{num}:{name}:{p.get('notified_on')}")
        out += [sdk.a_value(rel, "schema", "Ownership", ev, 2),
                sdk.a_edge(rel, "owner", owner, ev, 2), sdk.a_edge(rel, "asset", co, ev, 2)]
        for nature in p.get("natures_of_control") or []:
            out.append(sdk.a_value(rel, "role", nature, ev, 2))
        if p.get("notified_on"):
            out.append(sdk.a_value(rel, "startDate", p["notified_on"], ev, 2))
        if p.get("ceased_on"):
            out.append(sdk.a_value(rel, "endDate", p["ceased_on"], ev, 2))
    return out


def selfcheck():
    ok = (_natural("DOE, Jane Q") == "Jane Q Doe" and _partial_dob({"year": 1970, "month": 3}) == "1970-03"
          and _partial_dob(None) is None)
    return {"ok": ok}
