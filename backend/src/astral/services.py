"""Dedicated search and vector services (the "cluster" tier, docs/02): OpenSearch for text and Qdrant
for face embeddings, as REBUILDABLE PROJECTIONS of what Postgres holds.

Postgres stays the source of truth and the default. These services exist for scale, and they follow the
project's rules:

  * Derived, never authoritative. Everything in them can be dropped and rebuilt from Postgres by `sync`.
  * Degradation is visible (invariant 4). If a service is unreachable the caller falls back to Postgres
    and the result says so; it never silently answers from nothing.
  * Face embeddings are biometric data. Copying them to Qdrant widens where they live, so removal is
    reconciled as strictly as insertion: `sync` deletes points whose face row is gone (retention expiry,
    a purge, an erased case), erasure and purge call the same reconciliation, and a failure to delete is
    surfaced to the operator rather than swallowed.
  * Face search through Qdrant requests EXACT (not approximate) search, so it ranks the same as the
    Postgres path. The score is a similarity, never a verdict.
  * No credentials are stored here; endpoints and any basic-auth value come from configuration/secrets.

Qdrant (API key) and OpenSearch (Basic auth, security plugin on) have been run here over TLS with a private CA
from ASTRAL_SERVICES_CA_FILE. Untested: clusters of more than one node, mutual TLS, role-limited users, and
behaviour at corpus sizes where approximate indexes would matter.
"""

from __future__ import annotations

import base64
import functools
import json
import os
import re
import ssl
import urllib.error
import urllib.request
import uuid
from typing import Any, Iterable

import psycopg

TIMEOUT = 30.0
BATCH = 200


class ServiceError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a: Any, **k: Any) -> None:
        return None


@functools.lru_cache(maxsize=4)
def _tls_context(ca_file: str | None) -> ssl.SSLContext:
    # A service behind a private CA is trusted by naming that CA's file; verification is never switched off.
    return ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()


def _opener() -> urllib.request.OpenerDirector:
    """No redirects (a service that redirects is not followed); https verified against the system CAs, or against
    ASTRAL_SERVICES_CA_FILE when the services use a private CA."""
    return urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=_tls_context(os.environ.get("ASTRAL_SERVICES_CA_FILE") or None)))


def _call(method: str, url: str, body: Any = None, *, auth: str | None = None, ndjson: bool = False, ok: tuple[int, ...] = (200, 201)) -> Any:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = body.encode() if ndjson else json.dumps(body).encode()
        headers["Content-Type"] = "application/x-ndjson" if ndjson else "application/json"
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _opener().open(req, timeout=TIMEOUT) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except OSError as e:
        raise ServiceError(f"{url}: {type(e).__name__}: {e}") from e
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        parsed = {"raw": raw[:200].decode("utf-8", "replace")}
    if status not in ok:
        raise ServiceError(f"{method} {url.split('?')[0]} -> HTTP {status}: {str(parsed)[:200]}")
    return parsed


