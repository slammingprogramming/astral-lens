"""The analyst workbench: transforms on a selected entity, machines, link analysis, annotations, table
import, exports. HONEST SCOPE: connectors answer from a local fixture server; this proves the workflow,
the brakes (approval, unknown-not-negative, call limits) and the arithmetic, not any live registry."""

from __future__ import annotations

import copy
import json
import uuid
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from astral import analysis, machines, signing
from astral.api import create_app
from astral.config import Settings
from astral.plugins.trust import TrustStore

from conftest import REPO

CONNECTORS = REPO / "plugins" / "connectors"
LEI = "5493001KJTIIGC8Y1R12"
CSV = "name,country\nAcme Holdings Ltd,GB\nBeta Trading SA,LU\n"
MAP = {"schema": "Company", "key": "name", "name_column": "name", "columns": {"country": "jurisdiction"}}


def install(c, path):
    r = c.post("/plugins", json={"path": str(path)})
    assert r.status_code == 201, r.text
    c.post(f"/plugins/{r.json()['id']}/{r.json()['version']}/enabled")


@pytest.fixture()
def rig(db, dsn, tmp_path, server, actor):
    server.add("/lei-records", {"data": [{"type": "lei-records", "id": LEI, "attributes": {"lei": LEI, "entity": {
        "legalName": {"name": "ACME HOLDINGS LIMITED"}, "jurisdiction": "GB", "registeredAs": "12345678",
        "legalAddress": {"city": "London", "country": "GB"}, "status": "ACTIVE"}, "registration": {"status": "ISSUED"}}}]})
    app = create_app(Settings(dsn=dsn, data_dir=tmp_path / "data", signing_key=None), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        for p in c.get("/plugins").json():                    # shared test database: start from a known set
            if p["state"] == "enabled":
                c.post(f"/plugins/{p['id']}/{p['version']}/disabled")
        install(c, CONNECTORS / "gleif")
        install(c, CONNECTORS / "sec-edgar")
        install(c, CONNECTORS / "site-pack")
        case = c.post("/cases", json={"name": "workbench"}).json()["id"]
        yield c, case, server, tmp_path, app


def seed(c, case, csv=CSV, mapping=MAP, **kw):
    r = c.post(f"/cases/{case}/import/table", json={"csv_text": csv, "mapping": mapping, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def entity(c, case, caption):
    return next(e for e in c.get(f"/cases/{case}/entities").json() if e["caption"] == caption)["cluster_id"]


# ------------------------------------------------------------------ import
def test_table_import_stores_the_file_as_evidence_and_does_not_flatter_the_data(rig):
    c, case, *_ = rig
    res = seed(c, case)
    assert res["rows"] == 2 and res["entities"] == 2 and res["source_grade"] == "F" and res["info_grade"] == 6
    assert len(res["artifact"]) == 64 and c.get(f"/cases/{case}/artifacts/{res['artifact']}").status_code == 200
    cid = entity(c, case, "Acme Holdings Ltd")
    detail = json.dumps(c.get(f"/cases/{case}/entities/{cid}").json())
    assert "jurisdiction" in detail and "GB" in detail
    bad = [{**MAP, "schema": "company"}, {**MAP, "key": "nope"}, {**MAP, "columns": {"country": "schema"}},
           {**MAP, "links": [{"column": "country", "predicate": "x y", "schema": "Company"}]}]
    for m in bad:
        assert c.post(f"/cases/{case}/import/table", json={"csv_text": CSV, "mapping": m}).status_code == 422
    assert c.post(f"/cases/{case}/import/table", json={"csv_text": "", "mapping": MAP}).status_code == 422


def test_import_links_create_the_other_end_and_a_graph_edge(rig):
    c, case, *_ = rig
    seed(c, case, "person,employer\nJane Doe,Acme Holdings Ltd\nJohn Roe,Acme Holdings Ltd\n",
         {"schema": "Person", "key": "person", "name_column": "person", "links": [{"column": "employer", "predicate": "employer", "schema": "Company"}]})
    g = c.get(f"/cases/{case}/graph").json()
    assert len(g["nodes"]) == 3 and len(g["edges"]) == 2 and {e["label"] for e in g["edges"]} == {"employer"}
    assert all(e["source_grade"] == "F" and e["info_grade"] == 6 for e in g["edges"])


# ------------------------------------------------------------------ transforms
def test_transforms_are_offered_by_what_the_entity_can_supply(rig):
    c, case, *_ = rig
    seed(c, case)
    t = c.get(f"/cases/{case}/entities/{entity(c, case, 'Acme Holdings Ltd')}/transforms").json()
    by = {x["plugin"]: x for x in t["transforms"]}
    assert by["connector.gleif"]["ready"] and by["connector.gleif"]["inputs"] == {"name": "Acme Holdings Ltd"}
    assert not by["connector.site-pack"]["ready"] and by["connector.site-pack"]["missing"] == ["username"]
    assert by["connector.site-pack"]["needs_approval"] is True
    assert t["transforms"][0]["ready"]                                       # ready ones are listed first


def test_running_a_transform_adds_unmerged_results_and_reports_what_appeared(rig):
    c, case, server, *_ = rig
    seed(c, case)
    cid = entity(c, case, "Acme Holdings Ltd")
    r = c.post(f"/cases/{case}/entities/{cid}/transforms/connector.gleif/run", json={"config": {"base_url": server.url}})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "ok" and out["input"] == {"name": "Acme Holdings Ltd"} and out["coverage"][0]["outcome"] == "hits"
    assert [n["caption"] for n in out["new_entities"]] == ["ACME HOLDINGS LIMITED"] and "unmerged" in out["note"]
    assert len([e for e in c.get(f"/cases/{case}/entities").json() if e["schema_name"] == "Company"]) == 3   # two imported + one registry, not merged


def test_a_connector_that_is_not_configured_is_unknown_and_the_workbench_keeps_working(rig):
    c, case, *_ = rig
    seed(c, case)
    cid = entity(c, case, "Acme Holdings Ltd")
    out = c.post(f"/cases/{case}/entities/{cid}/transforms/connector.sec-edgar/run", json={}).json()
    assert out["status"] == "error" and out["coverage"][0]["outcome"] == "unknown" and "contact" in out["coverage"][0]["note"]
    assert c.post(f"/cases/{case}/entities/{cid}/transforms/connector.nope/run", json={}).status_code == 404
    missing = c.post(f"/cases/{case}/entities/{cid}/transforms/connector.site-pack/run", json={"approved": True})
    assert missing.status_code == 422 and "username" in missing.json()["detail"]


def test_a_connector_that_contacts_the_subject_needs_approval_every_time(rig):
    c, case, server, tmp, _ = rig
    seed(c, case, "handle\nalice\n", {"schema": "UserAccount", "key": "handle", "columns": {"handle": "username"}, "name_column": "handle"})
    cid = entity(c, case, "alice")
    d = tmp / "packs"
    d.mkdir()
    (d / "a.json").write_text(json.dumps({"site": "a.example", "licence": "AGPL-3.0-or-later",
        "check": {"url": server.url + "/p/@{username}", "present_when": {"status": 200}, "absent_when": {"status": 404}},
        "known": {"present": "alice"}, "fixtures": {"present": {"status": 200, "body": ""}, "absent": {"status": 404, "body": ""}}}))
    server.add("/p/@alice", "hi", ctype="text/html")
    cfg = {"config": {"packs_dir": str(d), "sites": ["a.example"]}}
    refused = c.post(f"/cases/{case}/entities/{cid}/transforms/connector.site-pack/run", json=cfg)
    assert refused.status_code == 409 and refused.json()["needs_approval"] and not server.requests
    ok = c.post(f"/cases/{case}/entities/{cid}/transforms/connector.site-pack/run", json={**cfg, "approved": True}).json()
    assert ok["status"] == "ok" and ok["coverage"][0]["outcome"] == "hits"
    assert c.post(f"/cases/{case}/entities/{cid}/transforms/connector.site-pack/run", json=cfg).status_code == 409   # approval does not persist


# ------------------------------------------------------------------ analysis
def path_graph():
    ids = list("abcde")
    return {"nodes": [{"id": i, "caption": i.upper(), "schema": "Person"} for i in ids + ["z"]],
            "edges": [{"id": f"e{i}", "source": a, "target": b, "label": "link", "source_grade": "B", "info_grade": 2, "independent_sources": 1, "assertions": [i]}
                      for i, (a, b) in enumerate(zip(ids, ids[1:]))]}


def test_analysis_arithmetic_on_graphs_with_known_answers():
    g = path_graph()                                    # a-b-c-d-e plus an isolated z
    adj = analysis.adjacency(g)
    bc = analysis.betweenness(adj)
    assert bc["c"] > bc["b"] > bc["a"] == 0 and bc["z"] == 0 and bc["b"] == bc["d"]
    comps = analysis.components(adj)
    assert [len(c) for c in comps] == [5, 1]
    assert analysis.shortest_paths(adj, "a", "e")["paths"] == [list("abcde")] and analysis.shortest_paths(adj, "a", "e")["length"] == 4
    none = analysis.shortest_paths(adj, "a", "z")
    assert none["paths"] == [] and "not evidence that none exists" in none["note"]
    assert analysis.neighbourhood(adj, "c", 1) == {"c": 0, "b": 1, "d": 1} and set(analysis.neighbourhood(adj, "c", 9)) == set("abcde")
    assert analysis.common_neighbours(adj, ["a", "c"]) == ["b"]
    pr = analysis.pagerank(adj)
    assert abs(sum(pr.values()) - 1) < 1e-3 and pr["c"] > pr["a"]
    star = {"nodes": [{"id": i, "schema": "x"} for i in "hxyz"], "edges": [{"source": "h", "target": t, "id": t} for t in "xyz"]}
    assert analysis.betweenness(analysis.adjacency(star))["h"] == 1.0
    two = {"nodes": [{"id": i, "schema": "x"} for i in "abcdef"], "edges": [{"source": s, "target": t, "id": s + t}
           for s, t in ["ab", "bc", "ac", "de", "ef", "df", "cd"]]}
    assert sorted(len(x) for x in analysis.communities(analysis.adjacency(two))) == [3, 3]
    with pytest.raises(KeyError):
        analysis.shortest_paths(adj, "a", "nope")


def test_all_shortest_paths_are_found_and_capped():
    g = {"nodes": [{"id": i, "schema": "x"} for i in "satbe"], "edges": [{"source": s, "target": t, "id": s + t} for s, t in ["sa", "st", "ae", "te", "sb", "be"]]}
    res = analysis.shortest_paths(analysis.adjacency(g), "s", "e", limit=2)
    assert res["length"] == 2 and len(res["paths"]) == 2 and res["truncated"]
    assert len(analysis.shortest_paths(analysis.adjacency(g), "s", "e", limit=9)["paths"]) == 3


def test_analysis_endpoints_use_the_case_graph(rig):
    c, case, *_ = rig
    seed(c, case, "person,employer\nJane Doe,Acme Holdings Ltd\nJohn Roe,Acme Holdings Ltd\nAnn Poe,Beta Trading SA\n",
         {"schema": "Person", "key": "person", "name_column": "person", "links": [{"column": "employer", "predicate": "employer", "schema": "Company"}]})
    a = c.get(f"/cases/{case}/analysis").json()
    assert a["nodes"] == 5 and a["edges"] == 3 and len(a["components"]) == 2 and "recorded links" in a["note"]
    jane, john, ann = (entity(c, case, n) for n in ("Jane Doe", "John Roe", "Ann Poe"))
    p = c.get(f"/cases/{case}/analysis/path", params={"a": jane, "b": john}).json()
    assert p["length"] == 2 and p["named"] == [["Jane Doe", "Acme Holdings Ltd", "John Roe"]]
    assert c.get(f"/cases/{case}/analysis/path", params={"a": jane, "b": ann}).json()["paths"] == []
    assert c.get(f"/cases/{case}/analysis/common", params={"ids": f"{jane},{john}"}).json()["common"][0]["caption"] == "Acme Holdings Ltd"
    assert c.get(f"/cases/{case}/analysis/neighbourhood", params={"id": jane, "hops": 2}).json()["count"] == 3
    assert c.get(f"/cases/{case}/analysis/path", params={"a": jane, "b": str(uuid.uuid4())}).status_code == 404


# ------------------------------------------------------------------ annotations, views, exports
def test_annotations_are_workspace_notes_not_evidence_and_are_erased_with_the_case(rig):
    c, case, server, tmp, app = rig
    seed(c, case)
    cid = entity(c, case, "Acme Holdings Ltd")
    before = len(c.get(f"/cases/{case}/entities/{cid}").json().get("properties", {}) or {})
    n = c.post(f"/cases/{case}/annotations", json={"kind": "note", "body": "check against the 2019 filing", "cluster_id": cid})
    assert n.status_code == 201
    c.post(f"/cases/{case}/annotations", json={"kind": "colour", "body": "#ff0000", "cluster_id": cid})
    c.post(f"/cases/{case}/annotations", json={"kind": "colour", "body": "#00ff00", "cluster_id": cid})        # replaces
    listed = c.get(f"/cases/{case}/annotations").json()
    assert [a["kind"] for a in listed].count("colour") == 1 and all(a["is_evidence"] is False for a in listed)
    for bad in ({"kind": "colour", "body": "red"}, {"kind": "banner", "body": "x"}, {"kind": "note", "body": " "},
                {"kind": "note", "body": "x", "cluster_id": str(uuid.uuid4())}):
        assert c.post(f"/cases/{case}/annotations", json=bad).status_code in (404, 422)
    assert "2019 filing" not in json.dumps(c.get(f"/cases/{case}/audit").json())               # text stays out of the audit log
    assert c.delete(f"/cases/{case}/annotations/{n.json()['id']}").status_code == 200
    assert c.delete(f"/cases/{case}/annotations/{n.json()['id']}").status_code == 404
    c.put(f"/cases/{case}/views", json={"name": "overview", "layout": "circle", "state": {"pos": {cid: [1, 2]}}})
    c.put(f"/cases/{case}/views", json={"name": "overview", "layout": "grid", "state": {}})              # upsert by name
    assert [(v["name"], v["layout"]) for v in c.get(f"/cases/{case}/views").json()] == [("overview", "grid")]
    assert c.post(f"/cases/{case}/erase", json={"legal_basis": "test"}).status_code in (200, 201)
    with app.state.db.connection() as conn:
        left = conn.execute("SELECT (SELECT count(*) FROM graph_annotation WHERE case_id=%s) a, (SELECT count(*) FROM graph_view WHERE case_id=%s) v",
                            (case, case)).fetchone()
    assert left["a"] == 0 and left["v"] == 0


def test_exports_are_well_formed_and_safe_to_open_in_a_spreadsheet(rig):
    c, case, *_ = rig
    seed(c, case, "person,employer\n=cmd|' /C calc'!A0,<b>Acme & Sons</b>\n",
         {"schema": "Person", "key": "person", "name_column": "person", "links": [{"column": "employer", "predicate": "employer", "schema": "Company"}]})
    gx = c.get(f"/cases/{case}/export/graph", params={"format": "graphml"})
    root = ET.fromstring(gx.text)                                                    # parses: names with & and < were escaped
    ns = {"g": "http://graphml.graphdrawing.org/xmlns"}
    assert len(root.findall(".//g:node", ns)) == 2 and len(root.findall(".//g:edge", ns)) == 1
    nodes = c.get(f"/cases/{case}/export/graph", params={"format": "nodes.csv"}).text
    assert "'=cmd" in nodes and "\n=cmd" not in nodes and "\r\n=cmd" not in nodes
    assert c.get(f"/cases/{case}/export/graph", params={"format": "json"}).json()["nodes"]
    assert c.get(f"/cases/{case}/export/graph", params={"format": "pdf"}).status_code == 422
    rep = c.get(f"/cases/{case}/report.html")
    assert "&lt;b&gt;Acme &amp; Sons&lt;/b&gt;" in rep.text and "<b>Acme" not in rep.text
    assert "not evidence on its own" in rep.text and "default-src 'none'" in rep.headers["content-security-policy"]
    pdf = c.get(f"/cases/{case}/report.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF") and pdf.headers["content-type"] == "application/pdf"
    from pypdf import PdfReader
    import io

    text = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf.content)).pages)
    assert "Acme & Sons" in text and "<b>" in text.replace("&lt;b&gt;", "<b>") and "not evidence on its own" in text.replace("\n", " ")


