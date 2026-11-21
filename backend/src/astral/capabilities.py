"""Core capabilities a plugin may call (`{"op": "call", ...}`, plugins/runtime.py).

A plugin must declare each capability in its manifest (`capabilities:`); an
undeclared call is refused. Capabilities that need a model resolve a *backend*
through the registry in `backends.py`, so CPU/GPU substitution is a
configuration change and is recorded, never silent (invariants 4 and 5).
"""

from __future__ import annotations

import base64
import urllib.parse
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

import psycopg

from astral import egress
from astral.db import Actor
from astral.plugins.manifest import PluginManifest
from astral.secrets import Redactor

TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml",
              "application/ld+json", "application/atom+xml", "application/rss+xml")
INLINE_TEXT_LIMIT = 2 * 1024 * 1024
INLINE_BINARY_LIMIT = 5 * 1024 * 1024


def is_text(ctype: str) -> bool:
    """Text a plugin can read inline: the listed types, and any structured-syntax suffix (application/vnd.api+json,
    application/geo+json, application/problem+xml, ...). GLEIF answers `application/vnd.api+json`; treating that as
    binary made a valid answer look like 'not JSON' until a live run showed it."""
    c = ctype.split(";")[0].strip().lower()
    return any(c.startswith(t) for t in TEXT_TYPES) or c.endswith(("+json", "+xml"))


class CapabilityError(Exception):
    """Reported to the plugin as {"ok": false, "error": ...}; never crashes the host."""


@dataclass
class CallContext:
    conn: psycopg.Connection
    actor: Actor
    case_id: uuid.UUID
    manifest: PluginManifest
    host: Any                                     # PluginHost (fetcher, store, policy)
    egress: egress.EgressPolicy
    redactor: Redactor = field(default_factory=Redactor)
    hosts_contacted: Counter = field(default_factory=Counter)
    calls: Counter = field(default_factory=Counter)

    @property
    def store(self):
        store = self.host.writer.store
        if store is None:
            raise CapabilityError("no evidence store configured")
        return store

    @property
    def acquired_by(self) -> str:
        return f"{self.manifest.id}@{self.manifest.version}"


Handler = Callable[[CallContext, dict[str, Any]], dict[str, Any]]


