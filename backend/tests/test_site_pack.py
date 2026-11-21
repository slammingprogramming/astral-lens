"""M6: the site-pack interpreter. HONEST SCOPE: the sites are a local HTTP server; this proves the
interpreter's decisions (present / absent / unknown), the control probe, licence handling and the
self-check. It does NOT show that any bundled pack still matches the live site: they ship with
verified_live=null."""

from __future__ import annotations

import importlib.util
import json

import pytest
from fastapi.testclient import TestClient

from astral import cases
from astral.api import create_app
from astral.config import Settings

from conftest import REPO

DIR = REPO / "plugins" / "connectors" / "site-pack"
spec = importlib.util.spec_from_file_location("site_pack_main", DIR / "main.py")
sp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sp)


def pack(base="http://x.invalid", **over):
    p = {"site": "example.social", "licence": "CC-BY-SA-4.0", "attribution": "third party",
         "check": {"url": base + "/@{username}", "present_when": {"status": 200, "body_absent": "no such user"},
                   "absent_when": {"status": 404}},
         "known": {"present": "alice", "absent": "zz9"},
         "fixtures": {"present": {"status": 200, "body": "<h1>hi</h1>"}, "absent": {"status": 404, "body": ""}}}
    p.update(over)
    return p


# ----------------------------------------------------------------- the classifier
def test_a_pack_can_only_say_absent_when_its_own_absent_rule_matches():
    p = sp.validate(pack())
    assert sp.classify(p, 200, "welcome", "u", truncated=False)[0] == "present"
    assert sp.classify(p, 404, "", "u", truncated=False)[0] == "absent"
    # a soft-404: HTTP 200 whose body says the user does not exist matches neither rule -> unknown, not present
    assert sp.classify(p, 200, "no such user", "u", truncated=False)[0] == "unknown"
    assert sp.classify(p, 302, "", "u", truncated=False)[0] == "unknown"           # the site changed shape
    noabs = sp.validate(pack(check={**pack()["check"], "absent_when": None}))
    v, why = sp.classify(noabs, 404, "", "u", truncated=False)
    assert v == "unknown" and "cannot say absent" in why                             # no rule, no absence claim


def test_rate_limits_blocks_and_truncated_bodies_are_unknown_never_absent_or_present():
    p = sp.validate(pack())
    for st in (403, 429, 500, 503):
        assert sp.classify(p, st, "", "u", truncated=False)[0] == "unknown"
    assert sp.classify(p, 200, "x" * 10, "u", truncated=True)[0] == "unknown"        # body_absent over a cut body
    assert sp.classify(p, 200, None, "u", truncated=False)[0] == "unknown"           # non-text body
    both = pack(check={"url": "http://x.invalid/{username}", "present_when": {"status": [200, 404]}, "absent_when": {"status": 404}})
    assert "ambiguous" in sp.classify(both, 404, "", "u", truncated=False)[1]
    with pytest.raises(sp.PackError, match="ambiguous"):                       # and such a pack cannot even load
        sp.validate(both)


def test_invalid_packs_are_rejected_for_the_right_reason():
    bad = {
        "no licence": pack(licence=""), "no placeholder": pack(check={"url": "http://x.invalid/", "present_when": {"status": 200}}),
        "reads 429 as an answer": pack(check={**pack()["check"], "absent_when": {"status": 429}}),
        "reads 503 as present": pack(check={**pack()["check"], "present_when": {"status": [200, 503]}}),
        "unknown key": pack(check={**pack()["check"], "present_when": {"status": 200, "css": "h1"}}),
        "no fixtures": pack(fixtures={}), "bad extract": pack(extract={"password": {"regex": "(.*)"}}),
        "fixtures disagree": pack(fixtures={"present": {"status": 404, "body": ""}, "absent": {"status": 404, "body": ""}}),
        "head with body rule": pack(check={**pack()["check"], "method": "HEAD"}),
        "file scheme": pack(check={**pack()["check"], "url": "file:///{username}"}),
    }
    for why, p in bad.items():
        with pytest.raises(sp.PackError):
            sp.validate(p)


