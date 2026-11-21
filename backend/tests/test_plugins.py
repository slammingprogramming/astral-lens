from __future__ import annotations

import textwrap
import uuid

import pytest
from pydantic import ValidationError

from astral.plugins import registry
from astral.plugins.host import PluginHost, RateLimitExceeded
from astral.plugins.lint import find_model_imports
from astral.plugins.manifest import PluginManifest
from astral.policy import ApprovalRequired

from conftest import EXAMPLES

EMAIL_DIR = EXAMPLES / "email-finder"

BASE = dict(api_version="astral.plugin/v1", id="t.p", version="1.0.0", title="t", stage="enrich",
            direction="pull", footprint="none", entrypoint="main.py:invoke")


def make_plugin(tmp_path, *, main: str, manifest_extra: str = "", pid: str = "t.p",
                footprint: str = "none", secrets: str = "[]") -> object:
    d = tmp_path / pid.replace(".", "_")
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.yaml").write_text(textwrap.dedent(f"""\
        api_version: "astral.plugin/v1"
        id: "{pid}"
        version: "1.0.0"
        title: "t"
        stage: enrich
        direction: pull
        footprint: {footprint}
        entrypoint: "main.py:invoke"
        secrets: {secrets}
        health: {{ check: "main.py:selfcheck" }}
        {manifest_extra}
        """))
    (d / "main.py").write_text(textwrap.dedent(main))
    return d


# ---------------------------------------------------------------- manifest
def test_manifest_validates_and_rejects():
    PluginManifest(**BASE)
    for bad in (dict(api_version="astral.plugin/v9"), dict(id="Bad Id"), dict(version="x"),
                dict(footprint="loud"), dict(entrypoint=None)):
        with pytest.raises(ValidationError):
            PluginManifest(**{**BASE, **bad})
    with pytest.raises(ValidationError):                       # config must be valid JSON Schema
        PluginManifest(**{**BASE, "config": {"type": "nonsense"}})


def test_config_defaults_and_validation():
    m = PluginManifest(**{**BASE, "config": {"type": "object", "properties": {
        "n": {"type": "integer", "default": 5}}}})
    assert m.validate_config({}) == {"n": 5}
    with pytest.raises(Exception):
        m.validate_config({"n": "five"})


def test_a_remote_plugin_cannot_claim_no_footprint():
    with pytest.raises(ValidationError):
        PluginManifest(**{**BASE, "resources": {"accelerator": "remote"}})


# -------------------------------------------------------------------- lint
def test_lint_rejects_direct_model_imports():
    src = "import torch\nfrom insightface.app import FaceAnalysis\nx = __import__('onnxruntime')\n"
    mods = {m for _, m in find_model_imports(src)}
    assert mods == {"torch", "insightface.app", "onnxruntime"}
    assert find_model_imports("import json, re\nfrom pathlib import Path\n") == []


def test_install_rejects_a_plugin_that_loads_a_model(db, actor, tmp_path):
    d = make_plugin(tmp_path, pid="t.model", main="import onnxruntime\ndef invoke(r): yield {}\n")
    with db.connection() as conn:
        with pytest.raises(registry.PluginError, match="model library"):
            registry.install_from_dir(conn, actor, d)


# ------------------------------------------------------------------ invoke
def test_example_plugin_end_to_end(db, kit):
    host = PluginHost(writer=kit.writer)
    with db.connection() as conn:
        registry.install_from_dir(conn, kit.actor, EMAIL_DIR)
        registry.set_state(conn, kit.actor, "example.email-finder", "1.0.0", "enabled")
        # sources are workspace-wide and other tests may have vouched for this one
        from astral import cases as _cases

        _cases.set_source_grade(conn, kit.actor, _cases.get_or_create_source(
            conn, kit.actor, kind="plugin", name="example.email-finder"), "F")
        text = "Contact a.b@Example.org or c@example.org; again A.B@example.org"
        sha = kit.artifact(conn, text.encode())
        res = host.invoke(conn, kit.actor, kit.case, "example.email-finder",
                          {"artifact": sha, "text": text})
        assert res.status == "ok" and res.stats["addresses"] == 2
        rows = conn.execute("SELECT derived_by, source_grade, info_grade FROM assertion"
                            " WHERE case_id=%s", (kit.case,)).fetchall()
    assert len(rows) == 1 + 2 * 3
    assert all(r["derived_by"]["plugin"] == "example.email-finder" for r in rows)
    assert {r["source_grade"] for r in rows} == {"F"}     # untested source stays F


def test_plugin_cannot_claim_a_better_source_grade_than_the_operator_set(db, kit, tmp_path):
    d = make_plugin(tmp_path, pid="t.boast", main="""
        def invoke(r):
            yield {"op": "emit", "assertions": [{"subject": "%s", "predicate": "p",
                   "object_value": "v", "evidence": [r["input"]["a"]], "source_grade": "A",
                   "info_grade": 1, "derived_by": {"plugin": "someone.else", "version": "9"}}]}
            yield {"op": "result", "status": "ok"}
        def selfcheck(): return {"ok": True}
    """ % uuid.uuid4())
    host = PluginHost(writer=kit.writer)
    with db.connection() as conn:
        registry.install_from_dir(conn, kit.actor, d)
        registry.set_state(conn, kit.actor, "t.boast", "1.0.0", "enabled")
        sha = kit.artifact(conn, b"x")
        res = host.invoke(conn, kit.actor, kit.case, "t.boast", {"a": sha})
        row = conn.execute("SELECT source_grade, derived_by FROM assertion WHERE id=%s",
                           (res.assertion_ids[0],)).fetchone()
    assert row["source_grade"] == "F"                         # capped to the source's grade
    assert row["derived_by"]["plugin"] == "t.boast"           # identity cannot be forged


def test_bad_emissions_become_gaps_not_a_failed_job(db, kit, tmp_path):
    d = make_plugin(tmp_path, pid="t.messy", main="""
        def invoke(r):
            ok = {"subject": "%s", "predicate": "p", "object_value": "v", "evidence": [r["input"]["a"]]}
            bad = {"subject": "%s", "predicate": "p", "object_value": "v", "evidence": ["f"*64]}
            junk = {"predicate": "p"}
            yield {"op": "emit", "assertions": [ok, bad, junk]}
            yield {"op": "result", "status": "ok"}
        def selfcheck(): return {"ok": True}
    """ % (uuid.uuid4(), uuid.uuid4()))
    host = PluginHost(writer=kit.writer)
    with db.connection() as conn:
        registry.install_from_dir(conn, kit.actor, d)
        registry.set_state(conn, kit.actor, "t.messy", "1.0.0", "enabled")
        sha = kit.artifact(conn, b"x")
        res = host.invoke(conn, kit.actor, kit.case, "t.messy", {"a": sha})
    assert res.status == "partial" and len(res.assertion_ids) == 1 and len(res.gaps) == 2


def test_quarantined_source_is_unknown_never_negative(db, kit, tmp_path):
    d = make_plugin(tmp_path, pid="t.rot", main="""
        def invoke(r):
            yield {"op": "result", "status": "ok", "stats": {"found": 0}}
        def selfcheck(): return {"ok": False, "error_class": "site_changed", "detail": "403"}
    """)
    host = PluginHost(writer=kit.writer, quarantine_after=2)
    with db.connection() as conn:
        registry.install_from_dir(conn, kit.actor, d)
        registry.set_state(conn, kit.actor, "t.rot", "1.0.0", "enabled")
        host.run_health_check(conn, kit.actor, "t.rot")
        assert registry.get(conn, "t.rot")["state"] == "enabled"      # one failure: not yet
        host.run_health_check(conn, kit.actor, "t.rot")
        assert registry.get(conn, "t.rot")["state"] == "quarantined"
        res = host.invoke(conn, kit.actor, kit.case, "t.rot", {})
    assert res.status == "unknown" and res.assertion_ids == []
    assert "UNKNOWN, not negative" in res.gaps[0]


def test_target_contact_needs_approval_every_time(db, kit, tmp_path):
    d = make_plugin(tmp_path, pid="t.touch", footprint="target_contact", main="""
        def invoke(r):
            yield {"op": "result", "status": "ok"}
        def selfcheck(): return {"ok": True}
    """)
    host = PluginHost(writer=kit.writer)
    with db.connection() as conn:
        registry.install_from_dir(conn, kit.actor, d)
        registry.set_state(conn, kit.actor, "t.touch", "1.0.0", "enabled")
        with pytest.raises(ApprovalRequired):
            host.invoke(conn, kit.actor, kit.case, "t.touch", {})
        assert host.invoke(conn, kit.actor, kit.case, "t.touch", {}, approved=True).status == "ok"
        with pytest.raises(ApprovalRequired):                   # approval does not persist
            host.invoke(conn, kit.actor, kit.case, "t.touch", {})


def test_a_plugin_only_receives_the_secrets_it_declares(db, kit, tmp_path):
    d = make_plugin(tmp_path, pid="t.keys", secrets="[WANTED_KEY]", main="""
        def invoke(r):
            yield {"op": "log", "level": "info", "msg": ",".join(sorted(r["secrets"]))}
            yield {"op": "result", "status": "ok"}
        def selfcheck(): return {"ok": True}
    """)

    class Secrets:
        def get(self, name):
            return {"WANTED_KEY": "k1", "OTHER_KEY": "k2"}.get(name)

    host = PluginHost(writer=kit.writer, secrets=Secrets())
    with db.connection() as conn:
        registry.install_from_dir(conn, kit.actor, d)
        registry.set_state(conn, kit.actor, "t.keys", "1.0.0", "enabled")
        res = host.invoke(conn, kit.actor, kit.case, "t.keys", {})
    assert res.logs == ["[info] WANTED_KEY"]


def test_rate_limit(db, kit, tmp_path):
    d = make_plugin(tmp_path, pid="t.rl", manifest_extra="rate_limit: { requests: 1, per: 60s }",
                    main="""
        def invoke(r):
            yield {"op": "result", "status": "ok"}
        def selfcheck(): return {"ok": True}
    """)
    host = PluginHost(writer=kit.writer)
    with db.connection() as conn:
        registry.install_from_dir(conn, kit.actor, d)
        registry.set_state(conn, kit.actor, "t.rl", "1.0.0", "enabled")
        host.invoke(conn, kit.actor, kit.case, "t.rl", {})
        with pytest.raises(RateLimitExceeded):
            host.invoke(conn, kit.actor, kit.case, "t.rl", {})


def test_disabled_plugin_does_not_run(db, kit):
    host = PluginHost(writer=kit.writer)
    with db.connection() as conn:
        registry.install_from_dir(conn, kit.actor, EMAIL_DIR)
        registry.set_state(conn, kit.actor, "example.email-finder", "1.0.0", "disabled")
        with pytest.raises(registry.PluginError, match="not enabled"):
            host.invoke(conn, kit.actor, kit.case, "example.email-finder", {})


def test_subprocess_runtime_gives_the_same_result_as_in_process(db, kit):
    """ADR-0005: same plugin, same protocol, different runtime."""
    host = PluginHost(writer=kit.writer)
    text = "x@example.org and y@example.org"
    with db.connection() as conn:
        registry.install_from_dir(conn, kit.actor, EMAIL_DIR)
        registry.set_state(conn, kit.actor, "example.email-finder", "1.0.0", "enabled")
        sha = kit.artifact(conn, text.encode())
        a = host.invoke(conn, kit.actor, kit.case, "example.email-finder",
                        {"artifact": sha, "text": text}, runtime="in_process")
        b = host.invoke(conn, kit.actor, kit.case, "example.email-finder",
                        {"artifact": sha, "text": text}, runtime="subprocess")
    assert a.status == b.status == "ok"
    assert len(a.assertion_ids) == len(b.assertion_ids) == 7


def test_container_runtime_without_docker_fails_visibly_and_the_small_tier_does_not_need_it(db, kit, monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda *_: None)
    host = PluginHost(writer=kit.writer)
    with db.connection() as conn:
        registry.install_from_dir(conn, kit.actor, EMAIL_DIR)
        registry.set_state(conn, kit.actor, "example.email-finder", "1.0.0", "enabled")
        res = host.invoke(conn, kit.actor, kit.case, "example.email-finder",
                          {"artifact": "0" * 64, "text": ""}, runtime="container")
    assert res.status == "error" and "needs Docker" in res.gaps[0]


def test_a_missing_required_config_value_is_a_named_configuration_error():
    from astral.plugins.manifest import ConfigInvalid, PluginManifest

    m = PluginManifest.model_validate({
        "api_version": "astral.plugin/v1", "id": "t.cfg", "version": "1.0.0", "title": "t", "stage": "ingest",
        "direction": "pull", "footprint": "none", "runtimes": ["in_process"], "entrypoint": "main.py:invoke",
        "config": {"type": "object", "required": ["contact"], "properties": {"contact": {"type": "string"}}}})
    with pytest.raises(ConfigInvalid, match="t.cfg is not configured correctly.*contact"):
        m.validate_config({})
    assert m.validate_config({"contact": "ops@example.org"})["contact"] == "ops@example.org"
