from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import pytest

from astral import cases
from astral.assertions import AssertionDraft, AssertionWriter, DerivedBy
from astral.db import SOLO_ACTOR, Database
from astral.evidence import EvidenceStore

ADMIN_DSN = os.environ.get("ASTRAL_TEST_ADMIN_DSN",
                           "postgresql://postgres:astral@localhost:55432/postgres")
REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "plugins" / "examples"


@pytest.fixture(autouse=True)
def fast_throttle(monkeypatch):
    """Published rate limits are honoured by sleeping between requests to a host.
    Tests record the waits instead of sleeping them; test_capabilities asserts
    the throttle computes the right interval."""
    from astral import egress

    waits: list[float] = []
    now = [1000.0]                                  # a fake clock that only recorded sleeps advance

    def sleep(s: float) -> None:
        waits.append(s)
        now[0] += s

    monkeypatch.setattr(egress, "SLEEP", sleep)
    monkeypatch.setattr(egress, "CLOCK", lambda: now[0])
    return waits

@pytest.fixture(scope="session")
def dsn():
    name = f"astral_test_{uuid.uuid4().hex[:8]}"
    try:
        admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    except psycopg.OperationalError as e:       # pragma: no cover
        pytest.skip(f"no Postgres for tests ({e}); start one or set ASTRAL_TEST_ADMIN_DSN")
    admin.execute(f'CREATE DATABASE "{name}"')
    base = ADMIN_DSN.rsplit("/", 1)[0]
    yield f"{base}/{name}"
    admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
    admin.close()


@pytest.fixture(scope="session")
def db(dsn):
    d = Database(dsn).open()
    d.migrate()
    d.bootstrap_solo()
    yield d
    d.close()


@pytest.fixture()
def store(tmp_path):
    return EvidenceStore(tmp_path / "evidence")


@pytest.fixture()
def actor():
    return SOLO_ACTOR


@pytest.fixture()
def case(db, actor):
    with db.connection() as conn:
        return cases.create_case(conn, actor, f"case-{uuid.uuid4().hex[:6]}")


@pytest.fixture()
def writer(store):
    return AssertionWriter(store=store)


class Kit:
    """Small helpers so tests read as scenarios."""

    def __init__(self, db, actor, store, writer, case):
        self.db, self.actor, self.store, self.writer, self.case = db, actor, store, writer, case

    def artifact(self, conn, data: bytes, **kw) -> str:
        return self.store.ingest(conn, self.actor, self.case, data, acquired_by="test",
                                 media_type="text/plain", **kw).hex

    def source(self, conn, name="src", grade="B", origin=None):
        sid = cases.get_or_create_source(conn, self.actor, kind="test", name=name,
                                         origin_key=origin, default_source_grade=grade)
        return sid

    def draft(self, source_id, sha, subject=None, predicate="name", value="x", **kw):
        by = kw.pop("derived_by", DerivedBy(plugin="t", version="1", backend="native"))
        return AssertionDraft(subject=subject or uuid.uuid4(), predicate=predicate,
                              object_value=value, source_id=source_id, evidence=[sha],
                              derived_by=by, **kw)

    def write(self, conn, **kw):
        return self.writer.write(conn, self.actor, self.case, self.draft(**kw))


@pytest.fixture()
def kit(db, actor, store, writer, case):
    return Kit(db, actor, store, writer, case)


# ------------------------------------------------------- local HTTP fixtures
class FixtureServer:
    """A real HTTP server on 127.0.0.1 that stands in for an upstream API, so
    connector tests exercise the whole path (plugin -> http.fetch -> egress ->
    evidence) without the network. Responses are hand-written in each service's
    documented shape; they prove the mapping, NOT that the live service still
    answers this way."""

    def __init__(self) -> None:
        import json as _json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import parse_qs, urlsplit

        self.routes: list[tuple[str, object]] = []      # (path prefix, response | callable)
        self.requests: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):                  # silence
                pass

            def _serve(self):
                u = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                n = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(n) if n else b""
                outer.requests.append({"method": self.command, "path": u.path, "query": q,
                                       "headers": {k.lower(): v for k, v in self.headers.items()},
                                       "body": body})
                for prefix, resp in sorted(outer.routes, key=lambda r: -len(r[0])):
                    if u.path.startswith(prefix):
                        if callable(resp):
                            resp = resp(outer.requests[-1])
                        status, ctype, data, extra = resp
                        if isinstance(data, (dict, list)):
                            data = _json.dumps(data).encode()
                        elif isinstance(data, str):
                            data = data.encode()
                        self.send_response(status)
                        self.send_header("Content-Type", ctype)
                        self.send_header("Content-Length", str(len(data)))
                        for k, v in extra.items():
                            self.send_header(k, v)
                        self.end_headers()
                        self.wfile.write(data)
                        return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST = do_PUT = _serve

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self._srv.server_address[1]}"
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def add(self, prefix: str, data, *, status: int = 200, ctype: str = "application/json",
            headers: dict | None = None) -> None:
        self.routes.append((prefix, (status, ctype, data, headers or {})))

    def add_fn(self, prefix: str, fn) -> None:
        self.routes.append((prefix, fn))

    def close(self) -> None:
        self._srv.shutdown()


@pytest.fixture()
def server():
    s = FixtureServer()
    yield s
    s.close()
