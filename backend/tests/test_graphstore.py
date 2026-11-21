"""The Apache AGE graph projection. The validation and parsing tests always run; the rest need a real AGE server
(ASTRAL_TEST_AGE=postgresql://user:pass@host:port/db with the age extension) and are skipped otherwise.
HONEST SCOPE: one single-node test container, small graphs, no authentication tuning, no TLS."""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from astral import graphstore, services
from astral.api import create_app
from astral.config import Settings

AGE = os.environ.get("ASTRAL_TEST_AGE")
needs_age = pytest.mark.skipif(not AGE, reason="ASTRAL_TEST_AGE not set: no Apache AGE server available")
CSV = "person,employer\nJane Doe,Acme Holdings Ltd\nJohn Roe,Acme Holdings Ltd\nAnn Poe,Beta Trading SA\n"
MAP = {"schema": "Person", "key": "person", "name_column": "person", "links": [{"column": "employer", "predicate": "employer", "schema": "Company"}]}


# ------------------------------------------------------------------ always run
@pytest.mark.parametrize("q,why", [
    ("", "empty"), ("MATCH (a) RETURN a; DROP TABLE x", "';'"), ("MATCH (a) RETURN $$ ; select 1 $$", "'$'"),
    ("CREATE (:Person {id:'x'}) RETURN 1", "CREATE"), ("MATCH (a) SET a.x = 1 RETURN a", "SET"), ("MATCH (a) DETACH DELETE a RETURN 1", "DETACH"),
    ("MATCH (a) RETURN a UNION MATCH (b) RETURN b", "UNION"), ("MATCH (a)", "RETURN"), ("MATCH (a) RETURN *", "RETURN"),
    ("MATCH (a {name: 'x) RETURN a", "string"), ("MATCH (a RETURN a", "bracket"), ("x" * 2001, "longer"),
    ("MATCH (a) RETURN " + ", ".join(f"a.p{i}" for i in range(13)), "12"),
])
def test_unsafe_or_malformed_queries_are_refused_before_anything_is_run(q, why):
    with pytest.raises(graphstore.GraphQueryError, match=__import__("re").escape(why)):
        graphstore.check_query(q)


def test_the_column_count_comes_from_the_final_top_level_return():
    assert graphstore.check_query("MATCH (a:Person) RETURN a") == 1
    assert graphstore.check_query("MATCH (a)-[r]->(b) RETURN a.id, type(r), b ORDER BY a.id LIMIT 5") == 3
    assert graphstore.check_query("MATCH (a) WITH a, count(*) AS n RETURN DISTINCT a.id, n") == 2
    assert graphstore.check_query("MATCH (a) RETURN {x: 1, y: [1,2,3]}, 'a, b, c'") == 2                 # commas inside brackets and strings
    assert graphstore.check_query("MATCH (a) WHERE a.id = 'CREATE SET DELETE' RETURN a") == 1            # keywords inside a string are text


def test_agtype_values_parse_to_plain_json():
    assert graphstore.parse_agtype('{"id": 1, "label": "Person", "properties": {"id": "a1"}}::vertex')["properties"] == {"id": "a1"}
    p = graphstore.parse_agtype('[{"id": 1, "label": "P", "properties": {"id": "a"}}::vertex, {"id": 2, "label": "e", "end_id": 3, "start_id": 1, "properties": {}}::edge, '
                                '{"id": 3, "label": "C", "properties": {"id": "c"}}::vertex]::path')
    assert len(p) == 3 and graphstore.parse_agtype("3::numeric") == 3 and graphstore.parse_agtype('"x"') == "x" and graphstore.parse_agtype(None) is None


