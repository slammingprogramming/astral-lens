"""Hybrid retrieval over a case's text, fused by reciprocal rank (RRF).

Rankers in use today: Postgres full-text search, and a fuzzy lexical ranker that
tolerates misspellings and transliteration variants. A DENSE ranker (BGE-M3) and a
cross-encoder reranker are NOT built: no embedding model is installed. The result
says which rankers ran, so a caller can tell lexical-only retrieval from the real
thing; it will miss paraphrases that share no words with the query.

Chunks are an index, not evidence. Each carries (text_sha, start_char, end_char),
which is the citation anchor a verified claim points at.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import psycopg

from astral.db import Actor
from astral.evidence import EvidenceStore

CHUNK = 800
OVERLAP = 120
_TOKEN = re.compile(r"\w{2,}", re.UNICODE)
STOPWORDS = frozenset(
    "the a an and or of to in on for with by is are was were be been this that these those from at as it its has have had "
    "not but which who whom whose what when where why how does did do can could would should will shall about over under "
    "into than then also there their them they you your his her him she he we our us i me my".split())


def content_tokens(text: str) -> list[str]:
    return [t for t in (x.lower() for x in _TOKEN.findall(text)) if t not in STOPWORDS]


def chunk_text(text: str, size: int = CHUNK, overlap: int = OVERLAP) -> list[tuple[int, int, str]]:
    """Windows that end on a line or sentence boundary when one is near. Offsets are
    into the original text, so every chunk is a verbatim slice."""
    out, n, pos = [], len(text), 0
    while pos < n:
        end = min(n, pos + size)
        if end < n:
            cut = max(text.rfind("\n", pos + size // 2, end), text.rfind(". ", pos + size // 2, end) + 1)
            end = cut if cut > pos + size // 2 else end
        body = text[pos:end]
        if body.strip():
            out.append((pos, end, body))
        if end >= n:
            break
        pos = max(end - overlap, pos + 1)
    return out


def ensure_indexed(conn: psycopg.Connection, store: EvidenceStore, actor: Actor, case_id: uuid.UUID) -> int:
    """Chunk every text artifact the projection knows about (`hasText`) that is not indexed yet."""
    shas = [r["sha"] for r in conn.execute(
        "SELECT DISTINCT value #>> '{}' AS sha FROM proj_property WHERE case_id=%s AND predicate='hasText'", (case_id,))]
    done = {bytes(r["text_sha"]).hex() for r in conn.execute(
        "SELECT DISTINCT text_sha FROM text_chunk WHERE case_id=%s", (case_id,))}
    added = 0
    for sha in shas:
        if sha in done:
            continue
        text = store.read(conn, actor, case_id, bytes.fromhex(sha)).decode("utf-8", "replace")
        for s, e, body in chunk_text(text):
            added += conn.execute(
                "INSERT INTO text_chunk (workspace_id, case_id, text_sha, start_char, end_char, body)"
                " VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (actor.workspace_id, case_id, bytes.fromhex(sha), s, e, body)).rowcount
    return added


def rrf(rankings: list[list[int]], k: int = 60) -> dict[int, float]:
    """Reciprocal rank fusion: score(d) = sum over rankers of 1/(k + rank)."""
    score: dict[int, float] = {}
    for r in rankings:
        for i, doc in enumerate(r, 1):
            score[doc] = score.get(doc, 0.0) + 1.0 / (k + i)
    return score


def _fts_ids(conn: psycopg.Connection, case_id: uuid.UUID, query: str, limit: int) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM text_chunk WHERE case_id=%s AND tsv @@ websearch_to_tsquery('simple', %s)"
        " ORDER BY ts_rank_cd(tsv, websearch_to_tsquery('simple', %s)) DESC, id LIMIT %s",
        (case_id, query, query, limit)).fetchall()
    if rows:
        return [r["id"] for r in rows]
    toks = content_tokens(query)
    if not toks:
        return []
    q = " | ".join(dict.fromkeys(toks))                      # any-content-word fallback when the strict query finds nothing
    return [r["id"] for r in conn.execute(
        "SELECT id FROM text_chunk WHERE case_id=%s AND tsv @@ to_tsquery('simple', %s)"
        " ORDER BY ts_rank(tsv, to_tsquery('simple', %s)) DESC, id LIMIT %s", (case_id, q, q, limit))]


def _ctx_ids(conn: psycopg.Connection, case_id: uuid.UUID, query: str, limit: int) -> list[int] | None:
    """Chunks whose model-written context header matches the question; None when the case has no headers (the ranker
    did not run). A header is a search aid only: it is model output, not evidence (astral.contextual)."""
    if not conn.execute("SELECT 1 FROM text_chunk_context WHERE case_id=%s LIMIT 1", (case_id,)).fetchone():
        return None
    toks = content_tokens(query)
    if not toks:
        return []
    q = " | ".join(dict.fromkeys(toks))
    return [r["id"] for r in conn.execute(
        "SELECT chunk_id AS id FROM text_chunk_context WHERE case_id=%s AND tsv @@ to_tsquery('simple', %s)"
        " ORDER BY ts_rank(tsv, to_tsquery('simple', %s)) DESC, chunk_id LIMIT %s", (case_id, q, q, limit))]


def _fuzzy_ids(conn: psycopg.Connection, case_id: uuid.UUID, query: str, limit: int) -> list[int]:
    from rapidfuzz import fuzz

    q = " ".join(content_tokens(query))                      # generic words must not create matches
    if not q:
        return []
    rows = conn.execute("SELECT id, body FROM text_chunk WHERE case_id=%s LIMIT 5000", (case_id,)).fetchall()
    scored = [(fuzz.partial_ratio(q, r["body"].lower()), r["id"]) for r in rows]
    return [i for s, i in sorted(scored, key=lambda x: (-x[0], x[1])) if s >= 80][:limit]


def search(conn: psycopg.Connection, case_id: uuid.UUID, query: str, *, k: int = 8, services: Any | None = None) -> dict[str, Any]:
    """`services` (astral.services.Services) adds a dedicated text ranker, a dense ranker and a reranker when
    configured. If one is configured but cannot answer, the result says so and the others still answer:
    degradation is visible."""
    fts, fuzzy = _fts_ids(conn, case_id, query, 50), _fuzzy_ids(conn, case_id, query, 50)
    rankings, rankers, notes = [fts, fuzzy], ["fts", "fuzzy"], list(_NOTES)
    ctx = _ctx_ids(conn, case_id, query, 50)
    if ctx is not None:
        rankings.append(ctx)
        rankers.append("context-headers")
    dense_rank: list[int] = []
    if services is not None:
        ids, why = services.text_ids(conn, case_id, query, 50)
        if ids is not None:
            rankings.append(ids)
            rankers.append("opensearch")
        elif why:
            notes.append(why)
        if services.dense is not None:
            dids, why = services.dense.ids(conn, case_id, query, 50)
            if dids is not None:
                dense_rank = dids
                rankings.append(dids)
                rankers.append(f"dense:{services.dense.model_id}" + ("@qdrant" if services.dense.vector is not None and not why else ""))
                notes[:] = [n for n in notes if n is not _NOTES[0]]
            if why:
                notes.append(why)
    want = max(k * 3, 20) if services is not None and services.dense is not None and services.dense.can_rerank else k
    fused = sorted(rrf(rankings).items(), key=lambda x: (-x[1], x[0]))[:want]
    if not fused:
        return {"hits": [], "rankers": rankers, "notes": notes}
    rows = {r["id"]: r for r in conn.execute(
        "SELECT id, text_sha, start_char, end_char, body FROM text_chunk WHERE id = ANY(%s)", ([i for i, _ in fused],))}
    rerank_score: dict[int, float] = {}
    if want > k:
        ranked, why = services.dense.rerank(query, [(i, rows[i]["body"]) for i, _ in fused])
        if ranked is not None:
            rerank_score = dict(ranked)
            fused = sorted(fused, key=lambda x: (-rerank_score.get(x[0], float("-inf")), -x[1], x[0]))
            rankers.append(f"rerank:{services.dense.backend.reranker_model}")
        elif why:
            notes.append(why)
    fused = fused[:k]
    headers = {r["chunk_id"]: r["header"] for r in conn.execute(
        "SELECT chunk_id, header FROM text_chunk_context WHERE chunk_id = ANY(%s)", ([i for i, _ in fused],))} if ctx is not None else {}
    hits = [{"chunk_id": i, "text_sha": bytes(rows[i]["text_sha"]).hex(), "start": rows[i]["start_char"],
             "end": rows[i]["end_char"], "text": rows[i]["body"], "rrf": round(s, 5),
             "fts_rank": fts.index(i) + 1 if i in fts else None, "fuzzy_rank": fuzzy.index(i) + 1 if i in fuzzy else None,
             "context_rank": ctx.index(i) + 1 if ctx and i in ctx else None, "context_header": headers.get(i),
             "dense_rank": dense_rank.index(i) + 1 if i in dense_rank else None,
             "rerank_score": round(rerank_score[i], 4) if i in rerank_score else None}
            for i, s in fused]
    if any(h["context_rank"] and not h["fts_rank"] for h in hits):
        notes.append("some passages were found through a model-written context header (a search aid, not evidence); "
                     "the passage text shown is the stored text, unchanged")
    return {"hits": hits, "rankers": rankers, "notes": notes}


_NOTES = ["lexical retrieval only: no dense embedder or reranker is installed, so paraphrases that share no words "
          "with the question can be missed"]
