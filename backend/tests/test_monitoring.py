"""M5: monitors (saved searches with diff views) and watch-term cross-referencing.

The connector is a tiny test plugin against a local server whose answers the test changes between
runs. That proves the diff rules (new, no longer reported, not asked, recovered) and the watch
cross-reference; it says nothing about how real registries change over time, and scheduled
execution is tested at the "which monitors are due" level, not by waiting on a clock."""

from __future__ import annotations

import json
import textwrap
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from astral import monitoring
from astral.api import create_app
from astral.config import Settings

from conftest import REPO

MAIN = textwrap.dedent('''
    from astral import sdk

    def invoke(request):
        inp, cfg = request["input"], request["config"]
        ns = request["context"]["entity_namespace"]
        try:
            res, body = yield from sdk.fetch_json(cfg["base_url"] + "/records", params={"name": inp["name"]})
        except sdk.CapabilityFailed as e:
            yield sdk.result(coverage=[sdk.coverage("Test registry", unknown=str(e))])
            return
        out = []
        for rec in body["records"]:
            eid = sdk.entity_id(ns, "co", rec["id"])
            ev = [res["sha256"]]
            out += [sdk.a_value(eid, "schema", "Company", ev, 2), sdk.a_value(eid, "name", rec["name"], ev, 2)]
            for d in rec.get("directors", []):
                out.append(sdk.a_value(eid, "director", d, ev, 2))
            if rec.get("iban"):
                out.append(sdk.a_value(eid, "iban", rec["iban"], ev, 2))
        yield sdk.emit(out)
        yield sdk.result(stats={"records": len(body["records"])}, coverage=[sdk.coverage("Test registry", hits=len(body["records"]))])

    def selfcheck():
        return {"ok": True}
''')


def acme(directors=("Jane Doe",), iban=None):
    return {"records": [{"id": "A1", "name": "Acme Holdings Ltd", "directors": list(directors), "iban": iban}]}


