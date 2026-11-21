"""parquet.query: read rows out of a REMOTE Parquet file by HTTP range requests, within hard budgets.

Overture Maps and similar open datasets are published only as very large Parquet files. A plugin cannot
(and must not) download them whole, nor open sockets of its own, so the host offers one bounded operation:
read the file footer, skip every row group the column statistics prove cannot match the bounding box, read
only the requested columns of the rest, filter the rows, and return at most `limit` of them.

Everything goes through the same egress policy and per-host throttle as http.fetch (Fetcher.fetch). Hard
limits, not defaults a caller can raise: MAX_BYTES total bytes read, MAX_REQUESTS range requests,
MAX_ROW_GROUPS row groups read, MAX_ROWS rows returned. Hitting one stops the read and the answer says so
(`stopped_by`, `truncated`); a plugin must then report the area as not fully covered, never as empty.

What the answer proves. `rows` are rows of that file as served, with the byte ranges read recorded in the
stored artifact's acquisition context. Statistics pruning is an optimisation that can only skip a row group
whose recorded min/max rule it out; a file whose statistics are missing is read, not skipped.
pyarrow (Apache-2.0) is an optional dependency (extra `parquet`).
"""

from __future__ import annotations

import datetime
import decimal
import io
import json
import math
from typing import Any

MAX_BYTES = 48 * 1024 * 1024
MAX_REQUESTS = 120
MAX_ROW_GROUPS = 40
MAX_ROWS = 500
FOOTER_GUESS = 256 * 1024
MERGE_GAP = 256 * 1024                      # byte ranges closer than this are fetched as one request


class ParquetError(Exception):
    pass


class RangeFile(io.RawIOBase):
    """A read-only, seekable view of a remote file made of Range requests, with a byte and request budget."""

    def __init__(self, get: Any, url: str, *, max_bytes: int, max_requests: int) -> None:
        self._get, self.url = get, url
        self.max_bytes, self.max_requests = max_bytes, max_requests
        self.bytes_read = self.requests = 0
        self._pos = 0
        self._cache: list[tuple[int, bytes]] = []
        self.size = self._probe()

    # -- budget-checked transport
    def _range(self, start: int, end: int) -> bytes:                      # inclusive end
        if self.requests >= self.max_requests:
            raise ParquetError("request budget reached")
        if self.bytes_read + (end - start + 1) > self.max_bytes:
            raise ParquetError("byte budget reached")
        self.requests += 1
        status, headers, body = self._get(start, end)
        if status == 206:
            pass
        elif status == 200 and start == 0 and len(body) <= end + 1:
            pass                                                           # a file smaller than the range: the whole file
        else:
            raise ParquetError(f"HTTP {status}: the server did not honour a range request, so the file cannot be read in parts")
        self.bytes_read += len(body)
        return body

    def _probe(self) -> int:
        if self.requests >= self.max_requests:
            raise ParquetError("request budget reached")
        self.requests += 1
        status, headers, body = self._get(0, 0)
        if status == 206:
            cr = headers.get("content-range", "")
            try:
                size = int(cr.rsplit("/", 1)[1])
            except (IndexError, ValueError) as e:
                raise ParquetError("the server's Content-Range did not give the file size") from e
            self.bytes_read += len(body)
            return size
        if status == 200 and len(body) <= 1:
            self.bytes_read += len(body)
            return len(body)
        raise ParquetError(f"HTTP {status}: the server did not honour a range request, so the file cannot be read in parts")

    # -- prefetch whole column-chunk ranges in few requests
    def prefetch(self, ranges: list[tuple[int, int]]) -> None:
        merged: list[list[int]] = []
        for a, b in sorted(ranges):
            if merged and a - merged[-1][1] <= MERGE_GAP:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        for a, b in merged:
            if not self._cached(a, b - a):
                self._cache.append((a, self._range(a, b - 1)))

    def _cached(self, pos: int, n: int) -> bytes | None:
        for start, data in self._cache:
            if start <= pos and pos + n <= start + len(data):
                return data[pos - start:pos - start + n]
        return None

    # -- io.RawIOBase
    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = 0) -> int:
        self._pos = offset if whence == 0 else (self._pos + offset if whence == 1 else self.size + offset)
        return self._pos

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self.size - self._pos
        n = max(0, min(n, self.size - self._pos))
        if n == 0:
            return b""
        hit = self._cached(self._pos, n)
        if hit is None:
            # the footer is read from the end: take a generous tail once so the metadata needs one request
            if self._pos + n >= self.size - 8 and n < FOOTER_GUESS:
                start = max(0, self.size - FOOTER_GUESS)
                data = self._range(start, self.size - 1)
                self._cache.append((start, data))
                hit = self._cached(self._pos, n)
            if hit is None:
                hit = self._range(self._pos, self._pos + n - 1)
        self._pos += len(hit)
        return hit

    def readinto(self, b: Any) -> int:
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)


def jsonable(v: Any) -> Any:
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).hex()                                              # e.g. WKB geometry: hex, decoded by the caller
    if isinstance(v, float):
        return None if math.isnan(v) or math.isinf(v) else v
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    return v


