"""Find faces in an image and index their embeddings for search.

Records that a face-shaped region exists (box, detector score) and nothing about
whose it is. Each assertion carries the backend, both models and their licence
classes. Zero faces is "none detected by this detector at this threshold": small,
profile, blurred or occluded faces are routinely missed, so it is not "no people".
The embedding goes to the biometric-tagged index, not into the assertion log.
"""

from astral import sdk


def invoke(request):
    inp, cfg = request["input"], request["config"]
    sha = inp["artifact"]
    parent = inp.get("frame_of")                 # a video keyframe: link its faces to the video
    frame_ms = inp.get("frame_ms")
    ns = request["context"]["entity_namespace"]
    try:
        r = yield from sdk.call("face.embed", sha256=sha, max_faces=cfg["max_faces"], min_score=cfg["min_score"],
                                prefer=cfg.get("prefer"), allow_noncommercial=cfg.get("allow_noncommercial", False))
    except sdk.CapabilityFailed as e:
        yield sdk.result(status="error", error=str(e), coverage=[sdk.coverage("face detection", unknown=str(e))])
        return
    be, lic = r["backend"], r["licences"]
    media = sdk.entity_id(ns, "media", parent or sha)
    ev = [sha] + ([parent] if parent else [])
    out, indexed = [], []
    for i, f in enumerate(r["faces"], 1):
        b = f["box"]
        fid = sdk.entity_id(ns, "face", f"{sha}:{frame_ms}:{b['x']},{b['y']},{b['w']},{b['h']}")
        by = {"backend": be["name"], "degraded": be["degraded"], "degraded_from": be["degraded_from"], "kind": "model",
              "model": r["model_id"],
              "params": {"detector": r["detector_id"], "embedder": r["model_id"], "det_score": f["det_score"],
                         "licence_class": {"detector": lic["detector"]["licence_class"],
                                           "embedder": lic["embedder"]["licence_class"]},
                         "commercial_use": {"detector": lic["detector"].get("commercial_use"),
                                            "embedder": lic["embedder"].get("commercial_use")},
                         "frame_ms": frame_ms}}
        out += [sdk.a_value(fid, "schema", "FaceRegion", ev, 2, derived_by=by),
                sdk.a_value(fid, "name", f"Face {i} ({sha[:8]})", ev, 2, derived_by=by),
                sdk.a_value(fid, "box", {**b, "image_w": r["image"]["width"], "image_h": r["image"]["height"]}, ev, 3,
                            derived_by=by),
                sdk.a_value(fid, "detectionScore", f["det_score"], ev, 3, derived_by=by),
                sdk.a_edge(fid, "faceOf", media, ev, 3, derived_by=by)]
        indexed.append({"entity": fid, "box": b, "landmarks": f.get("landmarks"), "det_score": f["det_score"],
                        "embedding": f["embedding"]})
    yield sdk.emit(out)
    if indexed:
        try:
            yield from sdk.call("face.index", sha256=sha, faces=indexed, model_id=r["model_id"],
                                backend=be["name"], frame_ms=frame_ms)
        except sdk.CapabilityFailed as e:
            yield sdk.log(f"faces were recorded but could not be indexed for search: {e}", "warning")
    if be["degraded"]:
        yield sdk.log(f"used {be['name']}; {be['degraded_from']} was not available", "warning")
    note = (f"{be['name']} (detector {r['detector_id']}, embedder {r['model_id']}); region only, no identity"
            if indexed else
            f"none detected by {r['detector_id']} at score >= {cfg['min_score']}; small, profile, blurred or "
            "occluded faces are often missed, so this is not proof no one is present")
    yield sdk.result(stats={"faces": len(indexed), "backend": be["name"], "model": r["model_id"]},
                     coverage=[sdk.coverage("face detection", hits=len(indexed), note=note)])


def selfcheck():
    return {"ok": True}