# ------------------------------------------------------------------ OpenSearch (text)
class OpenSearchService:
    name = "opensearch"

    def __init__(self, url: str, *, index: str = "astral-chunks", auth: str | None = None) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,60}", index):
            raise ServiceError("index must be lowercase letters, digits, '-' or '_'")
        self.url, self.index, self.auth = url.rstrip("/"), index, auth

    def ping(self) -> dict[str, Any]:
        info = _call("GET", self.url, auth=self.auth)
        return {"version": (info.get("version") or {}).get("number"), "distribution": (info.get("version") or {}).get("distribution")}

    def ensure_index(self) -> None:
        r = _call("PUT", f"{self.url}/{self.index}", {
            "settings": {"index": {"number_of_shards": 1, "number_of_replicas": 0}},
            "mappings": {"properties": {"case_id": {"type": "keyword"}, "chunk_id": {"type": "long"}, "text_sha": {"type": "keyword"},
                                         "body": {"type": "text"}}}}, auth=self.auth, ok=(200, 400))
        if r.get("status") == 400 and "resource_already_exists" not in json.dumps(r):
            raise ServiceError(f"could not create index: {str(r)[:200]}")

    def index_chunks(self, case_id: uuid.UUID, rows: Iterable[dict[str, Any]]) -> int:
        lines, n = [], 0
        for r in rows:
            lines.append(json.dumps({"index": {"_index": self.index, "_id": f"{case_id}:{r['id']}"}}))
            lines.append(json.dumps({"case_id": str(case_id), "chunk_id": r["id"], "text_sha": bytes(r["text_sha"]).hex(), "body": r["body"]}))
            n += 1
            if n % BATCH == 0:
                self._bulk(lines)
                lines = []
        if lines:
            self._bulk(lines)
        return n

    def _bulk(self, lines: list[str]) -> None:
        r = _call("POST", f"{self.url}/_bulk?refresh=wait_for", "\n".join(lines) + "\n", auth=self.auth, ndjson=True)
        if r.get("errors"):
            first = next((i["index"]["error"] for i in r.get("items", []) if i.get("index", {}).get("error")), "unknown")
            raise ServiceError(f"bulk indexing reported errors: {str(first)[:200]}")

    def search(self, case_id: uuid.UUID, query: str, k: int = 50) -> list[int]:
        r = _call("POST", f"{self.url}/{self.index}/_search", {
            "size": k, "_source": ["chunk_id"],
            "query": {"bool": {"filter": [{"term": {"case_id": str(case_id)}}], "must": [{"match": {"body": {"query": query, "operator": "or"}}}]}}},
            auth=self.auth)
        return [int(h["_source"]["chunk_id"]) for h in (r.get("hits") or {}).get("hits", [])]

    def delete_case(self, case_id: uuid.UUID) -> int:
        r = _call("POST", f"{self.url}/{self.index}/_delete_by_query?refresh=true&conflicts=proceed",
                  {"query": {"term": {"case_id": str(case_id)}}}, auth=self.auth)
        return int(r.get("deleted", 0))

    def count(self, case_id: uuid.UUID) -> int:
        return int(_call("POST", f"{self.url}/{self.index}/_count", {"query": {"term": {"case_id": str(case_id)}}}, auth=self.auth).get("count", 0))