def _stat(rg: Any, name: str) -> tuple[Any, Any] | None:
    for i in range(rg.num_columns):
        col = rg.column(i)
        if col.path_in_schema == name:
            st = col.statistics
            if st is not None and st.has_min_max:
                return st.min, st.max
            return None
    return None


def _may_match(rg: Any, box: dict[str, float], col: str) -> bool:
    """False only when the recorded statistics PROVE no row in the group can intersect the box."""
    xmin, xmax = _stat(rg, f"{col}.xmin"), _stat(rg, f"{col}.xmax")
    ymin, ymax = _stat(rg, f"{col}.ymin"), _stat(rg, f"{col}.ymax")
    if xmin and xmin[0] > box["xmax"]:
        return False
    if xmax and xmax[1] < box["xmin"]:
        return False
    if ymin and ymin[0] > box["ymax"]:
        return False
    if ymax and ymax[1] < box["ymin"]:
        return False
    return True


def query(get: Any, url: str, *, columns: list[str] | None = None, bbox: dict[str, float] | None = None, bbox_column: str = "bbox",
          where: dict[str, Any] | None = None, limit: int = 100, max_row_groups: int = MAX_ROW_GROUPS,
          max_bytes: int = MAX_BYTES, max_requests: int = MAX_REQUESTS) -> dict[str, Any]:
    """`get(start, end) -> (status, headers, body)` performs one range request through the host's policy."""
    try:
        import pyarrow.compute as pc
        import pyarrow.parquet as pq
    except ImportError as e:
        raise ParquetError("parquet.query needs the 'pyarrow' package (pip install pyarrow)") from e
    limit = max(1, min(int(limit), MAX_ROWS))
    max_row_groups = max(1, min(int(max_row_groups), MAX_ROW_GROUPS))
    max_bytes, max_requests = min(int(max_bytes), MAX_BYTES), min(int(max_requests), MAX_REQUESTS)
    f = RangeFile(get, url, max_bytes=max_bytes, max_requests=max_requests)
    stopped = None
    rows: list[dict[str, Any]] = []
    read = skipped = 0
    total: int | None = None
    try:
        pf = pq.ParquetFile(f)
        md = pf.metadata
        total = md.num_row_groups
        names = set(pf.schema_arrow.names)
        want = list(columns or names)
        for c in want + ([bbox_column] if bbox else []) + list(where or {}):
            if c not in names:
                raise ParquetError(f"the file has no column {c!r} (columns: {sorted(names)[:30]})")
        need = sorted(set(want) | ({bbox_column} if bbox else set()) | set(where or {}))
        box = {k: float(bbox[k]) for k in ("xmin", "ymin", "xmax", "ymax")} if bbox else None
        for i in range(md.num_row_groups):
            rg = md.row_group(i)
            if box and not _may_match(rg, box, bbox_column):
                skipped += 1
                continue
            if read >= max_row_groups:
                stopped = "row_groups"
                break
            ranges = []
            for j in range(rg.num_columns):
                col = rg.column(j)
                top = col.path_in_schema.split(".")[0]
                if top in need:
                    start = col.dictionary_page_offset if col.has_dictionary_page and col.dictionary_page_offset else col.data_page_offset
                    ranges.append((start, start + col.total_compressed_size))
            f.prefetch(ranges)
            table = pf.read_row_group(i, columns=need)
            read += 1
            mask = None
            if box:
                m = ((pc.field(bbox_column, "xmin") <= box["xmax"]) & (pc.field(bbox_column, "xmax") >= box["xmin"])
                     & (pc.field(bbox_column, "ymin") <= box["ymax"]) & (pc.field(bbox_column, "ymax") >= box["ymin"]))
                mask = m
            for k, v in (where or {}).items():
                eq = pc.field(k) == v
                mask = eq if mask is None else (mask & eq)
            if mask is not None:
                table = table.filter(mask)
            if table.num_rows:
                part = table.select(want).slice(0, limit - len(rows)).to_pylist()
                rows.extend(jsonable(r) for r in part)
            if len(rows) >= limit:
                stopped = "limit"
                break
    except ParquetError as e:
        if "budget" not in str(e):
            raise
        stopped = "bytes" if "byte" in str(e) else "requests"
    except OSError as e:
        raise ParquetError(f"{type(e).__name__}: {str(e)[:200]}") from e
    except Exception as e:                                                  # noqa: BLE001 - pyarrow raises ArrowInvalid etc. for a damaged file
        if stopped is None and e.__class__.__module__.startswith("pyarrow"):
            raise ParquetError(f"not a readable Parquet file: {str(e)[:200]}") from e
        raise
    return {"rows": rows, "row_groups_total": total, "row_groups_read": read,
            "row_groups_skipped_by_statistics": skipped, "bytes_read": f.bytes_read, "requests": f.requests,
            "file_size": f.size, "stopped_by": stopped or "end_of_file", "truncated": stopped in ("bytes", "requests", "row_groups", "limit"),
            "complete": stopped is None}


def ndjson(rows: list[dict[str, Any]]) -> bytes:
    return ("\n".join(json.dumps(r, sort_keys=True, separators=(",", ":")) for r in rows)).encode("utf-8")