# ------------------------------------------------------------------ machines
DEFN = {"schema": machines.SCHEMA, "machine": "company-lookup", "version": 1, "description": "registry lookup then sanctions",
        "seed": {"schemas": ["Company"]},
        "stages": [{"id": "reg", "transform": "connector.gleif", "on": ["Company"], "max_entities": 5, "config": {"base_url": "http://BASE"}}],
        "limits": {"max_calls": 5}}


def defn(server, **over):
    """A fresh machine name each time: the test database outlives a run and a name+version is immutable."""
    d = copy.deepcopy(DEFN)
    d["machine"] = f"m-{uuid.uuid4().hex[:10]}"
    d["stages"][0]["config"]["base_url"] = server.url
    d.update(over)
    return d


def test_machine_definitions_cannot_grant_themselves_permissions_or_exceed_limits():
    machines.validate(copy.deepcopy(DEFN))
    bad = {
        "self approval": {**DEFN, "stages": [{**DEFN["stages"][0], "allow_target_contact": True}]},
        "approved flag": {**DEFN, "stages": [{**DEFN["stages"][0], "approved": True}]},
        "no stages": {**DEFN, "stages": []}, "too many stages": {**DEFN, "stages": [{**DEFN["stages"][0], "id": f"s{i}"} for i in range(9)]},
        "dup id": {**DEFN, "stages": [DEFN["stages"][0], DEFN["stages"][0]]},
        "first stage from new": {**DEFN, "stages": [{**DEFN["stages"][0], "from": "new"}]},
        "call cap": {**DEFN, "limits": {"max_calls": 1000}}, "entity cap": {**DEFN, "stages": [{**DEFN["stages"][0], "max_entities": 500}]},
        "bad seed": {**DEFN, "seed": {"schemas": ["company"]}}, "nested config": {**DEFN, "stages": [{**DEFN["stages"][0], "config": {"x": {"y": 1}}}]},
    }
    for why, d in bad.items():
        with pytest.raises(machines.MachineError):
            machines.validate(copy.deepcopy(d))


