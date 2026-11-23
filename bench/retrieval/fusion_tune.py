"""Tune the weights of the rank fusion on SciFact's TRAIN claims; score the result once on its TEST claims.

    python fusion_tune.py DATA_DIR MODELS_CACHE OUT.json [--dsn DSN] [--docs N] [--threads N] [--rankings CACHE.json]

DATA_DIR is the scifact_eval.py layout plus train.tsv (BeIR/scifact-qrels, train split). Each abstract is one
chunk, as in scifact_eval.py. The three rankers the default stack has (Postgres full-text, the fuzzy ranker,
dense e5) are each run ONCE per claim and their top-50 lists cached (--rankings), so the weight search is pure
arithmetic and can be re-run without re-embedding.

Fusion: score(d) = sum over rankers r of w_r / (k + rank_r(d)). The search is a small grid over the full-text
and fuzzy weights (dense fixed at 1) and k, chosen by mean nDCG@10 on the train claims only. Test claims are
scored with the chosen setting, with equal weights (what shipped before) and with each ranker alone, so the
change can be judged on claims the search never saw. The same is done for the no-model default (full-text +
fuzzy, no dense).

What this does not show. SciFact is clean English scientific text with about one relevant abstract per claim;
weights tuned here are a better default than equal weights for THAT kind of text and are not known to transfer
to scanned, noisy or multilingual investigative material. The corpus cut is labelled in the output. The data
is CC BY-NC 4.0 (SciFact, AllenAI): used for this check, not shipped.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import math
import sys
import time
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
ap.add_argument("--dsn", default="postgresql://postgres:astral@localhost:55432/astral_scifact_tune")
ap.add_argument("--docs", type=int, default=2000, help="corpus size; every judged-relevant document is kept")
ap.add_argument("--threads", type=int, default=8)
ap.add_argument("--embed-model", default="intfloat/multilingual-e5-large")
ap.add_argument("--rankings", default="", help="cache of per-claim ranker lists; reused if it exists")
a = ap.parse_args()
data = Path(a.data)


def qrels(name: str) -> dict[str, set[str]]:
    rel: dict[str, set[str]] = {}
    with open(data / name, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            if int(row["score"]) > 0:
                rel.setdefault(str(row["query-id"]), set()).add(str(row["corpus-id"]))
    return rel


splits = {"train": qrels("train.tsv"), "test": qrels("test.tsv")}
cache = Path(a.rankings) if a.rankings else None

if cache and cache.exists():
    lists = json.loads(cache.read_text(encoding="utf-8"))
    meta = lists.pop("_meta")
    print(f"reusing cached rankings for {sum(len(v) for v in lists.values())} claims", file=sys.stderr)
else:
    corpus = pq.read_table(next((data / "corpus").glob("*.parquet"))).to_pylist()
    queries = {str(q["_id"]): q["text"] for q in pq.read_table(next((data / "queries").glob("*.parquet"))).to_pylist()}
    keep = set().union(*splits["train"].values(), *splits["test"].values())
    kept = [d for d in corpus if str(d["_id"]) in keep]
    corpus = kept + [d for d in corpus if str(d["_id"]) not in keep][:max(0, a.docs - len(kept))]
    meta = {"corpus": len(corpus), "judged_relevant_kept": len(kept), "embed_model": a.embed_model}
    print(f"{len(corpus)} documents ({len(kept)} judged relevant)", file=sys.stderr)

    base, name = a.dsn.rsplit("/", 1)
    with psycopg.connect(base + "/postgres", autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{name}"')
    db = Database(a.dsn)
    db.__enter__()
    db.migrate()
    db.bootstrap_solo()
    doc_of: dict[int, str] = {}
    with db.connection() as conn:
        case = create_case(conn, SOLO_ACTOR, "scifact-tune")
        for r in corpus:
            body = f"{r['title']}. {r['text']}".strip()
            row = conn.execute(
                "INSERT INTO text_chunk (workspace_id, case_id, text_sha, start_char, end_char, body) VALUES (%s,%s,%s,0,%s,%s) RETURNING id",
                (SOLO_ACTOR.workspace_id, case, hashlib.sha256(str(r["_id"]).encode()).digest(), len(body), body)).fetchone()
            doc_of[row["id"]] = str(r["_id"])
    emb = FastEmbed(a.embed_model, None, a.cache, threads=a.threads)
    t0 = time.time()
    with db.connection() as conn:
        done = 0
        while (n := ensure_embedded(conn, emb, case, limit=256)):
            conn.commit()
            done += n
            print(f"embedded {done}/{len(doc_of)} in {time.time() - t0:.0f}s", file=sys.stderr, flush=True)
    meta["embed_seconds"] = round(time.time() - t0)
    dense = Dense(emb)
    lists = {}
    t0 = time.time()
    with db.connection() as conn:
        for split, rel in splits.items():
            lists[split] = {}
            for i, qid in enumerate(q for q in rel if q in queries):
                text = queries[qid]
                lists[split][qid] = {
                    "fts": [doc_of[x] for x in retrieval._fts_ids(conn, case, text, 50)],
                    "fuzzy": [doc_of[x] for x in retrieval._fuzzy_ids(conn, case, text, 50)],
                    "dense": [doc_of[x] for x in dense.ids(conn, case, text, 50)[0]]}
                if i % 100 == 0:
                    print(f"{split}: ranked {i} claims in {time.time() - t0:.0f}s", file=sys.stderr, flush=True)
    meta["rank_seconds"] = round(time.time() - t0)
    if cache:
        cache.write_text(json.dumps({"_meta": meta, **lists}), encoding="utf-8")


def fuse(lst: dict[str, list[str]], w: dict[str, float], k: int) -> list[str]:
    score: dict[str, float] = {}
    for r, wr in w.items():
        if wr:
            for i, d in enumerate(lst[r], 1):
                score[d] = score.get(d, 0.0) + wr / (k + i)
    return [d for d, _ in sorted(score.items(), key=lambda x: (-x[1], x[0]))]


def metrics(split: str, w: dict[str, float], k: int) -> dict[str, float]:
    nd, rc, mr = [], [], []
    for qid, lst in lists[split].items():
        rel = splits[split][qid]
        ranked = fuse(lst, w, k)[:10]
        dcg = sum(1 / math.log2(i + 2) for i, d in enumerate(ranked) if d in rel)
        nd.append(dcg / sum(1 / math.log2(i + 2) for i in range(min(len(rel), 10))))
        rc.append(len(set(ranked) & rel) / len(rel))
        first = next((i for i, d in enumerate(ranked, 1) if d in rel), None)
        mr.append(1 / first if first else 0.0)
    n = len(nd)
    return {"ndcg10": round(sum(nd) / n, 4), "recall10": round(sum(rc) / n, 4), "mrr10": round(sum(mr) / n, 4), "claims": n}


W_LEX, W_FUZ, KS = (0, 0.1, 0.25, 0.5, 0.75, 1.0), (0, 0.1, 0.25, 0.5, 1.0), (5, 10, 20, 40, 60, 100)


def search(dense_on: bool) -> tuple[dict[str, float], int, float]:
    best = None
    for wf, wz, k in itertools.product(W_LEX if dense_on else (1.0,), W_FUZ, KS):
        w = {"fts": wf, "fuzzy": wz, "dense": 1.0 if dense_on else 0.0}
        if not any(w.values()):
            continue
        s = metrics("train", w, k)["ndcg10"]
        if best is None or s > best[2] + 1e-9:
            best = (w, k, s)
    return best


out: dict = {"protocol": "weights chosen on train claims by nDCG@10; test claims scored once", **meta,
             "grid": {"fts": W_LEX, "fuzzy": W_FUZ, "k": KS, "dense": 1.0}}
for dense_on, label in ((True, "hybrid (full-text + fuzzy + dense)"), (False, "lexical (full-text + fuzzy, no model)")):
    w, k, train_score = search(dense_on)
    equal = {"fts": 1.0, "fuzzy": 1.0, "dense": 1.0 if dense_on else 0.0}
    rows = {"chosen": {"weights": w, "k": k, "train_ndcg10": train_score},
            "test: equal weights, k=60 (before)": metrics("test", equal, 60),
            "test: chosen weights": metrics("test", w, k),
            "train: equal weights, k=60": metrics("train", equal, 60)}
    for r in ("fts", "fuzzy") + (("dense",) if dense_on else ()):
        rows[f"test: {r} alone"] = metrics("test", {r: 1.0}, 60)
    out[label] = rows
    print(label, json.dumps(rows, indent=1), file=sys.stderr, flush=True)
Path(a.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
print(json.dumps(out, indent=2))