# ------------------------------------------------------------------ Qdrant (face vectors)
class QdrantService:
    name = "qdrant"

    def __init__(self, url: str, *, exact: bool = True, api_key: str | None = None) -> None:
        self.url, self.exact, self.api_key = url.rstrip("/"), exact, api_key

    def _c(self, method: str, path: str, body: Any = None, ok: tuple[int, ...] = (200,)) -> Any:
        # Qdrant's api-key header is not Basic auth; handled inline to keep _call simple.
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["api-key"] = self.api_key
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with _opener().open(req, timeout=TIMEOUT) as r:
                raw, status = r.read(), r.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        except OSError as e:
            raise ServiceError(f"{self.url}{path}: {type(e).__name__}: {e}") from e
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {"raw": raw[:200].decode("utf-8", "replace")}
        if status not in ok:
            raise ServiceError(f"{method} {path} -> HTTP {status}: {str(parsed)[:200]}")
        return parsed

    def ping(self) -> dict[str, Any]:
        r = self._c("GET", "/")
        # Qdrant serves "/" without an API key, so on its own it says nothing about whether our key is accepted. An
        # authenticated read makes a missing or wrong key show up here, in the status, rather than at the first search.
        self._c("GET", "/collections")
        return {"version": r.get("version"), "title": r.get("title")}

    @staticmethod
    def collection(model_id: str, dim: int, kind: str = "faces") -> str:
        return f"{kind}_{re.sub(r'[^a-z0-9]', '_', model_id.lower())}_{int(dim)}"

    def collections(self) -> list[str]:
        return [c["name"] for c in self._c("GET", "/collections")["result"]["collections"]]

    def ensure_collection(self, model_id: str, dim: int, kind: str = "faces") -> str:
        name = self.collection(model_id, dim, kind)
        r = self._c("GET", f"/collections/{name}", ok=(200, 404))
        if r.get("status") != "ok" and "result" not in r:
            self._c("PUT", f"/collections/{name}", {"vectors": {"size": int(dim), "distance": "Cosine"}})
        return name

    def upsert_faces(self, model_id: str, rows: list[dict[str, Any]]) -> int:
        by_dim: dict[int, list[dict[str, Any]]] = {}
        for r in rows:
            by_dim.setdefault(len(r["embedding"]), []).append(r)
        n = 0
        for dim, group in by_dim.items():
            name = self.ensure_collection(model_id, dim)
            for i in range(0, len(group), BATCH):
                pts = [{"id": str(r["id"]), "vector": [float(x) for x in r["embedding"]],
                        "payload": {"case_id": str(r["case_id"]), "artifact": bytes(r["artifact_sha"]).hex(), "watchlist": r["watchlist"] is not None}}
                       for r in group[i:i + BATCH]]
                self._c("PUT", f"/collections/{name}/points?wait=true", {"points": pts})
                n += len(pts)
        return n

    def upsert_chunks(self, model_id: str, case_id: uuid.UUID, rows: list[dict[str, Any]]) -> int:
        """Dense text vectors. The point id is the chunk id; the payload is only the case, never the text."""
        by_dim: dict[int, list[dict[str, Any]]] = {}
        for r in rows:
            by_dim.setdefault(len(r["embedding"]), []).append(r)
        n = 0
        for dim, group in by_dim.items():
            name = self.ensure_collection(model_id, dim, "chunks")
            for i in range(0, len(group), BATCH):
                pts = [{"id": int(r["id"]), "vector": [float(x) for x in r["embedding"]], "payload": {"case_id": str(case_id)}}
                       for r in group[i:i + BATCH]]
                self._c("PUT", f"/collections/{name}/points?wait=true", {"points": pts})
                n += len(pts)
        return n

    def search_chunks(self, model_id: str, case_id: uuid.UUID, vector: list[float], *, k: int = 50) -> list[int]:
        """Chunk ids, most similar first. EXACT search, so it ranks like the Postgres path."""
        name = self.collection(model_id, len(vector), "chunks")
        r = self._c("POST", f"/collections/{name}/points/search", {
            "vector": [float(x) for x in vector], "limit": k, "with_payload": False,
            "filter": {"must": [{"key": "case_id", "match": {"value": str(case_id)}}]}, "params": {"exact": self.exact}}, ok=(200, 404))
        if "result" not in r:
            return []
        return [int(p["id"]) for p in r["result"]]

    def search(self, model_id: str, case_id: uuid.UUID, vector: list[float], *, k: int = 10, scope: str = "all",
               exclude_artifact: bytes | None = None, exclude_face: uuid.UUID | None = None) -> list[dict[str, Any]]:
        name = self.collection(model_id, len(vector))
        must = [{"key": "case_id", "match": {"value": str(case_id)}}]
        if scope == "watchlist":
            must.append({"key": "watchlist", "match": {"value": True}})
        elif scope == "case":
            must.append({"key": "watchlist", "match": {"value": False}})
        must_not: list[dict[str, Any]] = []
        if exclude_artifact is not None:
            must_not.append({"key": "artifact", "match": {"value": exclude_artifact.hex()}})
        if exclude_face is not None:
            must_not.append({"has_id": [str(exclude_face)]})
        r = self._c("POST", f"/collections/{name}/points/search", {"vector": [float(x) for x in vector], "limit": k, "with_payload": False,
                                                                  "filter": {"must": must, "must_not": must_not}, "params": {"exact": self.exact}}, ok=(200, 404))
        if "result" not in r:                                         # collection absent: nothing indexed for this model/dim yet
            return []
        return [{"id": p["id"], "score": float(p["score"])} for p in r["result"]]

    def point_ids(self, model_id: str, dim: int, case_id: uuid.UUID, kind: str = "faces") -> set[str]:
        name = self.collection(model_id, dim, kind)
        out: set[str] = set()
        offset: Any = None
        while True:
            body: dict[str, Any] = {"filter": {"must": [{"key": "case_id", "match": {"value": str(case_id)}}]}, "limit": 500, "with_payload": False, "with_vector": False}
            if offset is not None:
                body["offset"] = offset
            r = self._c("POST", f"/collections/{name}/points/scroll", body, ok=(200, 404))
            if "result" not in r:
                return out
            out |= {str(p["id"]) for p in r["result"]["points"]}
            offset = r["result"].get("next_page_offset")
            if offset is None:
                return out

    def delete_points(self, model_id: str, dim: int, ids: list[Any], kind: str = "faces") -> None:
        for i in range(0, len(ids), BATCH):
            self._c("POST", f"/collections/{self.collection(model_id, dim, kind)}/points/delete?wait=true", {"points": ids[i:i + BATCH]}, ok=(200, 404))

    def delete_case(self, case_id: uuid.UUID) -> int:
        n = 0
        for name in self.collections():
            if name.startswith(("faces_", "chunks_")):                 # biometric vectors and dense text vectors
                self._c("POST", f"/collections/{name}/points/delete?wait=true",
                        {"filter": {"must": [{"key": "case_id", "match": {"value": str(case_id)}}]}})
                n += 1
        return n