def test_a_machine_proposes_then_needs_approval_then_runs_with_a_full_trace(rig):
    c, case, server, *_ = rig
    seed(c, case)
    d = defn(server)
    assert c.post("/machines", json=d).status_code == 201
    a, b = entity(c, case, "Acme Holdings Ltd"), entity(c, case, "Beta Trading SA")
    p = c.post(f"/cases/{case}/machines/{d['machine']}/propose", json={"seeds": [a, b]})
    assert p.status_code == 201 and p.json()["status"] == "proposed" and p.json()["plan"]["max_calls"] == 5
    rid = p.json()["id"]
    assert c.post(f"/cases/{case}/machine-runs/{rid}/run").status_code == 422                 # not approved: refused
    assert not server.requests
    assert c.post(f"/cases/{case}/machine-runs/{rid}/approve", json={}).status_code == 200
    assert c.post(f"/cases/{case}/machine-runs/{rid}/approve", json={}).status_code == 422    # cannot approve twice
    out = c.post(f"/cases/{case}/machine-runs/{rid}/run").json()
    assert out["status"] == "done" and out["calls"] == 2 and out["coverage_summary"]["hits"] == 2
    assert [t["input"] for t in out["trace"] if t.get("input")] == [{"name": "Acme Holdings Ltd"}, {"name": "Beta Trading SA"}]
    assert c.get(f"/cases/{case}/machine-runs/{rid}").json()["status"] == "done"
    assert c.post(f"/cases/{case}/machine-runs/{rid}/run").status_code == 422                 # runs once