@pytest.fixture()
def env(db, dsn, tmp_path, server):
    state = {"status": 200, "body": acme()}
    server.add_fn("/records", lambda req: (state["status"], "application/json", state["body"], {}))
    d = tmp_path / "registry"
    d.mkdir()
    (d / "main.py").write_text(MAIN)
    (d / "plugin.yaml").write_text(textwrap.dedent(f'''\
        api_version: "astral.plugin/v1"
        id: "test.registry"
        version: "1.0.0"
        title: "Test registry"
        stage: ingest
        direction: pull
        footprint: third_party
        consumes:
          - type: Query
            required: [name]
        produces: [Company]
        capabilities: [http.fetch]
        config:
          type: object
          properties:
            base_url: {{ type: string, default: "{server.url}" }}
        runtimes: [in_process]
        entrypoint: "main.py:invoke"
        health: {{ check: "main.py:selfcheck" }}
        '''))
    app = create_app(Settings(dsn=dsn, data_dir=tmp_path / "data"), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        r = c.post("/plugins", json={"path": str(d)})
        assert r.status_code == 201, r.text
        for p in c.get("/plugins").json():                  # the test database is shared: only this registry may answer
            if p["state"] == "enabled" and p["id"] != "test.registry":
                c.post(f"/plugins/{p['id']}/{p['version']}/disabled")
        c.post("/plugins/test.registry/1.0.0/enabled")
        case = c.post("/cases", json={"name": "watching"}).json()["id"]
        mid = c.post(f"/cases/{case}/monitors", json={"name": "Acme", "query": {"name": "Acme Holdings Ltd"}, "interval_minutes": 60}).json()["id"]
        yield {"c": c, "case": case, "mid": mid, "state": state, "app": app,
               "run": lambda: c.post(f"/cases/{case}/monitors/{mid}/run").json()}


def labels(items):
    return sorted(i["label"] for i in items)


def test_first_run_reports_everything_as_new_and_says_so(env):
    r = env["run"]()
    assert r["first_run"] and r["coverage"] == {"test.registry": "answered"}
    assert any("director: Jane Doe" in x for x in labels(r["diff"]["new_facts"]))


def test_rerunning_unchanged_data_is_not_news(env):
    env["run"]()
    r = env["run"]()
    assert not r["first_run"] and r["diff"]["new_facts"] == [] and r["diff"]["no_longer_reported"] == []


def test_changes_show_as_new_and_no_longer_reported(env):
    env["run"]()
    env["state"]["body"] = acme(directors=("Bob Ray",), iban="GB82WEST12345698765432")
    d = env["run"]()["diff"]
    assert any("director: Bob Ray" in x for x in labels(d["new_facts"])) and any("iban" in x for x in labels(d["new_facts"]))
    assert any("director: Jane Doe" in x for x in labels(d["no_longer_reported"]))
    assert "nothing was deleted" in d["note"]


def test_a_source_that_could_not_be_asked_is_not_asked_never_gone_and_recovery_compares_with_the_last_answer(env):
    env["run"]()
    env["state"]["status"], env["state"]["body"] = 503, {"error": "down"}
    down = env["run"]()
    assert down["coverage"] == {"test.registry": "not_asked"}
    assert down["diff"]["no_longer_reported"] == [] and down["diff"]["not_asked_this_time"] == ["test.registry"]
    assert down["diff"]["coverage_changes"] == [{"plugin": "test.registry", "was": "answered", "now": "not_asked"}]
    # it comes back with different data: compared with the run BEFORE the outage, not with the empty outage run
    env["state"]["status"], env["state"]["body"] = 200, acme(directors=("Bob Ray",))
    back = env["run"]()["diff"]
    assert any("Bob Ray" in x for x in labels(back["new_facts"]))
    assert any("Jane Doe" in x for x in labels(back["no_longer_reported"]))
    assert not any("Acme Holdings Ltd" in x and "name:" in x for x in labels(back["new_facts"]))   # unchanged facts are not re-announced


def test_the_record_is_never_rewritten_by_a_diff(env, db):
    env["run"]()
    env["state"]["body"] = acme(directors=("Bob Ray",))
    env["run"]()
    with db.connection() as conn:
        n = conn.execute("SELECT count(*) AS n FROM assertion WHERE case_id=%s AND predicate='director'"
                         " AND object_value = '\"Jane Doe\"'::jsonb", (env["case"],)).fetchone()["n"]
        assert n >= 1                                                       # the earlier assertion is still there
        import psycopg

        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
            conn.execute("UPDATE monitor_run SET diff='{}'::jsonb WHERE case_id=%s", (env["case"],))


def mine(env, conn, at):
    """Monitors due at `at` in THIS test's case (the test database is shared)."""
    return [m for m in monitoring.due(conn, at) if str(m["case_id"]) == env["case"]]


def test_listing_shows_the_last_run_and_pausing_stops_it_being_due(env, db):
    env["run"]()
    listed = env["c"].get(f"/cases/{env['case']}/monitors").json()[0]
    assert listed["last_run"]["coverage"] == {"test.registry": "answered"}
    with db.connection() as conn:
        assert mine(env, conn, datetime.now(timezone.utc)) == []                        # just ran, interval 60 minutes
        assert mine(env, conn, datetime.now(timezone.utc) + timedelta(minutes=61))
    env["c"].put(f"/cases/{env['case']}/monitors/{env['mid']}", json={"active": False})
    with db.connection() as conn:
        assert mine(env, conn, datetime.now(timezone.utc) + timedelta(hours=5)) == []


def test_a_monitor_that_never_ran_is_due_and_a_manual_one_never_is(env, db):
    c, case = env["c"], env["case"]
    manual = c.post(f"/cases/{case}/monitors", json={"name": "manual", "query": {"name": "Zed"}}).json()["id"]
    with db.connection() as conn:
        due = {str(m["id"]) for m in mine(env, conn, datetime.now(timezone.utc))}
    assert env["mid"] in due and manual not in due


def test_the_searched_name_is_not_copied_into_the_audit_log(env, db):
    env["run"]()
    with db.connection() as conn:
        rows = conn.execute("SELECT action, detail FROM audit_log WHERE case_id=%s AND action LIKE 'monitor.%%'", (env["case"],)).fetchall()
    assert {r["action"] for r in rows} == {"monitor.create", "monitor.run"} and "Acme" not in json.dumps(rows, default=str)


def test_validation(env):
    c, case = env["c"], env["case"]
    assert c.post(f"/cases/{case}/monitors", json={"name": "x", "query": {"country": "GB"}}).status_code == 422
    assert c.post(f"/cases/{case}/monitors", json={"name": "x", "query": {"name": "A"}, "interval_minutes": 1}).status_code == 422
    assert c.post(f"/cases/{case}/monitors/00000000-0000-0000-0000-000000000000/run").status_code == 404


# ------------------------------------------------------------------ watch terms
def test_watch_terms_cross_reference_entities_and_document_text_with_anchors(env):
    c, case = env["c"], env["case"]
    env["state"]["body"] = acme(iban="GB82WEST12345698765432")
    env["run"]()
    for n in ("doc-ingest", "ner"):
        r = c.post("/plugins", json={"path": str(REPO / "plugins" / "core" / n)}).json()
        c.post(f"/plugins/{r['id']}/{r['version']}/enabled")
    text = "Memo: the Acme Holdings Ltd account at IBAN GB82WEST12345698765432 was reviewed by Jane Doe on Friday."
    sha = c.post(f"/cases/{case}/artifacts", content=text.encode(), headers={"content-type": "text/plain"}).json()["sha256"]
    c.post(f"/cases/{case}/artifacts/{sha}/ingest")
    assert c.post(f"/cases/{case}/watch-terms", json={"kind": "name", "value": "Acme Holdings"}).status_code == 201
    assert c.post(f"/cases/{case}/watch-terms", json={"kind": "identifier", "value": "GB82 WEST 1234 5698 7654 32"}).status_code == 201
    assert c.post(f"/cases/{case}/watch-terms", json={"kind": "name", "value": "Jane Doe"}).status_code == 201
    assert c.post(f"/cases/{case}/watch-terms", json={"kind": "name", "value": "jane doe"}).status_code == 422      # already watched
    m = c.get(f"/cases/{case}/watch/matches").json()
    assert "candidates" in m["note"]
    by = lambda term, kind: [x for x in m["matches"] if x["term"] == term and x["kind"] == kind]       # noqa: E731
    assert by("Acme Holdings", "entity")[0]["similarity"] >= 90
    assert by("GB82 WEST 1234 5698 7654 32", "entity")[0]["via"] == "iban"                              # exact, spacing ignored
    jt = by("Jane Doe", "text")[0]                                                                       # in the memo
    stored = c.get(f"/cases/{case}/artifacts/{jt['text_artifact']}").text
    assert stored[jt["start"]:jt["end"]] == "Jane Doe"                                                   # a real, checkable anchor
    assert all(x["caveat"] for x in m["matches"])


def test_a_monitor_run_reports_watch_matches_that_are_new_since_the_last_run(env):
    c, case = env["c"], env["case"]
    env["run"]()
    c.post(f"/cases/{case}/watch-terms", json={"kind": "name", "value": "Acme Holdings"})
    first = env["run"]()["diff"]["new_watch_matches"]
    second = env["run"]()["diff"]["new_watch_matches"]
    assert first and second == []                                           # announced once, not on every run


def test_removing_a_term_and_the_audit_trail(env, db):
    c, case = env["c"], env["case"]
    tid = c.post(f"/cases/{case}/watch-terms", json={"kind": "name", "value": "Secret Person"}).json()["id"]
    assert c.get(f"/cases/{case}/watch-terms").json()[0]["value"] == "Secret Person"
    assert c.delete(f"/cases/{case}/watch-terms/{tid}").json() == {"removed": 1}
    with db.connection() as conn:
        rows = conn.execute("SELECT action, detail FROM audit_log WHERE case_id=%s AND action LIKE 'watch.%%'", (case,)).fetchall()
    assert {r["action"] for r in rows} == {"watch.add", "watch.remove"} and "Secret" not in json.dumps(rows, default=str)


def test_a_connector_missing_required_configuration_is_not_asked_and_does_not_break_the_run(env):
    """Regression: an enabled plugin whose required config (SEC EDGAR needs a contact) was never
    supplied used to raise out of the whole run. It is a source that could not be asked: unknown."""
    c = env["c"]
    r = c.post("/plugins", json={"path": str(REPO / "plugins" / "connectors" / "sec-edgar")})
    assert r.status_code == 201, r.text
    c.post(f"/plugins/{r.json()['id']}/{r.json()['version']}/enabled")
    run = env["run"]()
    assert run["coverage"]["test.registry"] == "answered" and run["coverage"]["connector.sec-edgar"] == "not_asked"
