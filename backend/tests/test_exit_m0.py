"""M0 exit test (docs/11-roadmap.md):

    ingest a file, write a graded assertion through a plugin, drop and rebuild
    every projection, and explain the resulting conclusion back to the
    originating artifact.

Driven through the HTTP API, the way a real client would.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from astral import cases, projector
from astral.api import create_app
from astral.config import Settings

from conftest import EXAMPLES

DOC = (b"Meeting notes, 2026-03-02.\nContact Dana at dana@example.org "
       b"and cc finance@example.org.\n")


@pytest.fixture()
def client(db, dsn, tmp_path):
    app = create_app(Settings(dsn=dsn, data_dir=tmp_path), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        yield c


def test_m0_exit(client, db, actor):
    # 1. ingest a file
    case_id = client.post("/cases", json={"name": "m0-exit"}).json()["id"]
    up = client.post(f"/cases/{case_id}/artifacts", content=DOC,
                     headers={"content-type": "text/plain", "x-astral-filename": "notes.txt"})
    assert up.status_code == 201
    sha = up.json()["sha256"]

    # 2. write graded assertions through a plugin
    assert client.post("/plugins", json={"path": str(EXAMPLES / "email-finder")}).status_code == 201
    client.post("/plugins/example.email-finder/1.0.0/enabled")
    with db.connection() as conn:                  # the operator vouches for the source
        sid = cases.get_or_create_source(conn, actor, kind="plugin", name="example.email-finder")
        cases.set_source_grade(conn, actor, sid, "B")
    inv = client.post(f"/cases/{case_id}/plugins/example.email-finder/invoke",
                      json={"input": {"artifact": sha, "text": DOC.decode()}})
    body = inv.json()
    assert inv.status_code == 200 and body["status"] == "ok" and body["stats"]["addresses"] == 2
    assert len(body["assertion_ids"]) == 7

    rebuilt = client.post(f"/cases/{case_id}/rebuild").json()
    assert rebuilt["entities"] == 3 and rebuilt["edges"] == 2
    before = client.get(f"/cases/{case_id}/entities").json()
    assert {e["schema_name"] for e in before} == {"Document", "EmailAddress"}

    # 3. drop and rebuild EVERY projection
    with db.connection() as conn:
        import uuid as _u

        snap_before = projector.snapshot(conn, _u.UUID(case_id))
        projector.drop_projections(conn)
        assert conn.execute("SELECT (SELECT count(*) FROM proj_entity)"
                            " + (SELECT count(*) FROM proj_property)"
                            " + (SELECT count(*) FROM proj_edge) AS n").fetchone()["n"] == 0
    assert client.get(f"/cases/{case_id}/entities").json() == []        # truly gone
    client.post(f"/cases/{case_id}/rebuild")
    with db.connection() as conn:
        assert projector.snapshot(conn, _u.UUID(case_id)) == snap_before   # identical
    assert client.get(f"/cases/{case_id}/entities").json() == before

    # 4. explain a resulting conclusion back to the originating artifact
    dana = next(e for e in before
                if e["caption"] == "dana@example.org")
    ent = client.get(f"/cases/{case_id}/entities/{dana['cluster_id']}").json()
    name_prop = next(p for p in ent["properties"] if p["predicate"] == "name")
    assert (name_prop["source_grade"], name_prop["info_grade"]) == ("B", 2)   # graded, not default
    ex = client.get(f"/cases/{case_id}/assertions/{name_prop['assertion_id']}/explain").json()
    assert ex["originating_artifacts"] == [sha]
    leaf = ex["tree"]["artifacts"][0]
    assert leaf["acquisitions"][0]["acquired_by"] == "upload"
    assert leaf["acquisitions"][0]["acquired_from"] == "notes.txt"
    assert ex["tree"]["derived_by"]["plugin"] == "example.email-finder"
    assert ex["tree"]["derived_by"]["version"] == "1.0.0"

    # the audit trail covers the whole run and verifies
    assert client.get("/audit/verify").json() == {"ok": True, "first_bad_entry": None}


def test_api_refuses_cross_site_and_foreign_host_requests(client):
    no_hdr = TestClient(client.app)          # no custom header: what a hostile web page can send
    with no_hdr:
        assert no_hdr.post("/cases", json={"name": "csrf"}).status_code == 403
        assert no_hdr.get("/health", headers={"host": "evil.example"}).status_code == 400


def test_team_profile_refuses_to_start_without_real_auth(dsn, tmp_path):
    with pytest.raises(RuntimeError, match="no header-based or anonymous stand-in"):
        create_app(Settings(dsn=dsn, data_dir=tmp_path, profile="team"))


def test_unknown_case_looks_the_same_as_someone_elses(client):
    import uuid

    r = client.get(f"/cases/{uuid.uuid4()}")
    assert r.status_code == 404 and r.json()["detail"] == "case not found"
