"""Entry point INSIDE a plugin container. Standard library only: it is copied into the
container next to sdk.py, because the image has Python but not this package.

    python /opt/astral-rt/runner.py /plugin main.py:invoke

Same protocol as astral.plugins.runner: one JSON request line on stdin, one JSON message per
line on the real stdout, a reply line expected after each `call`. Plugin print() goes to stderr.
"""

import importlib.util
import json
import sys
from pathlib import Path


def load_callable(plugin_dir, entry):
    if ":" not in entry:
        raise RuntimeError(f"entrypoint {entry!r} must be 'file.py:function'")
    file, fn = entry.rsplit(":", 1)
    path = (plugin_dir / file).resolve()
    if plugin_dir.resolve() not in path.parents:
        raise RuntimeError("entrypoint escapes the plugin directory")
    spec = importlib.util.spec_from_file_location("_astral_plugin", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, fn)


def main():
    out = sys.stdout
    sys.stdout = sys.stderr
    plugin_dir, entry = Path(sys.argv[1]), sys.argv[2]
    request = json.loads(sys.stdin.readline())
    try:
        gen = iter(load_callable(plugin_dir, entry)(request))
        try:
            msg = next(gen)
            while True:
                out.write(json.dumps(msg) + "\n")
                out.flush()
                reply = None
                if msg.get("op") == "call":
                    line = sys.stdin.readline()
                    reply = json.loads(line) if line.strip() else None
                msg = gen.send(reply) if reply is not None else next(gen)
        except StopIteration:
            pass
    except Exception as e:  # report any plugin failure over the wire
        out.write(json.dumps({"op": "result", "call_id": request.get("call_id"),
                              "status": "error", "error": f"{type(e).__name__}: {e}"}) + "\n")
        out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
