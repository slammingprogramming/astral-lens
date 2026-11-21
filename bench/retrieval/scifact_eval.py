"""Retrieval quality on a real corpus: SciFact (BEIR), through Astral Lens's own retrieval code.

    python scifact_eval.py DATA_DIR MODELS_CACHE OUT.json [--dsn DSN] [--docs N] [--threads N]

DATA_DIR holds the files fetched from Hugging Face (not committed, not redistributed here):
    corpus/corpus-00000-of-00001.parquet   (BeIR/scifact: _id, title, text)
    queries/queries-00000-of-00001.parquet (BeIR/scifact: _id, text)
    test.tsv                               (BeIR/scifact-qrels: query-id, corpus-id, score)

Each abstract becomes ONE chunk of a throwaway case in a throwaway database, so what is measured is the retrieval
stack (Postgres full-text, the fuzzy ranker, dense e5 embeddings, reciprocal-rank fusion, the cross-encoder
reranker), not the chunker. The 300 test queries are scored with nDCG@10, Recall@10 and MRR@10 against the
published relevance judgements.

What this does and does not say. SciFact is scientific claims against abstracts: short English text, one
judged relevant document per claim on average. It says whether the pipeline is wired correctly and which
rankers help on a clean, well-formed corpus. It does NOT say how retrieval behaves on scanned, multilingual,
noisy investigative material, and its numbers are not comparable to a leaderboard unless the same protocol
is used. The corpus is CC BY-NC 4.0 (SciFact, AllenAI), so it is used for this check and not shipped.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pyarrow.parquet as pq

from astral import retrieval
from astral.cases import create_case
from astral.db import SOLO_ACTOR, Database
from astral.embed import Dense, FastEmbed, ensure_embedded

ap = argparse.ArgumentParser()
ap.add_argument("data")
ap.add_argument("cache")
ap.add_argument("out")
ap.add_argument("--dsn", default="postgresql://postgres:astral@localhost:55432/astral_scifact")
ap.add_argument("--docs", type=int, default=0, help="limit the corpus (0 = all); relevant documents are always kept")
ap.add_argument("--queries", type=int, default=0, help="score only the first N test queries (0 = all)")
ap.add_argument("--threads", type=int, default=8)
ap.add_argument("--embed-model", default="intfloat/multilingual-e5-large")
ap.add_argument("--rerank-model", default="BAAI/bge-reranker-v2-m3-int8")
a = ap.parse_args()

data = Path(a.data)
corpus = pq.read_table(next((data / "corpus").glob("*.parquet"))).to_pylist()           # [{_id, title, text}]
queries = pq.read_table(next((data / "queries").glob("*.parquet"))).to_pylist()         # [{_id, text}]
relevant: dict[str, set[str]] = {}
with open(data / "test.tsv", encoding="utf-8", newline="") as f:
    for row in csv.DictReader(f, delimiter="	"):
        if int(row["score"]) > 0:
            relevant.setdefault(str(row["query-id"]), set()).add(str(row["corpus-id"]))
queries = [q for q in queries if str(q["_id"]) in relevant]
if a.queries:
    queries = queries[:a.queries]
if a.docs:
    keep = set().union(*relevant.values())
    kept = [d for d in corpus if str(d["_id"]) in keep]
    corpus = kept + [d for d in corpus if str(d["_id"]) not in keep][:max(0, a.docs - len(kept))]
print(f"{len(corpus)} documents, {len(queries)} test queries, {sum(map(len, relevant.values()))} judgements", file=sys.stderr)


def connect_admin(dsn: str):
    base, name = dsn.rsplit("/", 1)
    return psycopg.connect(base + "/postgres", autocommit=True), name


with connect_admin(a.dsn)[0] as admin:
    name = a.dsn.rsplit("/", 1)[1]
    admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.execute(f'CREATE DATABASE "{name}"')

db = Database(a.dsn)
db.__enter__()
db.migrate()
db.bootstrap_solo()
doc_of: dict[int, str] = {}
with db.connection() as conn:
    case = create_case(conn, SOLO_ACTOR, "scifact")
    for r in corpus:
        body = f"{r['title']}. {r['text']}".strip()
        sha = hashlib.sha256(str(r["_id"]).encode()).digest()
        row = conn.execute(
            "INSERT INTO text_chunk (workspace_id, case_id, text_sha, start_char, end_char, body) VALUES (%s,%s,%s,0,%s,%s) RETURNING id",
            (SOLO_ACTOR.workspace_id, case, sha, len(body), body)).fetchone()
        doc_of[row["id"]] = str(r["_id"])

emb = FastEmbed(a.embed_model, a.rerank_model, a.cache, threads=a.threads)
t0 = time.time()
with db.connection() as conn:
    done = 0
    while True:
        n = ensure_embedded(conn, emb, case, limit=256)
        conn.commit()
        done += n
        print(f"embedded {done}/{len(doc_of)} in {time.time() - t0:.0f}s", file=sys.stderr, flush=True)
        if n == 0:
            break
embed_seconds = time.time() - t0


class Svc:
    """What retrieval.search needs from Services: no dedicated text ranker, optionally dense (and rerank)."""

    def __init__(self, dense):
        self.dense = dense

    def text_ids(self, *args, **kw):
        return None, None


plain = Dense(FastEmbed(a.embed_model, None, a.cache, threads=a.threads))          # same vectors, no reranker
full = Dense(emb)


def ndcg(ranked: list[str], rel: set[str], k: int = 10) -> float:
    import math

    dcg = sum(1 / math.log2(i + 2) for i, d in enumerate(ranked[:k]) if d in rel)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(len(rel), k)))
    return dcg / ideal if ideal else 0.0


def run(label: str, fn) -> dict:
    scores = {"ndcg10": [], "recall10": [], "mrr10": []}
    t = time.time()
    with db.connection() as conn:
        for q in queries:
            qid, text = str(q["_id"]), q["text"]
            ranked = [doc_of[i] for i in fn(conn, text)]
            rel = relevant[qid]
            scores["ndcg10"].append(ndcg(ranked, rel))
            scores["recall10"].append(len(set(ranked[:10]) & rel) / len(rel))
            first = next((i for i, d in enumerate(ranked[:10], 1) if d in rel), None)
            scores["mrr10"].append(1 / first if first else 0.0)
    n = len(queries)
    out = {k: round(sum(v) / n, 4) for k, v in scores.items()}
    out["seconds_per_query"] = round((time.time() - t) / n, 3)
    print(label, out, file=sys.stderr, flush=True)
    return out


def hits(svc):
    return lambda conn, text: [h["chunk_id"] for h in retrieval.search(conn, case, text, k=10, services=svc)["hits"]]


results = {
    "corpus": len(corpus), "queries": len(queries), "embed_model": a.embed_model, "rerank_model": a.rerank_model,
    "embed_seconds": round(embed_seconds), "threads": a.threads,
    "lexical (full-text only)": run("fts", lambda conn, t: retrieval._fts_ids(conn, case, t, 10)),
    "lexical + fuzzy (the no-model default)": run("lexical", hits(None)),
    "dense only": run("dense", lambda conn, t: plain.ids(conn, case, t, 10)[0]),
    "hybrid (full-text + fuzzy + dense, RRF)": run("hybrid", hits(Svc(plain))),
    "hybrid + cross-encoder rerank": run("hybrid+rerank", hits(Svc(full))),
}
Path(a.out).write_text(json.dumps(results, indent=2), encoding="utf-8")
print(json.dumps(results, indent=2))
