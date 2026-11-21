"""The plugin runtimes must not let a plugin hang or deadlock the host. Found when the container
runaway test hung for six minutes: the old timeout only applied after the plugin closed its output."""

from __future__ import annotations

from astral.plugins import registry
from astral.plugins.host import PluginHost

from test_plugins import make_plugin

SPIN = "def invoke(r):\n    while True:\n        pass\n    yield {}\ndef selfcheck(): return {'ok': True}\n"
CHATTY = ("import sys\ndef invoke(r):\n    sys.stderr.write('x' * 3_000_000)\n"
          "    yield {'op': 'result', 'status': 'ok', 'stats': {}}\ndef selfcheck(): return {'ok': True}\n")
SLOW_HOST = ("def invoke(r):\n    reply = yield {'op': 'call', 'id': 1, 'capability': 'artifact.put', 'args': {}}\n"
             "    yield {'op': 'result', 'status': 'ok', 'stats': {}}\ndef selfcheck(): return {'ok': True}\n")


def run(db, kit, tmp_path, pid, main):
    d = make_plugin(tmp_path, main=main, pid=pid)
    host = PluginHost(writer=kit.writer)
    with db.connection() as conn:
        registry.install_from_dir(conn, kit.actor, d)
        registry.set_state(conn, kit.actor, pid, "1.0.0", "enabled")
        return host.invoke(conn, kit.actor, kit.case, pid, {}, runtime="subprocess")


def test_a_plugin_that_spins_forever_is_killed_instead_of_hanging_the_host(db, kit, tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRAL_PLUGIN_TIMEOUT", "3")
    res = run(db, kit, tmp_path, "t.spin", SPIN)
    assert res.status == "error" and "no progress" in res.gaps[0]


def test_a_chatty_plugin_cannot_deadlock_the_host_through_stderr(db, kit, tmp_path):
    assert run(db, kit, tmp_path, "t.chatty", CHATTY).status == "ok"


def test_time_the_host_spends_working_for_the_plugin_does_not_count_against_it(db, kit, tmp_path, monkeypatch):
    """A plugin waiting on a slow capability (a model call) must not be killed for the host's slowness."""
    import time

    monkeypatch.setenv("ASTRAL_PLUGIN_TIMEOUT", "2")
    d = make_plugin(tmp_path, main=SLOW_HOST, pid="t.slowhost")
    host = PluginHost(writer=kit.writer)
    orig = host.capabilities.call

    def slow(*a, **k):
        time.sleep(4)                                              # longer than the plugin's own allowance
        return orig(*a, **k)

    monkeypatch.setattr(host.capabilities, "call", slow)
    with db.connection() as conn:
        registry.install_from_dir(conn, kit.actor, d)
        registry.set_state(conn, kit.actor, "t.slowhost", "1.0.0", "enabled")
        res = host.invoke(conn, kit.actor, kit.case, "t.slowhost", {}, runtime="subprocess")
    assert "no progress" not in " ".join(res.gaps)
