"""OFAC Specially Designated Nationals list, from an ingested SDN.XML file.

The operator downloads the list (OFAC Sanctions List Service) and ingests it as
an artifact; this plugin matches names against it. The file hash on every hit
says exactly which publication date produced it. A miss means "no entry in THIS
file matched at the configured threshold", never "not sanctioned": spelling
variants below the threshold, and the list's own gaps, are both possible.
"""

import re
from xml.etree import ElementTree as ET

from astral import names, sdk

CRYPTO = re.compile(r"Digital Currency Address - (\w+)", re.I)
ID_MAP = {"passport": "passportNumber", "tax id no.": "taxNumber", "registration id": "registrationNumber",
          "lei": "leiCode", "national id no.": "idNumber", "vessel registration identification": "imoNumber"}
SCHEMA = {"individual": "Person", "entity": "Company", "vessel": "Vessel", "aircraft": "Airplane"}


def _t(el, tag):
    for c in el:
        if c.tag.rsplit("}", 1)[-1] == tag:
            return (c.text or "").strip()
    return ""


def _kids(el, tag):
    for c in el:
        if c.tag.rsplit("}", 1)[-1] == tag:
            yield c


def _entry_names(entry):
    first, last = _t(entry, "firstName"), _t(entry, "lastName")
    main = f"{first} {last}".strip()
    akas = []
    for lst in _kids(entry, "akaList"):
        for aka in _kids(lst, "aka"):
            akas.append(f"{_t(aka, 'firstName')} {_t(aka, 'lastName')}".strip())
    return main, [a for a in akas if a]


def invoke(request):
    inp, cfg = request["input"], request["config"]
    ns = request["context"]["entity_namespace"]
    sha = inp["dataset_artifact"]
    try:
        info = yield from sdk.call("artifact.path", sha256=sha, verify=True)
    except sdk.CapabilityFailed as e:
        yield sdk.result(coverage=[sdk.coverage("OFAC SDN", unknown=str(e))])
        return
    published = None
    scanned, hits = 0, []
    try:
        for _, el in ET.iterparse(info["path"], events=("end",)):
            tag = el.tag.rsplit("}", 1)[-1]
            if tag == "Publish_Date":
                published = (el.text or "").strip()
            if tag != "sdnEntry":
                continue
            scanned += 1
            main, akas = _entry_names(el)
            is_person = _t(el, "sdnType").lower() == "individual"
            best = max((names.score(inp["name"], n, org=not is_person).score for n in [main, *akas]),
                       default=0.0)
            if best >= cfg["min_match"]:
                hits.append((best, el))
            else:
                el.clear()
    except ET.ParseError as e:
        yield sdk.result(coverage=[sdk.coverage("OFAC SDN", unknown=f"file is not valid XML: {e}")])
        return

    hits.sort(key=lambda t: -t[0])
    out = []
    for score, el in hits[: cfg["limit"]]:
        uid = _t(el, "uid")
        eid = sdk.entity_id(ns, "ofac", uid)
        main, akas = _entry_names(el)
        stype = _t(el, "sdnType").lower()
        ev = [sha]
        out += [sdk.a_value(eid, "schema", SCHEMA.get(stype, "LegalEntity"), ev, 2),
                sdk.a_value(eid, "name", main, ev, 2),
                sdk.a_value(eid, "listedIn", "OFAC SDN", ev, 2),
                sdk.a_value(eid, "sourceUid", uid, ev, 2)]
        out += [sdk.a_value(eid, "alias", a, ev, 2) for a in akas]
        for lst in _kids(el, "programList"):
            out += [sdk.a_value(eid, "sanctionProgram", (p.text or "").strip(), ev, 2) for p in _kids(lst, "program")]
        for lst in _kids(el, "dateOfBirthList"):
            for d in _kids(lst, "dateOfBirthItem"):
                iso = sdk.iso_date(_t(d, "dateOfBirth"))
                if iso:
                    out.append(sdk.a_value(eid, "birthDate", iso, ev, 2))
        for lst in _kids(el, "nationalityList"):
            out += [sdk.a_value(eid, "nationality", _t(n, "country"), ev, 2) for n in _kids(lst, "nationality")]
        for lst in _kids(el, "addressList"):
            for a in _kids(lst, "address"):
                line = ", ".join(x for x in (_t(a, "address1"), _t(a, "city"), _t(a, "country")) if x)
                if line:
                    out.append(sdk.a_value(eid, "address", line, ev, 2))
        for lst in _kids(el, "idList"):
            for i in _kids(lst, "id"):
                kind, num = _t(i, "idType"), _t(i, "idNumber")
                if not num:
                    continue
                if CRYPTO.match(kind):
                    out.append(sdk.a_value(eid, "cryptoAddress", num, ev, 2))
                elif kind.lower() in ID_MAP:
                    out.append(sdk.a_value(eid, ID_MAP[kind.lower()], num, ev, 2))
                else:
                    out.append(sdk.a_value(eid, "idNumber", f"{kind}: {num}", ev, 3))
    yield sdk.emit(out)
    note = f"scanned {scanned} entries in a local file (sha256 {sha[:12]}…)" + (
        f", published {published}" if published else "")
    yield sdk.result(stats={"entries": len(hits[: cfg["limit"]]), "scanned": scanned, "published": published},
                     coverage=[sdk.coverage("OFAC SDN file", hits=len(hits), note=note)])


def selfcheck():
    return {"ok": sdk.iso_date("1 Jan 1950") == "1950-01-01" and sdk.iso_date("1950") == "1950"}
