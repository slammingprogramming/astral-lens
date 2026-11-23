"""Lens stages beyond retrieval (artifact_facts, face_search) and the acceptance scenarios shipped
as built-in lenses and a built-in machine.

HONEST SCOPE. The face-search step is exercised here without face models (it must say it did not
run, not that nobody matched) and, when the LFW set and models are present, with real faces. The
name-to-network machine is checked for shape and for running against connectors that have no
key: it must report 'unknown', never 'no hits'. Nothing here contacts a registry.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from astral import ai, builtin, faces, lens, machines
from astral.api import create_app
from astral.config import Settings

from conftest import REPO
from test_faces import ingest as ingest_image, lfw, need_cv, need_faces, synth, upload

SHA = "ab" * 32
DOC = ("Acme Holdings Ltd was incorporated in London in 2015. Its director is Jane Q Doe. "
       "Contact the office on +44 20 7946 0018.\n")


@pytest.fixture()
def client(db, dsn, tmp_path):
    app = create_app(Settings(dsn=dsn, data_dir=tmp_path), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        for n in ("doc-ingest", "ner", "media-probe", "faces"):
            r = c.post("/plugins", json={"path": str(REPO / "plugins" / "core" / n)})
            assert r.status_code == 201, r.text
            c.post(f"/plugins/{r.json()['id']}/{r.json()['version']}/enabled")
        yield c


def run_lens(c, case, name, **inputs):
    p = c.post(f"/cases/{case}/lenses/{name}/run", json={"inputs": inputs})
    assert p.status_code == 201, p.text
    p = p.json()
    assert c.post(f"/cases/{case}/plans/{p['id']}/approve").status_code == 200
    r = c.post(f"/cases/{case}/plans/{p['id']}/run")
    assert r.status_code == 200, r.text
    return p, r.json()


# ------------------------------------------------------------------ unit
def test_new_step_types_are_typed_and_the_model_planner_cannot_use_them():
    ok = ai.validate_plan({"steps": [{"id": "f", "type": "face_search", "args": {"artifact": SHA.upper(), "k": 99}},
                                     {"id": "a", "type": "artifact_facts", "args": {"artifact": SHA}}]})
    assert ok["steps"][0]["args"] == {"artifact": SHA, "scope": "all", "k": ai.MAX_K}
    bad = [{"id": "f", "type": "face_search", "args": {"artifact": "not-a-hash"}},
           {"id": "f", "type": "face_search", "args": {"artifact": SHA, "scope": "internet"}},
           {"id": "a", "type": "artifact_facts", "args": {}},
           {"id": "s", "type": "search_text", "args": {"query": "q", "k": "many"}}]
    for s in bad:
        with pytest.raises(ai.PlanError):
            ai.validate_plan({"steps": [s]})
    with pytest.raises(ai.PlanError, match="unknown step type"):                 # a model may only plan retrieval
        ai.validate_plan({"steps": [{"id": "f", "type": "face_search", "args": {"artifact": SHA}}]}, allowed=ai.PLANNER_STEPS)
    est = ai.estimate(ok, None)
    assert est["face_searches"] == 1 and est["egress"] == "none"


def test_every_builtin_definition_validates():
    lenses, machs = builtin.definitions("lens"), builtin.definitions("machine")
    assert set(lenses) == {"photo-candidates", "media-dossier", "shared-identifier"}
    assert set(machs) == {"name-to-network"}
    q, plan = lens.instantiate(lenses["photo-candidates"], {"photo": SHA})
    assert SHA in q and [s["type"] for s in plan["steps"]] == ["face_search", "artifact_facts"]
    with pytest.raises(lens.LensError):                       # a photo input must be a file hash, not a name
        lens.instantiate(lenses["photo-candidates"], {"photo": "Jane Q Doe"})
    m = machs["name-to-network"]
    assert len(m["stages"]) <= machines.MAX_STAGES and m["limits"]["max_calls"] <= machines.MAX_CALLS
    assert all(s["transform"].startswith("connector.") and (REPO / "plugins" / "connectors" / s["transform"][10:]).is_dir()
               for s in m["stages"])


# ------------------------------------------------------------------ API
def test_builtins_install_on_request_with_their_own_origin(client, db):
    cat = client.get("/builtin").json()
    assert {(r["kind"], r["name"]) for r in cat} == {("lens", "photo-candidates"), ("lens", "media-dossier"),
                                                     ("lens", "shared-identifier"), ("machine", "name-to-network")}
    assert not any(r["installed"] for r in cat)
    assert client.get("/lenses").json() == []                           # nothing is installed behind the operator's back
    for r in cat:
        out = client.post(f"/builtin/{r['kind']}/{r['name']}/install")
        assert out.status_code == 201 and out.json()["sha256"] == r["sha256"]
        assert client.post(f"/builtin/{r['kind']}/{r['name']}/install").status_code == 201   # idempotent
    assert all(r["installed"] for r in client.get("/builtin").json())
    assert {(x["name"], x["origin"], x["trust"]) for x in client.get("/lenses").json()} == {
        ("photo-candidates", "builtin", "builtin"), ("media-dossier", "builtin", "builtin"), ("shared-identifier", "builtin", "builtin")}
    assert [(x["name"], x["origin"]) for x in client.get("/machines").json()] == [("name-to-network", "builtin")]
    assert client.post("/builtin/lens/no-such-lens/install").status_code == 404
    assert client.post("/builtin/plugin/x/install").status_code == 404
    with db.connection() as conn:
        n = conn.execute("SELECT count(*) AS n FROM audit_log WHERE action IN ('lens.save','machine.save')").fetchone()["n"]
    assert n == 4                                                       # re-installs did not write again


def test_media_dossier_cites_what_is_recorded_about_one_file(client):
    case = client.post("/cases", json={"name": "dossier"}).json()["id"]
    sha = client.post(f"/cases/{case}/artifacts", content=DOC.encode(), headers={"content-type": "text/plain"}).json()["sha256"]
    assert client.post(f"/cases/{case}/artifacts/{sha}/ingest").status_code == 200
    client.post("/builtin/lens/media-dossier/install")
    p, out = run_lens(client, case, "media-dossier", media=sha)
    assert p["planner"] == "lens:media-dossier@1"
    facts = [e for e in out["evidence"] if e["kind"] == "fact"]
    assert facts and all(e["assertion_id"] for e in facts)
    assert any("Jane Q Doe" in e["text"] or "Acme Holdings" in e["text"] for e in facts)   # entities found in the file
    assert out["claims"] and all(c["anchors"] for c in out["claims"])
    # a file the case does not hold is a gap, not an empty dossier
    _, none = run_lens(client, case, "media-dossier", media="cd" * 32)
    assert none["evidence"] == [] and any("nothing recorded in this case cites file" in n for n in none["notes"])


@need_cv
def test_face_step_without_models_says_it_did_not_run(client, monkeypatch):
    monkeypatch.setattr(faces, "models_dir", lambda: None)
    case = client.post("/cases", json={"name": "nomodels"}).json()["id"]
    sha, _ = ingest_image(client, case, synth(4), "image/png")
    client.post("/builtin/lens/photo-candidates/install")
    _, out = run_lens(client, case, "photo-candidates", photo=sha)
    (fc,) = out["face_candidates"]
    assert fc["status"] == "not run" and fc["reason"]
    assert any("face search s1 was not run" in n for n in out["notes"])
    assert "not evidence" in out["report_md"] or "not run" in out["report_md"]
    assert all(e["kind"] != "face" for e in out["evidence"])


@need_faces
def test_photo_lens_ranks_the_same_person_first_and_keeps_candidates_out_of_claims(client, db):
    case = client.post("/cases", json={"name": "photo"}).json()["id"]
    same = {ingest_image(client, case, lfw("George_W_Bush", n))[0] for n in (1, 2)}
    ingest_image(client, case, lfw("Tony_Blair", 1))
    probe = upload(client, case, lfw("George_W_Bush", 3))
    client.post("/builtin/lens/photo-candidates/install")
    _, out = run_lens(client, case, "photo-candidates", photo=probe)
    (fc,) = out["face_candidates"]
    assert fc["status"] == "done" and fc["candidates"][0]["artifact"] in same
    assert "not an identification" in fc["notice"] and "## Face candidates" in out["report_md"]
    assert not any(c["artifact"] in str(out["claims"]) for c in fc["candidates"])   # candidates are never claims
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE action='face.search' AND case_id=%s",
                            (case,)).fetchone()["n"] == 1


def test_name_to_network_without_keys_reports_unknown_not_no_hits(client, monkeypatch):
    # Only the keyed connectors are installed and their keys are removed, so nothing can leave this
    # machine; GLEIF (keyless, so it WOULD go out) is left uninstalled and must show as unknown too.
    for k in ("OPENSANCTIONS_API_KEY", "OPENCORPORATES_API_TOKEN", "COMPANIES_HOUSE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    for p in client.get("/plugins").json():                    # shared test database: start from a known set
        if p["state"] == "enabled":
            client.post(f"/plugins/{p['id']}/{p['version']}/disabled")
    for n in ("opensanctions", "opencorporates", "companies-house"):
        r = client.post("/plugins", json={"path": str(REPO / "plugins" / "connectors" / n)})
        assert r.status_code == 201, r.text
        client.post(f"/plugins/{r.json()['id']}/{r.json()['version']}/enabled")
    case = client.post("/cases", json={"name": "network"}).json()["id"]
    client.post("/builtin/machine/name-to-network/install")
    r = client.post(f"/cases/{case}/import/table", json={"csv_text": "name\nAcme Holdings Ltd\n",
                                                         "mapping": {"schema": "Company", "key": "name", "name_column": "name"}})
    assert r.status_code == 201, r.text
    seed = next(e for e in client.get(f"/cases/{case}/entities").json() if e["caption"] == "Acme Holdings Ltd")["cluster_id"]
    prop = client.post(f"/cases/{case}/machines/name-to-network/propose", json={"seeds": [seed]})
    assert prop.status_code == 201, prop.text
    assert prop.json()["plan"]["max_calls"] <= 45
    assert client.post(f"/cases/{case}/machine-runs/{prop.json()['id']}/approve", json={}).status_code == 200
    out = client.post(f"/cases/{case}/machine-runs/{prop.json()['id']}/run").json()
    assert out["status"] == "done"
    assert out["coverage_summary"]["no_hits"] == 0 and out["coverage_summary"]["hits"] == 0
    assert out["coverage_summary"]["unknown"] >= 4                      # sanctions, LEI, OpenCorporates, registry
    lei = next(t for t in out["trace"] if t.get("stage") == "lei")
    assert lei["outcome"] == "unknown"
