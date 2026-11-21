"""Capture one URL as a WARC and record what was captured, when, and how."""

from astral import sdk


def invoke(request):
    url = request["input"]["url"]
    ns = request["context"]["entity_namespace"]
    try:
        cap = yield from sdk.call("web.capture", url=url)
    except sdk.CapabilityFailed as e:
        yield sdk.result(status="error", error=str(e), coverage=[sdk.coverage("web capture", unknown=str(e))])
        return
    page = sdk.entity_id(ns, "webpage", cap["final_url"])
    ev = [cap["warc_sha256"], cap["payload_sha256"]]
    log = cap["log"]
    out = [sdk.a_value(page, "schema", "WebPage", ev, 2),
           sdk.a_value(page, "sourceUrl", cap["final_url"], ev, 2),
           sdk.a_value(page, "capturedAt", log["captured_at"], ev, 2),
           sdk.a_value(page, "httpStatus", cap["status"], ev, 2),
           sdk.a_value(page, "captureRendering", log["rendering"], ev, 2),
           sdk.a_value(page, "warcSha256", cap["warc_sha256"], ev, 2)]
    yield sdk.emit(out)
    note = None if cap["status"] < 400 else f"server answered HTTP {cap['status']}; the response is preserved"
    yield sdk.result(
        stats={"warc_sha256": cap["warc_sha256"], "payload_sha256": cap["payload_sha256"],
               "status": cap["status"], "media_type": cap["media_type"],
               "warc_verified": cap["warc_verified"]},
        coverage=[sdk.coverage("web capture", hits=1, note=note)])


def selfcheck():
    return {"ok": True}
