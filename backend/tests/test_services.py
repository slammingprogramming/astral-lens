"""Dedicated search and vector services. The degradation and bookkeeping tests run anywhere. The tests that
need a real OpenSearch / Qdrant run only when ASTRAL_TEST_OPENSEARCH / ASTRAL_TEST_QDRANT point at one, and
they were run against single-node, security-disabled test containers. NOT covered: a multi-node cluster,
TLS, authentication, or corpora large enough for approximate indexes to matter."""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from astral import cases, faces, retrieval, services
from astral.api import create_app
from astral.config import Settings
from astral.db import SOLO_ACTOR

DEAD = "http://127.0.0.1:9"
OS_URL = os.environ.get("ASTRAL_TEST_OPENSEARCH")
QD_URL = os.environ.get("ASTRAL_TEST_QDRANT")
needs_os = pytest.mark.skipif(not OS_URL, reason="ASTRAL_TEST_OPENSEARCH not set: no OpenSearch server available")
needs_qd = pytest.mark.skipif(not QD_URL, reason="ASTRAL_TEST_QDRANT not set: no Qdrant server available")


def new_case(db):
    with db.connection() as conn:
        return cases.create_case(conn, SOLO_ACTOR, f"svc-{uuid.uuid4().hex[:6]}")


def add_chunks(conn, case, bodies):
    sha = hashlib.sha256(b"".join(b.encode() for b in bodies)).digest()
    pos = 0
    for b in bodies:
        conn.execute("INSERT INTO text_chunk (workspace_id, case_id, text_sha, start_char, end_char, body) VALUES (%s,%s,%s,%s,%s,%s)",
                     (SOLO_ACTOR.workspace_id, case, sha, pos, pos + len(b), b))
        pos += len(b)


def add_faces(conn, case, n, *, watchlist=None, seed=0, dim=16, model="sface"):
    np = pytest.importorskip("numpy")           # optional: comes with the faces extra, not a core dependency
    rng = np.random.default_rng(seed)
    v = rng.normal(size=(n, dim))
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    fs = [{"entity": str(uuid.uuid4()), "box": {"x": 0, "y": 0, "w": 1, "h": 1}, "det_score": 0.9, "embedding": [float(x) for x in row]} for row in v]
    faces.index_faces(conn, SOLO_ACTOR, case, artifact_sha=bytes([seed + 1]) * 32, faces=fs, model_id=model, backend="t", watchlist=watchlist)
    return fs, v


@pytest.fixture()
def real(db):
    ix = f"astral-test-{uuid.uuid4().hex[:8]}"
    text = services.OpenSearchService(OS_URL, index=ix) if OS_URL else None
    vec = services.QdrantService(QD_URL) if QD_URL else None
    yield services.Services(text, vec)
    if text:
        try:
            services._call("DELETE", f"{OS_URL}/{ix}", ok=(200, 404))
        except services.ServiceError:
            pass


# ------------------------------------------------------------------ always run
def test_an_unreachable_service_degrades_visibly_and_postgres_still_answers(db):
    case = new_case(db)
    svc = services.Services(services.OpenSearchService(DEAD), services.QdrantService(DEAD))
    with db.connection() as conn:
        add_chunks(conn, case, ["the harbour master keeps the ledger", "unrelated paragraph about weather"])
        r = retrieval.search(conn, case, "harbour ledger", k=3, services=svc)
        assert r["hits"] and "opensearch" not in r["rankers"]
        assert any("opensearch unavailable" in n for n in r["notes"])
        fs, v = add_faces(conn, case, 5)
        hits, why = svc.face_hits(conn, case, fs[0]["embedding"], model_id="sface", scope="all", k=3, min_score=None,
                                  exclude_artifact=None, exclude_face=None)
        assert hits is None and "qdrant unavailable" in why
        st = svc.status(conn)
        assert st["text"]["reachable"] is False and st["vector"]["fallback"] == "postgres"


def test_a_failed_deletion_is_recorded_not_swallowed_and_is_retryable(db):
    case = new_case(db)
    svc = services.Services(services.OpenSearchService(DEAD), services.QdrantService(DEAD))
    with db.connection() as conn:
        out = svc.forget_case(conn, case)
        assert all(v.startswith("NOT removed") for v in out.values()) and len(out) == 2
        pend = svc.status(conn)["pending_deletes"]
        assert {p["service"] for p in pend if p["case_id"] == str(case)} == {"opensearch", "qdrant"}
        again = svc.reconcile_faces(conn, [case])
        assert again["NOT_RECONCILED"] == [str(case)] and "may still hold biometric vectors" in again["warning"]
        conn.execute("DELETE FROM service_pending_delete WHERE case_id=%s", (case,))


