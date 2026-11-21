"""The plugin <-> core capability channel and egress enforcement."""

from __future__ import annotations

import textwrap
import uuid

import pytest

from astral import cases, egress
from astral.plugins import registry
from astral.plugins.host import PluginHost

from test_plugins import make_plugin

FETCHER = """
    from astral import sdk
    def invoke(r):
        url = r["input"]["url"]
        try:
            res = yield from sdk.fetch(url)
        except sdk.CapabilityFailed as e:
            yield sdk.result(coverage=[sdk.coverage("fixture", unknown=str(e))])
            return
        yield sdk.result(stats={"status": res["status"], "sha": res.get("sha256"),
                                "text": res.get("text")},
                         coverage=[sdk.coverage("fixture", hits=1)])
    def selfcheck(): return {"ok": True}
"""


def _install(db, kit, tmp_path, pid, main=FETCHER, extra="", footprint="third_party"):
    d = make_plugin(tmp_path, pid=pid, main=main, footprint=footprint,
                    manifest_extra="capabilities: [http.fetch, artifact.put, artifact.get]\n"
                                   + textwrap.indent(extra, " " * 8))
    with db.connection() as conn:
        registry.install_from_dir(conn, kit.actor, d)
        registry.set_state(conn, kit.actor, pid, "1.0.0", "enabled")
    return PluginHost(writer=kit.writer)


def test_http_fetch_returns_body_and_ingests_it_as_evidence(db, kit, tmp_path, server):
    server.add("/hello", "hi there", ctype="text/plain")
    host = _install(db, kit, tmp_path, "t.fetch")
    with db.connection() as conn:
        res = host.invoke(conn, kit.actor, kit.case, "t.fetch", {"url": server.url + "/hello"})
        assert res.status == "ok" and res.stats["status"] == 200 and res.stats["text"] == "hi there"
        row = conn.execute("SELECT acquired_by, acquired_from, acquisition_ctx FROM artifact_acquisition"
                           " WHERE case_id=%s", (kit.case,)).fetchone()
        audit = conn.execute("SELECT detail FROM audit_log WHERE action='plugin.invoke'"
                             " AND case_id=%s ORDER BY id DESC LIMIT 1", (kit.case,)).fetchone()
    assert row["acquired_by"] == "t.fetch@1.0.0" and row["acquired_from"].endswith("/hello")
    assert row["acquisition_ctx"]["status"] == 200 and row["acquisition_ctx"]["egress"] == "direct"
    assert server.requests[0]["headers"]["user-agent"].startswith("AstralLens/")
    # the audit entry names the host and a count, never the URL path
    assert audit["detail"]["hosts_contacted"] == {"127.0.0.1": 1}
    assert "hello" not in str(audit["detail"])


@pytest.mark.parametrize("ctype", ["application/vnd.api+json", "application/geo+json; charset=utf-8", "application/problem+xml"])
def test_structured_syntax_suffixes_are_text_not_binary(db, kit, tmp_path, server, ctype):
    server.add("/api", '{"data": []}', ctype=ctype)
    host = _install(db, kit, tmp_path, "t.fetch.suffix")
    with db.connection() as conn:
        res = host.invoke(conn, kit.actor, kit.case, "t.fetch.suffix", {"url": server.url + "/api"})
    assert res.stats["text"] == '{"data": []}'


def test_a_failed_source_is_unknown_not_empty(db, kit, tmp_path, server):
    server.add("/boom", "oops", status=500, ctype="text/plain")
    main = FETCHER.replace("res = yield from sdk.fetch(url)", "res = yield from sdk.fetch_json(url)") \
                  .replace('"status": res["status"]', '"status": 0')
    host = _install(db, kit, tmp_path, "t.fail", main=textwrap.dedent(main))
    with db.connection() as conn:
        res = host.invoke(conn, kit.actor, kit.case, "t.fail", {"url": server.url + "/boom"})
    assert res.coverage[0]["outcome"] == "unknown"
    assert res.status == "partial" and "UNKNOWN, not negative" in res.gaps[0]


def test_no_hits_is_reported_as_no_hits_and_never_asserted(db, kit, tmp_path, server):
    server.add("/empty", {"results": []})
    main = """
        from astral import sdk
        def invoke(r):
            res, data = yield from sdk.fetch_json(r["input"]["url"])
            yield sdk.result(coverage=[sdk.coverage("fixture", hits=len(data["results"]))])
        def selfcheck(): return {"ok": True}
    """
    host = _install(db, kit, tmp_path, "t.empty", main=textwrap.dedent(main))
    with db.connection() as conn:
        res = host.invoke(conn, kit.actor, kit.case, "t.empty", {"url": server.url + "/empty"})
        n = conn.execute("SELECT count(*) AS n FROM assertion WHERE case_id=%s", (kit.case,)).fetchone()["n"]
    assert res.coverage == [{"source": "fixture", "outcome": "no_hits", "note": None}]
    assert n == 0 and res.gaps == []


def test_an_undeclared_capability_is_refused(db, kit, tmp_path, server):
    d = make_plugin(tmp_path, pid="t.sneak", footprint="third_party", main=FETCHER)  # no capabilities
    with db.connection() as conn:
        registry.install_from_dir(conn, kit.actor, d)
        registry.set_state(conn, kit.actor, "t.sneak", "1.0.0", "enabled")
        res = PluginHost(writer=kit.writer).invoke(
            conn, kit.actor, kit.case, "t.sneak", {"url": server.url + "/x"})
    assert res.coverage[0]["outcome"] == "unknown" and "not declared" in res.coverage[0]["note"]
    assert server.requests == []                       # nothing left the machine


