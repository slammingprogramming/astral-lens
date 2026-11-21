"""Read text from a still image and record it as a Document linked to the image.

OCR output is error-prone, so it is graded 4 and carries its mean word confidence.
An image with no recognised text is reported as "nothing recognised", which is not
the same as "contains no text".
"""

from astral import sdk


def invoke(request):
    sha = request["input"]["artifact"]
    ns = request["context"]["entity_namespace"]
    lang = request["config"].get("lang", "eng")
    try:
        o = yield from sdk.call("ocr.image", sha256=sha, lang=lang)
    except sdk.CapabilityFailed as e:
        yield sdk.result(status="error", error=str(e), coverage=[sdk.coverage("image text (OCR)", unknown=str(e))])
        return
    be = o["backend"]
    by = {"backend": be["name"], "degraded": be["degraded"], "degraded_from": be["degraded_from"],
          "kind": "model" if be.get("model") else "rule", "model": be.get("model"),
          "params": {"confidence": o.get("confidence"), "words": o.get("words"), "lang": o["lang"]}}
    text = o["text"].strip()
    if not text:
        yield sdk.result(stats={"words": 0}, coverage=[sdk.coverage(
            "image text (OCR)", note=f"{be['name']} recognised no text; the image may still contain some")])
        return
    t = yield from sdk.put_artifact(text, media_type="text/plain", parent_sha256=sha,
                                    derivation=f"ocr:{be['name']}:{o['lang']}")
    doc = sdk.entity_id(ns, "document", sha)
    ev = [sha, t["sha256"]]
    out = [sdk.a_value(doc, "schema", "Document", ev, 2, derived_by=by),
           sdk.a_value(doc, "title", "Text recognised in image", ev, 4, derived_by=by),
           sdk.a_value(doc, "mimeType", "text/plain", ev, 2, derived_by=by),
           sdk.a_value(doc, "hasText", t["sha256"], ev, 4, derived_by=by),
           sdk.a_edge(doc, "textOf", sdk.entity_id(ns, "media", sha), ev, 4, derived_by=by)]
    yield sdk.emit(out)
    yield sdk.result(stats={"text_artifact": t["sha256"], "words": o.get("words"), "backend": be["name"]},
                     coverage=[sdk.coverage("image text (OCR)", hits=o.get("words") or 1,
                                            note=f"OCR ({be['name']}), mean confidence {o.get('confidence')}")])


def selfcheck():
    return {"ok": True}
