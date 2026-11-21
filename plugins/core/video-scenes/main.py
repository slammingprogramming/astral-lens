"""Split a video at hard cuts and keep one keyframe per scene.

This is a cut detector (ffmpeg's frame-difference score), not scene understanding:
dissolves and slow camera moves are missed, so "N scenes" means "N hard cuts + 1".
Each keyframe is a derived artifact with a timecode, and text on it is read with
OCR when an OCR backend exists (OCR is error-prone: information grade 4).

Not done here: object detection, tracking and captioning. They need model weights
(and, for captions, a GPU) and are not part of this plugin.
"""

from astral import sdk


def _ts(ms):
    s, _ = divmod(int(ms), 1000)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def invoke(request):
    sha = request["input"]["artifact"]
    ns = request["context"]["entity_namespace"]
    cfg = request["config"]
    try:
        sc = yield from sdk.call("media.scenes", sha256=sha, threshold=cfg["threshold"], max_scenes=cfg["max_scenes"])
    except sdk.CapabilityFailed as e:
        yield sdk.result(status="error", error=str(e), coverage=[sdk.coverage("scene cuts", unknown=str(e))])
        return

    media = sdk.entity_id(ns, "media", sha)
    by = {"backend": "ffmpeg-scene", "degraded": False, "kind": "rule",
          "params": {"threshold": cfg["threshold"], "method": sc["method"]}}
    out, ocr_note, ocr_hits, frames, kf_list = [], None, 0, 0, []
    ocr_on = cfg["ocr"]
    for i, s in enumerate(sc["scenes"], 1):
        mid = (s["start_ms"] + s["end_ms"]) // 2
        sid = sdk.entity_id(ns, "scene", f"{sha}:{s['start_ms']}")
        ev = [sha]
        out += [sdk.a_value(sid, "schema", "VideoScene", ev, 2, derived_by=by),
                sdk.a_value(sid, "name", f"Scene {i} ({_ts(s['start_ms'])}–{_ts(s['end_ms'])})", ev, 2, derived_by=by),
                sdk.a_value(sid, "startMs", s["start_ms"], ev, 2, derived_by=by),
                sdk.a_value(sid, "endMs", s["end_ms"], ev, 2, derived_by=by),
                sdk.a_edge(sid, "sceneOf", media, ev, 2, derived_by=by)]
        try:
            fr = yield from sdk.call("media.frame", sha256=sha, t_ms=mid)
        except sdk.CapabilityFailed as e:
            out.append(sdk.a_value(sid, "keyframeStatus", f"no keyframe: {e}", ev, 2, derived_by=by))
            continue
        frames += 1
        kf_list.append([fr["sha256"], mid])
        out.append(sdk.a_value(sid, "keyframe", fr["sha256"], ev + [fr["sha256"]], 2, derived_by=by))
        if ocr_on:
            try:
                o = yield from sdk.call("ocr.image", sha256=fr["sha256"], lang=cfg["ocr_lang"])
            except sdk.CapabilityFailed as e:
                ocr_on, ocr_note = False, str(e)             # one reason, not one per frame
                continue
            if o["text"].strip():
                ocr_hits += 1
                ob = {"backend": o["backend"]["name"], "degraded": o["backend"]["degraded"],
                      "degraded_from": o["backend"]["degraded_from"], "kind": "model" if o["backend"].get("model") else "rule",
                      "params": {"confidence": o.get("confidence"), "words": o.get("words"), "lang": o["lang"],
                                 "keyframe": fr["sha256"], "t_ms": mid}}
                out.append(sdk.a_value(sid, "ocrText", o["text"].strip(), ev + [fr["sha256"]], 4, derived_by=ob))
    yield sdk.emit(out)
    cov = [sdk.coverage("scene cuts", hits=len(sc["scenes"]),
                        note=f"{sc['method']}; hard cuts only. "
                             + (f"TRUNCATED: {len(sc['scenes'])} of {sc['total']} scenes kept" if sc["truncated"] else ""))]
    if cfg["ocr"]:
        cov.append(sdk.coverage("keyframe OCR", unknown=ocr_note) if ocr_note else
                   sdk.coverage("keyframe OCR", hits=ocr_hits,
                                note=None if ocr_hits else "no text recognised on any keyframe; not proof there is none"))
    yield sdk.result(stats={"scenes": len(sc["scenes"]), "total": sc["total"], "truncated": sc["truncated"],
                            "keyframes": frames, "keyframe_artifacts": kf_list, "ocr_frames_with_text": ocr_hits}, coverage=cov)


def selfcheck():
    return {"ok": True}
