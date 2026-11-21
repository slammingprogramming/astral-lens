"""Resource-aware job queues over the one Postgres (ADR-0003, invariant 6).

Queues are partitioned by resource class, not task type: cpu, gpu-small,
gpu-large, remote. A worker subscribes only to classes its host can serve, so
the cpu-only profile simply runs no gpu-* workers. Per-class concurrency is
capped *across processes* by counting running jobs under an advisory lock,
which is the external-semaphore option 05 §3 allows.

The orchestrator sits behind the narrow `Orchestrator` protocol so the
Hatchet / DBOS / procrastinate decision stays reversible (ADR-0003).
"""

from __future__ import annotations

import json
import socket
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

import psycopg

from astral import audit
from astral.db import Actor, Database

QUEUES = ("cpu", "gpu-small", "gpu-large", "remote")


@dataclass
class Job:
    id: uuid.UUID
    queue: str
    stage: str
    payload: dict[str, Any]
    case_id: uuid.UUID | None
    attempts: int
    max_attempts: int


class Orchestrator(Protocol):
    def submit(self, conn: psycopg.Connection, actor: Actor, queue: str, stage: str,
               payload: dict[str, Any], **kw: Any) -> uuid.UUID: ...
    def claim(self, conn: psycopg.Connection, worker_id: str, queues: list[str],
              limits: dict[str, int]) -> list[Job]: ...
    def complete(self, conn: psycopg.Connection, job_id: uuid.UUID, result: dict[str, Any]) -> None: ...
    def fail(self, conn: psycopg.Connection, job_id: uuid.UUID, error: str, **kw: Any) -> str: ...


class PgQueue:
    def submit(
        self, conn: psycopg.Connection, actor: Actor, queue: str, stage: str,
        payload: dict[str, Any], *, case_id: uuid.UUID | None = None,
        depends_on: list[uuid.UUID] | None = None, max_attempts: int = 3,
        run_after: datetime | None = None,
    ) -> uuid.UUID:
        if queue not in QUEUES:
            raise ValueError(f"unknown queue {queue!r}; classes are {QUEUES}")
        jid = uuid.uuid4()
        conn.execute(
            "INSERT INTO job (id, workspace_id, case_id, queue, stage, payload, depends_on,"
            " max_attempts, run_after) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (jid, actor.workspace_id, case_id, queue, stage, json.dumps(payload),
             depends_on or [], max_attempts, run_after or datetime.now(timezone.utc)),
        )
        audit.record(conn, actor, "job.submit", case_id=case_id, target_kind="job",
                     target_id=str(jid), detail={"queue": queue, "stage": stage})
        conn.execute("SELECT pg_notify('astral_jobs', %s)", (queue,))   # delivered at commit
        return jid

    def claim(
        self, conn: psycopg.Connection, worker_id: str, queues: list[str],
        limits: dict[str, int] | None = None, *, batch: int = 1,
    ) -> list[Job]:
        limits = limits or {}
        out: list[Job] = []
        for q in queues:
            # serialize claimers per queue so the concurrency cap cannot race
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"queue:{q}",))
            room = batch
            cap = limits.get(q)
            if cap is not None:
                running = conn.execute(
                    "SELECT count(*) AS n FROM job WHERE queue = %s AND state = 'running'", (q,)
                ).fetchone()["n"]
                room = min(room, cap - running)
            if room <= 0:
                continue
            rows = conn.execute(
                "SELECT j.id FROM job j WHERE j.queue = %s AND j.state = 'queued'"
                " AND j.run_after <= now()"
                " AND NOT EXISTS (SELECT 1 FROM unnest(j.depends_on) d"
                "                 JOIN job dj ON dj.id = d WHERE dj.state <> 'succeeded')"
                " ORDER BY j.run_after, j.created_at FOR UPDATE OF j SKIP LOCKED LIMIT %s",
                (q, room),
            ).fetchall()
            for r in rows:
                j = conn.execute(
                    "UPDATE job SET state='running', attempts=attempts+1, locked_by=%s,"
                    " locked_at=now(), heartbeat_at=now() WHERE id=%s"
                    " RETURNING id, queue, stage, payload, case_id, attempts, max_attempts",
                    (worker_id, r["id"]),
                ).fetchone()
                out.append(Job(**j))
        return out

    def complete(self, conn: psycopg.Connection, job_id: uuid.UUID, result: dict[str, Any]) -> None:
        conn.execute(
            "UPDATE job SET state='succeeded', result=%s, finished_at=now(), error=NULL"
            " WHERE id=%s", (json.dumps(result), job_id))

    def fail(self, conn: psycopg.Connection, job_id: uuid.UUID, error: str, *,
             retry_delay: float = 5.0) -> str:
        """Requeue with delay while attempts remain; otherwise fail and cancel
        dependents — a gap, not a silent hole (partial results, 02 §9)."""
        j = conn.execute("SELECT attempts, max_attempts FROM job WHERE id=%s", (job_id,)).fetchone()
        if j["attempts"] < j["max_attempts"]:
            conn.execute(
                "UPDATE job SET state='queued', error=%s, locked_by=NULL, locked_at=NULL,"
                " run_after = now() + make_interval(secs => %s) WHERE id=%s",
                (error, retry_delay, job_id))
            return "requeued"
        conn.execute(
            "UPDATE job SET state='failed', error=%s, finished_at=now() WHERE id=%s",
            (error, job_id))
        self._cancel_dependents(conn, job_id)
        return "failed"

    def _cancel_dependents(self, conn: psycopg.Connection, failed: uuid.UUID) -> None:
        frontier = [failed]
        while frontier:
            nxt = []
            for f in frontier:
                for r in conn.execute(
                    "UPDATE job SET state='cancelled', error=%s, finished_at=now()"
                    " WHERE %s = ANY(depends_on) AND state IN ('queued') RETURNING id",
                    (f"upstream job {f} failed", f),
                ):
                    nxt.append(r["id"])
            frontier = nxt

    def reap_stale(self, conn: psycopg.Connection, older_than: timedelta) -> int:
        """Return crashed workers' jobs to the queue."""
        cur = conn.execute(
            "UPDATE job SET state='queued', locked_by=NULL, locked_at=NULL,"
            " error='worker lost' WHERE state='running' AND heartbeat_at < now() - %s",
            (older_than,))
        return cur.rowcount

    def heartbeat(self, conn: psycopg.Connection, job_id: uuid.UUID) -> None:
        conn.execute("UPDATE job SET heartbeat_at = now() WHERE id=%s", (job_id,))


