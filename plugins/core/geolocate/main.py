"""Guess WHICH PARTS OF THE WORLD an image resembles, as coarse regions. This is not a location.

A model compares the picture with 100,000 places in its training photographs and says which grid cells (5 degrees, roughly
550 km, by default) hold most of its score. The score is the model's, not a probability, and the best cell is often
wrong; photographs of ordinary places look like many places. Each region is recorded as a guess (information grade 5)
with its bounds and share, never as a coordinate, and the coverage entry says what it can and cannot support. Whether the
file itself claims a place (EXIF) is a separate record made elsewhere; the two are not merged here.

Runs only on a knowing opt-in: the model weights have no stated licence.
"""

from astral import sdk


def invoke(request):
    sha = request["input"]["artifact"]
    cfg = request["config"]
    ns = request["context"]["entity_namespace"]
    try:
        r = yield from sdk.call("geo.region", sha256=sha, allow_unverified_licence=cfg["allow_unverified_licence"],
                                top_k=cfg["top_k"], cell_degrees=cfg["cell_degrees"], max_regions=cfg["max_regions"],
                                prefer=cfg.get("prefer"))
    except sdk.CapabilityFailed as e:
        yield sdk.result(status="error", error=str(e), coverage=[sdk.coverage("region guess", unknown=str(e))])
        return
    be, lic = r["backend"], r["licences"]
    media = sdk.entity_id(ns, "media", sha)
    ev = [sha]
    by = {"backend": be["name"], "degraded": be["degraded"], "degraded_from": be["degraded_from"], "kind": "model",
          "model": r["model_id"],
          "params": {"top_k": r["top_k"], "cell_degrees": r["cell_degrees"], "gallery": r["gallery"],
                     "total_mass": r["total_mass"], "diffuse": r["diffuse"],
                     "licence_class": {"geoclip": lic["geoclip"].get("licence_class"), "backbone": lic["backbone"].get("licence_class")},
                     "commercial_use": {"geoclip": lic["geoclip"].get("commercial_use"), "backbone": lic["backbone"].get("commercial_use")}}}
    out = []
    for g in r["regions"]:
        gid = sdk.entity_id(ns, "georegion", f"{sha}:{r['cell_degrees']}:{g['south']},{g['west']}")
        bounds = {"south": g["south"], "west": g["west"], "north": g["north"], "east": g["east"]}
        out += [sdk.a_value(gid, "schema", "GeoRegionGuess", ev, 2, derived_by=by),
                sdk.a_value(gid, "name", f"Region guess {g['rank']} ({sha[:8]})", ev, 2, derived_by=by),
                sdk.a_value(gid, "regionBounds", bounds, ev, 5, derived_by=by),
                sdk.a_value(gid, "modelShare", {"of_returned": g["share_of_returned"], "mass": g["mass"], "rank": g["rank"]}, ev, 5,
                            derived_by=by),
                sdk.a_edge(gid, "guessFor", media, ev, 5, derived_by=by)]
    yield sdk.emit(out)
    if be["degraded"]:
        yield sdk.log(f"used {be['name']}; {be['degraded_from']} was not available", "warning")
    n = len(r["regions"])
    note = ("a model's guess from appearance, as coarse grid cells; not a location, not evidence of where this was taken, "
            "and often wrong. Compare it with what the file claims and with independent geolocation before relying on it")
    if r["diffuse"]:
        note += ". The model's score is spread thinly across many regions here, so even the best guess is weak"
    yield sdk.result(stats={"regions": n, "diffuse": r["diffuse"], "backend": be["name"], "model": r["model_id"]},
                     coverage=[sdk.coverage("region guess", hits=n, note=note)])


def selfcheck():
    return {"ok": True}
