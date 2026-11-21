# 04 — Plugin System

Astral Lens is intended to grow for years, which means most of its eventual
capability will not be written by its original authors. The plugin contract is
therefore a primary public interface, versioned and documented as such.

## 1. What the existing ecosystem teaches

A survey of the extension models in SpiderFoot, Recon-ng, IntelOwl, IntelMQ,
OpenCTI, MISP-modules, Maltego and theHarvester produced one dominant lesson:

> **In this space, composition happens at the data-exchange boundary — a
> message schema — not at the language import.**

The platforms that extend well (OpenCTI, MISP-modules, IntelMQ, Maltego) all
put a serialized contract between the core and the extension: STIX bundles over
a queue, JSON over HTTP, typed events through a pipeline, typed graph objects
over a transform protocol. The platforms whose extensions are Python
subclasses loaded in-process (SpiderFoot, Recon-ng, IntelOwl) are easier to
start with and harder to live with: one plugin's dependency conflict, memory
leak or crash is the whole system's problem, and every plugin runs with the
core's credentials and network identity.

Specific patterns worth adopting, each from a tool that proved it:

| Pattern | Seen in | Why it matters here |
|---|---|---|
| Out-of-process execution over a versioned serialized contract | OpenCTI connectors, MISP-modules, IntelMQ bots | Crash isolation, dependency isolation, language independence, per-plugin credentials and egress |
| Declared `watchedEvents` / `producedEvents` typing | SpiderFoot; Maltego input constraints; MISP `mispattributes` | Lets the UI *generate* the pivot menu for an entity instead of hard-coding it |
| Network activity classes — P0 third-party dataset, P1 DNS, P2 direct target contact | theHarvester | Exactly the OPSEC-footprint axis this project needs; the single best idea found in the survey |
| Provenance on every emitted item, with parent lineage | SpiderFoot event lineage; OpenCTI `created_by_ref` | Mandatory given this project's assertion-log data model |
| Data-driven definitions for a long tail of similar targets | Sherlock, WhatsMyName, Maigret site records | ~5,900 sites as data, not 5,900 Python files; code reserved for genuine logic |
| Built-in self-check with auto-disable | Maigret `--self-check --auto-disable` | Social and site connectors rot constantly; health must be a contract obligation |
| Config schema in the manifest, secrets by name | Recon-ng `meta`, MISP `moduleconfig` | Lets the core render settings UI and manage secrets without plugin-specific code |
| Registry rows rather than filesystem scanning | IntelOwl v5 database-backed plugin config | Enable, disable, reconfigure and version plugins without redeploying |

One counter-lesson: **licence contamination is real.** Several reference tools
are AGPL-3.0 (IntelOwl, IntelMQ, MISP, Datashare) or GPL (Recon-ng, Holehe,
theHarvester at GPL-2.0-only), and the WhatsMyName site dataset is CC BY-SA 4.0,
which makes any derived site dataset share-alike. The out-of-process boundary
is also the licence boundary, which is a second strong argument for it. See
[ADR-0002](adr/0002-licensing.md).

## 2. Three orthogonal axes, one plugin type

The surveyed tools each classify plugins along a *different* single axis:

- IntelOwl by **pipeline stage** — analyzer, connector, pivot, visualizer,
  ingestor.
- OpenCTI by **data-flow direction** — external-import, internal-enrichment,
  stream, export.
- SpiderFoot and theHarvester by **collection footprint** — passive, DNS-only,
  target-touching.

All three distinctions are real and useful, and none of them subsumes the
others. Making any one of them the class hierarchy forces the other two into
ad-hoc conventions. Astral Lens therefore defines **one** plugin type and
carries all three as declared metadata:

```yaml
# plugin.yaml
api_version: "astral.plugin/v1"
id: "registry.companies-house-uk"
version: "1.4.0"
title: "UK Companies House"

stage: enrich              # ingest | extract | enrich | resolve | analyze | render | export
direction: pull            # pull | push | internal
footprint: third_party     # none | third_party | dns | target_contact

consumes:                  # typed inputs; drives generated pivot menus
  - type: Company
    required: [name]
  - type: Identifier
    scheme: uk-crn
produces:
  - Company
  - Person
  - Directorship
  - Address
  - Document

config:                    # JSON Schema; the core renders the settings UI
  type: object
  properties:
    include_resigned: { type: boolean, default: false }
secrets: [COMPANIES_HOUSE_API_KEY]

rate_limit: { requests: 600, per: 300s, scope: credential }
resources: { accelerator: cpu_only, ram_mb: 256 }

health:                    # contract obligation, see §5
  check: "python -m plugin.selfcheck"
  interval: 24h
```

`footprint` is the axis that most tools lack and that investigators most need.
It is surfaced in the UI before a query runs and recorded in the audit log
after:

| `footprint` | Meaning | Operator consequence |
|---|---|---|
| `none` | Local computation only | Nothing leaves the box |
| `third_party` | Queries a third party about the target | The third party learns your interest; your API key is attributable to you |
| `dns` | Resolves names | Leaks to resolvers and authoritative servers |
| `target_contact` | Touches target-controlled infrastructure | The target can see you, and can fingerprint or deceive you |

The operator chooses freely. The tool simply never hides what a query costs
them.

## 3. The execution boundary

**One contract, two runtimes.** The serialized contract is the same either way;
what differs is where the code runs. The manifest declares which runtimes a
plugin supports, and the active compute profile picks among them.

