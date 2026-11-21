# ADR-0003: Orchestration without a second datastore

**Status:** Accepted (2026-11-05) — supersedes the earlier *Proposed* text.
The decision is the agent's, taken on measurement, and reverses the earlier
"Hatchet primary" framing; the project owner may veto it.

## Context

Astral Lens needs durable, resumable, DAG-shaped job execution: pipelines that
run for hours, checkpoint per node, survive restarts, and route work to
resource-partitioned worker pools (`cpu`, `gpu-small`, `gpu-large`, `remote`).

The reference target is a machine with 8 GB of VRAM and 32 GB of RAM running
Postgres, the API, workers and resident models simultaneously. Every additional
resident service competes for exactly the resource that is scarce. Two
invariants decide the shortlist before any feature comparison
([design invariants](../13-design-invariants.md)): **no second datastore at the
small tier** (6) and **the small tier must work with no container runtime** (7).

## What the research found (2026-11-05, primary docs)

| Candidate | Small-tier fit |
|---|---|
| **Hatchet** | Engine, API and dashboard are separate services. Hatchet Lite, Docker Compose and `hatchet server start` **all require Docker**; no native engine binary is documented. An "embedded mode" runs the engine and Postgres inside the worker process but is documented for dev/CI only. **Fails invariant 7 as documented.** Not benchmarked: this machine does have Docker, but a footprint number cannot rescue a candidate that is ineligible on the invariant, so its images were not pulled. If a Docker-free engine build exists, that conclusion should be revisited. |
| **procrastinate** (MIT) | pip library, no server, Postgres 13+. Named queues; a worker selects queues. No DAG, no durable step state, no documented cross-process concurrency cap. |
| **DBOS Transact** (MIT) | pip library, no server, Postgres only. Durable step checkpoints, code-level DAGs, per-queue `worker_concurrency` and cross-process `global_concurrency`, process-level queue listening. |
| PgQueuer, Absurd | Libraries; smaller or younger; DAG and cron not documented. |
| Prefect, Dagster | Several processes at rest; SQLite default. Rejected for the small tier. |
| Temporal | Several Go services; Postgres suffices but it is a cluster-tier option. |
| Restate | BSL 1.1, not OSI-approved. Excluded. |
| Windmill | AGPL-3.0, Docker-first. Excluded. |

## Measurement

`bench/orchestrators/` runs one scenario against each candidate on the
reference box (8-core / 16-thread desktop CPU, 32 GB RAM, 8 GB-VRAM consumer GPU; Postgres 16 in a
container on the same machine). Two worker processes — `cpu` (concurrency 4)
and `gpu-large` (concurrency 1) — then: 10 s idle; 100 trivial `cpu` jobs
submitted one at a time while a `gpu-large` job holds the GPU queue for 8 s;
three 1 s `gpu-large` jobs to check the cap; 200 trivial jobs for throughput.

| | Idle RSS, both workers | Idle CPU (10 s) | submit→start p50 / p95 while GPU busy | `gpu-large` cap | Throughput |
|---|---|---|---|---|---|
| **Astral PgQueue** (this repo, `astral.queues`) | 92 MB | 0.0% | **8.0 / 9.1 ms** | held | 34 jobs/s |
| **procrastinate** 3.10.0 | 98 MB | 0.0% | **8.1 / 10.7 ms** | held\* | 38 jobs/s |
| **DBOS Transact** 3.4.0, default 1 s polling | 156 MB | 0.0% | **1016 / 1054 ms** | held | 3.9 jobs/s |
| DBOS Transact, `polling_interval_sec=0.1` | 157 MB | 0.0% | 108 / 112 ms | held | 32 jobs/s |

\* procrastinate respects the cap in this test only because there is a single
`gpu-large` worker with concurrency 1. The library has no cross-process cap;
the benchmark does not discriminate there. DBOS's `global_concurrency` and
PgQueue's advisory-lock count both enforce it across processes.

