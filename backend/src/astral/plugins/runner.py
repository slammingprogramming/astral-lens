"""Child-process entry for SubprocessRuntime: `python -m astral.plugins.runner DIR ENTRY`.

Reads one JSON request line on stdin and writes one JSON message per line on
the real stdout. After a `call` message it blocks reading the host's reply line
and sends it back into the plugin generator. Plugin `print()` output is
redirected to stderr so it can never corrupt the protocol.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from astral.plugins.runtime import load_callable


def main() -> int:
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
    except Exception as e:  # noqa: BLE001 - report any plugin failure over the wire
        out.write(json.dumps({"op": "result", "call_id": request.get("call_id"),
                              "status": "error", "error": f"{type(e).__name__}: {e}"}) + "\n")
        out.flush()
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
