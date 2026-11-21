"""OpenSanctions, two modes with one output shape.

API mode (`/search/{dataset}`, key required) when no dataset artifact is given;
bulk mode when `input.dataset_artifact` names an ingested `entities.ftm.json`
(NDJSON, one FollowTheMoney entity per line), which is streamed from disk and
matched locally with the cross-script scorer. In bulk mode the file's SHA-256 is
the evidence, so the exact list version that produced a hit is on record.

A "no hit" is reported as no_hits for THAT list at THAT version; it is never
asserted as "not sanctioned".
"""

import json

from astral import names, sdk

SKIP = frozenset({"alias_note", "notes", "modifiedAt", "createdAt", "retrievedAt"})


def _schemas(inp):
    return {"Person"} if inp.get("schema") == "Person" else (
        {"Company", "Organization", "LegalEntity"} if inp.get("schema") in ("Company", "Organization") else None)


def invoke(request):
    inp, cfg = request["input"], request["config"]
    ns = request["context"]["entity_namespace"]
    if inp.get("dataset_artifact"):
        yield from _bulk(ns, inp, cfg)
        return
    key = request["secrets"].get("OPENSANCTIONS_API_KEY")
    if not key:
        yield sdk.result(coverage=[sdk.coverage(
            "OpenSanctions", unknown="OPENSANCTIONS_API_KEY is not configured and no bulk "
                                     "dataset_artifact was supplied")])
        return
    params = {"q": inp["name"], "limit": cfg["limit"]}
    if inp.get("schema"):
        params["schema"] = inp["schema"]
    if inp.get("country"):
        params["countries"] = inp["country"].lower()
    try:
        res, body = yield from sdk.fetch_json(
            f"{cfg['base_url'].rstrip('/')}/search/{cfg['dataset']}", params=params,
            headers={"Authorization": f"ApiKey {key}"})
    except sdk.CapabilityFailed as e:
        yield sdk.result(coverage=[sdk.coverage("OpenSanctions", unknown=str(e))])
        return
    out = []
    for ent in body.get("results") or []:
        _, a = sdk.ftm_assertions(ns, ent, [res["sha256"]], prefix="os", skip=SKIP)
        out += a + _lists(ns, ent, [res["sha256"]])
    yield sdk.emit(out)
    n = len(body.get("results") or [])
    yield sdk.result(stats={"entities": n}, coverage=[sdk.coverage(
        "OpenSanctions API", hits=n, note=f"dataset={cfg['dataset']}")])


def _lists(ns, ent, ev):
    eid = sdk.entity_id(ns, "os", str(ent["id"]))
    out = [sdk.a_value(eid, "listedIn", d, ev, 2) for d in ent.get("datasets") or []]
    out += [sdk.a_value(eid, "sanctionTopic", t, ev, 2) for t in (ent.get("properties") or {}).get("topics", [])]
    return out


def _bulk(ns, inp, cfg):
    sha = inp["dataset_artifact"]
    try:
        info = yield from sdk.call("artifact.path", sha256=sha, verify=True)
    except sdk.CapabilityFailed as e:
        yield sdk.result(coverage=[sdk.coverage("OpenSanctions bulk file", unknown=str(e))])
        return
    want = _schemas(inp)
    matched, scanned, bad = [], 0, 0
    with open(info["path"], encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                ent = json.loads(line)
            except ValueError:
                bad += 1
                continue
            scanned += 1
            if want and ent.get("schema") not in want:
                continue
            props = ent.get("properties") or {}
            cands = [*props.get("name", []), *props.get("alias", []), *props.get("previousName", [])]
            best = max((names.score(inp["name"], c, org=ent.get("schema") not in ("Person",)).score
                        for c in cands), default=0.0)
            if best >= cfg["min_match"]:
                matched.append((best, ent))
    matched.sort(key=lambda t: -t[0])
    out = []
    for score, ent in matched[: cfg["limit"]]:
        _, a = sdk.ftm_assertions(ns, ent, [sha], prefix="os", skip=SKIP)
        out += a + _lists(ns, ent, [sha])
    yield sdk.emit(out)
    note = f"scanned {scanned} entities in a local file (sha256 {sha[:12]}…)"
    if bad:
        note += f"; {bad} unreadable lines skipped"
    yield sdk.result(stats={"entities": len(matched[: cfg["limit"]]), "scanned": scanned},
                     coverage=[sdk.coverage("OpenSanctions bulk file", hits=len(matched), note=note)])


def selfcheck():
    return {"ok": _schemas({"schema": "Person"}) == {"Person"}}