def test_machine_seeds_must_match_and_limits_hold(rig):
    c, case, server, *_ = rig
    seed(c, case)
    seed(c, case, "handle\nalice\n", {"schema": "UserAccount", "key": "handle", "name_column": "handle"})
    d = defn(server, limits={"max_calls": 1})
    c.post("/machines", json=d)
    nm = d["machine"]
    a, b, alice = entity(c, case, "Acme Holdings Ltd"), entity(c, case, "Beta Trading SA"), entity(c, case, "alice")
    assert c.post(f"/cases/{case}/machines/{nm}/propose", json={"seeds": [alice]}).status_code == 422
    assert c.post(f"/cases/{case}/machines/{nm}/propose", json={"seeds": [str(uuid.uuid4())]}).status_code == 404
    assert c.post(f"/cases/{case}/machines/nope/propose", json={"seeds": [a]}).status_code == 404
    rid = c.post(f"/cases/{case}/machines/{nm}/propose", json={"seeds": [a, b]}).json()["id"]
    c.post(f"/cases/{case}/machine-runs/{rid}/approve", json={})
    out = c.post(f"/cases/{case}/machine-runs/{rid}/run").json()
    assert out["calls"] == 1 and any("call limit" in (t.get("note") or "") and t["outcome"] == "unknown" for t in out["trace"])


