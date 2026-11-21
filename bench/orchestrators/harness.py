"""Shared measurement protocol for the ADR-0003 orchestrator benchmark.

Every candidate runs the same scenario on the same Postgres:

  1. start two worker processes: `cpu` (concurrency 4) and `gpu-large` (concurrency 1)
  2. IDLE      sample RSS of the worker process trees and CPU% for 10 s
  3. LOADED    keep gpu-large busy with one 8 s job; submit 100 trivial cpu jobs one
               at a time and record submit -> job-start latency
  4. CAP       submit 3 gpu-large jobs of 1 s; the cap holds if they never overlap
  5. THROUGHPUT submit 200 trivial cpu jobs at once; time to drain

Jobs write (start, end) wall-clock times to bench_result so every candidate is
timed the same way regardless of what results API it has.
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from typing import Callable

import psutil
import psycopg

ADMIN = "postgresql://postgres:astral@localhost:55432/postgres"


def dsn_for(db: str) -> str:
    return f"postgresql://postgres:astral@localhost:55432/{db}"


def create_db(name: str) -> str:
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        c.execute(f'CREATE DATABASE "{name}"')
    d = dsn_for(name)
    with psycopg.connect(d, autocommit=True) as c:
        c.execute("CREATE TABLE bench_result (key text PRIMARY KEY, started double precision,"
                  " ended double precision)")
    return d


def run_job(dsn: str, key: str, sleep: float) -> None:
    """The unit of work every candidate executes."""
    t0 = time.time()
    if sleep:
        time.sleep(sleep)
    t1 = time.time()
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("INSERT INTO bench_result VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                  (key, t0, t1))


def _tree_rss_mb(procs: list[psutil.Process]) -> float:
    total = 0
    for p in procs:
        try:
            total += p.memory_info().rss
            total += sum(c.memory_info().rss for c in p.children(recursive=True))
        except psutil.Error:
            pass
    return total / 2**20


def _cpu(procs: list[psutil.Process]) -> float:
    t = 0.0
    for p in procs:
        try:
            t += p.cpu_percent(None)
            t += sum(c.cpu_percent(None) for c in p.children(recursive=True))
        except psutil.Error:
            pass
    return t


def _wait_row(dsn: str, key: str, timeout: float = 60) -> tuple[float, float]:
    end = time.time() + timeout
    with psycopg.connect(dsn, autocommit=True) as c:
        while time.time() < end:
            r = c.execute("SELECT started, ended FROM bench_result WHERE key=%s", (key,)).fetchone()
            if r:
                return r
            time.sleep(0.002)
    raise TimeoutError(key)


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def measure(name: str, dsn: str, spawn: Callable[[str, int], subprocess.Popen],
            submit: Callable[[str, str, float], None], startup_wait: float = 10.0) -> dict:
    workers = [spawn("cpu", 4), spawn("gpu-large", 1)]
    time.sleep(startup_wait)
    for w in workers:
        if w.poll() is not None:
            raise RuntimeError(f"{name}: worker exited early with {w.returncode}")
    procs = [psutil.Process(w.pid) for w in workers]
    out: dict = {"candidate": name}
    try:
        _cpu(procs)                                   # prime the counters
        time.sleep(10)
        out["idle_cpu_percent_10s"] = round(_cpu(procs), 2)
        out["idle_rss_mb_total"] = round(_tree_rss_mb(procs), 1)

        submit("gpu-large", "load", 8.0)
        time.sleep(1.0)
        lats = []
        for i in range(100):
            t = time.time()
            submit("cpu", f"lat{i}", 0.0)
            started, _ = _wait_row(dsn, f"lat{i}")
            lats.append((started - t) * 1000)
        out["latency_ms_while_gpu_busy"] = {
            "p50": round(statistics.median(lats), 1), "p95": round(_pct(lats, 0.95), 1),
            "max": round(max(lats), 1)}
        _wait_row(dsn, "load", 30)

        for i in range(3):
            submit("gpu-large", f"cap{i}", 1.0)
        spans = sorted(_wait_row(dsn, f"cap{i}", 30) for i in range(3))
        overlap = any(spans[i + 1][0] < spans[i][1] - 0.01 for i in range(2))
        out["gpu_large_cap_1_respected"] = not overlap

        t = time.time()
        for i in range(200):
            submit("cpu", f"tp{i}", 0.0)
        for i in range(200):
            _wait_row(dsn, f"tp{i}", 60)
        out["throughput_jobs_per_s"] = round(200 / (time.time() - t), 1)
        out["rss_mb_after_load"] = round(_tree_rss_mb(procs), 1)
    finally:
        for w in workers:
            w.kill()
    return out


def emit(result: dict) -> None:
    print(json.dumps(result))
    sys.stdout.flush()
