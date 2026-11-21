"""DBOS Transact adapter (3.x API). Run from a SHORT-path venv:  python bench_dbos.py driver

Note: on Windows, DBOS pulls SQLAlchemy 2.1, whose compiled extension fails to
load ("filename or extension is too long") when the venv path exceeds MAX_PATH.
That is an environment hazard worth recording for deep install locations.
"""

import os
import subprocess
import sys
import time

from dbos import DBOS

import harness

HERE = os.path.dirname(os.path.abspath(__file__))
Q = {}


@DBOS.workflow()
def job(dsn: str, key: str, sleep: float) -> None:
    harness.run_job(dsn, key, sleep)


def init(dsn: str, listen: list[str]) -> None:
    DBOS(config={"name": "bench", "system_database_url": dsn, "run_admin_server": False})
    DBOS.listen_queues(listen)                # named, and must precede launch
    DBOS.launch()
    # Queues are persisted rows registered after launch. global_concurrency is
    # the cross-process cap: 'one large model on the 8 GB box'.
    poll = float(os.environ.get("BENCH_POLL", "1.0"))        # 1.0 is the shipped default
    Q["cpu"] = DBOS.register_queue("cpu", worker_concurrency=4, polling_interval_sec=poll)
    Q["gpu-large"] = DBOS.register_queue("gpu-large", worker_concurrency=1, global_concurrency=1,
                                         polling_interval_sec=poll)


def main() -> None:
    dsn = harness.create_db("bench_dbos")
    init(dsn, listen=[])                      # the driver itself serves no queue
    env = {**os.environ, "BENCH_DSN": dsn, "PYTHONPATH": HERE}

    def spawn(queue, conc):
        return subprocess.Popen([sys.executable, __file__, "worker", queue], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def submit(queue, key, sleep):
        Q[queue].enqueue(job, dsn, key, sleep)

    harness.emit(harness.measure("dbos-transact", dsn, spawn, submit, startup_wait=15))


if __name__ == "__main__":
    if sys.argv[1] == "worker":
        init(os.environ["BENCH_DSN"], listen=[sys.argv[2]])
        while True:
            time.sleep(3600)
    else:
        main()