def test_a_footprint_none_plugin_cannot_use_the_network(db, kit, tmp_path, server):
    host = _install(db, kit, tmp_path, "t.liar", footprint="none")
    with db.connection() as conn:
        res = host.invoke(conn, kit.actor, kit.case, "t.liar", {"url": server.url + "/x"})
    assert "footprint 'none'" in res.coverage[0]["note"] and server.requests == []


def test_local_only_blocks_external_hosts_but_allows_loopback(db, actor, store, writer, tmp_path, server):
    server.add("/ok", "fine", ctype="text/plain")
    with db.connection() as conn:
        case = cases.create_case(conn, actor, "airgap",
                                 collection_identity={"egress": "local-only"})
    from conftest import Kit

    kit = Kit(db, actor, store, writer, case)
    host = _install(db, kit, tmp_path, "t.air")
    with db.connection() as conn:
        ok = host.invoke(conn, actor, case, "t.air", {"url": server.url + "/ok"})
        denied = host.invoke(conn, actor, case, "t.air", {"url": "http://example.com/"})
    assert ok.stats["status"] == 200
    assert denied.coverage[0]["outcome"] == "unknown" and "local-only" in denied.coverage[0]["note"]


def test_local_only_refuses_plugins_that_bypass_core_networking(db, actor, store, writer, tmp_path):
    with db.connection() as conn:
        case = cases.create_case(conn, actor, "airgap2", collection_identity={"egress": "local-only"})
    from conftest import Kit

    kit = Kit(db, actor, store, writer, case)
    host = _install(db, kit, tmp_path, "t.dns", footprint="dns")
    with db.connection() as conn, pytest.raises(registry.PluginError, match="local-only"):
        host.invoke(conn, actor, case, "t.dns", {"url": "http://127.0.0.1/"})


def test_redirects_cannot_escape_local_only(server):
    server.add("/bounce", "", status=302, ctype="text/plain",
               headers={"Location": "http://example.com/"})
    pol = egress.EgressPolicy("local-only")
    with pytest.raises(egress.EgressDenied):
        egress.Fetcher().fetch(pol, server.url + "/bounce")


def test_429_is_retried_with_retry_after(server):
    hits = []

    def flaky(req):
        hits.append(1)
        if len(hits) == 1:
            return (429, "text/plain", "slow down", {"Retry-After": "0"})
        return (200, "text/plain", "ok", {})

    server.add_fn("/flaky", flaky)
    res = egress.Fetcher().fetch(egress.EgressPolicy("direct"), server.url + "/flaky")
    assert res.status == 200 and len(hits) == 2


def test_proxy_mode_routes_through_the_configured_proxy(server):
    # the "proxy" is our own fixture server: it must receive the absolute-URI request
    server.add("/", "via proxy", ctype="text/plain")
    pol = egress.EgressPolicy("proxy", proxy=server.url)
    res = egress.Fetcher().fetch(pol, "http://203.0.113.9/target")      # TEST-NET-3, unroutable
    assert res.body == b"via proxy"
    assert server.requests[0]["path"].endswith("/target")


def test_capabilities_work_over_the_subprocess_runtime_too(db, kit, tmp_path, server):
    server.add("/hello", "hi sub", ctype="text/plain")
    host = _install(db, kit, tmp_path, "t.sub")
    with db.connection() as conn:
        res = host.invoke(conn, kit.actor, kit.case, "t.sub", {"url": server.url + "/hello"},
                          runtime="subprocess")
    assert res.status == "ok" and res.stats["text"] == "hi sub" and res.runtime == "subprocess"


def test_a_manifests_published_rate_limit_spaces_requests_to_the_host(db, kit, tmp_path, server,
                                                                       fast_throttle):
    server.add("/x", "ok", ctype="text/plain")
    main = """
        from astral import sdk
        def invoke(r):
            for _ in range(3):
                yield from sdk.fetch(r["input"]["url"])
            yield sdk.result()
        def selfcheck(): return {"ok": True}
    """
    host = _install(db, kit, tmp_path, "t.paced", main=textwrap.dedent(main),
                    extra="rate_limit: { requests: 60, per: 1m, scope: host }\n")
    with db.connection() as conn:
        host.invoke(conn, kit.actor, kit.case, "t.paced", {"url": server.url + "/x"})
    spaced = [w for w in fast_throttle if w > 0.5]
    assert len(spaced) == 2 and all(0.5 < w <= 1.0 for w in spaced)       # 60/min = 1 s apart


def test_artifact_put_and_get_round_trip(db, kit, tmp_path):
    main = """
        from astral import sdk
        def invoke(r):
            put = yield from sdk.put_artifact("derived text", media_type="text/plain",
                                               derivation="test")
            got = yield from sdk.call("artifact.get", sha256=put["sha256"])
            yield sdk.result(stats={"sha": put["sha256"], "text": got["text"]})
        def selfcheck(): return {"ok": True}
    """
    host = _install(db, kit, tmp_path, "t.art", main=textwrap.dedent(main))
    with db.connection() as conn:
        res = host.invoke(conn, kit.actor, kit.case, "t.art", {})
        n = conn.execute("SELECT count(*) AS n FROM artifact_acquisition WHERE sha256=%s",
                         (bytes.fromhex(res.stats["sha"]),)).fetchone()["n"]
    assert res.stats["text"] == "derived text" and n == 1
