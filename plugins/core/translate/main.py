"""Translate a text artifact, line by line, keeping the original beside it.

The translation is a NEW derived document linked to the original by
`translationOf`. It is machine output (information grade 4) and is never allowed
to replace the original as evidence: citations of the original stay valid, and the
line structure is preserved so transcript timecodes still line up.
"""

from astral import sdk


def invoke(request):
    inp, cfg = request["input"], request["config"]
    ns = request["context"]["entity_namespace"]
    orig, text_sha = inp["artifact"], inp["text_artifact"]
    target = cfg.get("target", "en")
    try:
        got = yield from sdk.call("artifact.get", sha256=text_sha)
        lines = got["text"].split("\n")
        tr = yield from sdk.call("translate.text", texts=lines, target=target)
    except sdk.CapabilityFailed as e:
        yield sdk.result(status="error", error=str(e),
                         coverage=[sdk.coverage(f"translation to {target}", unknown=str(e))])
        return
    if len(tr["texts"]) != len(lines):
        msg = f"backend returned {len(tr['texts'])} lines for {len(lines)}; refusing a misaligned translation"
        yield sdk.result(status="error", error=msg, coverage=[sdk.coverage(f"translation to {target}", unknown=msg)])
        return
    be = tr["backend"]
    body = "\n".join(tr["texts"])
    t = yield from sdk.put_artifact(body, media_type="text/plain", parent_sha256=text_sha,
                                    derivation=f"translation:{be['name']}:{target}")
    by = {"backend": be["name"], "degraded": be["degraded"], "degraded_from": be["degraded_from"],
          "kind": "model", "model": be.get("model"), "params": {"target": target, "source_text": text_sha}}
    d = sdk.entity_id(ns, "document-translation", f"{text_sha}:{target}")
    src = sdk.entity_id(ns, "document", orig)
    ev = [text_sha, t["sha256"]]
    out = [sdk.a_value(d, "schema", "Document", ev, 2, derived_by=by),
           sdk.a_value(d, "title", f"Machine translation ({target})", ev, 4, derived_by=by),
           sdk.a_value(d, "hasText", t["sha256"], ev, 4, derived_by=by),
           sdk.a_value(d, "language", target, ev, 4, derived_by=by),
           sdk.a_value(d, "machineTranslation", True, ev, 2, derived_by=by),
           sdk.a_edge(d, "translationOf", src, ev, 4, derived_by=by)]
    yield sdk.emit(out)
    yield sdk.result(stats={"translated_text_artifact": t["sha256"], "backend": be["name"], "lines": len(lines)},
                     coverage=[sdk.coverage(f"translation to {target}", hits=len(lines),
                                            note=f"machine translation ({be['name']}); read the original")])


def selfcheck():
    return {"ok": True}
