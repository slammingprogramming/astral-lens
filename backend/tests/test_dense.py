"""Dense retrieval and reranking. HONEST SCOPE: the always-run tests use a deterministic STAND-IN backend
(words mapped to shared concepts), which proves the plumbing, fusion, visible degradation and erasure. They say
nothing about how well any real model retrieves. The last tests run the real multilingual embedder and reranker
and only run when ASTRAL_TEST_EMBED_DIR points at a fastembed cache that already holds them."""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from astral import cases, embed, retrieval, services
from astral.api import create_app
from astral.config import Settings
from astral.db import SOLO_ACTOR

CONCEPTS = {"car": 0, "automobile": 0, "vehicle": 0, "boat": 1, "ship": 1, "vessel": 1, "money": 2, "payment": 2, "cash": 2}
DIM = 8


class Standin:
    """Not a model. Words that name the same concept share an axis; anything else lands on a hashed axis."""
    model_id = "standin-embedder"

    def __init__(self, fail=False, fail_rerank=False, rerank=False):
        self.fail, self.fail_rerank, self.rerank_calls = fail, fail_rerank, 0
        self.reranker_model = "standin-reranker" if rerank or fail_rerank else None

    def _vec(self, text):
        v = [0.0] * DIM
        for w in text.lower().replace("?", " ").replace(".", " ").split():
            if w in CONCEPTS:
                v[CONCEPTS[w]] += 1.0
            else:                                              # filler words only add faint noise
                v[3 + int(hashlib.sha256(w.encode()).hexdigest(), 16) % (DIM - 3)] += 0.05
        return v

    def embed_passages(self, texts):
        if self.fail:
            raise embed.DenseError("stand-in embedder is down")
        return [self._vec(t) for t in texts]

    def embed_query(self, q):
        if self.fail:
            raise embed.DenseError("stand-in embedder is down")
        return self._vec(q)

    def rerank(self, query, docs):
        self.rerank_calls += 1
        if self.fail_rerank:
            raise embed.DenseError("stand-in reranker is down")
        return [-float(len(d)) for d in docs]                  # shortest first: an order no other ranker would give


def new_case(db):
    with db.connection() as conn:
        return cases.create_case(conn, SOLO_ACTOR, f"dense-{uuid.uuid4().hex[:6]}")


def chunks(conn, case, bodies):
    sha = hashlib.sha256("".join(bodies).encode()).digest()
    pos = 0
    for b in bodies:
        conn.execute("INSERT INTO text_chunk (workspace_id, case_id, text_sha, start_char, end_char, body) VALUES (%s,%s,%s,%s,%s,%s)",
                     (SOLO_ACTOR.workspace_id, case, sha, pos, pos + len(b), b))
        pos += len(b)


def svc(backend):
    return services.Services(dense=embed.Dense(backend))


BODIES = ["The automobile was parked near the harbour at dawn.", "Quarterly volume was 41000 units across five sites.",
          "A ship left the port carrying containers.", "The director signed the annual report."]


def test_a_paraphrase_with_no_shared_words_is_found_only_by_the_dense_ranker(db):
    case = new_case(db)
    with db.connection() as conn:
        chunks(conn, case, BODIES)
        lexical = retrieval.search(conn, case, "Where is the vehicle?", k=2)
        assert not any("automobile" in h["text"] for h in lexical["hits"])               # the lexical rankers miss it
        r = retrieval.search(conn, case, "Where is the vehicle?", k=2, services=svc(Standin()))
        assert r["hits"][0]["text"].startswith("The automobile") and "dense:standin-embedder" in r["rankers"]
        assert r["hits"][0]["dense_rank"] == 1 and "lexical retrieval only" not in " ".join(r["notes"])
        again = retrieval.search(conn, case, "Where is the vehicle?", k=2, services=svc(Standin()))      # embeddings are stored, not recomputed
        n = conn.execute("SELECT count(*) AS n FROM text_embedding WHERE case_id=%s", (case,)).fetchone()["n"]
        assert n == 4 and again["hits"][0]["chunk_id"] == r["hits"][0]["chunk_id"]