Handler = Callable[[Job], dict[str, Any]]


class Worker:
    """Runs jobs for the queues this host can serve."""

    def __init__(self, db: Database, queues: list[str], handlers: dict[str, Handler],
                 limits: dict[str, int] | None = None, orchestrator: PgQueue | None = None,
                 worker_id: str | None = None) -> None:
        self.db, self.queues, self.handlers = db, queues, handlers
        self.limits = limits or {}
        self.orch = orchestrator or PgQueue()
        self.worker_id = worker_id or f"{socket.gethostname()}:{uuid.uuid4().hex[:6]}"
        self._stop = threading.Event()

    def run_once(self) -> int:
        with self.db.connection() as conn:
            jobs = self.orch.claim(conn, self.worker_id, self.queues, self.limits)
        for job in jobs:
            self._run(job)
        return len(jobs)

    def _run(self, job: Job) -> None:
        handler = self.handlers.get(job.stage)
        try:
            if handler is None:
                raise LookupError(f"no handler for stage {job.stage!r}")
            result = handler(job)
        except Exception as e:  # noqa: BLE001 - any handler failure is a recorded gap
            with self.db.connection() as conn:
                self.orch.fail(conn, job.id, f"{type(e).__name__}: {e}")
            return
        with self.db.connection() as conn:
            self.orch.complete(conn, job.id, result)

    def run_forever(self, *, threads: int = 1, poll: float = 5.0) -> None:
        """Serve the queues until stop(). Wakes on NOTIFY from submit() and
        falls back to polling every `poll` seconds (covers delayed retries and
        dependency unblocking, which do not NOTIFY)."""
        def loop() -> None:
            with psycopg.connect(self.db.dsn, autocommit=True) as listener:
                listener.execute("LISTEN astral_jobs")
                while not self._stop.is_set():
                    if self.run_once() == 0:
                        for _ in listener.notifies(timeout=poll, stop_after=1):
                            break

        pool = [threading.Thread(target=loop, daemon=True) for _ in range(threads)]
        for t in pool:
            t.start()
        for t in pool:
            t.join()

    def run_until_idle(self, max_rounds: int = 1000) -> int:
        total = 0
        for _ in range(max_rounds):
            n = self.run_once()
            if n == 0:
                break
            total += n
        return total

    def stop(self) -> None:
        self._stop.set()