# ------------------------------------------------------------------ keeping them in step with Postgres
def sync_text(conn: psycopg.Connection, svc: OpenSearchService, case_id: uuid.UUID) -> dict[str, Any]:
    svc.ensure_index()
    cur = conn.execute("SELECT cursor FROM service_sync WHERE case_id=%s AND service=%s AND kind='text'", (case_id, svc.name)).fetchone()
    last = int(cur["cursor"]) if cur else 0
    rows = conn.execute("SELECT id, text_sha, body FROM text_chunk WHERE case_id=%s AND id > %s ORDER BY id", (case_id, last)).fetchall()
    n = svc.index_chunks(case_id, rows) if rows else 0
    top = max([int(r["id"]) for r in rows], default=last)
    conn.execute("INSERT INTO service_sync (case_id, service, kind, cursor) VALUES (%s,%s,'text',%s)"
                 " ON CONFLICT (case_id, service, kind) DO UPDATE SET cursor=EXCLUDED.cursor, synced_at=now()", (case_id, svc.name, top))
    return {"indexed": n, "total_in_service": svc.count(case_id)}


def face_fingerprint(conn: psycopg.Connection, case_id: uuid.UUID) -> str:
    r = conn.execute("SELECT md5(coalesce(string_agg(id::text, ',' ORDER BY id), '')) AS f FROM face WHERE case_id=%s", (case_id,)).fetchone()
    return r["f"]


def sync_faces(conn: psycopg.Connection, svc: QdrantService, case_id: uuid.UUID) -> dict[str, Any]:
    """Make the service hold exactly the faces Postgres holds for this case: add the missing, DELETE the gone."""
    rows = conn.execute("SELECT id, case_id, artifact_sha, watchlist, model_id, embedding FROM face WHERE case_id=%s", (case_id,)).fetchall()
    groups: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault((r["model_id"], len(r["embedding"])), []).append(r)
    added = removed = 0
    for key in set(groups) | set(_models_in_service(svc)):
        have = svc.point_ids(key[0], key[1], case_id)
        want = {str(r["id"]) for r in groups.get(key, [])}
        missing = [r for r in groups.get(key, []) if str(r["id"]) not in have]
        if missing:
            added += svc.upsert_faces(key[0], missing)
        extra = sorted(have - want)
        if extra:
            svc.delete_points(key[0], key[1], extra)
            removed += len(extra)
    conn.execute("INSERT INTO service_sync (case_id, service, kind, cursor, fingerprint) VALUES (%s,%s,'faces',%s,%s)"
                 " ON CONFLICT (case_id, service, kind) DO UPDATE SET cursor=EXCLUDED.cursor, fingerprint=EXCLUDED.fingerprint, synced_at=now()",
                 (case_id, svc.name, len(rows), face_fingerprint(conn, case_id)))
    return {"upserted": added, "removed_stale": removed, "faces": len(rows)}


def chunk_fingerprint(conn: psycopg.Connection, case_id: uuid.UUID, model_id: str) -> str:
    r = conn.execute(
        "SELECT md5(%s || coalesce(string_agg(e.chunk_id::text, ',' ORDER BY e.chunk_id), '')) AS f FROM text_embedding e"
        " JOIN text_chunk c ON c.id=e.chunk_id WHERE e.case_id=%s AND e.model_id=%s", (model_id, case_id, model_id)).fetchone()
    return r["f"]