**What these numbers do not show.** One run per candidate, trivial jobs,
one machine, localhost Postgres. No failure injection (crash recovery, poison
jobs, restarts) was run against the libraries; only PgQueue's recovery is
covered by this repository's tests. Idle CPU reads 0.0% to one decimal for all
four. Hatchet was not measured. Treat the table as a footprint and latency
comparison, not a reliability verdict.

**Hazards found while measuring.**

- DBOS 3.x depends on SQLAlchemy 2.1, whose compiled extension **failed to
  load from a venv path over Windows' 260-character limit** ("filename or
  extension is too long"). The project's own checkout path here is long, so a
  Windows install under a deep directory would hit it.
- DBOS prints a promotional link to "DBOS Conductor" at launch. **Outbound
  behaviour audited 2026-11-06 by reading `dbos-transact-py` source** (3.4.0 /
  commit 718d066, with some files from main 236142b; read through fetch tools
  that summarise, so no line numbers and quotes are approximate). Findings:
  no telemetry, analytics or update check was found; the Conductor websocket
  (`wss://cloud.dbos.dev/conductor/v1alpha1`) connects only if `conductor_key`
  is set; an entitlement check to the Conductor host runs only if
  `auto_upgrade`, `sharding`, `suspend_mode` or `external_stores` is set (with
  no key it raises and sends nothing); OTLP traces and logs are off unless
  `enable_otlp` is set; 3.x opens no admin HTTP listener (the older
  `_admin_server.py` existed at 2.31.1); `dbos postgres start`, the GitHub
  template command and the Kafka decorator reach out only when invoked. So a
  no-egress deployment looks possible on the default configuration — **but the
  Conductor client lives in a closed `dbos_enterprise` package that was not
  audited, `_sys_db*.py`/`_scheduler.py` were not read for URL literals, and an
  `elif auto_upgradable:` branch in `_launch` was not fully traced.** Confirm
  with a packet capture or a deny-all-egress run before adoption.
- DBOS's default queue polling (1 s) puts a ~1 s floor under job-start latency;
  fixing it costs polling load on Postgres (not measured at scale).

## Decision

1. **Hatchet is not the small-tier orchestrator.** It violates invariant 7 as
   documented. It stays a cluster-tier candidate, to be measured when a
   cluster tier exists.
2. **The orchestrator remains behind the narrow `Orchestrator` protocol**
   (`astral.queues`). That is the load-bearing decision; the substrate is
   replaceable.
3. **For M0–M2 the substrate is the in-repo Postgres queue (`PgQueue`).** It
   matches procrastinate on footprint and latency, has no dependency beyond
   `psycopg`, and already provides the three things neither library gives for
   free: **DAG dependencies with failure propagation as a visible gap**,
   **cross-process per-resource-class concurrency caps** (an advisory-lock
   count — the "external semaphore" option of
   [05 §3](../05-compute-profiles.md)), and crash reaping. The Lens runner's
   node-level checkpointing maps onto job granularity.
4. **Reconsideration triggers** — adopt a library behind the same protocol if:
   scheduled/cron work, rate limiting or backoff policies outgrow what
   `PgQueue` offers (adopt **procrastinate** for scheduling and retries);
   step-level durable workflows inside a single node become necessary, or the
   cluster tier needs hardened multi-node semantics (evaluate **DBOS** — source audit
   found no default egress, runtime confirmation still owed — and **Hatchet**
   at the cluster tier).

## Consequences

- We own a small piece of infrastructure (~250 lines plus tests). That is real
  maintenance debt, accepted because the alternative also required building the
  DAG and cap layers above a library, as this ADR originally anticipated.
- The benchmark harness is in the repository so the decision can be re-run
  with new versions or on other hardware.
- `PgQueue` has no scheduler. Plugin self-checks on an interval (04 §5) need a
  periodic trigger; until one exists they run on demand.
- A heavier orchestrator is acceptable at the cluster tier, but the small tier
  sets the constraint, because the small tier is this project's primary user.
