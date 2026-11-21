"""Follow detected objects through a video and record each one as a track.

Frames are sampled (default 4 a second), objects are detected on each, and detections that overlap from one sampled
frame to the next are linked into a track. A track is "boxes with the same class label that plausibly follow one
another", not an identity: crossing paths can swap or split tracks, nothing is re-identified after a gap, and a track's
start and end are the first and last sampled frame in which it was seen. The class label is a guess from an 80-class
vocabulary. A video longer than the frame cap is NOT scanned past the cap, and the coverage says so.
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
        r = yield from sdk.call("object.track", sha256=sha, fps=cfg["fps"], max_frames=cfg["max_frames"],
                                min_score=cfg["min_score"], prefer=cfg.get("prefer"))
    except sdk.CapabilityFailed as e:
        yield sdk.result(status="error", error=str(e), coverage=[sdk.coverage("object tracking", unknown=str(e))])
        return
    if not r["frames_sampled"]:
        why = "no video frames could be read from this file"
        yield sdk.result(status="error", error=why, coverage=[sdk.coverage("object tracking", unknown=why)])
        return
    be, lic = r["backend"], r["licence"]
    media = sdk.entity_id(ns, "media", sha)
    by = {"backend": be["name"], "degraded": be["degraded"], "degraded_from": be["degraded_from"], "kind": "model",
          "model": r["model_id"],
          "params": {"detector": r["model_id"], "tracker": "bytetrack-style (Kalman prediction, Hungarian IoU matching)",
                     "fps": r["fps"], "min_score": cfg["min_score"], "vocabulary": "COCO 80 classes",
                     "licence_class": lic.get("licence_class"), "commercial_use": lic.get("commercial_use")}}
    ev = [sha]
    out = []
    for t in r["tracks"]:
        tid = sdk.entity_id(ns, "track", f"{sha}:{r['fps']}:{t['track']}")
        label = f"{t['label']} track {t['track']} ({_ts(t['start_ms'])}-{_ts(t['end_ms'])})"
        out += [sdk.a_value(tid, "schema", "ObjectTrack", ev, 2, derived_by=by),
                sdk.a_value(tid, "name", label, ev, 2, derived_by=by),
                sdk.a_value(tid, "objectClass", t["label"], ev, 4, derived_by=by),
                sdk.a_value(tid, "startMs", t["start_ms"], ev, 3, derived_by=by),
                sdk.a_value(tid, "endMs", t["end_ms"], ev, 3, derived_by=by),
                sdk.a_value(tid, "framesSeen", t["frames"], ev, 3, derived_by=by),
                sdk.a_value(tid, "meanScore", t["mean_score"], ev, 3, derived_by=by),
                sdk.a_value(tid, "path", {"frame": r["frame"], "fps": r["fps"], "boxes": t["boxes"]}, ev, 4, derived_by=by),
                sdk.a_edge(tid, "trackOf", media, ev, 3, derived_by=by)]
    yield sdk.emit(out)
    if be["degraded"]:
        yield sdk.log(f"used {be['name']}; {be['degraded_from']} was not available", "warning")
    n = len(r["tracks"])
    scope = f"{r['frames_sampled']} frames sampled at {r['fps']}/s, covering the first {_ts(r['covered_ms'])}"
    if r["truncated"]:
        scope += f"; TRUNCATED at the {cfg['max_frames']}-frame cap, so the rest of the video was not scanned"
    note = (f"{scope}. Tracks group boxes, not identities; crossings can swap or split them; class labels are guesses "
            "from an 80-class vocabulary") if n else (
        f"{scope}. No object of the 80 classes was followed for two sampled frames; objects outside that vocabulary, "
        "or small, distant or briefly visible ones, are often missed, so this is not proof nothing moves in the video")
    yield sdk.result(stats={"tracks": n, "frames_sampled": r["frames_sampled"], "detections": r["detections"],
                            "truncated": r["truncated"], "backend": be["name"], "model": r["model_id"]},
                     coverage=[sdk.coverage("object tracking", hits=n, note=note)])


def selfcheck():
    return {"ok": True}