def test_a_machine_never_contacts_the_subject_unless_the_approval_says_so(rig):
    c, case, server, tmp, _ = rig
    seed(c, case, "handle\nalice\n", {"schema": "UserAccount", "key": "handle", "columns": {"handle": "username"}, "name_column": "handle"})
    d = tmp / "packs"
    d.mkdir()
    (d / "a.json").write_text(json.dumps({"site": "a.example", "licence": "AGPL-3.0-or-later",
        "check": {"url": server.url + "/p/@{username}", "present_when": {"status": 200}, "absent_when": {"status": 404}},
        "known": {"present": "alice"}, "fixtures": {"present": {"status": 200, "body": ""}, "absent": {"status": 404, "body": ""}}}))
    server.add("/p/@alice", "hi", ctype="text/html")
    m = {"schema": machines.SCHEMA, "machine": f"who-{uuid.uuid4().hex[:8]}", "version": 1, "seed": {"schemas": ["UserAccount"]}, "limits": {"max_calls": 3},
         "stages": [{"id": "sites", "transform": "connector.site-pack", "on": ["UserAccount"], "config": {"packs_dir": str(d), "sites": "a.example"}}]}
    m["stages"][0]["config"] = {"packs_dir": str(d)}
    c.post("/machines", json=m)
    alice = entity(c, case, "alice")
    rid = c.post(f"/cases/{case}/machines/{m['machine']}/propose", json={"seeds": [alice]}).json()["id"]
    c.post(f"/cases/{case}/machine-runs/{rid}/approve", json={"allow_target_contact": False})
    out = c.post(f"/cases/{case}/machine-runs/{rid}/run").json()
    assert out["calls"] == 0 and "contacts the subject" in out["trace"][-1]["note"] and not server.requests
    rid = c.post(f"/cases/{case}/machines/{m['machine']}/propose", json={"seeds": [alice]}).json()["id"]
    c.post(f"/cases/{case}/machine-runs/{rid}/approve", json={"allow_target_contact": True})
    out = c.post(f"/cases/{case}/machine-runs/{rid}/run").json()
    assert out["calls"] == 1 and server.requests


