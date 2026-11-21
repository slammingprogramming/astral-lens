"""Dense retrieval: a multilingual embedder and a cross-encoder reranker, both optional, both local.

Why it exists: lexical rankers miss a paraphrase that shares no words with the question. A dense ranker
finds it; a cross-encoder reranker then orders the fused candidates by reading query and passage together.

What it is not. Embeddings are an index over text chunks, never evidence (retrieval.py): a hit still has
to survive citation checking against the stored bytes. Nothing here leaves the machine: the models are
ONNX files run in-process through fastembed, and the cache directory is the only thing written.

Degradation is visible (invariant 4). If the embedder or reranker cannot load or run, the caller gets
`(None, reason)` and the answer says which ranker was missing; lexical retrieval still answers.

The embedder is chosen by name. `intfloat/multilingual-e5-*` need "query: " / "passage: " prefixes, which
this module adds itself rather than trusting a library default.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg

BATCH = 16
MAX_CANDIDATES = 40
MAX_CHARS = 2000                                    # a chunk is ~800 characters; this bounds a hostile one


class DenseError(Exception):
    pass


def _prefixes(model: str) -> tuple[str, str]:
    return ("query: ", "passage: ") if "e5" in model.lower() else ("", "")


class FastEmbed:
    """Lazy wrapper over fastembed (pip install fastembed). Imports and model loads happen on first use."""

    def __init__(self, model: str, reranker: str | None = None, cache_dir: str | None = None, threads: int | None = None) -> None:
        self.model, self.reranker_model, self.cache_dir, self.threads = model, reranker, cache_dir, threads
        self._emb: Any = None
        self._rr: Any = None

    @property
    def model_id(self) -> str:
        return self.model

    def _embedder(self) -> Any:
        if self._emb is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as e:
                raise DenseError("fastembed is not installed (pip install fastembed)") from e
            try:
                self._emb = TextEmbedding(model_name=self.model, cache_dir=self.cache_dir, threads=self.threads)
            except Exception as e:                          # noqa: BLE001 - any load failure degrades visibly
                raise DenseError(f"embedding model {self.model!r} could not be loaded: {type(e).__name__}: {str(e)[:160]}") from e
        return self._emb

    def _reranker(self) -> Any:
        if self._rr is None:
            if not self.reranker_model:
                raise DenseError("no reranker model is configured")
            try:
                from fastembed.rerank.cross_encoder import TextCrossEncoder
            except ImportError as e:
                raise DenseError("fastembed is not installed (pip install fastembed)") from e
            try:
                self._rr = TextCrossEncoder(model_name=self.reranker_model, cache_dir=self.cache_dir, threads=self.threads)
            except Exception as e:                          # noqa: BLE001
                raise DenseError(f"reranker {self.reranker_model!r} could not be loaded: {type(e).__name__}: {str(e)[:160]}") from e
        return self._rr

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        pre = _prefixes(self.model)[1]
        try:
            return [[float(x) for x in v] for v in self._embedder().embed([pre + t[:MAX_CHARS] for t in texts], batch_size=BATCH)]
        except DenseError:
            raise
        except Exception as e:                              # noqa: BLE001
            raise DenseError(f"embedding failed: {type(e).__name__}: {str(e)[:160]}") from e

    def embed_query(self, query: str) -> list[float]:
        pre = _prefixes(self.model)[0]
        try:
            return [float(x) for x in next(iter(self._embedder().embed([pre + query[:MAX_CHARS]], batch_size=1)))]
        except DenseError:
            raise
        except Exception as e:                              # noqa: BLE001
            raise DenseError(f"embedding failed: {type(e).__name__}: {str(e)[:160]}") from e

    def rerank(self, query: str, docs: list[str]) -> list[float]:
        try:
            return [float(s) for s in self._reranker().rerank(query[:MAX_CHARS], [d[:MAX_CHARS] for d in docs], batch_size=BATCH)]
        except DenseError:
            raise
        except Exception as e:                              # noqa: BLE001
            raise DenseError(f"reranking failed: {type(e).__name__}: {str(e)[:160]}") from e


# ------------------------------------------------------------------ storage and ranking
def ensure_embedded(conn: psycopg.Connection, backend: Any, case_id: uuid.UUID, *, limit: int = 2000) -> int:
    """Embed the case's chunks that have no vector for this model yet. Returns how many were added."""
    rows = conn.execute(
        "SELECT c.id, c.body FROM text_chunk c WHERE c.case_id=%s AND NOT EXISTS"
        " (SELECT 1 FROM text_embedding e WHERE e.chunk_id=c.id AND e.model_id=%s) ORDER BY c.id LIMIT %s",
        (case_id, backend.model_id, limit)).fetchall()
    added = 0
    for i in range(0, len(rows), BATCH * 4):
        part = rows[i:i + BATCH * 4]
        for r, v in zip(part, backend.embed_passages([x["body"] for x in part])):
            added += conn.execute(
                "INSERT INTO text_embedding (chunk_id, case_id, model_id, dim, embedding) VALUES (%s,%s,%s,%s,%s::real[])"
                " ON CONFLICT DO NOTHING", (r["id"], case_id, backend.model_id, len(v), v)).rowcount
    return added


