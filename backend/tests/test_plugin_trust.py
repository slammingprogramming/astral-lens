"""M5: plugin signing and the trust store. Everything here is local: keys are generated in
the test, the "publisher" is the test, and nothing is fetched. What is NOT covered: a
registry or marketplace that distributes signed plugins (not built), key revocation, and
any statement about whether a signed plugin is safe. A signature says who and unaltered."""

from __future__ import annotations

import json

import pytest

from astral import signing
from astral.plugins import registry, trust
from astral.plugins.host import PluginHost

from test_plugins import make_plugin

MAIN = "def invoke(r):\n    yield {'op': 'result', 'status': 'ok', 'stats': {}}\ndef selfcheck(): return {'ok': True}\n"


@pytest.fixture()
def publisher(tmp_path):
    priv, pub = signing.generate_keypair()
    kdir = tmp_path / "trusted"
    return {"priv": priv, "pub": pub, "store": trust.TrustStore(kdir), "dir": kdir}


def signed_plugin(tmp_path, publisher, pid="t.signed"):
    d = make_plugin(tmp_path, main=MAIN, pid=pid)
    (d / "helper.py").write_text("X = 1")
    m = registry.load_manifest(d)
    trust.sign_package(d, m.id, m.version, publisher["priv"])
    return d


def test_unsigned_installs_but_is_flagged_and_can_be_refused(db, actor, tmp_path, publisher):
    d = make_plugin(tmp_path, main=MAIN, pid="t.unsigned")
    with db.connection() as conn:
        registry.install_from_dir(conn, actor, d, trust=publisher["store"])
        assert registry.trust_record(conn.execute("SELECT signature FROM plugin WHERE id='t.unsigned'").fetchone())["status"] == "unsigned"
        with pytest.raises(registry.PluginError, match="requires plugins signed by a trusted key"):
            registry.install_from_dir(conn, actor, d, trust=publisher["store"], require_trusted=True)


def test_signed_by_an_unknown_key_is_valid_but_not_trusted_until_the_operator_trusts_it(db, actor, tmp_path, publisher):
    d = signed_plugin(tmp_path, publisher)
    with db.connection() as conn:
        registry.install_from_dir(conn, actor, d, trust=publisher["store"])
        assert registry.trust_record(conn.execute("SELECT signature FROM plugin WHERE id='t.signed'").fetchone())["status"] == "signed-untrusted"
        with pytest.raises(registry.PluginError):
            registry.install_from_dir(conn, actor, d, trust=publisher["store"], require_trusted=True)
        publisher["store"].add(publisher["pub"], "acme-labs")
        registry.install_from_dir(conn, actor, d, trust=publisher["store"], require_trusted=True)
        assert registry.trust_record(conn.execute("SELECT signature FROM plugin WHERE id='t.signed'").fetchone())["status"] == "trusted"
    assert trust.TrustStore(publisher["dir"]).keys == {signing.fingerprint(publisher["pub"]): "acme-labs"}   # persisted


@pytest.mark.parametrize("attack", ["edit_file", "add_file", "remove_file", "other_version", "forged_signature", "swapped_key"])
def test_a_signature_that_does_not_hold_is_always_refused(db, actor, tmp_path, publisher, attack):
    d = signed_plugin(tmp_path, publisher, pid=f"t.{attack}")
    sig = json.loads((d / "plugin.sig").read_text())
    if attack == "edit_file":
        (d / "main.py").write_text(MAIN + "# backdoor\n")
    elif attack == "add_file":
        (d / "extra.py").write_text("print('hi')\n")
    elif attack == "remove_file":
        (d / "helper.py").unlink()
    elif attack == "other_version":
        (d / "plugin.yaml").write_text((d / "plugin.yaml").read_text().replace('version: "1.0.0"', 'version: "1.0.1"'))
    elif attack == "forged_signature":
        sig["signature"] = signing.sign(signing.generate_keypair()[0], b"something else")
        (d / "plugin.sig").write_text(json.dumps(sig))
    elif attack == "swapped_key":                                       # re-sign with an attacker key but keep the trusted name
        other = signing.generate_keypair()
        m = registry.load_manifest(d)
        trust.sign_package(d, m.id, m.version, other[0])
        publisher["store"].add(publisher["pub"], "acme-labs")           # operator trusts the REAL publisher only
        with db.connection() as conn:
            registry.install_from_dir(conn, actor, d, trust=publisher["store"])   # valid, but by an untrusted key
            row = conn.execute("SELECT signature FROM plugin WHERE id=%s", (f"t.{attack}",)).fetchone()
            assert registry.trust_record(row)["status"] == "signed-untrusted"
        return
    with db.connection() as conn:
        with pytest.raises(registry.PluginError, match="signature rejected"):
            registry.install_from_dir(conn, actor, d, trust=publisher["store"])


def test_a_signed_plugin_changed_after_install_is_quarantined_and_reports_unknown(db, kit, tmp_path, publisher):
    publisher["store"].add(publisher["pub"], "acme-labs")
    d = signed_plugin(tmp_path, publisher, pid="t.after")
    host = PluginHost(writer=kit.writer)
    with db.connection() as conn:
        registry.install_from_dir(conn, kit.actor, d, trust=publisher["store"])
        registry.set_state(conn, kit.actor, "t.after", "1.0.0", "enabled")
        assert host.invoke(conn, kit.actor, kit.case, "t.after", {}).status == "ok"
        (d / "main.py").write_text(MAIN + "# edited after install\n")
        res = host.invoke(conn, kit.actor, kit.case, "t.after", {})
        assert res.status == "unknown" and "no longer match what was signed" in res.gaps[0]
        assert registry.get(conn, "t.after")["state"] == "quarantined"


def test_unsigned_plugins_are_not_rechecked_so_development_stays_fluid(db, kit, tmp_path):
    d = make_plugin(tmp_path, main=MAIN, pid="t.dev")
    host = PluginHost(writer=kit.writer)
    with db.connection() as conn:
        registry.install_from_dir(conn, kit.actor, d)
        registry.set_state(conn, kit.actor, "t.dev", "1.0.0", "enabled")
        (d / "main.py").write_text(MAIN + "# edited\n")
        assert host.invoke(conn, kit.actor, kit.case, "t.dev", {}).status == "ok"


def test_pycache_and_signature_file_do_not_count_as_package_contents(tmp_path):
    d = make_plugin(tmp_path, main=MAIN, pid="t.files")
    (d / "__pycache__").mkdir()
    (d / "__pycache__" / "main.cpython-313.pyc").write_bytes(b"x")
    assert set(trust.package_files(d)) == {"plugin.yaml", "main.py"}