def test_machine_packages_are_hash_checked_and_signed_ones_verified(rig, tmp_path):
    c, case, server, *_ = rig
    priv, pub = signing.generate_keypair()
    store = TrustStore(tmp_path / "t")
    pkg = machines.export({"definition": defn(server)})
    assert machines.verify_package(pkg, store)[1]["status"] == "unsigned"
    signed = machines.export({"definition": defn(server)}, (priv, None))
    assert machines.verify_package(signed, store)[1]["status"] == "signed-untrusted"
    store.add(pub, "lab")
    assert machines.verify_package(signed, store)[1]["status"] == "trusted"
    tampered = copy.deepcopy(signed)
    tampered["definition"]["description"] = "x"
    with pytest.raises(machines.MachineError, match="altered"):
        machines.verify_package(tampered, store)
    with pytest.raises(machines.MachineError):
        machines.verify_package({**pkg, "schema": "astral.lens-package/v1"}, store)            # a lens package is not a machine
    r = c.post("/machines/import", json=pkg)
    assert r.status_code == 201 and r.json()["trust"] == "unsigned"
    assert c.post("/machines/import", json={**tampered}).status_code == 422


def test_scheduled_machines_run_passively_and_only_when_due(rig):
    c, case, server, tmp, app = rig
    seed(c, case)
    d = defn(server)
    c.post("/machines", json=d)
    a = entity(c, case, "Acme Holdings Ltd")
    assert c.post(f"/cases/{case}/machine-schedules", json={"machine": d["machine"], "seeds": [a], "interval_minutes": 1}).status_code == 422
    assert c.post(f"/cases/{case}/machine-schedules", json={"machine": "nope", "seeds": [a], "interval_minutes": 60}).status_code == 404
    sid = c.post(f"/cases/{case}/machine-schedules", json={"machine": d["machine"], "seeds": [a], "interval_minutes": 60}).json()["id"]
    from datetime import datetime, timedelta, timezone
    with app.state.db.connection() as conn:
        first = [r for r in machines.run_due(app.state.host, conn) if r["schedule"] == sid]
        again = [r for r in machines.run_due(app.state.host, conn) if r["schedule"] == sid]
        later = [r for r in machines.run_due(app.state.host, conn, datetime.now(timezone.utc) + timedelta(minutes=61)) if r["schedule"] == sid]
    assert first and first[0]["status"] == "done" and not again and later
    listed = c.get(f"/cases/{case}/machine-schedules").json()
    assert [s["id"] for s in listed] == [sid] and listed[0]["enabled"] is True and listed[0]["last_run_at"]
    assert c.post(f"/cases/{case}/machine-schedules/{sid}/disable").json() == {"enabled": False}
    with app.state.db.connection() as conn:
        gone = [r for r in machines.run_due(app.state.host, conn, datetime.now(timezone.utc) + timedelta(days=2)) if r["schedule"] == sid]
    assert not gone and c.get(f"/cases/{case}/machine-schedules").json()[0]["enabled"] is False        # disabled: kept, not run
    assert c.post(f"/cases/{case}/machine-schedules/{uuid.uuid4()}/disable").status_code == 404
    assert c.post(f"/cases/{case}/machine-schedules/{sid}/delete").status_code == 404
    assert c.post(f"/cases/{case}/machine-schedules/{sid}/enable").json() == {"enabled": True}


