"""Extract the text of an ingested document and record the Document entity.

The extracted text is stored as a DERIVED artifact (parent = the original), so
every later span citation points at stable bytes. The backend that parsed the
file, and whether a better one was skipped, is written into each assertion's
derived_by (invariant 4).
"""

from astral import sdk


def invoke(request):
    sha = request["input"]["artifact"]
    ns = request["context"]["entity_namespace"]
    try:
        ex = yield from sdk.call("text.extract", sha256=sha, prefer=request["config"].get("prefer"))
    except sdk.CapabilityFailed as e:
        yield sdk.result(status="error", error=str(e),
                         coverage=[sdk.coverage("document text", unknown=str(e))])
        return

    be = ex["backend"]
    by = {"backend": be["name"], "degraded": be["degraded"], "degraded_from": be["degraded_from"],
          "kind": "model" if be.get("model") else "rule", "model": be.get("model")}
    doc = sdk.entity_id(ns, "document", sha)
    text, meta = ex["text"], ex.get("meta", {})
    out = [sdk.a_value(doc, "schema", "Document", [sha], 2, derived_by=by),
           sdk.a_value(doc, "mimeType", ex["media_type"], [sha], 2, derived_by=by)]
    if meta.get("title"):
        out.append(sdk.a_value(doc, "title", meta["title"], [sha], 3, derived_by=by))
    for h, v in (meta.get("headers") or {}).items():
        out.append(sdk.a_value(doc, "email" + h.replace("-", ""), v, [sha], 3, derived_by=by))

    stats = {"backend": be["name"], "degraded": be["degraded"], "chars": len(text)}
    if not text.strip():
        # An empty extraction is "we could not read it", never "it says nothing".
        why = meta.get("warning") or "the parser returned no text"
        out.append(sdk.a_value(doc, "contentStatus", "no_text_extracted", [sha], 2, derived_by=by))
        yield sdk.emit(out)
        yield sdk.result(stats=stats, coverage=[sdk.coverage("document text", unknown=why)])
        return

    t = yield from sdk.put_artifact(text, media_type="text/plain", parent_sha256=sha,
                                    derivation=f"text-extract:{be['name']}")
    out.append(sdk.a_value(doc, "hasText", t["sha256"], [sha, t["sha256"]], 2, derived_by=by))
    stats["text_artifact"] = t["sha256"]
    if be["degraded"]:
        yield sdk.log(f"parsed with {be['name']}; {be['degraded_from']} was not available "
                      f"({'; '.join(be['skipped'])})", "warning")
    yield sdk.emit(out)
    yield sdk.result(stats=stats, coverage=[sdk.coverage("document text", hits=1)])


def selfcheck():
    return {"ok": True}
