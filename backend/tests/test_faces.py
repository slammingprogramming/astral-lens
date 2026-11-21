"""M3: perceptual hashing, face detection/embedding/search, watchlist, retention.

HONEST SCOPE. Face tests use real photographs only when ASTRAL_FACE_TEST_DIR points
at an extracted LFW `lfw_funneled` directory (LFW photos are copyrighted and are not
in the repository); otherwise they skip. They check that the wiring is right and that
same-person photos outrank different-person ones on THESE images. They say nothing
about hard images (CCTV, profile, occlusion), where the default models are far less
reliable. The pgvector path is tested only when the test database has the extension.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from astral import faces, pdq
from astral.api import create_app
from astral.backends import module_available
from astral.config import Settings

from conftest import REPO

need_cv = pytest.mark.skipif(not module_available("cv2"), reason="opencv not installed")
LFW = os.environ.get("ASTRAL_FACE_TEST_DIR")
need_faces = pytest.mark.skipif(
    not (module_available("cv2") and faces.models_dir() and LFW and Path(LFW).exists()),
    reason="needs opencv, the YuNet/SFace models and ASTRAL_FACE_TEST_DIR (an LFW lfw_funneled directory)")


def lfw(person: str, n: int) -> bytes:
    return (Path(LFW) / person / f"{person}_{n:04d}.jpg").read_bytes()


def synth(seed: int, size: int = 256) -> bytes:
    import cv2
    import numpy as np

    rng = np.random.default_rng(seed)
    img = np.full((size, size, 3), 40, np.uint8)
    for _ in range(14):
        c = tuple(int(x) for x in rng.integers(30, 255, 3))
        p = rng.integers(10, size - 10, 4)
        if rng.random() < 0.5:
            cv2.circle(img, (int(p[0]), int(p[1])), int(rng.integers(8, 60)), c, -1)
        else:
            cv2.rectangle(img, (int(p[0]), int(p[1])), (int(p[2]), int(p[3])), c, -1)
    return cv2.imencode(".png", img)[1].tobytes()


def variant(data: bytes, kind: str) -> bytes:
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if kind == "resize":
        img = cv2.resize(cv2.resize(img, (140, 140)), (256, 256))
    elif kind == "bright":
        img = cv2.convertScaleAbs(img, alpha=1.0, beta=18)
    elif kind == "blur":
        img = cv2.GaussianBlur(img, (5, 5), 0)
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 35 if kind == "jpeg" else 90])[1].tobytes()


# ------------------------------------------------------------------- hashing
@need_cv
def test_perceptual_hash_separates_reencodings_from_different_pictures():
    base = synth(1)
    h0, q0 = pdq.hash_image(base)
    assert len(h0) == 64 and q0 > 0.1
    for kind in ("resize", "jpeg", "bright", "blur"):
        d = pdq.distance(h0, pdq.hash_image(variant(base, kind))[0])
        assert d <= 24, (kind, d)
    others = [pdq.distance(h0, pdq.hash_image(synth(s))[0]) for s in range(2, 12)]
    assert min(others) > 60, others                      # unrelated pictures sit near 128 of 256


@need_cv
def test_flat_image_has_low_quality():
    import cv2
    import numpy as np

    flat = cv2.imencode(".png", np.full((128, 128, 3), 90, np.uint8))[1].tobytes()
    assert pdq.hash_image(flat)[1] < 0.05


# --------------------------------------------------------------- API fixtures
@pytest.fixture()
def client(db, dsn, tmp_path):
    app = create_app(Settings(dsn=dsn, data_dir=tmp_path), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        for n in ("media-probe", "faces"):
            r = c.post("/plugins", json={"path": str(REPO / "plugins" / "core" / n)})
            assert r.status_code == 201, r.text
            c.post(f"/plugins/{r.json()['id']}/{r.json()['version']}/enabled")
        yield c


def upload(client, case, data: bytes, mime="image/jpeg") -> str:
    r = client.post(f"/cases/{case}/artifacts", content=data, headers={"content-type": mime})
    assert r.status_code == 201, r.text
    return r.json()["sha256"]


def ingest(client, case, data: bytes, mime="image/jpeg"):
    sha = upload(client, case, data, mime)
    r = client.post(f"/cases/{case}/artifacts/{sha}/ingest")
    assert r.status_code == 200, r.text
    return sha, r.json()


@need_cv
def test_near_duplicates_found_through_the_api(client):
    case = client.post("/cases", json={"name": "dups"}).json()["id"]
    base = synth(3)
    a, ra = ingest(client, case, base, "image/png")
    b, _ = ingest(client, case, variant(base, "resize"))
    c, _ = ingest(client, case, synth(9), "image/png")
    assert {x["source"]: x for x in ra["steps"]["probe"]["coverage"]}["perceptual hash"]["outcome"] == "hits"
    pairs = client.get(f"/cases/{case}/images/near-duplicates").json()
    assert [(p["a"], p["b"]) for p in pairs] in ([(a, b)], [(b, a)])
    assert pairs[0]["algo"] == "pdq-like-1" and pairs[0]["distance"] <= 24
    assert all(c not in (p["a"], p["b"]) for p in pairs)


@need_cv
def test_without_face_models_search_says_so_not_no_match(client, monkeypatch):
    monkeypatch.setenv("ASTRAL_FACE_MODELS_DIR", str(Path("nowhere")))
    monkeypatch.setattr(faces, "models_dir", lambda: None)
    case = client.post("/cases", json={"name": "nomodels"}).json()["id"]
    sha, rep = ingest(client, case, synth(4), "image/png")
    cov = {c["source"]: c for c in rep["steps"]["faces"]["coverage"]}["face detection"]
    assert cov["outcome"] == "unknown" and "not found" in cov["note"]
    r = client.post(f"/cases/{case}/faces/search", json={"artifact": sha})
    assert r.status_code == 503


# ----------------------------------------------------------- real faces (LFW)
@need_faces
def test_same_person_outranks_different_people_with_provenance(client, db):
    case = client.post("/cases", json={"name": "faces"}).json()["id"]
    people = ["George_W_Bush", "Colin_Powell", "Tony_Blair"]
    shas = {}
    for p in people:
        for n in (1, 2):
            shas[(p, n)], rep = ingest(client, case, lfw(p, n))
            fc = {c["source"]: c for c in rep["steps"]["faces"]["coverage"]}["face detection"]
            assert fc["outcome"] == "hits" and "no identity" in fc["note"]

    listed = client.get(f"/cases/{case}/faces").json()
    assert len(listed) >= 6 and all(f["model_id"] == "sface" and f["backend"] == "yunet-sface" for f in listed)
    assert all(f["expires_at"] for f in listed)                          # biometric clock runs from the first write
    crop = client.get(f"/cases/{case}/faces/{listed[0]['id']}/crop")
    assert crop.status_code == 200 and crop.headers["content-type"] == "image/jpeg"

    probe = upload(client, case, lfw("George_W_Bush", 3))
    r = client.post(f"/cases/{case}/faces/search", json={"artifact": probe, "scope": "case", "k": 6}).json()
    top = r["candidates"][0]
    assert top["artifact"] in (shas[("George_W_Bush", 1)], shas[("George_W_Bush", 2)])
    assert top["score"] > r["candidates"][-1]["score"] and top["above_reference"] is True
    assert top["backend"] == "yunet-sface" and r["licences"]["sface"]["licence_class"] == "permissive"
    assert "not an identification" in r["notice"]

    # the search was recorded, with ids/counts only
    with db.connection() as conn:
        d = conn.execute("SELECT detail FROM audit_log WHERE action='face.search' AND case_id=%s ORDER BY id DESC LIMIT 1",
                         (case,)).fetchone()["detail"]
    assert d["model"] == "sface" and d["results"] == len(r["candidates"]) and "label" not in str(d)
    assert client.get("/audit/verify").json()["ok"] is True

    # the assertion log holds regions, never an identity
    ents = client.get(f"/cases/{case}/entities").json()
    assert any(e["schema_name"] == "FaceRegion" for e in ents)


@need_faces
def test_watchlist_is_operator_supplied_and_labels_stay_out_of_the_audit_log(client, db):
    case = client.post("/cases", json={"name": "watch"}).json()["id"]
    ref = upload(client, case, lfw("Colin_Powell", 1))
    e = client.post(f"/cases/{case}/watchlist", json={"artifact": ref, "label": "SECRET LABEL", "basis": "client brief"})
    assert e.status_code == 201, e.text
    ingest(client, case, lfw("Colin_Powell", 2))
    ingest(client, case, lfw("Tony_Blair", 1))
    probe = upload(client, case, lfw("Colin_Powell", 3))
    r = client.post(f"/cases/{case}/faces/search", json={"artifact": probe, "scope": "watchlist"}).json()
    assert [c["label"] for c in r["candidates"]] == ["SECRET LABEL"]
    with db.connection() as conn:
        rows = conn.execute("SELECT detail FROM audit_log WHERE case_id=%s", (case,)).fetchall()
    assert "SECRET LABEL" not in str(rows) and "client brief" not in str(rows)
    assert client.delete(f"/cases/{case}/watchlist/{e.json()['id']}").json() == {"removed": 1}
    assert client.post(f"/cases/{case}/faces/search", json={"artifact": probe, "scope": "watchlist"}).json()["candidates"] == []


@need_faces
def test_enrolment_needs_exactly_one_face_and_a_blank_image_finds_none(client):
    case = client.post("/cases", json={"name": "enrol"}).json()["id"]
    r = client.post(f"/cases/{case}/watchlist", json={"artifact": upload(client, case, synth(5), "image/png"), "label": "x"})
    assert r.status_code == 422 and "exactly one" in r.json()["detail"]
    sha, rep = ingest(client, case, synth(6), "image/png")
    c = {x["source"]: x for x in rep["steps"]["faces"]["coverage"]}["face detection"]
    assert c["outcome"] == "no_hits" and "not proof" in c["note"]


@need_faces
def test_biometric_retention_clock_and_purge(client, db, actor):
    case = client.post("/cases", json={"name": "ret", "retention_policy": {"biometric_days": 2}}).json()["id"]
    ingest(client, case, lfw("Tony_Blair", 1))
    f = client.get(f"/cases/{case}/faces").json()[0]
    exp = datetime.fromisoformat(f["expires_at"])
    assert timedelta(days=1.9) < exp - datetime.now(timezone.utc) < timedelta(days=2.1)
    with db.connection() as conn:
        assert faces.purge_expired(conn, actor) == 0
        assert faces.purge_expired(conn, actor, now=datetime.now(timezone.utc) + timedelta(days=3)) >= 1
    assert client.get(f"/cases/{case}/faces").json() == []


@need_faces
def test_noncommercial_backend_is_never_chosen_without_opt_in(client):
    from astral.faces import InsightFaceOptIn

    assert InsightFaceOptIn().supports() is False
    reg = client.app.state.host.backends
    assert reg.resolve("face.embed", prefer="insightface").backend.name == "yunet-sface"


def test_pgvector_path_matches_exact_when_the_extension_exists(db, actor):
    import numpy as np

    with db.connection() as conn:
        try:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except Exception:                                   # noqa: BLE001 - extension not installed on this server
            conn.rollback()
        if not faces.pgvector_available(conn):
            pytest.skip("test database has no pgvector extension")
        from astral import cases

        case = cases.create_case(conn, actor, f"pgv-{uuid.uuid4().hex[:6]}")
        rng = np.random.default_rng(0)
        vecs = rng.normal(size=(40, 128))
        vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
        fs = [{"entity": str(uuid.uuid4()), "box": {"x": 0, "y": 0, "w": 1, "h": 1}, "det_score": 0.9,
               "embedding": [float(x) for x in v]} for v in vecs]
        faces.index_faces(conn, actor, case, artifact_sha=b"\x01" * 32, faces=fs, model_id="sface", backend="t")
        q = [float(x) for x in vecs[7] + 0.05 * rng.normal(size=128)]
        a = faces.search(conn, case, q, model_id="sface", k=5, use_pgvector=True)
        b = faces.search(conn, case, q, model_id="sface", k=5, use_pgvector=False)
    assert [x["face_id"] for x in a] == [x["face_id"] for x in b]
    assert all(abs(x["score"] - y["score"]) < 1e-4 for x, y in zip(a, b))
