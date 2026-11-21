# Astral Lens

[![CI](https://github.com/slammingprogramming/astral-lens/actions/workflows/ci.yml/badge.svg)](https://github.com/slammingprogramming/astral-lens/actions/workflows/ci.yml)
[![License: AGPL-3.0-or-later](https://img.shields.io/badge/license-AGPL--3.0--or--later-blue.svg)](LICENSE)

**A self-hosted OSINT aggregation and multimodal investigation platform.**

Astral Lens takes inputs of almost any shape — a photograph, a video, an audio
recording, a document dump, a username, a domain, a company name, or a plain
English question — runs them through configurable pipelines of algorithmic,
programmatic and AI stages, and returns structured, sourced, auditable
intelligence.

It is designed to be:

- **Self-hosted and sovereign.** Your evidence and your queries never have to
  leave your machine. Every external call is an explicit, visible choice.
- **Runnable on what you actually own.** The reference target is a single
  desktop with **8 GB VRAM and 32 GB RAM**. It scales down to CPU-only and up
  to a multi-GPU cluster from the same codebase, by changing a profile — not a
  fork. See [docs/05-compute-profiles.md](docs/05-compute-profiles.md).
- **Expandable by design.** Capability lives in plugins with a declarative
  manifest. Adding a new data source, model or analytic is writing one class
  and one manifest, not surgery on the core. See
  [docs/04-plugin-system.md](docs/04-plugin-system.md).
- **Provenance-first.** Nothing is ever asserted without a traceable source.
  The knowledge graph is a *projection* of an append-only assertion log, so
  every conclusion can be explained, replayed, contradicted or retracted. See
  [docs/03-data-model.md](docs/03-data-model.md).

## Status

**M0 (foundations) is implemented**: the append-only assertion log with its
database-enforced guards, the content-addressed evidence store, the plugin
contract and host, projections that can be dropped and rebuilt, resource-aware
queues, the model residency manager, a licence-aware model registry, a
hash-chained audit log, case-scoped erasure and an API skeleton.

**M1 (text investigation) is implemented**: nine connectors, a coverage-first
"name to network" report where an unreachable source is *unknown* and never
"no results", human-decided entity resolution, evidence-grade WARC capture, a
graph whose edges click through to the raw API response, and a web UI served at
`/ui/`. The connectors have only been tested against local fixtures, not the live
services, and the heavier NER and document backends are not installed.

**M2 (media), M3 (faces) and M4 (the AI layer) are partly implemented**, each
with a status block in [docs/11-roadmap.md](docs/11-roadmap.md) that separates
what was verified from what was not. In short: ffprobe/ffmpeg metadata, scene
and OCR paths and time-anchored transcripts work, but no speech model has run
(only a stub); face search (YuNet + SFace) was verified on real photographs but
returns candidates, never identifications; the Ask tab plans, asks for approval,
retrieves, and checks every citation against the stored bytes. By default no
language model is configured, so answers are verbatim quotations that can reach at
most "weakly verified". A small local model (Phi-3.5-mini via llama.cpp) has been
run once on 16 synthetic questions: 83 % of its claims passed citation checking and
it was less accurate than plain quotation, so treat model answers as unproven.

**M5 (exhibits and teams) is mostly built and M6 (breadth) partly:** hash-verifiable
exhibit bundles with a standalone verifier, signed plugins, OIDC roles/comments/audit
review, a hardened container runtime, monitoring with diff views, a read-only MCP server,
shareable lenses, a site-pack interpreter and an Esplora connector.

**M7 adds** a Maltego-style workbench (entity transforms, graph analysis, annotations,
saved views, table import, GraphML/CSV export, HTML and PDF reports, and approved "machines" with a form editor and schedules),
crypto and geospatial connectors, a plugin/pack registry with a community-repository
format ([docs/12](docs/12-community-repository.md)), write-once (S3 Object Lock)
preservation, optional OpenSearch and Qdrant services, and optional dense retrieval with a
reranker (multilingual-e5-large and bge-reranker-v2-m3-int8 through fastembed).

**Also built:** an optional Apache AGE Cypher view of the case graph (with a query panel in
the Workbench), Overture Maps (through a bounded Parquet range-read capability) and KartaView
connectors, and dense text vectors in Qdrant. KartaView's public endpoints were unreachable when
checked.

A query builder for the Cypher view and browser sign-in for the team profile (OpenID Connect,
PKCE) are built.

**Not built:** any public community repository.

**What has and has not been checked.** OIDC (including the browser sign-in) and the MCP server were tested against local
fixtures and a local fake provider only, and so were most connectors; a few keyless ones were also run once against the live
services (see the roadmap's live-check list, which includes a bug that run found). Write-once
storage was run against a real SeaweedFS server, but without authentication. Dense retrieval was
checked on two queries, not on a real corpus. The two bundled site packs have never been run
against the real sites. Each roadmap status block says exactly what was and was not checked.

```bash
docker run -d --name astral-pg-dev -e POSTGRES_PASSWORD=astral -p 55432:5432 postgres:16-alpine
cd backend
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest            # .venv\Scripts\ on Windows
.venv/bin/astral preflight  # what this machine can run, and which profile it gets

cd ../frontend && npm install && npm run build   # the API serves it at /ui/
cd ../backend && .venv/bin/python tests/demo_server.py   # UI + a seeded case, on local fixtures only
```

Contributors should read the [design invariants](docs/13-design-invariants.md)
before making changes: they recur across the documents and the source, and
breaking one is a design regression, not a local bug.

## Documentation

| Doc | Contents |
|---|---|
| [01 — Vision & Scope](docs/01-vision-and-scope.md) | What this is, who it serves, explicit non-goals |
| [02 — Architecture](docs/02-architecture.md) | Services, data flow, boundaries |
| [03 — Data Model](docs/03-data-model.md) | Artifacts, assertions, entities, cases |
| [04 — Plugin System](docs/04-plugin-system.md) | The extension model |
| [05 — Compute Profiles](docs/05-compute-profiles.md) | Hardware tiering, GPU/CPU fallback, model residency |
| [06 — Multimodal Pipelines](docs/06-multimodal-pipelines.md) | Image, face, audio, video, document processing |
| [07 — Data Sources](docs/07-data-sources.md) | Connector catalogue and priorities |
| [08 — AI Layer](docs/08-ai-layer.md) | NL query planning, RAG, citation enforcement |
| [09 — UI & UX](docs/09-ui-and-ux.md) | Graph, timeline, map, document review, reporting |
| [10 — Security & Audit](docs/10-security-and-audit.md) | Tenancy, RBAC, audit log, evidence integrity |
| [11 — Roadmap](docs/11-roadmap.md) | Phased delivery plan |
| [13 — Design Invariants](docs/13-design-invariants.md) | The numbered rules the source and documents cite |
| [14 — Configuration](docs/14-configuration.md) | Every environment variable, and team sign-in setup |
| [adr/](docs/adr/) | Architecture decision records |

## Contact and support the project

**Contact:** [SimpleX](https://smp14.simplex.im/a#3gZ-zeHs4QrFZKLAN0o3SC_XQJXhj1eYBVTO_c0FAtg)
(open the link with the SimpleX app). For a security problem, use this rather than a public
issue; see [SUPPORT.md](SUPPORT.md).

Astral Lens is free software and will stay that way. If you find it useful and want to chip in,
any of these is welcome; it is entirely optional.

**Fiat donation:** <https://ko-fi.com/scenprogramming>

| Coin | Address |
| --- | --- |
| Bitcoin (BTC) | `sp1qq2637t6ve9zp4wh0e4ly2u7h7jwfn0k6c4535lxpp3wrqvmkua6xjqkwevyz0wwqx5ly2f0q29qh48wgf9gl0zt049za9kr9m8xgkey4cyggxa0x` |
| Ethereum (ETH) | `0x8F238db898102125Fd968ed7CaD959Cb594a7e93` |
| Monero (XMR) | `8AYK93mQEzsFFEFkuPN7wC9rPcXCyEXa1Ye5bH9U2GR3PpaZiYc84SxjMcdSdVMaaQUGmCXYkuxdDSG5bM3EVyqR25nbumV` |

The same details are in the app under Settings, in a collapsed "About, contact and supporting
Astral Lens" section at the bottom. Nothing in the app depends on them or contacts any of these
services.

## License

**AGPL-3.0-or-later.** See [ADR-0002](docs/adr/0002-licensing.md) for what that
does and does not cover — model weights and datasets carry their own licences.
The example plugin under `plugins/examples/` is MIT. The full licence text is in
[`LICENSE`](LICENSE) (the unmodified FSF text of the GNU Affero General Public
License, version 3).