def sync_chunks(conn: psycopg.Connection, svc: QdrantService, case_id: uuid.UUID, model_id: str) -> dict[str, Any]:
    """Make Qdrant hold exactly the dense vectors Postgres holds for this case and model: add the missing, delete the gone."""
    rows = conn.execute("SELECT e.chunk_id AS id, e.embedding FROM text_embedding e JOIN text_chunk c ON c.id=e.chunk_id"
                        " WHERE e.case_id=%s AND e.model_id=%s", (case_id, model_id)).fetchall()
    by_dim: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        by_dim.setdefault(len(r["embedding"]), []).append(r)
    safe = re.sub(r"[^a-z0-9]", "_", model_id.lower())
    dims = set(by_dim) | {int(m.group(1)) for n in svc.collections() if (m := re.fullmatch(rf"chunks_{safe}_(\d+)", n))}
    added = removed = 0
    for dim in dims:
        have = svc.point_ids(model_id, dim, case_id, "chunks")
        group = by_dim.get(dim, [])
        missing = [r for r in group if str(r["id"]) not in have]
        if missing:
            added += svc.upsert_chunks(model_id, case_id, missing)
        extra = sorted(have - {str(r["id"]) for r in group}, key=int)
        if extra:
            svc.delete_points(model_id, dim, [int(x) for x in extra], "chunks")
            removed += len(extra)
    conn.execute("INSERT INTO service_sync (case_id, service, kind, cursor, fingerprint) VALUES (%s,%s,'chunks',%s,%s)"
                 " ON CONFLICT (case_id, service, kind) DO UPDATE SET cursor=EXCLUDED.cursor, fingerprint=EXCLUDED.fingerprint, synced_at=now()",
                 (case_id, svc.name, len(rows), chunk_fingerprint(conn, case_id, model_id)))
    return {"upserted": added, "removed_stale": removed, "vectors": len(rows)}


def dense_ids_qdrant(conn: psycopg.Connection, svc: QdrantService, backend: Any, case_id: uuid.UUID, query: str, limit: int) -> list[int]:
    """Dense ranking served by Qdrant. Qdrant supplies ids only: an id whose chunk Postgres no longer holds is dropped."""
    from astral.embed import ensure_embedded

    ensure_embedded(conn, backend, case_id)
    have = conn.execute("SELECT fingerprint FROM service_sync WHERE case_id=%s AND service=%s AND kind='chunks'", (case_id, svc.name)).fetchone()
    if not have or have["fingerprint"] != chunk_fingerprint(conn, case_id, backend.model_id):
        sync_chunks(conn, svc, case_id, backend.model_id)
    ids = svc.search_chunks(backend.model_id, case_id, backend.embed_query(query), k=limit)
    if not ids:
        return []
    live = {int(r["id"]) for r in conn.execute("SELECT id FROM text_chunk WHERE case_id=%s AND id = ANY(%s)", (case_id, ids))}
    return [i for i in ids if i in live]


def _models_in_service(svc: QdrantService) -> list[tuple[str, int]]:
    out = []
    for c in svc._c("GET", "/collections")["result"]["collections"]:
        m = re.fullmatch(r"faces_(.+)_(\d+)", c["name"])
        if m:
            out.append((m.group(1), int(m.group(2))))
    return out


# ------------------------------------------------------------------ the object the rest of the code holds
def hydrate_faces(conn: psycopg.Connection, case_id: uuid.UUID, hits: list[dict[str, Any]], *, model_id: str,
                  min_score: float | None = None) -> list[dict[str, Any]]:
    """Qdrant supplies ids and scores only. Everything shown comes from Postgres, and a point whose face row is
    gone (expired, removed, erased) is dropped: a stale index can never show a face Postgres no longer holds."""
    from astral.faces import REFERENCE_THRESHOLD

    if not hits:
        return []
    rows = {str(r["id"]): r for r in conn.execute(
        "SELECT id, artifact_sha, frame_ms, entity, box, det_score, backend, watchlist, label FROM face"
        " WHERE case_id=%s AND id = ANY(%s::uuid[])", (case_id, [h["id"] for h in hits])).fetchall()}
    out = []
    for h in hits:
        r = rows.get(str(h["id"]))
        if r is None or (min_score is not None and h["score"] < min_score):
            continue
        out.append({"face_id": str(r["id"]), "entity": str(r["entity"]), "artifact": bytes(r["artifact_sha"]).hex(),
                    "frame_ms": r["frame_ms"], "box": r["box"], "det_score": r["det_score"], "score": round(h["score"], 4),
                    "backend": r["backend"], "model_id": model_id, "watchlist": r["watchlist"], "label": r["label"],
                    "reference_threshold": REFERENCE_THRESHOLD if model_id == "sface" else None,
                    "above_reference": (h["score"] >= REFERENCE_THRESHOLD) if model_id == "sface" else None})
    return out


