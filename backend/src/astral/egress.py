"""Egress: the one place core code leaves the machine on a plugin's behalf.

Plugins do not open sockets of their own; they ask for the `http.fetch`
capability (capabilities.py) and core performs the request under the case's
collection identity. That makes the egress mode enforceable rather than
advisory (10 §2) and lets every request be summarised in the audit log.

Modes (stored in `case_.collection_identity["egress"]`):

  direct      requests go out as this host (the solo default: no friction)
  proxy       requests go through `collection_identity["proxy"]`
  local-only  nothing leaves the machine; loopback / private addresses only

The audit log records the *host* and request count, never the URL path or
query: the log is immutable, and a path can identify a person (see evidence.py).
"""

from __future__ import annotations

import ipaddress
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_UA = "AstralLens/0.1 (+https://github.com/; self-hosted investigation platform)"
MAX_BODY = 50 * 1024 * 1024
SLEEP = time.sleep          # indirection so tests can record waits instead of sleeping them
CLOCK = time.monotonic     # ...and a clock they can drive, so recorded waits do not depend on wall-clock speed


class EgressDenied(Exception):
    pass


@dataclass(frozen=True)
class EgressPolicy:
    mode: str = "direct"
    proxy: str | None = None
    user_agent: str = DEFAULT_UA

    @classmethod
    def from_identity(cls, identity: dict[str, Any] | None) -> "EgressPolicy":
        identity = identity or {}
        mode = identity.get("egress", "direct")
        if mode not in ("direct", "proxy", "local-only"):
            raise EgressDenied(f"unknown egress mode {mode!r}")
        if mode == "proxy" and not identity.get("proxy"):
            raise EgressDenied("egress mode 'proxy' needs collection_identity.proxy")
        return cls(mode, identity.get("proxy"), identity.get("user_agent") or DEFAULT_UA)


def is_local_host(host: str) -> bool:
    """Loopback, private and link-local addresses, and `localhost` names."""
    host = host.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:                                    # a name: resolve and require all-local
            infos = socket.getaddrinfo(host, None)
        except OSError:
            return False
        return bool(infos) and all(
            _local_ip(ipaddress.ip_address(i[4][0])) for i in infos)
    return _local_ip(ip)


def _local_ip(ip: ipaddress._BaseAddress) -> bool:
    return ip.is_loopback or ip.is_private or ip.is_link_local


@dataclass
class FetchResult:
    status: int
    headers: dict[str, str]
    body: bytes
    final_url: str
    elapsed_ms: int
    truncated: bool = False
    redirects: list[str] = field(default_factory=list)


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, policy: EgressPolicy, trail: list[str]) -> None:
        self.policy, self.trail = policy, trail

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(self.policy, newurl)           # a redirect cannot leave local-only
        self.trail.append(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_url(policy: EgressPolicy, url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise EgressDenied(f"scheme {parts.scheme!r} is not allowed")
    if not parts.hostname:
        raise EgressDenied("url has no host")
    if policy.mode == "local-only" and not is_local_host(parts.hostname):
        raise EgressDenied(
            f"egress mode is local-only; refusing to contact {parts.hostname}")
    return parts.hostname


class Fetcher:
    """urllib-based HTTP client: policy-checked, rate-limited per host, honours
    429 / Retry-After. Stdlib only, so the small tier needs nothing extra."""

    def __init__(self, *, timeout: float = 30.0, max_retries: int = 2,
                 max_retry_wait: float = 20.0) -> None:
        self.timeout, self.max_retries, self.max_retry_wait = timeout, max_retries, max_retry_wait
        self._last: dict[str, float] = {}

    def fetch(
        self, policy: EgressPolicy, url: str, *, method: str = "GET",
        headers: dict[str, str] | None = None, body: bytes | None = None,
        min_interval: float = 0.0, max_bytes: int = MAX_BODY,
    ) -> FetchResult:
        host = check_url(policy, url)
        hdrs = {"User-Agent": policy.user_agent, "Accept-Encoding": "identity"}
        hdrs.update(headers or {})
        attempt = 0
        while True:
            self._throttle(host, min_interval)
            trail: list[str] = []
            handlers: list[Any] = [_GuardedRedirect(policy, trail)]
            if policy.mode == "proxy":
                handlers.append(urllib.request.ProxyHandler(
                    {"http": policy.proxy, "https": policy.proxy}))
            elif policy.mode == "local-only" or is_local_host(host):
                handlers.append(urllib.request.ProxyHandler({}))      # never via a system proxy
            opener = urllib.request.build_opener(*handlers)
            req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
            started = time.monotonic()
            try:
                with opener.open(req, timeout=self.timeout) as resp:
                    return self._result(resp, resp.status, url, started, max_bytes, trail)
            except urllib.error.HTTPError as e:
                if e.code in (429, 503) and attempt < self.max_retries:
                    wait = _retry_after(e.headers.get("Retry-After"), self.max_retry_wait)
                    attempt += 1
                    SLEEP(wait)
                    continue
                return self._result(e, e.code, url, started, max_bytes, trail)

    def _throttle(self, host: str, min_interval: float) -> None:
        if min_interval > 0:
            wait = self._last.get(host, 0.0) + min_interval - CLOCK()
            if wait > 0:
                SLEEP(wait)
        self._last[host] = CLOCK()

    @staticmethod
    def _result(resp: Any, status: int, url: str, started: float, max_bytes: int,
                trail: list[str]) -> FetchResult:
        data = resp.read(max_bytes + 1)
        headers = {k.lower(): v for k, v in resp.headers.items()}
        truncated = len(data) > max_bytes
        if headers.get("content-encoding", "").strip().lower() == "gzip":
            # Only a connector that asked for gzip gets it (the default request says identity). The decoded size
            # is bounded by max_bytes, so a compression bomb cannot exhaust memory; the stored artifact is the
            # decoded body, and the header says so.
            import zlib

            d = zlib.decompressobj(16 + zlib.MAX_WBITS)
            try:
                data = d.decompress(data, max_bytes + 1)
            except zlib.error:
                data = b""
                truncated = True
            truncated = truncated or len(data) > max_bytes or bool(d.unconsumed_tail)
            headers["x-astral-decoded"] = "gzip"
        return FetchResult(
            status=status,
            headers=headers,
            body=data[:max_bytes], final_url=getattr(resp, "url", None) or url,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            truncated=truncated, redirects=trail,
        )


def _retry_after(value: str | None, cap: float) -> float:
    try:
        return max(0.0, min(float(value), cap)) if value else 1.0
    except ValueError:
        return 1.0