def test_geo_view_places_only_what_has_a_valid_position(rig):
    c, case, *_ = rig
    seed(c, case, "name,lat,lon\nTower,51.5,-0.12\nNowhere,95,0\nHalf,10,\n",
         {"schema": "Address", "key": "name", "name_column": "name", "columns": {"lat": "latitude", "lon": "longitude"}})
    g = c.get(f"/cases/{case}/geo").json()
    assert [p["caption"] for p in g["points"]] == ["Tower"] and len(g["unplaced"]) == 2 and "says nothing about where" in g["note"]


# ------------------------------------------------------------------ large graphs must not hang a request
def test_large_graphs_use_sampled_centrality_that_is_labelled_and_still_finds_the_hub():
    import time

    from astral import analysis

    n = 3000                                                              # above EXACT_LIMIT: exact Brandes here would take minutes
    nodes = [{"id": f"n{i}", "caption": f"n{i}", "schema": "Person"} for i in range(n)]
    edges = [{"id": f"e{i}", "source": "n0", "target": f"n{i}", "label": "associate", "source_grade": "C", "info_grade": 3, "independent_sources": 1}
             for i in range(1, n)]
    edges += [{"id": f"r{i}", "source": f"n{i}", "target": f"n{i + 1}", "label": "associate", "source_grade": "C", "info_grade": 3, "independent_sources": 1}
              for i in range(1, 400)]
    t = time.perf_counter()
    out = analysis.analyse({"nodes": nodes, "edges": edges})
    assert time.perf_counter() - t < 60
    assert out["approximate"] and "3000 nodes" in out["approximate"]["reason"]
    assert out["betweenness"][0]["id"] == "n0" and out["closeness"][0]["id"] == "n0" and out["degree"][0]["id"] == "n0"
    small = analysis.analyse({"nodes": nodes[:10], "edges": [e for e in edges if e["source"] == "n0" and int(e["target"][1:]) < 10]})
    assert small["approximate"] is None                                    # small graphs stay exact
    adj = {f"x{i}": [f"x{(i + 1) % 40}", f"x{(i - 1) % 40}"] for i in range(40)}                 # a ring: every node equally central
    exact = analysis.betweenness(adj)
    est = analysis.betweenness(adj, sorted(adj)[::4])
    assert max(abs(exact[v] - est[v]) for v in adj) < 0.5 * max(exact.values())                  # the estimator is the right order of magnitude
