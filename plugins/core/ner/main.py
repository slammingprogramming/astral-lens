"""Find entities in a document's text and link the document to them.

Identity is NOT decided here. A checksum-valid IBAN, LEI or crypto address, or
an e-mail address, is an identifier and gets one entity per value. A person or
organisation NAME is not an identifier: "John Smith" in two documents may be two
people. Names therefore get one entity per (document, name), and the
resolution step proposes links between them for a human to judge.
"""

from astral import names, sdk

PRED = {"email": "email", "phone": "phone", "iban": "iban", "lei": "leiCode",
        "btc": "cryptoAddress", "eth": "cryptoAddress", "url": "sourceUrl"}


def _grade(m):
    if m["kind"] == "identifier":
        return 2 if m["validated"] else 3
    return 4                                   # a name guessed from text: doubtful until reviewed


def invoke(request):
    inp, cfg = request["input"], request["config"]
    ns = request["context"]["entity_namespace"]
    orig, text_sha = inp["artifact"], inp["text_artifact"]
    try:
        got = yield from sdk.call("artifact.get", sha256=text_sha)
        ex = yield from sdk.call("ner.extract", text=got["text"], labels=cfg.get("labels") or None,
                                 prefer=cfg.get("prefer"))
    except sdk.CapabilityFailed as e:
        yield sdk.result(status="error", error=str(e), coverage=[sdk.coverage("entity extraction", unknown=str(e))])
        return

    be = ex["backend"]
    kind = "model" if be.get("model") else "rule"
    doc = sdk.entity_id(ns, "document", orig)
    out, seen, count = [], {}, {}
    cap = cfg["max_occurrences"]
    for m in ex["mentions"]:
        norm = names.normalize(m["text"]) if m["kind"] == "name" else m["text"].strip().lower()
        key = (m["label"], norm) if m["kind"] == "identifier" else (m["label"], orig + ":" + norm)
        eid = sdk.entity_id(ns, m["label"], key[1])
        count[eid] = count.get(eid, 0) + 1
        if count[eid] > cap:
            continue
        by = {"backend": be["name"], "degraded": be["degraded"],
              "degraded_from": be["degraded_from"], "kind": kind, "model": be.get("model"),
              "params": {"span": [m["start"], m["end"]], "text": m["text"], "label": m["label"],
                         "score": m["score"], "validated": m["validated"],
                         "text_artifact": text_sha}}
        ev = [text_sha, orig]
        g = _grade(m)
        if eid not in seen:
            seen[eid] = m["label"]
            out.append(sdk.a_value(eid, "schema", m["schema"], ev, g, derived_by=by))
            out.append(sdk.a_value(eid, "name", m["text"].strip(), ev, g, derived_by=by))
            if m["label"] in PRED:
                out.append(sdk.a_value(eid, PRED[m["label"]], m["text"].strip(), ev, g, derived_by=by))
        out.append(sdk.a_edge(doc, "mentions", eid, ev, g, derived_by=by))
    if be["degraded"]:
        yield sdk.log(f"extraction used {be['name']}; {be['degraded_from']} was not available "
                      f"({'; '.join(be['skipped'])}). Person/organisation recall is limited.", "warning")
    yield sdk.emit(out)
    yield sdk.result(
        stats={"mentions": len(ex["mentions"]), "entities": len(seen), "backend": be["name"],
               "degraded": be["degraded"]},
        coverage=[sdk.coverage("entity extraction", hits=len(ex["mentions"]),
                               note=None if ex["mentions"] else
                               "nothing matched; names without a title or legal-form suffix are "
                               "invisible to the rules backend")])


def selfcheck():
    return {"ok": True}
