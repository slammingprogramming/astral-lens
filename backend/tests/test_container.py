"""M5: the container plugin runtime. These tests run REAL containers from an image already present
on the machine (python:3.13-slim by default, ASTRAL_PLUGIN_IMAGE to change); they are skipped,
not faked, when Docker or the image is missing. The Docker VM here is shared with other work, so
every container is short-lived and capped.

What is NOT shown: isolation against a hostile plugin that exploits the container runtime or the
shared kernel; behaviour under a rootless runner, Podman, or a remote Docker host; GPU plugins."""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap

import pytest

from astral.plugins import registry
from astral.plugins.host import PluginHost
from astral.plugins.runtime import ContainerRuntime, RuntimeError_

from test_plugins import EMAIL_DIR, make_plugin

IMAGE = os.environ.get("ASTRAL_PLUGIN_IMAGE", "python:3.13-slim")


def _docker_ready() -> bool:
    d = shutil.which("docker")
    if not d:
        return False
    try:
        return subprocess.run([d, "image", "inspect", IMAGE], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


needs_docker = pytest.mark.skipif(not _docker_ready(), reason=f"Docker or image {IMAGE} not available")


def test_the_command_is_locked_down(tmp_path):
    if not shutil.which("docker"):
        pytest.skip("no docker on PATH")
    rt = ContainerRuntime(IMAGE, memory="128m", cpus="0.5", pids=32)
    argv, cleanup = rt._command(tmp_path, "main.py:invoke")
    cleanup()
    j = " ".join(argv)
    for flag in ("--network none", "--read-only", "--cap-drop ALL", "no-new-privileges", "--memory 128m", "--cpus 0.5",
                 "--pids-limit 32", "--user 65534:65534", ":/plugin:ro", ":/opt/astral-rt:ro", "--rm"):
        assert flag in j, flag
    assert "--privileged" not in j and "docker.sock" not in j and "--network host" not in j


def test_no_docker_is_a_clear_error_not_a_silent_fallback(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda *_: None)
    with pytest.raises(RuntimeError_, match="needs Docker"):
        ContainerRuntime()


@needs_docker
def test_the_example_plugin_gives_the_same_result_in_a_container(db, kit):
    host = PluginHost(writer=kit.writer)
    text = "x@example.org and y@example.org"
    with db.connection() as conn:
        registry.install_from_dir(conn, kit.actor, EMAIL_DIR)
        registry.set_state(conn, kit.actor, "example.email-finder", "1.0.0", "enabled")
        sha = kit.artifact(conn, text.encode())
        a = host.invoke(conn, kit.actor, kit.case, "example.email-finder", {"artifact": sha, "text": text}, runtime="in_process")
        b = host.invoke(conn, kit.actor, kit.case, "example.email-finder", {"artifact": sha, "text": text}, runtime="container")
    assert a.status == b.status == "ok", b.gaps
    assert len(a.assertion_ids) == len(b.assertion_ids) == 7
    assert b.runtime == "container"


PROBE = '''
    import os, socket
    def invoke(r):
        out = {}
        try:
            socket.create_connection(("1.1.1.1", 53), timeout=3)
            out["network"] = "REACHED"
        except OSError as e:
            out["network"] = "blocked"
        for name, path in (("write_plugin_dir", "/plugin/x"), ("write_root", "/etc/x"), ("write_rt", "/opt/astral-rt/x")):
            try:
                open(path, "w").write("x")
                out[name] = "WROTE"
            except OSError:
                out[name] = "blocked"
        out["sees_host_users_dir"] = os.path.exists("/Users") or os.path.exists("/home/host")
        out["uid"] = os.getuid()
        out["tmp_writable"] = os.access("/tmp", os.W_OK)
        out["host_env_leaked"] = "ASTRAL_SECRETS_PASSPHRASE" in os.environ or "USERPROFILE" in os.environ
        yield {"op": "result", "status": "ok", "stats": out}
    def selfcheck(): return {"ok": True}
'''


@needs_docker
def test_a_plugin_inside_the_container_cannot_reach_the_network_the_host_or_its_own_files(db, kit, tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRAL_SECRETS_PASSPHRASE", "must-not-leak")
    d = make_plugin(tmp_path, main=textwrap.dedent(PROBE), pid="t.probe")
    host = PluginHost(writer=kit.writer)
    with db.connection() as conn:
        registry.install_from_dir(conn, kit.actor, d)
        registry.set_state(conn, kit.actor, "t.probe", "1.0.0", "enabled")
        res = host.invoke(conn, kit.actor, kit.case, "t.probe", {}, runtime="container")
    assert res.status == "ok", res.gaps
    s = res.stats
    assert s["network"] == "blocked"
    assert s["write_plugin_dir"] == s["write_root"] == s["write_rt"] == "blocked"
    assert s["uid"] == 65534 and s["tmp_writable"] is True
    assert s["sees_host_users_dir"] is False and s["host_env_leaked"] is False


@needs_docker
def test_a_runaway_plugin_is_killed_and_leaves_no_container_behind(db, kit, tmp_path, monkeypatch):
    spin = "def invoke(r):\n    while True:\n        pass\n    yield {}\ndef selfcheck(): return {'ok': True}\n"
    d = make_plugin(tmp_path, main=spin, pid="t.spin")
    host = PluginHost(writer=kit.writer)
    monkeypatch.setenv("ASTRAL_PLUGIN_TIMEOUT", "5")
    with db.connection() as conn:
        registry.install_from_dir(conn, kit.actor, d)
        registry.set_state(conn, kit.actor, "t.spin", "1.0.0", "enabled")
        res = host.invoke(conn, kit.actor, kit.case, "t.spin", {}, runtime="container")
    assert res.status == "error"
    left = subprocess.run([shutil.which("docker"), "ps", "-a", "--filter", "name=astral-plugin-", "--format", "{{.Names}}"],
                          capture_output=True, text=True, timeout=30).stdout.split()
    assert left == []
