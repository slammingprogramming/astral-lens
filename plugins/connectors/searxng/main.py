"""SearXNG JSON search.

Search results are LEADS. An assertion here says "engine E returned URL U for
query Q", nothing about the page's truth, and the snippet is the engine's text,
not a capture. Engines that SearXNG reports as unresponsive are coverage gaps:
results from the remaining engines are not "what the web says".
"""

from astral import sdk


def invoke(request):
    inp, cfg = request["input"], request["config"]
    ns = request["context"]["entity_namespace"]
    q = inp.get("query") or inp["name"]
    out, seen, unresponsive, engines = [], set(), {}, set()
    gaps = []
    for page in range(1, cfg["pages"] + 1):
        try:
            res, body = yield from sdk.fetch_json(
                cfg["base_url"].rstrip("/") + "/search",
                params={"q": q, "format": "json", "categories": cfg["categories"], "pageno": page})
        except sdk.CapabilityFailed as e:
            gaps.append(f"page {page}: {e}")
            break
        for eng, why in body.get("unresponsive_engines") or []:
            unresponsive[eng] = why
        for r in body.get("results") or []:
            url = r.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            engines.update(r.get("engines") or [])
            ev = [res["sha256"]]
            page_id = sdk.entity_id(ns, "webpage", url)
            out += [sdk.a_value(page_id, "schema", "WebPage", ev, 3),
                    sdk.a_value(page_id, "sourceUrl", url, ev, 3)]
            if r.get("title"):
                out.append(sdk.a_value(page_id, "title", r["title"], ev, 3))
            if r.get("content"):
                out.append(sdk.a_value(page_id, "searchSnippet", r["content"], ev, 4))
            out.append(sdk.a_value(page_id, "foundByQuery", q, ev, 3))
    yield sdk.emit(out)
    cov = []
    if gaps and not seen:
        cov.append(sdk.coverage("SearXNG", unknown="; ".join(gaps)))
    else:
        note = f"engines answering: {', '.join(sorted(engines)) or 'none reported'}"
        cov.append(sdk.coverage("SearXNG", hits=len(seen), note=note))
        if unresponsive:
            cov.append(sdk.coverage("SearXNG engines", unknown="unresponsive: " + ", ".join(
                f"{k} ({v})" for k, v in sorted(unresponsive.items()))))
        if gaps:
            cov.append(sdk.coverage("SearXNG (later pages)", unknown="; ".join(gaps)))
    yield sdk.result(stats={"results": len(seen)}, coverage=cov)


def selfcheck():
    return {"ok": True}
