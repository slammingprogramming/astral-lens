"""Evidence-grade web capture (M1; the gap doc 09 / 04 identify in open source).

A capture is a WARC 1.1 file (one gzip member per record: warcinfo, request,
response) plus a capture log. The WARC is ingested into the evidence store, so
its SHA-256 is the per-capture hash, and the payload is stored again as a
derived artifact so it can be parsed and cited on its own.

What this is NOT, stated plainly:
* It does not execute JavaScript. A page that renders its content client-side
  is captured as the server sent it. A headless-browser capture backend is a
  later addition; until then the capture log says `rendering: none`.
* Response headers are reconstructed from the parsed header list (urllib does
  not expose the raw bytes), so original header casing and order are not
  preserved. The warcinfo record says so.
* A hash proves these bytes have not changed since capture. It does not prove
  the server sent them; anti-forgery needs a third-party timestamp (RFC 3161),
  which is an M5 exhibit-bundle feature.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

TOOL = "astral-lens-capture/1"


def _b32(digest: bytes) -> str:
    return "sha256:" + base64.b32encode(digest).decode()


def _record(fields: list[tuple[str, str]], block: bytes) -> bytes:
    head = "WARC/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in fields)
    head += f"Content-Length: {len(block)}\r\n\r\n"
    return gzip.compress(head.encode("utf-8") + block + b"\r\n\r\n")


def _rid() -> str:
    return f"<urn:uuid:{uuid.uuid4()}>"


def _now(ts: datetime | None = None) -> str:
    return (ts or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_warc(*, url: str, status: int, response_headers: dict[str, str],
               request_headers: dict[str, str], body: bytes, captured_at: datetime | None = None,
               operator: str | None = None, egress_mode: str = "direct") -> bytes:
    when = _now(captured_at)
    info_rid = _rid()
    info = (f"software: {TOOL}\r\nformat: WARC File Format 1.1\r\n"
            f"description: single-URL evidence capture\r\n"
            f"rendering: none (no JavaScript executed)\r\n"
            f"egress: {egress_mode}\r\n"
            + (f"operator: {operator}\r\n" if operator else "")
            + "note: response headers reconstructed from the parsed header list\r\n").encode()
    parts = [_record([("WARC-Type", "warcinfo"), ("WARC-Date", when), ("WARC-Record-ID", info_rid),
                      ("Content-Type", "application/warc-fields"),
                      ("WARC-Block-Digest", _b32(hashlib.sha256(info).digest()))], info)]

    path = url.split("://", 1)[-1]
    path = "/" + path.split("/", 1)[1] if "/" in path else "/"
    host = url.split("://", 1)[-1].split("/", 1)[0]
    req_hdrs = {"Host": host, **request_headers}
    req = (f"GET {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in req_hdrs.items())
           + "\r\n").encode()
    req_rid = _rid()
    parts.append(_record([("WARC-Type", "request"), ("WARC-Target-URI", url), ("WARC-Date", when),
                          ("WARC-Record-ID", req_rid), ("WARC-Concurrent-To", _rid()),
                          ("Content-Type", "application/http;msgtype=request"),
                          ("WARC-Block-Digest", _b32(hashlib.sha256(req).digest()))], req))

    reason = {200: "OK", 301: "Moved Permanently", 302: "Found", 304: "Not Modified",
              403: "Forbidden", 404: "Not Found", 500: "Internal Server Error"}.get(status, "")
    resp = (f"HTTP/1.1 {status} {reason}\r\n"
            + "".join(f"{k}: {v}\r\n" for k, v in response_headers.items())
            + "\r\n").encode() + body
    parts.append(_record([("WARC-Type", "response"), ("WARC-Target-URI", url), ("WARC-Date", when),
                          ("WARC-Record-ID", _rid()),
                          ("Content-Type", "application/http;msgtype=response"),
                          ("WARC-Block-Digest", _b32(hashlib.sha256(resp).digest())),
                          ("WARC-Payload-Digest", _b32(hashlib.sha256(body).digest())),
                          ("X-Astral-Payload-Sha256", hashlib.sha256(body).hexdigest())], resp))
    return b"".join(parts)


@dataclass
class WarcRecord:
    headers: dict[str, str]
    block: bytes


def read_warc(data: bytes) -> list[WarcRecord]:
    raw = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
    out, pos = [], 0
    while pos < len(raw):
        end = raw.index(b"\r\n\r\n", pos)
        head = raw[pos:end].decode("utf-8")
        lines = head.split("\r\n")
        if not lines[0].startswith("WARC/"):
            raise ValueError(f"not a WARC record at offset {pos}")
        hdrs = {}
        for ln in lines[1:]:
            k, _, v = ln.partition(":")
            hdrs[k.strip()] = v.strip()
        n = int(hdrs["Content-Length"])
        start = end + 4
        out.append(WarcRecord(hdrs, raw[start:start + n]))
        pos = start + n + 4
    return out


def verify_warc(data: bytes) -> dict[str, Any]:
    """Recompute every block and payload digest. Returns {ok, records, problems}."""
    problems, recs = [], read_warc(data)
    for i, r in enumerate(recs):
        want = r.headers.get("WARC-Block-Digest")
        if want and want != _b32(hashlib.sha256(r.block).digest()):
            problems.append(f"record {i} ({r.headers.get('WARC-Type')}): block digest mismatch")
        pd = r.headers.get("WARC-Payload-Digest")
        if pd and r.headers.get("WARC-Type") == "response":
            body = r.block.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in r.block else b""
            if pd != _b32(hashlib.sha256(body).digest()):
                problems.append(f"record {i}: payload digest mismatch")
    return {"ok": not problems, "records": len(recs), "problems": problems}


def payload_of(data: bytes) -> bytes:
    for r in read_warc(data):
        if r.headers.get("WARC-Type") == "response":
            return r.block.split(b"\r\n\r\n", 1)[1]
    raise ValueError("no response record")
