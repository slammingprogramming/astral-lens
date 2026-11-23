# 02 — Architecture

## 1. Shape

```
┌──────────────────────────────────────────────────────────────┐
│  React + TypeScript UI    ·    MCP server    ·    CLI        │
└───────────────────────────────┬──────────────────────────────┘
                                │  FastAPI (REST + SSE)
┌───────────────────────────────▼──────────────────────────────┐
│  Policy layer — authz, budgets, egress rules, footprint gate │
├──────────────────────────────────────────────────────────────┤
│  Planner        Lens runner        Resolver      Reporter    │
├──────────────────────────────────────────────────────────────┤
│  Orchestrator  ·  resource-aware queues (cpu/gpu-small/…)    │
├───────────────┬──────────────────┬───────────────────────────┤
│ Plugin host   │ Model residency  │  Assertion writer         │
│ in-proc/ctr   │ manager          │  (append-only)            │
└───────┬───────┴────────┬─────────┴───────────┬───────────────┘
        │                │                     │
   plugins (04)    backends (05/06/08)    Postgres + blob store (03)
```

Everything above the orchestrator is stateless. All state lives in Postgres and
the content-addressed artifact store, which is what makes the single-box and
multi-node deployments the same system with different process counts.

## 2. Components

| Component | Responsibility | Spec |
|---|---|---|
| **API** | FastAPI; REST for CRUD, SSE for job progress; OpenAPI generated | — |
| **Policy layer** | Validates every plugin call against allowlists, argument schemas, egress rules, footprint class and per-case budgets, **before** execution | [08 §3](08-ai-layer.md), [10](10-security-and-audit.md) |
| **Planner** | Compiles a question into an inspectable DAG; never executes unapproved | [08 §3](08-ai-layer.md) |
| **Lens runner** | Executes a Lens (saved pipeline) or an approved plan; checkpoints each node | below, §4 |
| **Plugin host** | Loads and invokes plugins in-process or in a container per profile | [04](04-plugin-system.md) |
| **Model residency manager** | VRAM accounting, admission control, LRU eviction with pinning, device serialization | [05](05-compute-profiles.md) |
| **Assertion writer** | The *only* path to layer 1. Enforces evidence, `derived_by`, grading | [03](03-data-model.md) |
| **Resolver** | Candidate generation (Splink), judgment edges, cluster recomputation | [03 §5](03-data-model.md) |
| **Projector** | Rebuilds layer 2 and 3 from layers 0–1; incremental or full | [03 §1](03-data-model.md) |
| **Reporter** | Structured claim generation, citation verification, exhibit export | [08 §5](08-ai-layer.md), [09 §9](09-ui-and-ux.md) |

## 3. Orchestration

The orchestrator sits behind a narrow `Orchestrator` protocol. For M0–M2 the
substrate is an in-repo Postgres queue (`astral.queues`), chosen on measurement
in [ADR-0003](adr/0003-orchestration.md): the decision criterion is that
*whatever we choose must not add a second datastore, or require a container
runtime, at the small tier.* Celery is excluded (Redis or RabbitMQ). **Hatchet**
is excluded from the small tier because every documented self-host path needs
Docker, and remains a cluster-tier candidate; **procrastinate** and **DBOS
Transact** are the named replacements if scheduling or durable-workflow needs
outgrow the in-repo queue.

Queues are partitioned by resource class, not by task type:

```
cpu          ← parsing, hashing, NER, rules, projections, FTS
gpu-small    ← face embed, OCR, detection, ASR-on-GPU, ≤2 GB models
gpu-large    ← VLM captioning, LLM generation, >2 GB models
remote       ← hosted API calls; rate-limited, no local resources
```

Workers subscribe to classes their host can serve. The `cpu-only` profile
simply has no `gpu-*` workers, and every stage that declares
`Accel.PREFERRED` routes to its CPU backend automatically. This is how one
codebase spans a laptop and a cluster without branching.

## 4. Lenses

A **Lens** is a named, versioned pipeline — a DAG of stages with bound
parameters. Lenses are the unit of reusable methodology and the thing that
turns an ad-hoc investigation into something repeatable and reviewable.

```yaml
lens: person-from-photograph
version: 3
inputs:  [image]
stages:
  - face.detect
  - face.embed
  - face.search:    {indexes: [case, watchlist], threshold: 0.38}
  - image.reverse:  {backends: [tineye, yandex]}
  - image.geolocate
  - entity.propose
outputs: [candidate_identities, regions, source_links]
```

Lenses are data, declare their resource requirements by composition, and are
exportable and shareable between installs. The five acceptance scenarios in
[01 — Vision & Scope](01-vision-and-scope.md) were meant to be shipped Lenses.

