"""List Wayback Machine snapshots of a URL (CDX server, JSON array output).

An empty list means the Archive holds no snapshot matching the query; it does
not mean the page never existed. A snapshot record is a pointer: to preserve
the archived page as evidence, run `core.web-capture` on `archiveUrl`.
"""

from astral import sdk


def invoke(request):
    inp, cfg = request["input"], request["config"]
    ns = request["context"]["entity_namespace"]
    params = {"url": inp["url"], "output": "json", "limit": cfg["limit"], "filter": "statuscode:200"}
    if cfg["collapse_identical"]:
        params["collapse"] = "digest"
    try:
        res, rows = yield from sdk.fetch_json(cfg["base_url"].rstrip("/") + "/cdx/search/cdx", params=params)
    except sdk.CapabilityFailed as e:
        yield sdk.result(coverage=[sdk.coverage("Wayback CDX", unknown=str(e))])
        return
    if not rows:
        yield sdk.result(coverage=[sdk.coverage("Wayback CDX", hits=0,
                                                note="no snapshot in the Archive; not evidence the page never existed")])
        return
    head, data = rows[0], rows[1:]
    ev = [res["sha256"]]
    page = sdk.entity_id(ns, "webpage", inp["url"])
    out = [sdk.a_value(page, "schema", "WebPage", ev, 2), sdk.a_value(page, "sourceUrl", inp["url"], ev, 2)]
    for row in data:
        r = dict(zip(head, row))
        snap = sdk.entity_id(ns, "snapshot", f"{r['timestamp']}:{r['original']}")
        archive = f"https://web.archive.org/web/{r['timestamp']}/{r['original']}"
        out += [sdk.a_value(snap, "schema", "WebSnapshot", ev, 2),
                sdk.a_value(snap, "name", f"{r['original']} @ {r['timestamp']}", ev, 2),
                sdk.a_value(snap, "archiveUrl", archive, ev, 2),
                sdk.a_value(snap, "snapshotTimestamp", r["timestamp"], ev, 2),
                sdk.a_value(snap, "contentDigest", r.get("digest"), ev, 2),
                sdk.a_value(snap, "mimeType", r.get("mimetype"), ev, 2),
                sdk.a_edge(snap, "snapshotOf", page, ev, 2)]
    yield sdk.emit(out)
    yield sdk.result(stats={"snapshots": len(data)}, coverage=[sdk.coverage("Wayback CDX", hits=len(data))])


def selfcheck():
    return {"ok": True}