def test_the_reranker_reorders_the_fused_candidates_and_says_so(db):
    case = new_case(db)
    with db.connection() as conn:
        chunks(conn, case, BODIES)
        b = Standin(rerank=True)
        r = retrieval.search(conn, case, "ship vessel boat cash payment", k=3, services=svc(b))
        assert "rerank:standin-reranker" in r["rankers"] and b.rerank_calls == 1
        lengths = [len(h["text"]) for h in r["hits"]]
        assert lengths == sorted(lengths) and all(h["rerank_score"] is not None for h in r["hits"])      # the reranker's order won


def test_a_dead_embedder_or_reranker_degrades_visibly_and_lexical_retrieval_still_answers(db):
    case = new_case(db)
    with db.connection() as conn:
        chunks(conn, case, BODIES)
        r = retrieval.search(conn, case, "director annual report", k=2, services=svc(Standin(fail=True)))
        assert r["hits"] and "director" in r["hits"][0]["text"] and not any(x.startswith("dense:") for x in r["rankers"])
        assert any("dense retrieval unavailable" in n for n in r["notes"]) and any("lexical retrieval only" in n for n in r["notes"])
        r2 = retrieval.search(conn, case, "director annual report", k=2, services=svc(Standin(fail_rerank=True)))
        assert any(x.startswith("dense:") for x in r2["rankers"]) and not any(x.startswith("rerank:") for x in r2["rankers"])
        assert any("reranker unavailable" in n for n in r2["notes"]) and r2["hits"]


def test_missing_fastembed_is_reported_not_raised(db, monkeypatch):
    import builtins

    real = builtins.__import__

    def deny(name, *a, **k):
        if name == "fastembed" or name.startswith("fastembed."):
            raise ImportError("no fastembed")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", deny)
    d = embed.Dense(embed.FastEmbed("intfloat/multilingual-e5-large", "BAAI/bge-reranker-v2-m3"))
    case = new_case(db)
    with db.connection() as conn:
        chunks(conn, case, BODIES)
        ids, why = d.ids(conn, case, "anything", 5)
    assert ids is None and "fastembed is not installed" in why


def test_e5_models_get_their_required_prefixes():
    assert embed._prefixes("intfloat/multilingual-e5-large") == ("query: ", "passage: ")
    assert embed._prefixes("BAAI/bge-small-en") == ("", "")


