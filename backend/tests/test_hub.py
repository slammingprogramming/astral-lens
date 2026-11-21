"""The plugin registry and community repositories. HONEST SCOPE: repositories are directories (and one
local HTTP server) built by the code under test. This proves the verification chain and the hostile-archive
refusals; no public repository exists, and a signature never shows that a package is safe."""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from astral import hub, lens, machines, signing
from astral.api import create_app
from astral.config import Settings
from astral.plugins.trust import TrustStore

from conftest import REPO

# The shared test database already knows (and other tests have enabled) the real wayback-cdx plugin, so
# these tests install a renamed copy: otherwise "installed disabled" would depend on test order.
_WB = Path(tempfile.mkdtemp(prefix="hubtest-")) / "hubtest-wayback"
shutil.copytree(REPO / "plugins" / "connectors" / "wayback-cdx", _WB)
_y = (_WB / "plugin.yaml").read_text(encoding="utf-8")
(_WB / "plugin.yaml").write_text(_y.replace('id: "connector.wayback-cdx"', 'id: "connector.hubtest-wayback"', 1), encoding="utf-8")

CONN = REPO / "plugins" / "connectors"
PACK = {"site": "example.test", "licence": "CC-BY-SA-4.0", "attribution": "third party", "pack_version": 2,
        "check": {"url": "http://x.invalid/{username}", "present_when": {"status": 200}, "absent_when": {"status": 404}},
        "known": {"present": "a"}, "fixtures": {"present": {"status": 200, "body": ""}, "absent": {"status": 404, "body": ""}}}


@pytest.fixture()
def repo_dir(tmp_path):
    priv, pub = signing.generate_keypair()
    pack = tmp_path / "pack.json"
    pack.write_text(json.dumps(PACK))
    d = {"schema": lens.SCHEMA, "lens": "company-pay", "version": 1, "description": "d", "inputs": ["company"],
         "question": "Which IBAN receives payments for {company}?", "steps": [{"id": "s1", "type": "search_text", "args": {"query": "{company}"}}]}
    lens_pkg = tmp_path / "lens.json"
    lens_pkg.write_text(json.dumps(lens.export({"definition": d})))
    out = tmp_path / "repo"
    hub.build(out, plugins=[CONN / "gleif", _WB], packs=[pack], packages=[lens_pkg], name="test", signing_key=(priv, None))
    return out, priv, pub


def test_a_built_repository_verifies_and_searches(repo_dir):
    out, priv, pub = repo_dir
    repo = hub.Repository(str(out))
    assert {(e["kind"], e["id"]) for e in repo.index["packages"]} == {("plugin", "connector.gleif"), ("plugin", "connector.hubtest-wayback"),
                                                                      ("site-pack", "example.test"), ("lens", "company-pay")}
    assert [e["id"] for e in repo.search("hubtest")] == ["connector.hubtest-wayback"] and [e["id"] for e in repo.search(kind="lens")] == ["company-pay"]
    store = TrustStore(out.parent / "trust")
    for e in repo.index["packages"]:
        repo.fetch(e)
        assert hub.verdict(e, store)["status"] == "signed-untrusted"
    store.add(pub, "pub")
    assert hub.verdict(repo.index["packages"][0], store)["status"] == "trusted"