class Capabilities:
    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, name: str, fn: Handler) -> None:
        self._handlers[name] = fn

    def known(self) -> set[str]:
        return set(self._handlers)

    def call(self, ctx: CallContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name not in ctx.manifest.capabilities:
            raise CapabilityError(f"capability {name!r} is not declared in the manifest")
        fn = self._handlers.get(name)
        if fn is None:
            raise CapabilityError(f"capability {name!r} is not available on this install")
        ctx.calls[name] += 1
        return fn(ctx, args)


# ------------------------------------------------------------------ http.fetch
def _http_fetch(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    if ctx.manifest.footprint == "none":
        raise CapabilityError("a plugin declaring footprint 'none' may not use http.fetch")
    url = str(args.get("url") or "")
    params = args.get("params") or {}
    if params:
        sep = "&" if urllib.parse.urlsplit(url).query else "?"
        url += sep + urllib.parse.urlencode(params, doseq=True)
    body = args.get("body_b64")
    data = base64.b64decode(body) if body else (
        args["body_text"].encode("utf-8") if args.get("body_text") is not None else None)
    rl = ctx.manifest.rate_limit
    min_interval = (rl.window_seconds() / rl.requests) if rl else 0.0
    try:
        res = ctx.host.fetcher.fetch(
            ctx.egress, url, method=str(args.get("method", "GET")).upper(),
            headers={str(k): str(v) for k, v in (args.get("headers") or {}).items()},
            body=data, min_interval=min_interval,
            max_bytes=int(args.get("max_bytes") or egress.MAX_BODY))
    except egress.EgressDenied as e:
        raise CapabilityError(str(e)) from e
    except OSError as e:                          # DNS failure, refused, timeout, TLS
        raise CapabilityError(f"network error: {type(e).__name__}: {e}") from e

    ctx.hosts_contacted[urllib.parse.urlsplit(url).hostname or "?"] += 1
    safe_url = ctx.redactor.url(res.final_url)       # an API token in a query string must not be stored
    ctype = res.headers.get("content-type", "application/octet-stream")
    out: dict[str, Any] = {
        "status": res.status, "headers": res.headers, "final_url": safe_url,
        "elapsed_ms": res.elapsed_ms, "truncated": res.truncated,
    }
    if args.get("ingest", True):
        ing = ctx.store.ingest(
            ctx.conn, ctx.actor, ctx.case_id, res.body, acquired_by=ctx.acquired_by,
            media_type=ctype.split(";")[0].strip() or None, acquired_from=safe_url,
            acquisition_ctx={"method": str(args.get("method", "GET")).upper(),
                             "status": res.status, "egress": ctx.egress.mode,
                             "user_agent": ctx.egress.user_agent,
                             "redirects": [ctx.redactor.url(u) for u in res.redirects],
                             "truncated": res.truncated,
                             "headers": {k: v for k, v in res.headers.items()
                                         if k in ("content-type", "date", "etag", "last-modified",
                                                  "server", "content-length")}})
        out["sha256"] = ing.hex
    if is_text(ctype) and len(res.body) <= INLINE_TEXT_LIMIT:
        out["text"] = res.body.decode(_charset(ctype), "replace")
    elif len(res.body) <= INLINE_BINARY_LIMIT:
        out["body_b64"] = base64.b64encode(res.body).decode()
    return out


# --------------------------------------------------------------------- parquet.query
def _parquet_query(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    """Read rows from a remote Parquet file by range requests, within hard byte/request/row-group/row budgets
    (parquet_scan.py). Same egress policy and per-host throttle as http.fetch; the rows returned are stored
    as one newline-delimited artifact whose acquisition context records what was read."""
    from astral import parquet_scan

    if ctx.manifest.footprint == "none":
        raise CapabilityError("a plugin declaring footprint 'none' may not use parquet.query")
    url = str(args.get("url") or "")
    try:
        egress.check_url(ctx.egress, url)
    except egress.EgressDenied as e:
        raise CapabilityError(str(e)) from e
    rl = ctx.manifest.rate_limit
    min_interval = (rl.window_seconds() / rl.requests) if rl else 0.0

    def get(start: int, end: int) -> tuple[int, dict[str, str], bytes]:
        try:
            res = ctx.host.fetcher.fetch(ctx.egress, url, headers={"Range": f"bytes={start}-{end}"}, min_interval=min_interval,
                                         max_bytes=end - start + 2)
        except egress.EgressDenied as e:
            raise CapabilityError(str(e)) from e
        except OSError as e:
            raise CapabilityError(f"network error: {type(e).__name__}: {e}") from e
        ctx.hosts_contacted[urllib.parse.urlsplit(url).hostname or "?"] += 1
        return res.status, res.headers, res.body

    started = __import__("time").monotonic()
    try:
        out = parquet_scan.query(get, url, columns=args.get("columns"), bbox=args.get("bbox"), bbox_column=str(args.get("bbox_column") or "bbox"),
                                 where=args.get("where"), limit=int(args.get("limit") or 100),
                                 max_row_groups=int(args.get("max_row_groups") or parquet_scan.MAX_ROW_GROUPS))
    except parquet_scan.ParquetError as e:
        raise CapabilityError(str(e)) from e
    out["elapsed_ms"] = int((__import__("time").monotonic() - started) * 1000)
    safe_url = ctx.redactor.url(url)
    out["final_url"] = safe_url
    if args.get("ingest", True):
        ing = ctx.store.ingest(ctx.conn, ctx.actor, ctx.case_id, parquet_scan.ndjson(out["rows"]), acquired_by=ctx.acquired_by,
                               media_type="application/x-ndjson", acquired_from=safe_url,
                               acquisition_ctx={"method": "GET+Range", "egress": ctx.egress.mode, "user_agent": ctx.egress.user_agent,
                                                "file_size": out["file_size"], "bytes_read": out["bytes_read"], "requests": out["requests"],
                                                "row_groups_read": out["row_groups_read"], "row_groups_total": out["row_groups_total"],
                                                "row_groups_skipped_by_statistics": out["row_groups_skipped_by_statistics"],
                                                "stopped_by": out["stopped_by"], "bbox": args.get("bbox"), "where": args.get("where")})
        out["sha256"] = ing.hex
    return out


# --------------------------------------------------------------------- ws.collect
MAX_WS_SECONDS, MAX_WS_MESSAGES, MAX_WS_BYTES = 30.0, 5000, 8 * 1024 * 1024


def _ws_collect(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    """Listen on a WebSocket for a BOUNDED time and keep what arrived: one subscription frame out, then messages in,
    stopping at whichever of the time, message-count or byte limit comes first. A plugin never opens its own socket;
    this goes through the same egress policy as http.fetch. The frame that is sent is NOT stored (it carries the
    API key); the messages received are stored as one newline-delimited artifact."""
    if ctx.manifest.footprint == "none":
        raise CapabilityError("a plugin declaring footprint 'none' may not use ws.collect")
    url = str(args.get("url") or "")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("ws", "wss"):
        raise CapabilityError("ws.collect needs a ws:// or wss:// URL")
    try:
        egress.check_url(ctx.egress, ("https" if parts.scheme == "wss" else "http") + url[len(parts.scheme):])
    except egress.EgressDenied as e:
        raise CapabilityError(str(e)) from e
    if ctx.egress.mode == "proxy":
        raise CapabilityError("WebSocket collection is not supported through a proxy egress; no connection was made")
    try:
        from websockets.sync.client import connect
    except ImportError as e:
        raise CapabilityError("ws.collect needs the 'websockets' package (pip install websockets)") from e
    import time

    seconds = min(float(args.get("max_seconds") or 10), MAX_WS_SECONDS)
    max_messages = min(int(args.get("max_messages") or 1000), MAX_WS_MESSAGES)
    max_bytes = min(int(args.get("max_bytes") or MAX_WS_BYTES), MAX_WS_BYTES)
    send = args.get("send_text")
    messages: list[str] = []
    size = 0
    reason = "time"
    started = time.monotonic()
    try:
        with connect(url, open_timeout=15, close_timeout=3, max_size=1 << 20, proxy=None,
                     additional_headers={"User-Agent": ctx.egress.user_agent}) as ws:
            if send is not None:
                ws.send(str(send))
            while True:
                left = seconds - (time.monotonic() - started)
                if left <= 0:
                    break
                try:
                    msg = ws.recv(timeout=left)
                except TimeoutError:
                    break
                text = msg.decode("utf-8", "replace") if isinstance(msg, (bytes, bytearray)) else str(msg)
                if size + len(text) > max_bytes:
                    reason = "bytes"
                    break
                messages.append(text)
                size += len(text)
                if len(messages) >= max_messages:
                    reason = "messages"
                    break
    except Exception as e:                                          # noqa: BLE001 - connection, TLS, protocol, close codes
        code = getattr(getattr(e, "response", None), "status_code", None)
        closed = getattr(getattr(e, "rcvd", None), "reason", None) or getattr(getattr(e, "rcvd", None), "code", None)
        detail = f"HTTP {code}" if code else (f"closed by the server ({closed})" if closed else f"{type(e).__name__}: {str(e)[:120]}")
        if messages:                                                # keep what arrived before the connection ended
            reason = f"connection ended early ({detail})"
        else:
            raise CapabilityError(f"WebSocket failed: {detail}") from e
    ctx.hosts_contacted[parts.hostname or "?"] += 1
    safe_url = ctx.redactor.url(url)
    body = "\n".join(messages).encode("utf-8")
    out: dict[str, Any] = {"count": len(messages), "stopped_by": reason, "truncated": reason in ("bytes", "messages"),
                           "elapsed_ms": int((time.monotonic() - started) * 1000), "final_url": safe_url}
    if args.get("ingest", True):
        ing = ctx.store.ingest(ctx.conn, ctx.actor, ctx.case_id, body, acquired_by=ctx.acquired_by, media_type="application/x-ndjson",
                               acquired_from=safe_url, acquisition_ctx={"method": "WS", "egress": ctx.egress.mode, "user_agent": ctx.egress.user_agent,
                                                                        "messages": len(messages), "stopped_by": reason, "listen_seconds": seconds})
        out["sha256"] = ing.hex
    if len(body) <= INLINE_TEXT_LIMIT:
        out["text"] = body.decode("utf-8", "replace")
    return out


def _charset(ctype: str) -> str:
    for part in ctype.split(";")[1:]:
        k, _, v = part.strip().partition("=")
        if k.lower() == "charset" and v:
            return v.strip("\"'")
    return "utf-8"


# --------------------------------------------------------------------- artifacts
def _artifact_put(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    if args.get("data_b64") is not None:
        data = base64.b64decode(args["data_b64"])
    elif args.get("text") is not None:
        data = str(args["text"]).encode("utf-8")
    else:
        raise CapabilityError("artifact.put needs data_b64 or text")
    parent = bytes.fromhex(args["parent_sha256"]) if args.get("parent_sha256") else None
    ing = ctx.store.ingest(
        ctx.conn, ctx.actor, ctx.case_id, data, acquired_by=ctx.acquired_by,
        media_type=args.get("media_type"), acquired_from=args.get("acquired_from"),
        acquisition_ctx=args.get("ctx") or {}, parent_sha256=parent,
        derivation=args.get("derivation"))
    return {"sha256": ing.hex, "new_blob": ing.new_blob, "byte_size": len(data)}


def _artifact_get(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    from astral.policy import PermissionDenied

    try:
        data = ctx.store.read(ctx.conn, ctx.actor, ctx.case_id, bytes.fromhex(args["sha256"]))
    except (PermissionDenied, KeyError, ValueError, FileNotFoundError) as e:
        raise CapabilityError("artifact not found in this case") from e
    row = ctx.conn.execute("SELECT media_type FROM artifact WHERE sha256 = %s",
                           (bytes.fromhex(args["sha256"]),)).fetchone()
    out: dict[str, Any] = {"media_type": row["media_type"] if row else None,
                           "byte_size": len(data)}
    if len(data) > INLINE_BINARY_LIMIT:
        raise CapabilityError("artifact is too large to return inline")
    if row and is_text(row["media_type"]):
        out["text"] = data.decode("utf-8", "replace")
    else:
        out["data_b64"] = base64.b64encode(data).decode()
    return out


def _artifact_path(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    """Local path of an artifact, for plugins that stream large bulk datasets.
    Only valid for in-process and subprocess runtimes (a container cannot see
    the host path), and the file is read-only (write-once blobs)."""
    from astral.policy import PermissionDenied

    try:
        sha = bytes.fromhex(args["sha256"])
        path = ctx.store.path_for(ctx.conn, ctx.actor, ctx.case_id, sha)
    except (PermissionDenied, KeyError, ValueError) as e:
        raise CapabilityError("artifact not found in this case") from e
    if not path.is_file():
        raise CapabilityError("artifact bytes are missing from the store")
    if args.get("verify") and not ctx.store.verify_file(sha):
        raise CapabilityError("artifact bytes do not match their SHA-256")
    row = ctx.conn.execute("SELECT media_type, byte_size FROM artifact WHERE sha256 = %s",
                           (sha,)).fetchone()
    return {"path": str(path), "media_type": row["media_type"], "byte_size": row["byte_size"]}


def default_capabilities() -> Capabilities:
    caps = Capabilities()
    caps.register("http.fetch", _http_fetch)
    caps.register("ws.collect", _ws_collect)
    caps.register("parquet.query", _parquet_query)
    caps.register("artifact.put", _artifact_put)
    caps.register("artifact.get", _artifact_get)
    caps.register("artifact.path", _artifact_path)
    from astral.defaults import register_extras   # late: defaults imports this module

    register_extras(caps)
    return caps
