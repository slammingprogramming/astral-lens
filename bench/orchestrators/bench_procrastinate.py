"""procrastinate adapter. Run with the bench venv:  python bench_procrastinate.py driver"""

import os
import subprocess
import sys

import procrastinate

import harness

DSN = os.environ.get("BENCH_DSN") or harness.dsn_for("bench_procrastinate")
app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=DSN),
                        import_paths=["bench_procrastinate"])


@app.task(name="job_cpu", queue="cpu")
def job_cpu(key: str, sleep: float):
    harness.run_job(DSN, key, sleep)


@app.task(name="job_gpu", queue="gpu-large")
def job_gpu(key: str, sleep: float):
    harness.run_job(DSN, key, sleep)


def main() -> None:
    dsn = harness.create_db("bench_procrastinate")
    env = {**os.environ, "BENCH_DSN": dsn, "PYTHONPATH": os.path.dirname(__file__)}
    py = sys.executable
    subprocess.run([py, "-m", "procrastinate", "--app=bench_procrastinate.app", "schema", "--apply"],
                   env=env, check=True, capture_output=True)

    def spawn(queue: str, conc: int):
        return subprocess.Popen(
            [py, "-m", "procrastinate", "--app=bench_procrastinate.app", "worker",
             "--queues", queue, "--concurrency", str(conc)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    with app.open():
        def submit(queue, key, sleep):
            (job_gpu if queue == "gpu-large" else job_cpu).defer(key=key, sleep=sleep)

        harness.emit(harness.measure("procrastinate", dsn, spawn, submit))


if __name__ == "__main__":
    main()