def test_the_loader_names_every_pack_it_refuses(tmp_path):
    (tmp_path / "a_good.json").write_text(json.dumps(pack()))
    (tmp_path / "unlicensed.json").write_text(json.dumps(pack(site="other.example", licence="")))
    (tmp_path / "broken.json").write_text("{not json")
    (tmp_path / "z_dupe.json").write_text(json.dumps(pack()))
    packs, bad = sp.load([tmp_path])
    assert [p["site"] for p in packs] == ["example.social"] and {n.split("\\")[-1].split("/")[-1] for n, _ in bad} == \
        {"unlicensed.json", "broken.json", "z_dupe.json"}


def test_the_bundled_packs_are_consistent_and_selfcheck_says_what_it_cannot_see():
    out = sp.selfcheck()
    assert out["ok"] and "offline" in out["detail"]
    packs, bad = sp.load([sp.BUNDLED])
    assert not bad and {p["site"] for p in packs} >= {"github.com", "gitlab.com"}
    assert all(p["licence"] == "AGPL-3.0-or-later" and p["verified_live"] is None for p in packs)   # nothing claims live verification


# ----------------------------------------------------------------- through the platform
@pytest.fixture()
def rig(db, dsn, tmp_path, server, actor):
    app = create_app(Settings(dsn=dsn, data_dir=tmp_path / "data"), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        r = c.post("/plugins", json={"path": str(DIR)})
        assert r.status_code == 201, r.text
        c.post(f"/plugins/{r.json()['id']}/{r.json()['version']}/enabled")
        case = c.post("/cases", json={"name": "site-pack"}).json()["id"]
        with db.connection() as conn:
            cases.get_or_create_source(conn, actor, kind="plugin", name="connector.site-pack", origin_key="site-pack",
                                       default_source_grade="E")
        yield app, c, case, db, actor, server, tmp_path


def run(rig, username, packs, **cfg):
    app, c, case, db, actor, server, tmp = rig
    d = tmp / "packs"
    d.mkdir(exist_ok=True)
    for f in d.glob("*.json"):
        f.unlink()
    for name, p in packs.items():
        (d / f"{name}.json").write_text(json.dumps(p))
    import uuid
    with db.connection() as conn:
        return app.state.host.invoke(conn, actor, uuid.UUID(case), "connector.site-pack", {"username": username},
                                     config={"packs_dir": str(d), "sites": [p["site"] for p in packs.values()], **cfg}, approved=True)


def test_present_absent_and_unknown_are_each_reported_as_what_they_are(rig):
    server = rig[5]
    server.add("/p/@alice", "<h1>Alice</h1>", ctype="text/html")
    server.add("/p/@bob", "gone", status=404, ctype="text/html")
    server.add("/q/@alice", "<h1>Alice</h1>", ctype="text/html")                  # control account exists
    server.add("/q/@bob", "slow down", status=429, ctype="text/html", headers={"Retry-After": "0"})
    server.add("/r/@bob", "weird", status=302, ctype="text/html")
    base = server.url
    sites = {"a": pack(site="a.example", check={"url": base + "/p/@{username}", "present_when": {"status": 200}, "absent_when": {"status": 404}},
                       licence="AGPL-3.0-or-later"),
             "b": pack(site="b.example", check={"url": base + "/q/@{username}", "present_when": {"status": 200}, "absent_when": {"status": 404}},
                       licence="CC-BY-SA-4.0"),
             "c": pack(site="c.example", check={"url": base + "/r/@{username}", "present_when": {"status": 200}, "absent_when": {"status": 404}},
                       licence="CC0-1.0")}
    # alice exists on a and b. c has no such account (404) but ALSO no control account, so its 404
    # cannot be trusted: the pack's own control fails and the answer is unknown, not "absent"
    res = run(rig, "alice", {k: {**v, "known": {"present": "alice"}} for k, v in sites.items()})
    cov = {c["source"]: c for c in res.coverage}
    assert cov["a.example"]["outcome"] == "hits" and cov["b.example"]["outcome"] == "hits" and res.stats["present"] == 2
    assert cov["c.example"]["outcome"] == "unknown" and "control check failed" in cov["c.example"]["note"]
    # bob: absent on a (control alice passes), rate-limited on b (unknown), 302 on c (unknown)
    res = run(rig, "bob", {k: {**v, "known": {"present": "alice"}} for k, v in sites.items()})
    cov = {c["source"]: c for c in res.coverage}
    assert cov["a.example"]["outcome"] == "no_hits" and "says nothing about other sites" in cov["a.example"]["note"]
    assert cov["b.example"]["outcome"] == "unknown" and "rate-limited" in cov["b.example"]["note"]
    assert cov["c.example"]["outcome"] == "unknown" and "may have changed" in cov["c.example"]["note"]
    assert res.stats["pack_licences"] == {"a.example": "AGPL-3.0-or-later", "b.example": "CC-BY-SA-4.0", "c.example": "CC0-1.0"}


def test_a_negative_is_withheld_when_the_control_account_no_longer_resolves(rig):
    server = rig[5]
    server.add("/p/@bob", "gone", status=404, ctype="text/html")                     # site now 404s for everyone, including the control
    p = pack(site="a.example", check={"url": server.url + "/p/@{username}", "present_when": {"status": 200}, "absent_when": {"status": 404}})
    res = run(rig, "bob", {"a": p})
    cov = res.coverage[0]
    assert cov["outcome"] == "unknown" and "control check failed" in cov["note"]
    res = run(rig, "bob", {"a": p}, control_probe=False)                             # operator can switch the probe off, knowingly
    assert res.coverage[0]["outcome"] == "no_hits"
    nocontrol = {**p, "known": {}}
    assert "no known-present control" in run(rig, "bob", {"a": nocontrol}).coverage[0]["note"]


def test_a_found_account_becomes_assertions_with_the_response_as_evidence_and_doubtful_profile_text(rig):
    app, c, case, db, actor, server, tmp = rig
    server.add("/p/@alice", "<title>x</title><h1 class='n'>Alice &amp; Co</h1>", ctype="text/html")
    p = pack(site="a.example", check={"url": server.url + "/p/@{username}", "present_when": {"status": 200}, "absent_when": {"status": 404}},
             extract={"name": {"regex": "<h1 class='n'>(.*?)</h1>"}})
    res = run(rig, "alice", {"a": p})
    assert res.status == "ok" and len(res.assertion_ids) == 5
    c.post(f"/cases/{case}/rebuild")
    ents = c.get(f"/cases/{case}/entities").json()
    acct = next(e for e in ents if e["schema_name"] == "UserAccount")
    detail = c.get(f"/cases/{case}/entities/{acct['cluster_id']}").json()
    flat = json.dumps(detail)
    assert "Alice & Co" in flat and "a.example" in flat


def test_bad_input_and_odd_usernames_never_reach_the_network(rig):
    server = rig[5]
    p = pack(site="a.example", check={"url": server.url + "/p/@{username}", "present_when": {"status": 200}},
             username_regex="^[a-z]{3,5}$")
    before = len(server.requests)
    res = run(rig, "ALICE", {"a": p})                                                # valid generally, invalid for this site
    assert res.coverage[0]["outcome"] == "unknown" and "not applicable" in res.coverage[0]["note"] and len(server.requests) == before
    res = run(rig, "a/../b?x=1", {"a": p})
    assert res.status == "error" and len(server.requests) == before


def test_max_sites_is_reported_as_not_asked_rather_than_silently_truncated(rig):
    server = rig[5]
    server.add("/p/", "x", ctype="text/html")
    packs = {f"s{i}": pack(site=f"s{i}.example", check={"url": server.url + f"/p/{i}/{{username}}", "present_when": {"status": 200}})
             for i in range(4)}
    res = run(rig, "alice", packs, max_sites=2)
    assert any(c["source"] == "site-pack limit" and c["outcome"] == "unknown" and "2 more" in c["note"] for c in res.coverage)