As built, the example above is narrower. A lens is made of the AI layer's
read-only steps (`search_text`, `entity_facts`, `artifact_facts`,
`face_search`), so it reads what the case already holds and cannot run a
pipeline or contact a service. Scenarios 1, 2 and 4 ship as built-in lenses
(`photo-candidates`, `media-dossier`, `shared-identifier`). Scenario 3 has to
query registries, so it ships as a built-in *machine* (`name-to-network`),
which waits for approval like any connector chain. Scenario 5 is the exhibit
export, not a pipeline. Reverse-image search is not a stage: every service
for it needs a paid key. Built-ins are installed into a workspace only on
request (`GET /builtin`, `POST /builtin/{kind}/{name}/install`).

A plan produced by the planner can be **saved as a Lens**, which is how
methodology accumulates without anyone writing code.

## 5. Deployment profiles

| Profile | Processes | Datastores |
|---|---|---|
| **Solo** (default) | One API process, one worker per resource class the box supports | One Postgres, one filesystem blob store |
| **Team** | API replicas behind a proxy, worker pools per class, optional container plugin runtime | Postgres, object store (S3-compatible, Object Lock in **compliance** mode) |
| **Cluster** | As above, plus dedicated search and vector services and optionally a graph projection | Adds OpenSearch/Qdrant/graph DB as *rebuildable projections only* |

The progression never changes the system of record. Doc 03's layering is what
makes that true: anything added at a larger tier is a projection that can be
dropped.

**Install target for the solo profile: one command, no container runtime
required.** Doc 04 §2 makes this a hard constraint on the plugin host, because
requiring a Docker socket on a personal machine is both a privilege boundary
and a barrier to the project's primary user.

## 6. Tenancy seams

Per the user's day-one decision: the schema carries `workspace_id`,
`case_id`, principals and role grants from the first migration; the solo
profile collapses them to a single implicit workspace with no login ceremony.
Every query is written against the seam from the start. Retrofitting tenancy
means rewriting every query in the system, so the cost of doing it now is
close to zero and the cost of doing it later is close to total. See
[ADR-0004](adr/0004-tenancy-seams-from-day-one.md).

## 7. Extension points

Seven, each a declared interface rather than a code change:

1. **Plugins** — collection, enrichment, analysis, export ([04](04-plugin-system.md)).
2. **Backends** — ASR, face, OCR, VLM, LLM, embedding implementations
   ([05](05-compute-profiles.md)). No plugin imports a model library directly.
3. **Schemata** — FtM-form YAML for new entity types ([03 §4](03-data-model.md)).
4. **Site packs** — username/account enumeration as data ([04 §4](04-plugin-system.md)).
5. **Lenses** — pipelines as data (§4 above).
6. **Exporters** — STIX 2.1, MISP, FtM, CSV/JSON over layer 2.
7. **Policies** — retention, egress, footprint and budget rules as
   configuration ([10](10-security-and-audit.md)).

The test of this design: **adding a new OSINT source should require writing a
manifest and a function, and touching no core code, no UI code and no
migration.** If it does not, the extension point is wrong.

## 8. Technology choices

| Layer | Choice | Reason |
|---|---|---|
| API | FastAPI + Pydantic v2 | Typed contracts shared with plugin manifests; async; OpenAPI for free |
| DB | PostgreSQL 16+ | One datastore at the small tier; FTS, JSONB, arrays, pgvector, optional AGE |
| Vectors | pgvector at small tier; Qdrant (Apache-2.0) at larger | Avoids a second service until corpus size demands it ([06 §1](06-multimodal-pipelines.md)) |
| Queue | In-repo Postgres queue behind an `Orchestrator` protocol ([ADR-0003](adr/0003-orchestration.md)) | Postgres-backed; no Redis and no container runtime at the small tier |
| Inference | ONNX Runtime (vision), llama.cpp (LLM/embed/rerank), faster-whisper (ASR) | All ship CPU and GPU paths from one interface |
| UI | React + TypeScript, Cytoscape.js / Sigma.js | [09](09-ui-and-ux.md) |
| Containers | Optional, not required at small tier | [04 §2](04-plugin-system.md) |

## 9. Failure posture

Four rules that follow from the data model and recur across every doc, stated
once here because they are architectural, not local:

1. **Unknown is not negative.** A failed or quarantined source yields
   "unknown", never "not found". A false exculpatory result is the most
   dangerous output this system can produce.
2. **Partial results are first-class.** A job that completes nine of ten stages
   records nine stages' assertions and an explicit gap, rather than failing
   whole.
3. **Degradation is visible, never silent.** Backend substitution is recorded
   in `derived_by` and shown in the UI ([05 §6](05-compute-profiles.md)).
4. **Nothing is deleted, only retracted.** Including when a source is found to
   have been wrong for a month ([03 §9](03-data-model.md)).
