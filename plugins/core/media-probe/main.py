"""Describe a media file: container, streams, and what its metadata CLAIMS.

Dates, device and GPS from EXIF or container tags are claims made by the file,
not facts about the event: anyone can edit them. They are recorded at information
grade 4 and labelled `claimed_by_file`. Content Credentials (C2PA) are reported
only as present or not found; finding none proves nothing.
"""

import re

from astral import sdk

SCHEMA = {"video": "Video", "audio": "Audio", "image": "Image"}
CLAIM = 4


def _iso6709(s):
    m = re.match(r"^([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)", s or "")
    return {"lat": float(m[1]), "lon": float(m[2])} if m else None


def invoke(request):
    sha = request["input"]["artifact"]
    ns = request["context"]["entity_namespace"]
    try:
        info = yield from sdk.call("media.probe", sha256=sha)
    except sdk.CapabilityFailed as e:
        yield sdk.result(status="error", error=str(e), coverage=[sdk.coverage("media metadata", unknown=str(e))])
        return

    kind = info["kind"]
    ent = sdk.entity_id(ns, "media", sha)
    by = {"backend": "ffprobe", "degraded": False, "kind": "rule"}
    claim = {**by, "params": {"claimed_by_file": True}}
    tags = info["tags"]
    out = [sdk.a_value(ent, "schema", SCHEMA.get(kind, "Document"), [sha], 2, derived_by=by),
           sdk.a_value(ent, "name", tags.get("title") or f"{kind.title()} {sha[:8]}", [sha],
                       CLAIM if tags.get("title") else 2, derived_by=claim if tags.get("title") else by),
           sdk.a_value(ent, "container", info["container"], [sha], 2, derived_by=by),
           sdk.a_value(ent, "contentHash", sha, [sha], 2, derived_by=by)]
    if info["duration_s"] is not None:
        out.append(sdk.a_value(ent, "durationSeconds", round(info["duration_s"], 3), [sha], 2, derived_by=by))
    for s in info["streams"]:
        if s["type"] == "video" and not s.get("attached_pic"):
            out += [sdk.a_value(ent, "videoCodec", s["codec"], [sha], 2, derived_by=by),
                    sdk.a_value(ent, "width", s["width"], [sha], 2, derived_by=by),
                    sdk.a_value(ent, "height", s["height"], [sha], 2, derived_by=by)]
            if s.get("fps"):
                out.append(sdk.a_value(ent, "frameRate", s["fps"], [sha], 2, derived_by=by))
        elif s["type"] == "audio":
            out.append(sdk.a_value(ent, "audioCodec", s["codec"], [sha], 2, derived_by=by))
    out.append(sdk.a_value(ent, "hasAudio", info["has_audio"], [sha], 2, derived_by=by))
    if tags.get("creation_time"):
        out.append(sdk.a_value(ent, "dateCreated", tags["creation_time"], [sha], CLAIM, derived_by=claim))
    loc = _iso6709(tags.get("location") or tags.get("com.apple.quicktime.location.iso6709"))
    if loc:
        out.append(sdk.a_value(ent, "claimedLocation", loc, [sha], CLAIM, derived_by=claim))
    for k, pred in (("encoder", "software"), ("make", "deviceMake"), ("model", "deviceModel")):
        if tags.get(k):
            out.append(sdk.a_value(ent, pred, tags[k], [sha], CLAIM, derived_by=claim))

    cov = [sdk.coverage("container metadata", hits=1)]
    stats = {"kind": kind, "duration_s": info["duration_s"], "has_audio": info["has_audio"],
             "has_video": info["has_video"]}

    # EXIF (stills; a camera video rarely carries it)
    try:
        ex = yield from sdk.call("exif.extract", sha256=sha)
    except sdk.CapabilityFailed as e:
        ex = {}
        cov.append(sdk.coverage("EXIF", unknown=str(e)))
    else:
        if ex.get("warning"):
            cov.append(sdk.coverage("EXIF", unknown=ex["warning"]))
        elif ex:
            cov.append(sdk.coverage("EXIF", hits=1, note="claims made by the file; trivially edited"))
        else:
            cov.append(sdk.coverage("EXIF", note="no EXIF block found; most stripped or re-encoded media has none"))
    for k, pred in (("captured", "dateCreated"), ("make", "deviceMake"), ("model", "deviceModel"),
                    ("software", "software")):
        if ex.get(k):
            out.append(sdk.a_value(ent, pred, ex[k], [sha], CLAIM, derived_by={
                **claim, "params": {"claimed_by_file": True, "source": "exif",
                                    **({"timezone": ex.get("offset_time") or "unspecified"} if k == "captured" else {})}}))
    if ex.get("gps"):
        out.append(sdk.a_value(ent, "claimedLocation", ex["gps"], [sha], CLAIM,
                               derived_by={**claim, "params": {"claimed_by_file": True, "source": "exif-gps"}}))

    # Content Credentials
    try:
        c = yield from sdk.call("c2pa.check", sha256=sha)
    except sdk.CapabilityFailed as e:
        c = None
        cov.append(sdk.coverage("C2PA Content Credentials", unknown=str(e)))
    if c is not None:
        if c.get("unsupported"):
            cov.append(sdk.coverage("C2PA Content Credentials", unknown="container type not scanned for credentials"))
        elif not c["present"]:
            cov.append(sdk.coverage("C2PA Content Credentials",
                                    note="none found. Most media carries none, so this proves nothing about "
                                         "authenticity or manipulation"))
        else:
            v = c.get("validation")
            state = ("present_unvalidated" if v is None else "present_valid" if v.get("validated")
                     else "present_invalid")
            out.append(sdk.a_value(ent, "contentCredentials", state, [sha], 3,
                                   derived_by={"backend": c.get("verifier") or "c2pa-scan", "degraded": v is None,
                                               "degraded_from": "c2patool" if v is None else None, "kind": "rule",
                                               "params": {"container": c["container"], "validation": v}}))
            cov.append(sdk.coverage("C2PA Content Credentials", hits=1,
                                    note={"present_unvalidated": "manifest present but NOT validated (no verifier "
                                                                 "installed); presence alone proves little",
                                          "present_valid": "manifest present and validates; it vouches for the "
                                                           "signer's claims, not that the scene is true",
                                          "present_invalid": "manifest present but FAILED validation"}[state]))
            stats["c2pa"] = state
    if kind == "image":
        try:
            h = yield from sdk.call("image.hash", sha256=sha)
            out.append(sdk.a_value(ent, "perceptualHash", h["hash"], [sha], 3, derived_by={
                "backend": h["algo"], "degraded": False, "kind": "rule",
                "params": {"quality": h["quality"], "note": "screening signal for near-duplicates, not proof"}}))
            cov.append(sdk.coverage("perceptual hash", hits=1, note=f"{h['algo']}, quality {h['quality']}"))
        except sdk.CapabilityFailed as e:
            cov.append(sdk.coverage("perceptual hash", unknown=str(e)))
    yield sdk.emit(out)
    yield sdk.result(stats=stats, coverage=cov)


def selfcheck():
    return {"ok": True}
