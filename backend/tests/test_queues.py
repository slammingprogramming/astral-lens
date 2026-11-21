from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest

from astral.queues import PgQueue, Worker


@pytest.fixture()
def q(db):
    # isolate from other tests' jobs
    with db.connection() as conn:
        conn.execute("DELETE FROM job")
    return PgQueue()


def test_unknown_resource_class_is_rejected(db, q, actor):
    with db.connection() as conn:
        with pytest.raises(ValueError, match="unknown queue"):
            q.submit(conn, actor, "gpu-huge", "s", {})


def test_dag_runs_in_dependency_order(db, q, actor):
    order = []
    with db.connection() as conn:
        a = q.submit(conn, actor, "cpu", "a", {})
        b = q.submit(conn, actor, "cpu", "b", {}, depends_on=[a])
        q.submit(conn, actor, "cpu", "c", {}, depends_on=[a, b])
    w = Worker(db, ["cpu"], {s: (lambda j, s=s: order.append(s) or {"ok": s}) for s in "abc"})
    assert w.run_until_idle() == 3
    assert order == ["a", "b", "c"]


def test_a_worker_only_serves_the_classes_its_host_has(db, q, actor):
    with db.connection() as conn:
        q.submit(conn, actor, "gpu-large", "llm", {})
        q.submit(conn, actor, "cpu", "hash", {})
    cpu_only = Worker(db, ["cpu"], {"hash": lambda j: {}, "llm": lambda j: {}})
    assert cpu_only.run_until_idle() == 1                    # the cpu-only profile runs no gpu worker
    with db.connection() as conn:
        left = conn.execute("SELECT queue FROM job WHERE state='queued'").fetchall()
    assert [r["queue"] for r in left] == ["gpu-large"]


def test_per_class_concurrency_cap_holds_across_workers(db, q, actor):
    with db.connection() as conn:
        for i in range(4):
            q.submit(conn, actor, "gpu-large", "llm", {"i": i})
    running, peak, lock = [0], [0], threading.Lock()

    def handler(job):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.2)
        with lock:
            running[0] -= 1
        return {}

    def run():
        Worker(db, ["gpu-large"], {"llm": handler}, limits={"gpu-large": 1}).run_until_idle()

    # claim happens under an advisory lock and the job is marked running before
    # the handler starts, so a second process sees the slot taken
    ts = [threading.Thread(target=run) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] == 1                                      # 'one large model on the 8 GB box'


def test_failure_retries_then_cancels_dependents_as_a_gap(db, q, actor):
    with db.connection() as conn:
        a = q.submit(conn, actor, "cpu", "boom", {}, max_attempts=2)
        b = q.submit(conn, actor, "cpu", "after", {}, depends_on=[a])

    def boom(job):
        raise RuntimeError("nope")

    w = Worker(db, ["cpu"], {"boom": boom, "after": lambda j: {}})
    w.run_once()
    with db.connection() as conn:                            # first failure: requeued with delay
        assert conn.execute("SELECT state FROM job WHERE id=%s", (a,)).fetchone()["state"] == "queued"
        conn.execute("UPDATE job SET run_after = now() WHERE id=%s", (a,))
    w.run_once()
    with db.connection() as conn:
        states = {r["id"]: r for r in conn.execute("SELECT id, state, error FROM job")}
    assert states[a]["state"] == "failed" and "nope" in states[a]["error"]
    assert states[b]["state"] == "cancelled" and "upstream" in states[b]["error"]


def test_crashed_workers_jobs_return_to_the_queue(db, q, actor):
    with db.connection() as conn:
        jid = q.submit(conn, actor, "cpu", "x", {})
    with db.connection() as conn:
        assert len(q.claim(conn, "dead-worker", ["cpu"])) == 1
    with db.connection() as conn:
        conn.execute("UPDATE job SET heartbeat_at = now() - interval '1 hour' WHERE id=%s", (jid,))
    with db.connection() as conn:
        assert q.reap_stale(conn, timedelta(minutes=5)) == 1
        assert conn.execute("SELECT state FROM job WHERE id=%s", (jid,)).fetchone()["state"] == "queued"


def test_concurrent_claimers_never_double_run_a_job(db, q, actor):
    with db.connection() as conn:
        for _ in range(12):
            q.submit(conn, actor, "cpu", "once", {})
    seen, lock = [], threading.Lock()

    def handler(job):
        with lock:
            seen.append(job.id)
        return {}

    ts = [threading.Thread(target=lambda: Worker(db, ["cpu"], {"once": handler}).run_until_idle())
          for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(seen) == 12 and len(set(seen)) == 12
