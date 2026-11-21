# 01 — Vision & Scope

## The problem

OSINT tooling today splits into three unsatisfying categories.

**Monolithic scanners** (SpiderFoot, Recon-ng, theHarvester) are good at
breadth-first enumeration from a single seed — a domain, an IP, an email. They
are weak at anything that is not a string: they cannot take a photograph as
input, they have no concept of a case, and their output is a flat list of
correlations rather than a reasoned picture.

**Link-analysis platforms** (Maltego, i2 Analyst's Notebook, Videris, Palantir
Gotham) are excellent at the analyst's *thinking* surface — entities, links,
timelines — but the good ones are proprietary, expensive, cloud-tethered, and
closed to the kind of extension that a specific investigation demands. Maltego
without transform subscriptions is an empty canvas.

**Document and media platforms** (Aleph, Datashare, Nuix, Magnet AXIOM) handle
corpora and forensic artifacts well but are not collectors; they expect you to
already have the material, and they do not reach out to the live internet.

Nothing in the open-source world accepts a face, a voice, a video and a natural
language question as peer inputs to the same investigation, enriches them with
both deterministic tooling and modern models, and produces a sourced product
you could defend. That is the gap Astral Lens exists to fill.

## What Astral Lens is

A **self-hosted investigation platform** with four layers:

1. **Collection** — connectors that pull from APIs, archives, open datasets and
   the live web, plus direct ingest of files the investigator already holds.
2. **Processing** — multimodal extraction. Transcribe the audio, diarize the
   speakers, detect and embed the faces, OCR the frames, parse the PDFs, read
   the EXIF, translate the text, pull the entities out.
3. **Analysis** — entity resolution, graph construction, temporal and
   geospatial correlation, and an AI layer that plans queries and drafts
   findings against an evidence store it is not allowed to depart from.
4. **Product** — a case graph, timelines, maps, and exportable reports in which
   every claim carries its citation.

## Who it serves

Two deployment shapes, supported from the same codebase and designed in from
day one rather than retrofitted:

- **Solo profile (default).** One investigator, one box, one implicit
  workspace. No login ceremony, no approval chains, no friction. Everything the
  tool can do is available immediately.
- **Team profile.** Multiple analysts, per-case compartmentalization, roles,
  and a supervisor-visible audit trail. Enabled by configuration.

The multi-tenancy, RBAC and audit *seams* exist in the schema and service
boundaries in v1 even though the solo profile leaves them collapsed. This is
the single most expensive thing to add later and the cheapest to design for
now; see [ADR-0004](adr/0004-tenancy-seams-from-day-one.md).

## Design principles

**1. Provenance is not a feature, it is the data model.**
Every fact in the system is an assertion with a source, a method, a confidence
and two timestamps (when it was true, when we learned it). The graph you look
at is a materialized view over that log. This is what makes contradictions
representable, merges reversible, conclusions explainable and runs replayable.

**2. The hardware floor is a first-class requirement.**
A tool that needs an A100 is a tool most investigators cannot run. Astral Lens
targets 8 GB VRAM / 32 GB RAM as its reference box and treats every model
dependency as an interface with a small-tier implementation, a large-tier
implementation and a hosted-API implementation. Capability degrades gracefully
and *visibly* — the UI tells you that you are running the 8 GB face model, not
the good one.

**3. Determinism where possible, AI where it earns its place.**
Models are used for perception (transcription, detection, embedding,
extraction) and for drafting and planning. They are not used as the system of
record, and they are never permitted to introduce an uncited claim into a
product. An LLM's output is a proposal; the assertion log is the truth.

**4. Extension is the normal case.**
This is intended to become a powerful investigative and forensic tool over
years, which means most of its eventual capability does not exist yet and will
not be written by the original authors. The plugin contract is therefore a
primary interface, versioned and documented like a public API.

**5. OPSEC is the operator's to decide, but never by accident.**
Every connector declares its collection footprint — whether it touches the
target, whether it leaks the investigator's IP or the target's identity to a
third party. The operator chooses freely; the tool simply never hides what a
query will cost them.

## Explicit non-goals

- **Not a Clearview clone.** Astral Lens performs face search against media the
  investigator has ingested into a case, against datasets they lawfully hold,
  and against third-party services they have credentials for. It does not ship
  a mass-scraped face index of the general public, and there is no plan to
  build one. This is both a legal reality and a scope decision: that index is
  the product of the scraping, not of this software.
- **Not an exploitation or intrusion framework.** Collection is observational.
  Nothing in the core authenticates against, probes for vulnerabilities in, or
  attempts access to a target system. Active reconnaissance that touches the
  target (port scans, subdomain brute force) is supported but flagged as such.
- **Not a SIEM or a threat-intel platform.** OpenCTI and MISP are good at that
  and Astral Lens should interoperate with them rather than compete. The focus
  is investigation of people, organizations, assets and events — not of malware.
- **Not a managed service.** No multi-customer cloud offering is contemplated.
  Every design tradeoff favours the self-hoster.
- **Not a chatbot.** The natural language interface compiles to an inspectable
  pipeline. If the user cannot see and edit what the question turned into, it
  is not finished.

## What "success" looks like

Concrete acceptance scenarios the design must satisfy:

1. **Photograph to identity.** Given a single photo of a person, Astral Lens
   detects the faces, embeds them, searches every face in the case corpus and
   any configured external service, returns ranked candidates with the source
   image for each, and records what it searched and why.
2. **Video to dossier.** Given a two-hour video, it produces a transcript with
   speaker turns, a list of distinct faces with appearance timestamps, detected
   on-screen text and logos, named entities, locations guessed from imagery,
   and a navigable timeline — on the 8 GB box, overnight, without crashing.
3. **Name to network.** Given a person's name and a country, it queries company
   registries, sanctions and leak datasets, resolves the entity against what it
   already knows, and draws the ownership and association graph with every edge
   clickable through to its source record.
4. **Question to answer.** Given "who are the directors of the companies that
   share this phone number", it compiles a pipeline, shows it, runs it, and
   answers with citations — and declines to answer rather than guess when the
   evidence is not there.
5. **Case to exhibit.** Given a finished case, it exports a report with
   calibrated confidence language, a source appendix, and hashes for every
   piece of evidence relied on.
