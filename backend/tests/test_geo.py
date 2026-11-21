"""Region-level geolocation from appearance.

HONEST SCOPE. The pooling into regions, the opt-in and the recording are tested with constructed predictions and a
scripted backend: they prove what is claimed and recorded, not how good the model is. The real GeoCLIP model needs PyTorch,
transformers, the `geoclip` package and a local copy of the CLIP backbone (ASTRAL_GEOCLIP_CLIP_DIR); none is a dependency
of the project, so those tests skip here and the one real run is recorded in docs/11 with what it showed.
"""

from __future__ import annotations

import pytest

from astral import geo
from astral.backends import module_available

from conftest import REPO


def test_regionize_pools_predictions_into_cells_and_reports_bounds_not_points():
    coords = [(48.86, 2.35), (48.9, 2.2), (48.5, 2.9), (35.68, 139.69), (35.7, 139.7), (-33.86, 151.2)]
    probs = [0.3, 0.2, 0.1, 0.15, 0.1, 0.05]
    out = geo.regionize(coords, probs, cell=5.0, max_regions=3)
    r = out["regions"]
    assert [x["rank"] for x in r] == [1, 2, 3]
    assert r[0]["south"] == 45.0 and r[0]["north"] == 50.0 and r[0]["west"] == 0.0 and r[0]["east"] == 5.0     # the Paris cell
    assert r[0]["mass"] == pytest.approx(0.6) and r[0]["predictions"] == 3
    assert r[1]["south"] == 35.0 and r[1]["west"] == 135.0 and r[1]["mass"] == pytest.approx(0.25)
    assert all(set(x) >= {"south", "north", "west", "east"} and not {"lat", "lon", "latitude"} & set(x) for x in r)  # no point is offered
    assert r[0]["share_of_returned"] == pytest.approx(0.6 / 0.9, abs=0.001)
    assert out["diffuse"] is False and out["total_mass"] == pytest.approx(0.9)


def test_a_thin_spread_is_flagged_diffuse():
    coords = [(10.0 * i - 80, 20.0 * i - 170) for i in range(16)]
    out = geo.regionize(coords, [1 / 16] * 16)
    assert out["diffuse"] is True and out["regions"][0]["share_of_returned"] == pytest.approx(1 / 16, abs=0.001)


def test_edges_wrap_and_clamp_and_empty_is_empty():
    out = geo.regionize([(90.0, 180.0), (-90.0, -180.0), (10.0, 190.0)], [0.4, 0.4, 0.2], cell=5.0)
    cells = {(x["south"], x["west"]) for x in out["regions"]}
    assert (85.0, -180.0) in cells                         # lon 180 wraps to -180; lat 90 stays in the top row
    assert (-90.0, -180.0) in cells and (10.0, -170.0) in cells
    assert all(-90 <= x["south"] < x["north"] <= 90 and -180 <= x["west"] < x["east"] <= 180 for x in out["regions"])
    assert geo.regionize([], [])["regions"] == [] and geo.regionize([(1, 1)], [])["regions"] == []


def test_the_real_backend_refuses_without_an_opt_in_and_says_what_is_missing(monkeypatch):
    b = geo.GeoClipBackend()
    assert b.supports() is False and b.supports(allow_unverified_licence=True) is True
    monkeypatch.delenv("ASTRAL_GEOCLIP_CLIP_DIR", raising=False)
    ok, why = b.available()
    assert ok is False and ("not installed" in why or "ASTRAL_GEOCLIP_CLIP_DIR" in why)


# --------------------------------------------------------------------------------- plugin plumbing
class Scripted:
    name, capability = "geoclip", "geo.region"
    model_id = "geoclip"

    def __init__(self):
        self.ran = 0

    def available(self):
        return True, ""

    def supports(self, allow_unverified_licence=False, **_):
        return bool(allow_unverified_licence)

    def run(self, *, data, top_k=100, cell_degrees=5.0, max_regions=5, **_):
        self.ran += 1
        out = geo.regionize([(48.86, 2.35), (48.9, 2.2), (35.68, 139.69)], [0.5, 0.2, 0.1], cell=cell_degrees, max_regions=max_regions)
        return {**out, "top_k": top_k, "gallery": 100000, "image": {"width": 10, "height": 10}}


@pytest.fixture()
def client(db, dsn, tmp_path):
    from fastapi.testclient import TestClient

    from astral.api import create_app
    from astral.config import Settings

    app = create_app(Settings(dsn=dsn, data_dir=tmp_path), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        for n in ("media-probe", "geolocate"):
            r = c.post("/plugins", json={"path": str(REPO / "plugins" / "core" / n)})
            assert r.status_code == 201, r.text
            c.post(f"/plugins/{r.json()['id']}/{r.json()['version']}/enabled")
        yield c


def setup_case(client, backend):
    import cv2
    import numpy as np

    client.app.state.host.backends._by_cap["geo.region"] = [backend]
    case = client.post("/cases", json={"name": "geo"}).json()["id"]
    png = cv2.imencode(".png", np.full((32, 32, 3), 90, np.uint8))[1].tobytes()
    sha = client.post(f"/cases/{case}/artifacts", content=png, headers={"content-type": "image/png"}).json()["sha256"]
    return case, sha


def invoke(client, case, sha, **cfg):
    r = client.post(f"/cases/{case}/plugins/core.geolocate/invoke", json={"input": {"artifact": sha}, "config": cfg})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.skipif(not module_available("cv2"), reason="opencv not installed")
def test_without_the_opt_in_the_model_does_not_run_and_the_step_says_why(client):
    b = Scripted()
    case, sha = setup_case(client, b)
    res = invoke(client, case, sha)
    assert res["status"] == "error" and b.ran == 0 and res["assertion_ids"] == []
    assert "allow_unverified_licence" in str(res)


@pytest.mark.skipif(not module_available("cv2"), reason="opencv not installed")
def test_with_the_opt_in_regions_are_recorded_as_graded_guesses_never_points(client, db):
    b = Scripted()
    case, sha = setup_case(client, b)
    res = invoke(client, case, sha, allow_unverified_licence=True, max_regions=2)
    assert res["status"] == "ok" and b.ran == 1 and res["stats"]["regions"] == 2
    assert len(res["assertion_ids"]) == 10                                   # 5 assertions per region
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT predicate, object_value, info_grade, derived_by FROM assertion WHERE case_id=%s"
            " AND predicate IN ('regionBounds','modelShare','guessFor')", (case,)).fetchall()
    assert len(rows) == 6 and all(r["info_grade"] == 5 for r in rows)
    assert {r["predicate"] for r in rows} == {"regionBounds", "modelShare", "guessFor"}
    bounds = [r["object_value"] for r in rows if r["predicate"] == "regionBounds"]
    assert all(set(b) == {"south", "west", "north", "east"} for b in bounds)       # bounds only: no coordinate pair is stored
    assert all(r["derived_by"]["params"]["licence_class"]["geoclip"] == "unknown" for r in rows)


def test_geoclip_is_registered_as_unknown_licence_and_not_default():
    from astral.compute.models import load_registry

    reg = load_registry()
    for mid in ("geoclip", "clip-vit-large-patch14"):
        m = reg[mid] if not hasattr(reg, "get") else reg.get(mid)
        assert m.licence_class == "unknown" and m.commercial_use is None and not m.default
