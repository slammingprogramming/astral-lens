"""Plugin runtimes. One wire contract, interchangeable runtimes (ADR-0005).

Even the in-process runtime exchanges serialized JSON messages rather than
Python objects, so a plugin can be promoted from in-process to a launched
process (or a container) without changing its code.

    core -> plugin  {"op":"invoke","call_id","input","config","secrets","budget","context"}
    plugin -> core  {"op":"emit","call_id","assertions":[...]}   (repeatable)
                    {"op":"log","level","msg"}
                    {"op":"progress","done","total"}
                    {"op":"call","id","capability","args"}   -> core replies
                    {"op":"result","call_id","status","stats"}
    core -> plugin  {"op":"reply","id","ok","result"|"error"}   (only after a "call")

A plugin is a generator; `reply = yield {"op": "call", ...}` receives core's
answer. Plugins never open sockets or load models themselves: they ask core for
`http.fetch`, `artifact.put`, `text.extract`... so egress and model residency
stay enforceable (invariant 5, 10 §2).
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol


class RuntimeError_(Exception):
    pass


def _split_entry(entry: str) -> tuple[str, str]:
    if ":" not in entry:
        raise RuntimeError_(f"entrypoint {entry!r} must be 'file.py:function'")
    f, fn = entry.rsplit(":", 1)
    return f, fn


def load_callable(plugin_dir: Path, entry: str) -> Callable[..., Any]:
    file, fn = _split_entry(entry)
    path = (plugin_dir / file).resolve()
    if plugin_dir.resolve() not in path.parents:
        raise RuntimeError_("entrypoint escapes the plugin directory")
    spec = importlib.util.spec_from_file_location(f"_astral_plugin_{abs(hash(str(path)))}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError_(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return getattr(module, fn)
    except AttributeError as e:
        raise RuntimeError_(f"{path} has no function {fn!r}") from e


def _wire(obj: Any) -> Any:
    """Force a message through serialization: no live objects cross the boundary."""
    return json.loads(json.dumps(obj, default=_reject))


def _reject(o: Any) -> Any:
    raise TypeError(f"{type(o).__name__} is not serializable across the plugin boundary")


class Runtime(Protocol):
    def run(self, plugin_dir: Path, entry: str, request: dict[str, Any]) -> Iterator[dict[str, Any]]: ...


class InProcessRuntime:
    name = "in_process"

    def run(self, plugin_dir: Path, entry: str, request: dict[str, Any]) -> Iterator[dict[str, Any]]:
        gen = iter(load_callable(plugin_dir, entry)(_wire(request)))
        reply = None
        try:
            msg = next(gen)
            while True:
                reply = yield _wire(msg)                 # the host's answer to a "call"
                msg = gen.send(_wire(reply)) if reply is not None else next(gen)
        except StopIteration:
            return


class SubprocessRuntime:
    """Stdio line-delimited JSON to a launched Python process. Same plugin code,
    process-level crash and dependency isolation, still no container runtime."""

    name = "subprocess"

    def __init__(self, timeout: float | None = None) -> None:
        # how long the PLUGIN may go silent; ASTRAL_PLUGIN_TIMEOUT overrides the 120 s default
        self.timeout = timeout if timeout is not None else float(os.environ.get("ASTRAL_PLUGIN_TIMEOUT", "120"))

    def _command(self, plugin_dir: Path, entry: str) -> tuple[list[str], Callable[[], None]]:
        """The argv to launch and a cleanup to run afterwards. Subclasses change where it runs."""
        return [sys.executable, "-I", "-m", "astral.plugins.runner", str(plugin_dir), entry], lambda: None

    def run(self, plugin_dir: Path, entry: str, request: dict[str, Any]) -> Iterator[dict[str, Any]]:
        argv, cleanup = self._command(plugin_dir, entry)
        try:
            yield from self._drive(argv, request)
        finally:
            cleanup()

    def _drive(self, argv: list[str], request: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Run the child and speak the protocol. A watchdog bounds how long the PLUGIN may go
        silent (computing, looping, hung): it is armed while the child owns the turn and
        disarmed while the host is working for it, so a plugin waiting on a slow model call is
        not killed but one that spins forever is. The child's stderr is drained continuously
        so a chatty plugin cannot fill the pipe and deadlock."""
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        assert proc.stdin and proc.stdout and proc.stderr
        fired = threading.Event()
        timer: list[threading.Timer | None] = [None]
        err_tail: list[str] = [""]

        def disarm() -> None:
            if timer[0]:
                timer[0].cancel()

        def arm() -> None:
            disarm()
            timer[0] = threading.Timer(self.timeout, lambda: (fired.set(), proc.kill()))
            timer[0].daemon = True
            timer[0].start()

        def drain() -> None:
            for chunk in iter(lambda: proc.stderr.read(4096), ""):
                err_tail[0] = (err_tail[0] + chunk)[-2000:]

        threading.Thread(target=drain, daemon=True).start()
        arm()
        try:
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
            for line in proc.stdout:
                disarm()                                  # the child spoke; the host's time is not the plugin's
                line = line.strip()
                if not line:
                    arm()
                    continue
                msg = json.loads(line)
                reply = yield msg
                if msg.get("op") == "call":              # the child blocks until it gets one
                    proc.stdin.write(json.dumps(reply if reply is not None else
                                                {"op": "reply", "ok": False,
                                                 "error": "host sent no reply"}) + "\n")
                    proc.stdin.flush()
                arm()
            disarm()
            proc.stdin.close()
            rc = proc.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeError_("plugin process timed out")
        except (BrokenPipeError, OSError):
            if not fired.is_set():
                raise
            rc = -9
        finally:
            disarm()
            if proc.poll() is None:
                proc.kill()
        if fired.is_set():
            raise RuntimeError_(f"plugin made no progress for {self.timeout:g}s and was killed")
        if rc != 0:
            raise RuntimeError_(f"plugin process exited {rc}: {err_tail[0]}")