def test_the_fingerprint_changes_when_the_graph_changes():
    g = {"nodes": [{"id": "a", "schema": "Person"}], "edges": []}
    h = graphstore.fingerprint(g)
    g["edges"].append({"id": "e1", "source": "a", "target": "a", "label": "x", "source_grade": "B", "info_grade": 2, "independent_sources": 1})
    assert graphstore.fingerprint(g) != h
    g["edges"][0]["info_grade"] = 3
    assert graphstore.fingerprint(g) not in (h,) and len({graphstore.fingerprint(g), h}) == 2


def test_an_unreachable_age_server_is_a_service_error_that_does_not_leak_the_password():
    svc = graphstore.GraphService("postgresql://u:hunter2secret@127.0.0.1:9/none")
    with pytest.raises(services.ServiceError) as e:
        svc.ping()
    assert "hunter2secret" not in str(e.value)


def test_the_api_says_so_when_no_projection_is_configured(db, dsn, tmp_path):
    app = create_app(Settings(dsn=dsn, data_dir=tmp_path), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        case = c.post("/cases", json={"name": "g"}).json()["id"]
        r = c.post(f"/cases/{case}/graph/cypher", json={"query": "MATCH (a) RETURN a"})
        assert r.status_code == 400 and "ASTRAL_AGE_DSN" in r.text
        assert c.get("/services").json()["graph"] == {"configured": False, "fallback": "postgres"}


# ------------------------------------------------------------------ against a real AGE server
@pytest.fixture()
def rig(db, dsn, tmp_path):
    app = create_app(Settings(dsn=dsn, data_dir=tmp_path), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        app.state.services = services.Services(graph=graphstore.GraphService(AGE))
        case = c.post("/cases", json={"name": "age"}).json()["id"]
        r = c.post(f"/cases/{case}/import/table", json={"csv_text": CSV, "mapping": MAP})
        assert r.status_code == 201, r.text
        yield c, case, app
        app.state.services.graph.delete_case(uuid.UUID(case))


def cy(c, case, q):
    r = c.post(f"/cases/{case}/graph/cypher", json={"query": q})
    assert r.status_code == 200, r.text
    return r.json()


@needs_age
def test_cypher_answers_come_from_a_rebuilt_projection_and_are_hydrated_from_postgres(rig):
    c, case, app = rig
    r = cy(c, case, "MATCH (p:Person)-[e:employer]->(co:Company) RETURN p, co, e")
    assert r["projection"]["rebuilt_for_this_query"] is True and len(r["rows"]) == 3
    names = sorted((row[0]["caption"], row[1]["caption"]) for row in r["rows"])
    assert names == [("Ann Poe", "Beta Trading SA"), ("Jane Doe", "Acme Holdings Ltd"), ("John Roe", "Acme Holdings Ltd")]
    e = r["rows"][0][2]
    assert e["kind"] == "edge" and e["label"] == "employer" and e["source_grade"] in "ABCDEF" and isinstance(e["info_grade"], int)
    again = cy(c, case, "MATCH (co:Company)<-[:employer]-(p) RETURN co.id, count(p) ORDER BY count(p) DESC")
    assert again["projection"]["rebuilt_for_this_query"] is False and again["rows"][0][1] == 2
    assert "recorded links" in again["note"]


@needs_age
def test_paths_come_back_as_nodes_and_edges_and_the_graph_follows_a_change_in_postgres(rig):
    c, case, app = rig
    p = cy(c, case, "MATCH path=(a:Person {id: '%s'})-[*1..2]-(b:Person) RETURN path" % next(
        e["cluster_id"] for e in c.get(f"/cases/{case}/entities").json() if e["caption"] == "Jane Doe"))
    el = p["rows"][0][0]
    assert el["kind"] == "path" and [x["kind"] for x in el["elements"]] == ["node", "edge", "node", "edge", "node"] and el["elements"][-1]["caption"] == "John Roe"
    assert c.post(f"/cases/{case}/import/table", json={"csv_text": "person,employer\nKay Lee,Beta Trading SA\n", "mapping": MAP}).status_code == 201
    r = cy(c, case, "MATCH (p:Person) RETURN p.id")
    assert len(r["rows"]) == 4 and r["projection"]["rebuilt_for_this_query"] is True


@needs_age
def test_writes_are_refused_by_the_checker_and_by_the_database_itself(rig):
    c, case, app = rig
    cy(c, case, "MATCH (a) RETURN a")
    assert c.post(f"/cases/{case}/graph/cypher", json={"query": "CREATE (:Person {id: 'x'})"}).status_code == 400
    # bypass the checker: the READ ONLY transaction must still refuse a write
    svc = app.state.services.graph
    name = graphstore.graph_name(uuid.UUID(case))
    with pytest.raises(graphstore.GraphQueryError, match="read-only"):
        import psycopg

        with svc._connect() as conn:
            conn.autocommit = False
            conn.execute("SET TRANSACTION READ ONLY")
            try:
                conn.execute(f"SELECT * FROM ag_catalog.cypher('{name}', $astral$ CREATE (:Person {{id: 'zz'}}) $astral$) AS (v ag_catalog.agtype)")
            except psycopg.Error as e:
                raise graphstore.GraphQueryError(str(e)) from e
    assert len(cy(c, case, "MATCH (p:Person) RETURN p.id")["rows"]) == 3                              # nothing was written


@needs_age
def test_the_row_cap_and_the_timeout_bound_a_query_and_a_syntax_error_is_reported_not_raised(rig):
    c, case, app = rig
    app.state.services.graph.max_rows = 2
    r = cy(c, case, "MATCH (p:Person) RETURN p.id")
    assert len(r["rows"]) == 2 and r["truncated"] is True
    bad = c.post(f"/cases/{case}/graph/cypher", json={"query": "MATCH (p:Person RETURN p"})
    assert bad.status_code == 400
    bad = c.post(f"/cases/{case}/graph/cypher", json={"query": "MATCH (p:Person) WHERE p.id = RETURN p"})
    assert bad.status_code == 400 and "syntax" in bad.text.lower()


@needs_age
def test_one_case_cannot_see_anothers_graph_and_erasure_removes_the_projection(rig, db):
    c, case, app = rig
    other = c.post("/cases", json={"name": "other"}).json()["id"]
    assert c.post(f"/cases/{other}/import/table", json={"csv_text": "person,employer\nZed,Zeta Co\n", "mapping": MAP}).status_code == 201
    mine = {row[0] for row in cy(c, case, "MATCH (p:Person) RETURN p.id")["rows"]}
    theirs = {row[0] for row in cy(c, other, "MATCH (p:Person) RETURN p.id")["rows"]}
    assert len(mine) == 3 and len(theirs) == 1 and not mine & theirs
    svc = app.state.services.graph
    assert svc.has(uuid.UUID(case)) and svc.has(uuid.UUID(other))
    assert c.post(f"/cases/{other}/erase", json={"legal_basis": "request"}).status_code == 200
    assert not svc.has(uuid.UUID(other)) and svc.has(uuid.UUID(case))
    assert c.get("/services").json()["graph"]["reachable"] is True
    assert c.post(f"/cases/{case}/services/sync").json()["graph"]["rebuilt"] is False


@needs_age
def test_a_stale_vertex_is_marked_not_shown_as_current(rig, db):
    c, case, app = rig
    cy(c, case, "MATCH (p:Person) RETURN p.id")                                           # project now
    gone = uuid.uuid4()
    svc = app.state.services.graph
    # the projection holds a vertex Postgres does not: hydrate must flag it rather than present a name
    res = {"columns": 1, "rows": [[{"id": 1, "label": "Person", "properties": {"id": str(gone)}}]], "truncated": False}
    with db.connection() as conn:
        out = graphstore.hydrate(conn, uuid.UUID(case), res)
    assert out["rows"][0][0] == {"kind": "node", "id": str(gone), "schema": "Person", "caption": None, "stale": True} and out["stale_vertices"] == 1
    assert svc is not None