def test_erasing_a_case_removes_its_embeddings_and_the_api_reports_the_configuration(db, dsn, tmp_path):
    app = create_app(Settings(dsn=dsn, data_dir=tmp_path), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        app.state.services = svc(Standin(rerank=True))
        st = c.get("/ai/status").json()
        assert "dense:standin-embedder" in st["retrieval"] and "rerank:standin-reranker" in st["retrieval"] and st["not_configured"] == []
        assert c.get("/services").json()["dense"] == {"configured": True, "embedder": "standin-embedder", "reranker": "standin-reranker", "vectors": "postgres"}
        case = c.post("/cases", json={"name": "d"}).json()["id"]
        cid = uuid.UUID(case)
        with db.connection() as conn:
            chunks(conn, cid, BODIES)
        assert c.post(f"/cases/{case}/services/sync").json()["dense"] == {"embedded": 4, "model": "standin-embedder"}
        assert c.post(f"/cases/{case}/erase", json={"legal_basis": "request"}).status_code == 200
        with db.connection() as conn:
            assert conn.execute("SELECT count(*) AS n FROM text_embedding WHERE case_id=%s", (cid,)).fetchone()["n"] == 0
        app.state.services = services.Services()
        assert "dense embedder" in c.get("/ai/status").json()["not_configured"]


# ------------------------------------------------------------------ text vectors served by Qdrant
QD_URL = os.environ.get("ASTRAL_TEST_QDRANT")
needs_qdrant = pytest.mark.skipif(not QD_URL, reason="ASTRAL_TEST_QDRANT not set: no Qdrant server available")


def qsvc(backend, url):
    q = services.QdrantService(url)
    return services.Services(vector=q, dense=embed.Dense(backend, vector=q)), q


def test_unreachable_qdrant_falls_back_to_postgres_vectors_and_says_so(db):
    case = new_case(db)
    with db.connection() as conn:
        chunks(conn, case, BODIES)
        s, _ = qsvc(Standin(), "http://127.0.0.1:9")
        r = retrieval.search(conn, case, "Where is the vehicle?", k=2, services=s)
        assert r["hits"][0]["text"].startswith("The automobile") and "dense:standin-embedder" in r["rankers"]
        assert any("qdrant unavailable" in n for n in r["notes"]) and not any(x.endswith("@qdrant") for x in r["rankers"])


@needs_qdrant
def test_qdrant_serves_the_dense_ranking_and_ranks_like_postgres(db):
    case, other = new_case(db), new_case(db)
    with db.connection() as conn:
        chunks(conn, case, BODIES)
        chunks(conn, other, ["A vehicle was seen in another case entirely."])
        s, q = qsvc(Standin(), QD_URL)
        via = retrieval.search(conn, case, "Where is the vehicle?", k=3, services=s)
        pg = retrieval.search(conn, case, "Where is the vehicle?", k=3, services=svc(Standin()))
        assert "dense:standin-embedder@qdrant" in via["rankers"] and not via["notes"][1:]
        assert [h["chunk_id"] for h in via["hits"]] == [h["chunk_id"] for h in pg["hits"]]
        assert len(q.point_ids("standin-embedder", DIM, case, "chunks")) == 4
        retrieval.search(conn, other, "vehicle", k=1, services=s)
        assert all(h["text"] != "A vehicle was seen in another case entirely." for h in via["hits"])     # case isolation
        assert len(q.point_ids("standin-embedder", DIM, other, "chunks")) == 1
        assert s.forget_case(conn, case)["qdrant"].startswith("removed")
        assert q.point_ids("standin-embedder", DIM, case, "chunks") == set() and len(q.point_ids("standin-embedder", DIM, other, "chunks")) == 1
        s.forget_case(conn, other)


@needs_qdrant
def test_a_stale_text_vector_is_never_shown_and_sync_removes_it(db, monkeypatch):
    case = new_case(db)
    with db.connection() as conn:
        chunks(conn, case, BODIES)
        s, q = qsvc(Standin(), QD_URL)
        retrieval.search(conn, case, "vehicle", k=2, services=s)
        gone = conn.execute("SELECT id FROM text_chunk WHERE case_id=%s AND body LIKE 'The automobile%%'", (case,)).fetchone()["id"]
        conn.execute("DELETE FROM text_chunk WHERE id=%s", (gone,))
        assert gone in q.search_chunks("standin-embedder", case, Standin()._vec("vehicle"), k=10)            # the index is stale
        monkeypatch.setattr(services, "sync_chunks", lambda *a, **k: None)                                    # even without a sync...
        assert gone not in services.dense_ids_qdrant(conn, q, Standin(), case, "vehicle", 10)                 # ...it is never returned
        monkeypatch.undo()
        services.dense_ids_qdrant(conn, q, Standin(), case, "vehicle", 10)                                    # the fingerprint differs: resync
        assert str(gone) not in q.point_ids("standin-embedder", DIM, case, "chunks")
        s.forget_case(conn, case)


# ------------------------------------------------------------------ the real models
EMBED_DIR = os.environ.get("ASTRAL_TEST_EMBED_DIR")
needs_models = pytest.mark.skipif(not EMBED_DIR or not Path(EMBED_DIR).exists(),
                                  reason="ASTRAL_TEST_EMBED_DIR (a fastembed cache holding the e5 and reranker models) not set")


@needs_models
def test_real_multilingual_embedder_finds_cross_language_paraphrases_and_the_reranker_orders_sensibly(db):
    backend = embed.FastEmbed("intfloat/multilingual-e5-large", os.environ.get("ASTRAL_TEST_RERANKER", "BAAI/bge-reranker-v2-m3-int8"), cache_dir=EMBED_DIR)
    case = new_case(db)
    bodies = ["Its director is Jane Doe, appointed in 2019.", "Quarterly volume was 41000 units across five sites.",
              "The warehouse in Rotterdam stores frozen fish.", "Payments are made to the account held at the Zurich branch."]
    with db.connection() as conn:
        chunks(conn, case, bodies)
        s = services.Services(dense=embed.Dense(backend))
        de = retrieval.search(conn, case, "Wer leitet das Unternehmen?", k=1, services=s)                  # German: "Who runs the company?"
        assert de["hits"][0]["text"].startswith("Its director"), de["hits"]
        assert any(x.startswith("dense:") for x in de["rankers"]) and any(x.startswith("rerank:") for x in de["rankers"])
        fr = retrieval.search(conn, case, "Où est stocké le poisson congelé ?", k=1, services=s)           # French: "Where is frozen fish stored?"
        assert fr["hits"][0]["text"].startswith("The warehouse")