class ContainerRuntime(SubprocessRuntime):
    """The same stdio protocol, run inside a throw-away container (04 §3, ADR-0005).

    The plugin directory and a small stdlib-only runtime bundle are mounted READ-ONLY; the
    root filesystem is read-only, there is no network (everything the plugin may do reaches
    the outside through core's capability channel, so egress policy cannot be bypassed),
    all capabilities are dropped, privileges cannot grow, and memory, CPU and process
    counts are capped. This is the strongest isolation the project offers, not a proof of
    safety: a container shares the host kernel.
    """

    name = "container"

    def __init__(self, image: str | None = None, *, timeout: float | None = None, memory: str = "512m", cpus: str = "1",
                 pids: int = 128, docker: str | None = None) -> None:
        super().__init__(timeout)
        self.image = image or os.environ.get("ASTRAL_PLUGIN_IMAGE", "python:3.13-slim")
        self.memory, self.cpus, self.pids = memory, cpus, pids
        self.docker = docker or shutil.which("docker")
        if not self.docker:
            raise RuntimeError_("the container runtime needs Docker (or a compatible CLI) on PATH; none was found. "
                                "Use the in_process or subprocess runtime, or install a container runtime")

    def _stage(self) -> Path:
        here = Path(__file__).resolve().parent
        stage = Path(tempfile.mkdtemp(prefix="astral-rt-"))
        (stage / "astral").mkdir()
        (stage / "astral" / "__init__.py").write_text("", encoding="utf-8")
        shutil.copy(here.parent / "sdk.py", stage / "astral" / "sdk.py")
        shutil.copy(here / "container_entry.py", stage / "runner.py")
        return stage

    def _command(self, plugin_dir: Path, entry: str) -> tuple[list[str], Callable[[], None]]:
        stage = self._stage()
        cname = f"astral-plugin-{uuid.uuid4().hex[:12]}"
        argv = [self.docker, "run", "--rm", "-i", "--name", cname, "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", str(self.pids),
                "--memory", self.memory, "--cpus", self.cpus, "--user", "65534:65534",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
                "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONPATH=/opt/astral-rt", "-e", "HOME=/tmp",
                "-v", f"{Path(plugin_dir).resolve()}:/plugin:ro", "-v", f"{stage}:/opt/astral-rt:ro",
                "--entrypoint", "python", self.image, "-s", "-B", "/opt/astral-rt/runner.py", "/plugin", entry]

        def cleanup() -> None:
            subprocess.run([self.docker, "rm", "-f", cname], capture_output=True, timeout=30)   # a killed client leaves the container
            shutil.rmtree(stage, ignore_errors=True)

        return argv, cleanup


def runtime_for(name: str) -> Runtime:
    if name == "in_process":
        return InProcessRuntime()
    if name == "subprocess":
        return SubprocessRuntime()
    if name == "container":
        return ContainerRuntime()
    raise RuntimeError_(f"unknown runtime {name!r}")
