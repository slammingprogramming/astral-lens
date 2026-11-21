"""pgvector HNSW sizing, latency and recall (06 §9 item 3).

    python pgvector_bench.py            # runs the default case list

Data are SYNTHETIC but identity-clustered: unit-vector centroids, ten members
per identity at cos(member, centroid) ~ 0.78 (same-identity pairs ~0.6,
different ~0). Queries are fresh members of existing identities, so the true
neighbours are the identity's other photos. Random-direction data would be
unrealistically hard for ANN indexes; real face embeddings will differ, so
recall here is indicative, while SIZE and BUILD TIME depend only on N and
dimension and are the trustworthy numbers.
"""

import json
import os
import sys
import time

import numpy as np
import psycopg
from pgvector import HalfVector
from pgvector.psycopg import register_vector

DSN = "postgresql://postgres:astral@localhost:55433/postgres"
SIGMA = 0.8
PER_ID = 10
N_QUERIES = 200
K = 10


def unit(a):
    return a / np.linalg.norm(a, axis=1, keepdims=True)


EFS = tuple(int(e) for e in os.environ.get("EFS", "40,100").split(","))
CHUNK = 50_000         # vectors generated, loaded and scored at a time (bounded memory)


def centroids_and_queries(n, dim, seed):
    rng = np.random.default_rng(seed)
    n_id = n // PER_ID
    cent = unit(rng.standard_normal((n_id, dim), dtype=np.float32))
    q_id = rng.integers(0, n_id, N_QUERIES)
    queries = unit(cent[q_id] + SIGMA * unit(rng.standard_normal((N_QUERIES, dim), dtype=np.float32)))
    return cent, queries.astype(np.float32)


def chunk(cent, i0, i1, dim, seed):
    """Vectors i0..i1: member of identity i // PER_ID. Deterministic per chunk."""
    rng = np.random.default_rng(seed + 7919 * (i0 // CHUNK + 1))
    ids = np.arange(i0, i1) // PER_ID
    return unit(cent[ids] + SIGMA * unit(rng.standard_normal((i1 - i0, dim), dtype=np.float32))
                ).astype(np.float32)


def run(dim, n, vtype):
    seed = dim * 31 + n
    cent, queries = centroids_and_queries(n, dim, seed)
    best_s = np.full((N_QUERIES, K), -np.inf, np.float32)      # running exact top-10
    best_i = np.zeros((N_QUERIES, K), np.int64)
    out = {"dim": dim, "n": n, "type": vtype}

    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("CREATE EXTENSION IF NOT EXISTS vector")
        register_vector(c)
        c.execute("DROP TABLE IF EXISTS v")
        c.execute(f"CREATE TABLE v (id int, emb {vtype}({dim}))")
        t0 = time.time()
        gt_s = 0.0
        with c.cursor().copy("COPY v (id, emb) FROM STDIN") as cp:
            for i0 in range(0, n, CHUNK):
                i1 = min(n, i0 + CHUNK)
                data = chunk(cent, i0, i1, dim, seed)
                for j in range(len(data)):
                    cp.write_row((i0 + j, HalfVector(data[j]) if vtype == "halfvec" else data[j]))
                tg = time.time()                                # exact ground truth, streamed
                sc = queries @ data.T
                cand_s = np.concatenate([best_s, sc], axis=1)
                cand_i = np.concatenate([best_i, np.tile(np.arange(i0, i1), (N_QUERIES, 1))], axis=1)
                top = np.argpartition(-cand_s, K - 1, axis=1)[:, :K]
                best_s = np.take_along_axis(cand_s, top, 1)
                best_i = np.take_along_axis(cand_i, top, 1)
                gt_s += time.time() - tg
        gt = best_i
        out["load_s"] = round(time.time() - t0 - gt_s, 1)
        out["ground_truth_s"] = round(gt_s, 1)
        c.execute("VACUUM ANALYZE v")
        # pg_table_size includes the TOAST table: 2 KB vectors are stored out of line,
        # so pg_relation_size alone reports a misleadingly tiny heap.
        out["table_mb"] = round(c.execute("SELECT pg_table_size('v')").fetchone()[0] / 2**20, 1)

        ops = "halfvec_cosine_ops" if vtype == "halfvec" else "vector_cosine_ops"
        t0 = time.time()
        c.execute(f"CREATE INDEX v_hnsw ON v USING hnsw (emb {ops}) WITH (m=16, ef_construction=64)")
        out["hnsw_build_s"] = round(time.time() - t0, 1)
        idx = c.execute("SELECT pg_relation_size('v_hnsw')").fetchone()[0]
        out["hnsw_index_mb"] = round(idx / 2**20, 1)
        out["hnsw_bytes_per_vector"] = round(idx / n, 1)
        out["table_plus_index_mb_per_million"] = round(
            (out["table_mb"] + out["hnsw_index_mb"]) * 1e6 / n, 0)

        cast = "::halfvec(%d)" % dim if vtype == "halfvec" else ""
        for ef in EFS:
            c.execute(f"SET hnsw.ef_search = {ef}")
            for q in queries[:30]:
                c.execute(f"SELECT id FROM v ORDER BY emb <=> %s{cast} LIMIT {K}", (q,)).fetchall()
            lat, hits = [], 0
            for qi, q in enumerate(queries):
                t = time.perf_counter()
                got = [r[0] for r in c.execute(
                    f"SELECT id FROM v ORDER BY emb <=> %s{cast} LIMIT {K}", (q,)).fetchall()]
                lat.append((time.perf_counter() - t) * 1000)
                hits += len(set(got) & set(gt[qi].tolist()))
            lat.sort()
            out[f"ef{ef}"] = {"recall_at_10": round(hits / (K * N_QUERIES), 4),
                              "p50_ms": round(lat[len(lat) // 2], 2),
                              "p95_ms": round(lat[int(len(lat) * 0.95)], 2)}
        c.execute("DROP TABLE v")
    return out


if __name__ == "__main__":
    # 1M-row cases overran a 16 GB Docker VM on a shared 32 GB host and took the
    # Docker engine (and other people's containers) down, so the default stops at
    # 300k rows. Index size is linear in N; build time and recall are not.
    cases = [(512, 100_000, "vector"), (512, 300_000, "vector"),
             (512, 300_000, "halfvec"), (128, 300_000, "vector")]
    if len(sys.argv) > 1:                    # e.g.  512:300000:vector 128:300000:vector
        cases = [(int(a), int(b), c) for a, b, c in (x.split(":") for x in sys.argv[1:])]
    for dim, n, t in cases:
        print(json.dumps(run(dim, n, t)), flush=True)
