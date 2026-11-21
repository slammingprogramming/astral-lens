"""Find everyday objects in an image and record each as a region with a class guess.

A region says "the detector thinks one of its 80 classes is here, with this confidence", and nothing more: not who a
person is, not what a vehicle's registration is, not anything outside the 80 classes. Zero objects is "none detected
by this detector at this threshold": objects outside the vocabulary, or small, distant, dark or partly hidden ones, are
routinely missed, so it is not "nothing there". Each assertion carries the backend, the model and its licence class.
"""

from astral import sdk


def invoke(request):
    inp, cfg = request["input"], request["config"]
    sha = inp["artifact"]
    parent = inp.get("frame_of")                 # a video keyframe: link its objects to the video
    frame_ms = inp.get("frame_ms")
    ns = request["context"]["entity_namespace"]
    try:
        r = yield from sdk.call("object.detect", sha256=sha, min_score=cfg["min_score"], max_objects=cfg["max_objects"],
                                prefer=cfg.get("prefer"))
    except sdk.CapabilityFailed as e:
        yield sdk.result(status="error", error=str(e), coverage=[sdk.coverage("object detection", unknown=str(e))])
        return
    be, lic = r["backend"], r["licence"]
    media = sdk.entity_id(ns, "media", parent or sha)
    ev = [sha] + ([parent] if parent else [])
    by = {"backend": be["name"], "degraded": be["degraded"], "degraded_from": be["degraded_from"], "kind": "model",
          "model": r["model_id"],
          "params": {"min_score": cfg["min_score"], "vocabulary": "COCO 80 classes",
                     "licence_class": lic.get("licence_class"), "commercial_use": lic.get("commercial_use"),
                     "frame_ms": frame_ms}}
    out = []
    for i, o in enumerate(r["objects"], 1):
        b = o["box"]
        oid = sdk.entity_id(ns, "object", f"{sha}:{frame_ms}:{o['label']}:{b['x']},{b['y']},{b['w']},{b['h']}")
        out += [sdk.a_value(oid, "schema", "ObjectRegion", ev, 2, derived_by=by),
                sdk.a_value(oid, "name", f"{o['label']} {i} ({sha[:8]})", ev, 2, derived_by=by),
                sdk.a_value(oid, "objectClass", o["label"], ev, 4, derived_by=by),
                sdk.a_value(oid, "box", {**b, "image_w": r["image"]["width"], "image_h": r["image"]["height"]}, ev, 3, derived_by=by),
                sdk.a_value(oid, "detectionScore", o["score"], ev, 3, derived_by=by),
                sdk.a_edge(oid, "objectOf", media, ev, 3, derived_by=by)]
    yield sdk.emit(out)
    if be["degraded"]:
        yield sdk.log(f"used {be['name']}; {be['degraded_from']} was not available", "warning")
    n = len(r["objects"])
    note = (f"{be['name']} ({r['model_id']}); class guesses from an 80-class vocabulary, regions only, no identification"
            if n else
            f"none of the 80 classes detected by {r['model_id']} at score >= {cfg['min_score']}; objects outside that "
            "vocabulary, and small, distant, dark or partly hidden ones, are often missed, so this is not proof nothing is there")
    yield sdk.result(stats={"objects": n, "backend": be["name"], "model": r["model_id"]},
                     coverage=[sdk.coverage("object detection", hits=n, note=note)])


def selfcheck():
    return {"ok": True}