def test_index_names_are_restricted():
    with pytest.raises(services.ServiceError):
        services.OpenSearchService("http://x", index="Bad/Name")


def test_unconfigured_services_change_nothing(db):
    svc = services.Services()
    case = new_case(db)
    with db.connection() as conn:
        add_chunks(conn, case, ["plain text only"])
        r = retrieval.search(conn, case, "plain text", services=svc)
        assert r["rankers"] == ["fts", "fuzzy"] and len(r["notes"]) == 1
        assert svc.forget_case(conn, case) == {} and svc.reconcile_faces(conn) == {} and not svc.configured


def test_the_api_reports_service_state_and_erasure_says_what_it_could_not_remove(db, dsn, tmp_path):
    app = create_app(Settings(dsn=dsn, data_dir=tmp_path), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        app.state.services = services.Services(services.OpenSearchService(DEAD), services.QdrantService(DEAD))
        assert c.get("/services").json()["text"]["reachable"] is False
        case = c.post("/cases", json={"name": "svc"}).json()["id"]
        r = c.post(f"/cases/{case}/erase", json={"legal_basis": "request"}).json()
        assert r["services"]["opensearch"].startswith("NOT removed") and r["services"]["qdrant"].startswith("NOT removed")
        assert len(c.get("/services").json()["pending_deletes"]) >= 2
        c.post("/services/retry-deletes")
        with app.state.db.connection() as conn:
            conn.execute("DELETE FROM service_pending_delete WHERE case_id=%s", (case,))
        app.state.services = services.Services()
        assert c.post(f"/cases/{case}/services/sync").status_code == 400


# ------------------------------------------------------------------ against real servers
@needs_os
def test_opensearch_ranks_as_an_extra_ranker_and_is_case_isolated(db, real):
    a, b = new_case(db), new_case(db)
    with db.connection() as conn:
        add_chunks(conn, a, ["The vessel Marlin docked at pier nine", "customs inspected the containers", "weather was calm"])
        add_chunks(conn, b, ["The vessel Marlin appears only in the other case"])
        r = retrieval.search(conn, a, "vessel Marlin pier", k=5, services=real)
        assert "opensearch" in r["rankers"]
        assert r["hits"][0]["text"].startswith("The vessel Marlin docked")
        assert all("other case" not in h["text"] for h in r["hits"])
        n1 = services.sync_text(conn, real.text, a)
        assert n1["indexed"] == 0 and n1["total_in_service"] == 3          # nothing new, nothing duplicated
        add_chunks(conn, a, ["a new note about Marlin cargo"])
        assert services.sync_text(conn, real.text, a)["indexed"] == 1
        assert real.text.count(a) == 4


@needs_os
def test_erasing_a_case_removes_its_text_from_opensearch(db, real):
    a, b = new_case(db), new_case(db)
    with db.connection() as conn:
        add_chunks(conn, a, ["alpha secret text"])
        add_chunks(conn, b, ["bravo text kept"])
        real.sync_case(conn, a)
        real.sync_case(conn, b)
        assert real.text.count(a) == 1 and real.text.count(b) == 1
        out = real.forget_case(conn, a)
        assert out["opensearch"].startswith("removed")
    assert real.text.count(a) == 0 and real.text.count(b) == 1


@needs_qd
def test_qdrant_exact_search_matches_postgres_exactly(db, real):
    np = pytest.importorskip("numpy")
    case = new_case(db)
    with db.connection() as conn:
        fs, v = add_faces(conn, case, 60, seed=3)
        q = [float(x) for x in v[11] + 0.02 * np.random.default_rng(1).normal(size=16)]
        mine, why = real.face_hits(conn, case, q, model_id="sface", scope="all", k=8, min_score=None, exclude_artifact=None, exclude_face=None)
        pg = faces.search(conn, case, q, model_id="sface", k=8, use_pgvector=False)
    assert why is None
    assert [x["face_id"] for x in mine] == [x["face_id"] for x in pg]
    assert all(abs(x["score"] - y["score"]) < 1e-4 for x, y in zip(mine, pg))
    assert mine[0]["face_id"] == fs[11]["entity"]


@needs_qd
def test_scope_and_exclusions_are_applied_by_the_service(db, real):
    case = new_case(db)
    with db.connection() as conn:
        base, v = add_faces(conn, case, 10, seed=5)
        wl, wv = add_faces(conn, case, 4, seed=6, watchlist="default")
        q = base[0]["embedding"]
        only_wl, _ = real.face_hits(conn, case, q, model_id="sface", scope="watchlist", k=20, min_score=None, exclude_artifact=None, exclude_face=None)
        only_case, _ = real.face_hits(conn, case, q, model_id="sface", scope="case", k=20, min_score=None, exclude_artifact=None, exclude_face=None)
        no_self, _ = real.face_hits(conn, case, q, model_id="sface", scope="all", k=20, min_score=None, exclude_artifact=None,
                                    exclude_face=uuid.UUID(base[0]["entity"]))
        no_art, _ = real.face_hits(conn, case, q, model_id="sface", scope="all", k=20, min_score=None, exclude_artifact=bytes([7]) * 32, exclude_face=None)
    assert len(only_wl) == 4 and all(h["watchlist"] for h in only_wl)
    assert len(only_case) == 10 and not any(h["watchlist"] for h in only_case)
    assert base[0]["entity"] not in {h["face_id"] for h in no_self}
    assert len(no_art) == 10


@needs_qd
def test_biometric_vectors_leave_qdrant_when_they_expire_or_are_removed(db, real):
    case, other = new_case(db), new_case(db)
    with db.connection() as conn:
        fs, _ = add_faces(conn, case, 6, seed=8)
        ofs, _ = add_faces(conn, other, 3, seed=9)
        real.sync_case(conn, case)
        real.sync_case(conn, other)
        assert len(real.vector.point_ids("sface", 16, case)) == 6
        ids = [f["entity"] for f in fs[:2]]
        conn.execute("UPDATE face SET expires_at=%s WHERE id = ANY(%s::uuid[])", (datetime.now(timezone.utc) - timedelta(days=1), ids))
        assert faces.purge_expired(conn, SOLO_ACTOR) >= 2
        res = real.reconcile_faces(conn)
        assert res["qdrant_points_removed"] >= 2 and "NOT_RECONCILED" not in res
        left = real.vector.point_ids("sface", 16, case)
        assert len(left) == 4 and not (set(ids) & left)
        assert len(real.vector.point_ids("sface", 16, other)) == 3
        # a removed watchlist photo is gone from the index too
        wl, _ = add_faces(conn, case, 1, seed=10, watchlist="default")
        real.sync_case(conn, case)
        conn.execute("DELETE FROM face WHERE id=%s", (uuid.UUID(wl[0]["entity"]),))
        real.reconcile_faces(conn, [case])
        assert wl[0]["entity"] not in real.vector.point_ids("sface", 16, case)
        out = real.forget_case(conn, case)
        assert out["qdrant"].startswith("removed")
    assert real.vector.point_ids("sface", 16, case) == set()
    assert len(real.vector.point_ids("sface", 16, other)) == 3


@needs_qd
def test_a_stale_index_can_never_show_a_face_postgres_no_longer_holds(db, real):
    case = new_case(db)
    with db.connection() as conn:
        fs, _ = add_faces(conn, case, 5, seed=12)
        real.sync_case(conn, case)
        conn.execute("DELETE FROM face WHERE id=%s", (uuid.UUID(fs[0]["entity"]),))      # deleted behind the index's back
        raw = real.vector.search("sface", case, fs[0]["embedding"], k=5)
        assert fs[0]["entity"] in {h["id"] for h in raw}                                 # the service is stale...
        shown = services.hydrate_faces(conn, case, raw, model_id="sface")
        assert fs[0]["entity"] not in {h["face_id"] for h in shown}                      # ...and it is not shown
        mine, _ = real.face_hits(conn, case, fs[0]["embedding"], model_id="sface", scope="all", k=5, min_score=None, exclude_artifact=None, exclude_face=None)
        assert fs[0]["entity"] not in {h["face_id"] for h in mine}
        assert fs[0]["entity"] not in real.vector.point_ids("sface", 16, case)           # and the search itself repaired it


@needs_os
@needs_qd
def test_the_api_uses_the_services_and_erasure_clears_them(db, dsn, tmp_path, real):
    app = create_app(Settings(dsn=dsn, data_dir=tmp_path), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        app.state.services = real
        case = c.post("/cases", json={"name": "api"}).json()["id"]
        cid = uuid.UUID(case)
        with db.connection() as conn:
            fs, _ = add_faces(conn, cid, 5, seed=14)
        r = c.post(f"/cases/{case}/faces/search", json={"face_id": fs[0]["entity"], "k": 3}).json()
        assert r["engine"] == "qdrant (exact)" and r["degraded"] is None and len(r["candidates"]) == 3
        assert c.post(f"/cases/{case}/services/sync").json()["faces"]["faces"] == 5
        er = c.post(f"/cases/{case}/erase", json={"legal_basis": "request"}).json()
        assert er["services"]["qdrant"].startswith("removed")
        assert real.vector.point_ids("sface", 16, cid) == set()