class Services:
    """Whatever dedicated services are configured. Every method degrades visibly: it returns what it could not do."""

    def __init__(self, text: OpenSearchService | None = None, vector: QdrantService | None = None, dense: Any | None = None,
                 graph: Any | None = None) -> None:
        self.text, self.vector, self.dense, self.graph = text, vector, dense, graph

    @classmethod
    def from_settings(cls, settings: Any) -> "Services":
        text = vec = None
        if getattr(settings, "opensearch_url", None):
            text = OpenSearchService(settings.opensearch_url, index=settings.opensearch_index, auth=os.environ.get("ASTRAL_OPENSEARCH_AUTH") or None)
        if getattr(settings, "qdrant_url", None):
            vec = QdrantService(settings.qdrant_url, api_key=os.environ.get("ASTRAL_QDRANT_API_KEY") or None)
        dense = None
        if getattr(settings, "embed_model", None):
            from astral.embed import Dense, FastEmbed

            dense = Dense(FastEmbed(settings.embed_model, getattr(settings, "rerank_model", None) or None,
                                    cache_dir=getattr(settings, "embed_cache_dir", None) or None), vector=vec)
        graph = None
        if getattr(settings, "age_dsn", None):
            from astral.graphstore import GraphService

            graph = GraphService(settings.age_dsn)
        return cls(text, vec, dense, graph)

    @property
    def configured(self) -> bool:
        return bool(self.text or self.vector or self.dense or self.graph)

    def status(self, conn: psycopg.Connection) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for label, svc in (("text", self.text), ("vector", self.vector), ("graph", self.graph)):
            if svc is None:
                out[label] = {"configured": False, "fallback": "postgres"}
                continue
            try:
                out[label] = {"configured": True, "service": svc.name, "reachable": True, **svc.ping()}
            except ServiceError as e:
                out[label] = {"configured": True, "service": svc.name, "reachable": False, "error": str(e)[:200], "fallback": "postgres"}
        out["dense"] = ({"configured": False} if self.dense is None else
                        {"configured": True, "embedder": self.dense.model_id, "reranker": getattr(self.dense.backend, "reranker_model", None),
                         "vectors": "qdrant (exact)" if self.dense.vector is not None else "postgres"})
        out["pending_deletes"] = [{"case_id": str(r["case_id"]), "service": r["service"], "kind": r["kind"], "reason": r["reason"],
                                   "noted_at": r["noted_at"].isoformat()} for r in conn.execute(
            "SELECT case_id, service, kind, reason, noted_at FROM service_pending_delete ORDER BY id")]
        return out

    # ---- text
    def text_ids(self, conn: psycopg.Connection, case_id: uuid.UUID, query: str, limit: int) -> tuple[list[int] | None, str | None]:
        """(ids, None) when the service ranked; (None, reason) when it could not, so the caller says so."""
        if self.text is None:
            return None, None
        try:
            sync_text(conn, self.text, case_id)
            return self.text.search(case_id, query, limit), None
        except ServiceError as e:
            return None, f"opensearch unavailable ({str(e)[:120]}); answered from Postgres text search only"

    # ---- faces
    def face_hits(self, conn: psycopg.Connection, case_id: uuid.UUID, embedding: list[float], *, model_id: str, scope: str, k: int,
                  min_score: float | None, exclude_artifact: bytes | None, exclude_face: uuid.UUID | None) -> tuple[list[dict[str, Any]] | None, str | None]:
        if self.vector is None:
            return None, None
        try:
            have = conn.execute("SELECT fingerprint FROM service_sync WHERE case_id=%s AND service=%s AND kind='faces'", (case_id, self.vector.name)).fetchone()
            if not have or have["fingerprint"] != face_fingerprint(conn, case_id):
                sync_faces(conn, self.vector, case_id)
            raw = self.vector.search(model_id, case_id, embedding, k=k, scope=scope, exclude_artifact=exclude_artifact, exclude_face=exclude_face)
            return hydrate_faces(conn, case_id, raw, model_id=model_id, min_score=min_score), None
        except ServiceError as e:
            return None, f"qdrant unavailable ({str(e)[:120]}); searched Postgres instead"

    # ---- keeping deletions honest
    def _note_pending(self, conn: psycopg.Connection, case_id: uuid.UUID, service: str, kind: str, reason: str) -> None:
        conn.execute("INSERT INTO service_pending_delete (case_id, service, kind, reason) VALUES (%s,%s,%s,%s)"
                     " ON CONFLICT (case_id, service, kind) DO UPDATE SET reason=EXCLUDED.reason, noted_at=now()",
                     (case_id, service, kind, reason[:300]))

    def forget_case(self, conn: psycopg.Connection, case_id: uuid.UUID) -> dict[str, str]:
        """After a case is erased: remove its text and its biometric vectors from the services. A failure is
        recorded in service_pending_delete and returned: the caller must tell the operator."""
        out: dict[str, str] = {}
        for svc in (self.text, self.vector, self.graph):
            if svc is None:
                continue
            try:
                n = svc.delete_case(case_id)
                out[svc.name] = f"removed ({n})"
                conn.execute("DELETE FROM service_pending_delete WHERE case_id=%s AND service=%s", (case_id, svc.name))
            except ServiceError as e:
                self._note_pending(conn, case_id, svc.name, "case", str(e))
                out[svc.name] = f"NOT removed: {str(e)[:160]}. Recorded for retry; the service still holds this case's data."
        return out

    def reconcile_faces(self, conn: psycopg.Connection, case_ids: Iterable[uuid.UUID] | None = None) -> dict[str, Any]:
        """After a purge/expiry/watchlist removal: make Qdrant match Postgres for every case it has been told about."""
        if self.vector is None:
            return {}
        ids = list(case_ids) if case_ids is not None else [r["case_id"] for r in conn.execute(
            "SELECT case_id FROM service_sync WHERE service=%s AND kind='faces'", (self.vector.name,))]
        removed, failed = 0, []
        for cid in ids:
            try:
                removed += sync_faces(conn, self.vector, cid)["removed_stale"]
                conn.execute("DELETE FROM service_pending_delete WHERE case_id=%s AND service=%s AND kind='faces'", (cid, self.vector.name))
            except ServiceError as e:
                self._note_pending(conn, cid, self.vector.name, "faces", str(e))
                failed.append(str(cid))
        out: dict[str, Any] = {"qdrant_points_removed": removed}
        if failed:
            out["NOT_RECONCILED"] = failed
            out["warning"] = "qdrant could not be reached; it may still hold biometric vectors Postgres has deleted. Recorded for retry."
        return out

    def retry_pending(self, conn: psycopg.Connection) -> dict[str, Any]:
        done = []
        for r in conn.execute("SELECT case_id, service, kind FROM service_pending_delete ORDER BY id").fetchall():
            if r["kind"] == "case":
                res = self.forget_case(conn, r["case_id"])
                done.append({"case_id": str(r["case_id"]), "service": r["service"], "result": res.get(r["service"])})
            else:
                done.append({"case_id": str(r["case_id"]), "service": r["service"], "result": self.reconcile_faces(conn, [r["case_id"]])})
        return {"retried": done}

    def sync_case(self, conn: psycopg.Connection, case_id: uuid.UUID) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for label, fn, svc in (("text", sync_text, self.text), ("faces", sync_faces, self.vector)):
            if svc is None:
                continue
            try:
                out[label] = fn(conn, svc, case_id)
            except ServiceError as e:
                out[label] = {"error": str(e)[:200]}
        if self.graph is not None:
            from astral import graphstore

            try:
                out["graph"] = graphstore.sync(conn, self.graph, case_id)
            except ServiceError as e:
                out["graph"] = {"error": str(e)[:200]}
        if self.dense is not None:
            from astral.embed import DenseError, ensure_embedded

            try:
                out["dense"] = {"embedded": ensure_embedded(conn, self.dense.backend, case_id), "model": self.dense.model_id}
                if self.dense.vector is not None:
                    out["dense"]["qdrant"] = sync_chunks(conn, self.dense.vector, case_id, self.dense.model_id)
            except DenseError as e:
                out["dense"] = {"error": str(e)[:200]}
            except ServiceError as e:
                out["dense"]["qdrant"] = {"error": str(e)[:200]}
        return out