def test_builds_are_reproducible(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    hub.build(a, plugins=[CONN / "gleif"])
    hub.build(b, plugins=[CONN / "gleif"])
    assert json.loads((a / "index.json").read_text())["packages"][0]["sha256"] == json.loads((b / "index.json").read_text())["packages"][0]["sha256"]


def test_tampering_with_the_file_the_index_or_the_signature_is_caught(repo_dir):
    out, priv, pub = repo_dir
    idx = json.loads((out / "index.json").read_text())
    e = next(x for x in idx["packages"] if x["id"] == "connector.gleif")
    (out / e["path"]).write_bytes((out / e["path"]).read_bytes() + b"x")
    with pytest.raises(hub.HubError, match="does not match the size/hash"):
        hub.Repository(str(out)).fetch(e)
    forged = json.loads(json.dumps(e))
    forged["sha256"] = "0" * 64                                                    # an attacker who rewrites the hash cannot re-sign it
    with pytest.raises(hub.HubError, match="does not verify"):
        hub.verdict(forged, None)
    other_priv, other_pub = signing.generate_keypair()
    swapped = json.loads(json.dumps(e))
    swapped["signature"]["public_key"] = other_pub.decode()
    with pytest.raises(hub.HubError, match="does not verify"):
        hub.verdict(swapped, None)
    assert hub.verdict({**e, "signature": None}, None)["status"] == "unsigned"


def zip_of(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members:
            z.writestr(name, data)
    return buf.getvalue()


def test_hostile_archives_are_refused_before_anything_is_written(tmp_path):
    cases = {
        "parent traversal": [("../evil.py", b"x")], "absolute": [("/etc/passwd", b"x")], "drive": [("C:/x.py", b"x")], "backslash": [("..\\evil.py", b"x")],
        "empty": [], "too many": [(f"f{i}.txt", b"x") for i in range(hub.MAX_FILES + 1)], "huge file": [("big.bin", b"0" * (hub.MAX_FILE + 1))],
        "duplicate": [("A.txt", b"1"), ("a.txt", b"2")],
    }
    for why, members in cases.items():
        dest = tmp_path / why.replace(" ", "_")
        dest.mkdir()
        with pytest.raises(hub.HubError):
            hub.safe_extract(zip_of(members), dest)
        assert list(dest.rglob("*")) == [] or why == "huge file"                  # nothing outside (or inside) was written for the early refusals
    link = io.BytesIO()
    with zipfile.ZipFile(link, "w") as z:
        i = zipfile.ZipInfo("link")
        i.external_attr = (0o120777 << 16)                                          # a symlink entry
        z.writestr(i, "/etc/passwd")
    with pytest.raises(hub.HubError, match="regular file"):
        hub.safe_extract(link.getvalue(), tmp_path / "ln")
    with pytest.raises(hub.HubError, match="not a valid zip"):
        hub.safe_extract(b"not a zip", tmp_path / "bad")
    assert not (tmp_path / "evil.py").exists()


@pytest.fixture()
def rig(db, dsn, tmp_path, repo_dir):
    out, priv, pub = repo_dir
    app = create_app(Settings(dsn=dsn, data_dir=tmp_path / "data", repositories=(str(out),)), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        yield c, app, out, tmp_path / "data", pub


def test_installing_a_plugin_goes_through_review_and_leaves_it_disabled(rig):
    c, app, out, data, pub = rig
    st = c.get("/hub").json()
    assert st["repositories"][0]["packages"] == 4 and st["egress"] == "local-only"
    r = c.post("/hub/install", json={"repository": str(out), "kind": "plugin", "id": "connector.hubtest-wayback"})
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["state"] == "disabled" and j["publisher"]["status"] == "signed-untrusted" and (data / "plugins" / "connector.hubtest-wayback" / "1.0.0" / "plugin.yaml").exists()
    listed = {p["id"]: p for p in c.get("/plugins").json()}
    assert listed["connector.hubtest-wayback"]["state"] == "disabled"
    again = c.post("/hub/install", json={"repository": str(out), "kind": "plugin", "id": "connector.hubtest-wayback"})
    assert again.status_code == 201                                               # same bytes: re-register is fine


def test_a_changed_package_cannot_replace_an_installed_version_and_a_bad_plugin_leaves_nothing(rig, tmp_path):
    c, app, out, data, pub = rig
    c.post("/hub/install", json={"repository": str(out), "kind": "plugin", "id": "connector.gleif"})
    evil = tmp_path / "evil-src"
    (evil).mkdir()
    for f in (CONN / "gleif").iterdir():
        if f.is_file():
            (evil / f.name).write_bytes(f.read_bytes())
    (evil / "main.py").write_text("print('changed')\n")
    out2 = tmp_path / "repo2"
    hub.build(out2, plugins=[evil], name="evil")
    repo2 = hub.Repository(str(out2))
    with app.state.db.connection() as conn, pytest.raises(hub.HubError, match="already installed with different content"):
        hub.install(conn, app.state.system_actor, repo2, "plugin", "connector.gleif", None, data_dir=data, trust=None)
    bad = tmp_path / "bad-src"
    bad.mkdir()
    (bad / "plugin.yaml").write_text("api_version: 'astral.plugin/v1'\nid: 'x.bad'\nversion: '1.0.0'\ntitle: 'bad'\nstage: ingest\ndirection: pull\nfootprint: none\n")
    out3 = tmp_path / "repo3"
    with pytest.raises(Exception):
        hub.build(out3, plugins=[bad], name="bad")                                  # an invalid manifest cannot even be published


def test_require_trusted_refuses_unknown_publishers_and_accepts_trusted_ones(rig):
    c, app, out, data, pub = rig
    with app.state.db.connection() as conn:
        repo = hub.Repository(str(out))
        store = TrustStore(data / "t")
        with pytest.raises(hub.HubError, match="requires a trusted publisher"):
            hub.install(conn, app.state.system_actor, repo, "plugin", "connector.hubtest-wayback", None, data_dir=data, trust=store, require_trusted=True)
        store.add(pub, "pub")
        r = hub.install(conn, app.state.system_actor, repo, "plugin", "connector.hubtest-wayback", None, data_dir=data, trust=store, require_trusted=True)
        assert r["publisher"]["status"] == "trusted"


def test_site_packs_are_stored_in_a_directory_per_licence(rig):
    c, app, out, data, pub = rig
    r = c.post("/hub/install", json={"repository": str(out), "kind": "site-pack", "id": "example.test"}).json()
    assert r["packs_dir"].endswith("CC-BY-SA-4.0") and (data / "packs" / "CC-BY-SA-4.0" / "example.test.json").exists()
    assert not (data / "packs" / "AGPL-3.0-or-later").exists() and "validates the pack" in r["note"]


def test_lenses_install_through_the_same_chain(rig):
    c, app, out, data, pub = rig
    assert c.post("/hub/install", json={"repository": str(out), "kind": "lens", "id": "company-pay"}).status_code == 201
    assert any(x["name"] == "company-pay" and x["trust"] == "unsigned" for x in c.get("/lenses").json())


def test_unknown_things_are_refused_cleanly(rig):
    c, app, out, data, pub = rig
    assert c.post("/hub/install", json={"repository": "/elsewhere", "kind": "plugin", "id": "x"}).status_code == 404
    assert c.post("/hub/install", json={"repository": str(out), "kind": "widget", "id": "x"}).status_code == 422
    assert c.post("/hub/install", json={"repository": str(out), "kind": "plugin", "id": "connector.nope"}).status_code == 422
    assert c.get("/hub/search", params={"q": "gleif"}).json()["results"][0]["id"] == "connector.gleif"


def test_remote_repositories_need_the_operator_to_allow_egress(tmp_path, server, repo_dir):
    out, priv, pub = repo_dir
    for f in out.rglob("*"):
        if f.is_file():
            rel = "/" + f.relative_to(out).as_posix()
            server.add(rel, f.read_bytes(), ctype="application/octet-stream")
    local = hub.Repository(server.url, remote_policy=hub.egress.EgressPolicy("local-only"))
    assert len(local.index["packages"]) == 4                                        # loopback is local, so local-only allows it
    local.fetch(local.find("plugin", "connector.gleif"))
    far = hub.Repository("http://repo.example.invalid", remote_policy=hub.egress.EgressPolicy("local-only"))
    with pytest.raises(hub.HubError, match="local-only"):
        far.index
    with pytest.raises(hub.HubError, match="disabled"):
        hub.Repository("http://repo.example.invalid").index
    for bad in ("../x", "/etc/passwd", "a/../../b", "c:/x"):
        with pytest.raises(hub.HubError):
            local.read(bad, 100)


def test_updates_are_found_by_version_not_by_name(rig, tmp_path):
    c, app, out, data, pub = rig
    c.post("/hub/install", json={"repository": str(out), "kind": "plugin", "id": "connector.hubtest-wayback"})
    src = tmp_path / "wb2"
    src.mkdir()
    for f in (_WB).iterdir():
        if f.is_file():
            (src / f.name).write_bytes(f.read_bytes())
    (src / "plugin.yaml").write_text((src / "plugin.yaml").read_text().replace('version: "1.0.0"', 'version: "1.1.0"'))
    newer = tmp_path / "newer"
    hub.build(newer, plugins=[src], name="newer")
    with app.state.db.connection() as conn:
        ups = hub.updates(conn, [hub.Repository(str(newer)), hub.Repository(str(tmp_path / "missing"))])
    assert [u for u in ups if u.get("id") == "connector.hubtest-wayback"] == [
        {"id": "connector.hubtest-wayback", "installed": "1.0.0", "available": "1.1.0", "repository": str(newer), "signed": False}]
    assert any("error" in u for u in ups)                                           # an unreadable repository is reported, not silently skipped