| Runtime | Default for | Rationale |
|---|---|---|
| `in_process` | First-party, CPU-light, dependency-clean plugins — especially at the `small-gpu` and `cpu-only` tiers | A container per plugin is too expensive on a 32 GB box. Dozens of idle containers will consume the memory the models need. |
| `container` | Third-party plugins, anything with heavy or conflicting native dependencies, anything untrusted, and the default at `server` and `cluster` tiers | Crash and dependency isolation, per-plugin egress identity, licence boundary |

This is a deliberate revision of an earlier draft that made containers the
universal default. On the reference hardware that choice is wrong: the
isolation is worth less than the RAM it costs. Isolation is bought where it is
actually needed — untrusted or dependency-hostile code — and skipped where it
is not.

Even `in_process` plugins talk through the serialized contract rather than
calling core functions directly, so a plugin can be promoted from in-process to
container without changing its code. That portability is the point of putting a
message schema in the middle.

The wire protocol, for both runtimes — stdio line-delimited JSON for a launched
process, HTTP for a long-lived container:

```
core → plugin   {"op":"invoke","call_id":"...","input":{...},"config":{...},"secrets":{...},"budget":{...}}
plugin → core   {"op":"emit","call_id":"...","assertions":[...]}       (repeatable, streaming)
plugin → core   {"op":"log","level":"info","msg":"..."}
plugin → core   {"op":"progress","done":12,"total":40}
plugin → core   {"op":"result","call_id":"...","status":"ok","stats":{...}}
```

At the `container` runtime this buys crash isolation, dependency isolation
(each plugin owns its own image, which matters enormously given how much of
this ecosystem pins conflicting versions), language independence, per-plugin
network egress and proxy identity, and a clean licence boundary.

Running containers requires a Docker socket or a rootless runner, which is
itself a privilege boundary and a reason the small tier should not need it to
function. A small-tier install must be fully useful with no container runtime
present at all.

**Model-backed stages are special.** A plugin that needs an accelerator does
**not** load the model itself. It calls a core capability interface
(`asr.transcribe`, `face.embed`) which the model residency manager schedules
against the device. This is what keeps the 8 GB tier viable and is covered in
[05 — Compute Profiles](05-compute-profiles.md). A plugin that imports a model
library directly has hard-coded a hardware tier and will be rejected in review.

### Sandboxing

Container plugins get the usual controls: read-only root filesystem, dropped
capabilities, no host network by default, declared egress allowlist, CPU and
memory limits, and no access to the evidence store except through the core API.
WASM sandboxing was considered and deferred: it is attractive for untrusted
third-party plugins but excludes almost the entire Python OSINT and ML
ecosystem, which is precisely what we need to call. Revisit for a future
untrusted-plugin marketplace. See [ADR-0005](adr/0005-plugin-boundary.md).

## 4. Site packs: data, not code

Username and account-existence enumeration across thousands of sites must not
be thousands of plugins. Following Sherlock, WhatsMyName and Maigret, one
plugin interprets a declarative **site pack**:

```yaml
site: "example.social"
category: social
check:
  url: "https://example.social/@{username}"
  method: GET
  exists_when: { status: 200, body_absent: "not found" }
extract:          # optional: profile fields → assertions
  display_name: { css: "h1.profile-name" }
  bio:           { css: "div.bio" }
footprint: target_contact
```

The same mechanism serves Google-style dork packs, registry URL templates and
breach-source descriptors. Code is reserved for genuine logic: authentication
dances, pagination, response decoding, anti-bot handling.

**Licence caution.** The WhatsMyName dataset is CC BY-SA 4.0. If its records
are imported, the derived pack is share-alike. Either keep third-party packs in
a separately licensed directory with attribution, or author packs
independently. This must be decided before shipping, not after.

## 5. Health is a contract obligation

The research survey was unambiguous that connector rot is the defining
operational problem of OSINT tooling: Sherlock carrying hundreds of open issues
and PRs, Maigret warning its site checks "need active maintenance", Holehe
flagging rate-limited sites, Photon and Creepy showing dormancy. A platform
that aggregates many sources inherits all of their decay.

So health is built into the contract rather than left to users discovering
silent failure:

- Every plugin declares a **self-check** — for site packs, a known-present and
  known-absent fixture, following Maigret's approach.
- The core runs self-checks on a schedule and records a **health history** per
  plugin: last success, last failure, error class, latency trend.
- A plugin failing self-checks is **auto-quarantined**: it stops running
  silently and is surfaced in the UI as degraded, with the distinction between
  "the site changed", "we are rate-limited", "credentials expired" and "we are
  blocked" made explicit, because the remedies differ.
- **A quarantined source is reported as unknown, never as a negative result.**
  This matters more than anything else in this document. "No account found"
  from a broken checker is an actively misleading intelligence product, and the
  surveyed tools routinely conflate the two.

## 6. Registration and lifecycle

Plugins are registry rows, not filesystem scans, following IntelOwl's v5
move. A plugin is installed from a local path, a git reference or an OCI image;
the core validates its manifest against the `astral.plugin/v1` schema, records
it, and leaves it disabled until configured. Enabling, disabling,
reconfiguring, pinning a version and rolling back require no redeploy.

Because the data model is an append-only assertion log, plugin identity and
version are recorded in `derived_by` on every assertion a plugin produces. The
consequences are worth stating plainly: you can ask what a case would look like
without a given source, re-run a single plugin at a new version and diff the
result, and retract everything a plugin asserted when it is discovered to have
been broken for a month. That last capability is the real payoff of the
out-of-process, fully attributed design.

## 7. Contract versioning

`api_version` is the compatibility gate. The core supports the current major
version and the one before it. Breaking changes mean a new major version and a
documented migration; additive changes are minor. The contract schema itself
is published in the repository and is testable, so a plugin author can validate
against it without running the platform.
