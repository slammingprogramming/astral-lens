"""Our own Postgres queue (astral.queues). Run with the BACKEND venv:
    python bench_pgqueue.py driver
"""

import os
import subprocess
import sys

import harness

HERE = os.path.dirname(os.path.abspath(__file__))


def worker_main(queue: str, conc: int) -> None:
    from astral.db import Database
    from astral.queues import Worker

    dsn = os.environ["BENCH_DSN"]
    db = Database(dsn, max_size=conc + 4).open()

    def handler(job):
        harness.run_job(dsn, job.payload["key"], job.payload["sleep"])
        return {}

    limits = {"gpu-large": 1}
    Worker(db, [queue], {"job": handler}, limits=limits).run_forever(threads=conc, poll=5.0)


def main() -> None:
    from astral.db import SOLO_ACTOR, Database
    from astral.queues import PgQueue

    dsn = harness.create_db("bench_pgqueue")
    db = Database(dsn).open()
    db.migrate()
    db.bootstrap_solo()
    q = PgQueue()
    env = {**os.environ, "BENCH_DSN": dsn, "PYTHONPATH": HERE}

    def spawn(queue, conc):
        return subprocess.Popen([sys.executable, __file__, "worker", queue, str(conc)], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def submit(queue, key, sleep):
        with db.connection() as conn:
            q.submit(conn, SOLO_ACTOR, queue, "job", {"key": key, "sleep": sleep})

    harness.emit(harness.measure("astral-pgqueue", dsn, spawn, submit))


if __name__ == "__main__":
    if sys.argv[1] == "worker":
        worker_main(sys.argv[2], int(sys.argv[3]))
    else:
        main()