def dense_ids(conn: psycopg.Connection, backend: Any, case_id: uuid.UUID, query: str, limit: int) -> list[int]:
    """Exact cosine over the case's chunk vectors (numpy). Exact, not approximate, like the face search."""
    import numpy as np

    ensure_embedded(conn, backend, case_id)
    q = np.array(backend.embed_query(query), dtype=np.float64)
    rows = conn.execute("SELECT chunk_id, embedding FROM text_embedding WHERE case_id=%s AND model_id=%s AND dim=%s",
                        (case_id, backend.model_id, len(q))).fetchall()
    if not rows:
        return []
    m = np.array([r["embedding"] for r in rows], dtype=np.float64)
    m /= np.linalg.norm(m, axis=1, keepdims=True).clip(1e-12)
    sims = m @ (q / (np.linalg.norm(q) or 1.0))
    order = np.argsort(-sims)[:limit]
    return [int(rows[i]["chunk_id"]) for i in order]


class Dense:
    """What `Services` holds for dense retrieval. Methods return (value, None) or (None, reason)."""

    name = "dense"

    def __init__(self, backend: Any, vector: Any | None = None) -> None:
        self.backend = backend
        self.vector = vector                                # a QdrantService that serves the same vectors, or None

    @property
    def model_id(self) -> str:
        return self.backend.model_id

    @property
    def can_rerank(self) -> bool:
        return bool(getattr(self.backend, "reranker_model", None))

    def ids(self, conn: psycopg.Connection, case_id: uuid.UUID, query: str, limit: int) -> tuple[list[int] | None, str | None]:
        note = None
        try:
            if self.vector is not None:
                from astral import services as S

                try:
                    return S.dense_ids_qdrant(conn, self.vector, self.backend, case_id, query, limit), None
                except S.ServiceError as e:
                    note = f"qdrant unavailable ({str(e)[:120]}); ranked the dense vectors from Postgres instead"
            return dense_ids(conn, self.backend, case_id, query, limit), note
        except DenseError as e:
            return None, f"dense retrieval unavailable ({str(e)[:140]}); lexical rankers only"

    def rerank(self, query: str, candidates: list[tuple[int, str]]) -> tuple[list[tuple[int, float]] | None, str | None]:
        """candidates: (chunk_id, text). -> [(chunk_id, score)] best first."""
        if not self.can_rerank or not candidates:
            return None, None
        try:
            scores = self.backend.rerank(query, [t for _, t in candidates[:MAX_CANDIDATES]])
        except DenseError as e:
            return None, f"reranker unavailable ({str(e)[:140]}); kept the fused order"
        return sorted(((cid, s) for (cid, _), s in zip(candidates, scores)), key=lambda x: -x[1]), None
