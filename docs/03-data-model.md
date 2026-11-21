# 03 — Data Model

> **The central decision.** Astral Lens does not store facts. It stores
> *assertions* — claims made by an identified source, by an identified method,
> at an identified time, with graded reliability. The entity graph an analyst
> looks at is a **projection** of an append-only assertion log, never the
> system of record.

Every other property this project wants — explainability, reproducibility,
reversible merges, representable contradictions, retraction of a source
discovered to be broken, and defensible exhibits — falls out of that one
choice. Retrofitting it is effectively impossible, so it is settled first.

## 1. Layers

```
Layer 3   Projections      graph views, timelines, maps, search indexes
                           (derived, disposable, rebuildable)
            ▲
Layer 2   Entities         FollowTheMoney-shaped entities and clusters
                           (derived from judgments + assertions)
            ▲
Layer 1   Assertions       append-only claim log, bitemporal, graded
            ▲
Layer 0   Evidence         content-addressed immutable artifacts
```

Only layers 0 and 1 are authoritative. Layers 2 and 3 can be dropped and
rebuilt from them at any time, which is also how schema migrations and model
upgrades are handled.

## 2. Layer 0 — the evidence store

Content-addressed, immutable, write-once. Every byte that enters the system —
an uploaded video, a fetched HTML page, an API JSON response, a generated
transcript — becomes an artifact keyed by its SHA-256 digest.

```sql
CREATE TABLE artifact (                   -- one row per distinct byte string
  sha256          bytea PRIMARY KEY,
  media_type      text NOT NULL,
  byte_size       bigint NOT NULL,
  storage_uri     text NOT NULL,          -- filesystem or object store
  first_seen_at   timestamptz NOT NULL,
  parent_sha256   bytea REFERENCES artifact(sha256),  -- derived artifacts
  derivation      text                    -- 'transcode', 'frame@12.3s', ...
);

CREATE TABLE artifact_acquisition (       -- one row per time a case obtained it
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  workspace_id    uuid NOT NULL,
  case_id         uuid NOT NULL,
  sha256          bytea NOT NULL REFERENCES artifact(sha256),
  acquired_by     text NOT NULL,          -- plugin id @ version, or 'upload'
  acquired_from   text,                   -- URL, API endpoint, device, path
  acquisition_ctx jsonb NOT NULL,         -- request headers, egress identity,
                                          -- persona, response status, TLS cert
  acquired_at     timestamptz NOT NULL,
  acquired_principal uuid NOT NULL
);
```

*Refinement made in implementation (migration 0001).* An earlier draft put
the acquisition fields on `artifact` while also saying one artifact has several
acquisition records; only a separate table can do both. It also closes a leak:
**an artifact is visible to a case only through an acquisition row in that
case**, so content-addressed dedup can never reveal that another case holds the
same bytes. Hard deletion removes a case's acquisitions and drops the blob only
when no case holds it.

Deduplication is free and valuable: the same image arriving from three sources
is one artifact with three acquisition records, which is itself an
intelligence signal.

**Chain of custody.** Hashes are computed at acquisition, before any
processing, and never recomputed from a mutated file. Artifacts are never
modified in place; a redaction or a transcode produces a *new* artifact with
`parent_sha256` set. Web captures record the full request and response context
so a page can be shown as it was, not as it is now — the capability the survey
found missing from every open-source tool and present only in commercial
products like Hunchly.

## 3. Layer 1 — assertions

The core table. Append-only: **no row is ever updated.**

```sql
CREATE TABLE assertion (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  case_id       uuid NOT NULL REFERENCES case_(id),

  -- the claim
  subject       uuid NOT NULL,            -- entity id
  predicate     text NOT NULL,            -- FtM property or relationship
  object_kind   text NOT NULL,            -- 'entity' | 'literal'
  object_entity uuid,
  object_value  jsonb,

  -- where it came from
  source_id     uuid NOT NULL REFERENCES source(id),
  evidence      bytea[] NOT NULL,         -- artifact digests supporting it
  derived_from  bigint[] NOT NULL DEFAULT '{}',  -- upstream assertion ids
  derived_by    jsonb NOT NULL,           -- {plugin, version, backend,
                                          --  model, model_version, params}

  -- bitemporality: two independent time axes
  recorded_at   timestamptz NOT NULL,     -- when WE learned it
  valid_from    timestamptz,              -- when it became true in the world
  valid_to      timestamptz,              -- when it stopped being true

  -- grading: two independent axes (Admiralty), pessimistic by default
  source_grade  char(1) NOT NULL DEFAULT 'F',     -- A..F  source reliability
  info_grade    smallint NOT NULL DEFAULT 6,      -- 1..6  information credibility

  created_by    uuid NOT NULL             -- principal or agent
);

-- Retraction is its own append-only fact, not a column on the assertion.
CREATE TABLE assertion_retraction (
  assertion_id  bigint NOT NULL UNIQUE REFERENCES assertion(id),
  retracted_at  timestamptz NOT NULL,
  retracted_by  uuid NOT NULL,
  reason        text NOT NULL,
  batch_id      uuid                      -- one plugin/source sweep
);
```

*Refinement made in implementation (migration 0001).* The earlier draft
declared "no row is ever updated" and then listed `retracted_at`,
`retracted_by` and `retract_reason` on the assertion — a contradiction.
Retraction now lives in `assertion_retraction`, so `assertion` really is
append-only, and the database enforces it with triggers (UPDATE and DELETE are
refused; DELETE is allowed only inside the case-scoped hard-delete path).
`workspace_id` is carried on the row so the tenancy seam is in the schema from
the first migration, and the same triggers verify that every evidence digest
was acquired **in the same case** and that every `derived_from` assertion
exists in that case. Inserts are accepted only from the assertion writer.

### Bitemporality

Two time axes, both required, routinely confused. `recorded_at` is transaction
time — when the system learned the claim. `valid_from`/`valid_to` is valid
time — when the claim was true of the world. A company director who served
2019–2022, discovered today, has `valid_from` 2019 and `recorded_at` today.

This supports the two questions investigations actually ask:

- **Truth-as-of-T:** who were the directors in March 2020?
- **Belief-as-of-T:** what did this case believe in March 2020, and on what
  basis? — the question asked when a conclusion is challenged, and the one
  single-timestamp systems cannot answer at all.

### Grading: two axes, never collapsed

Following the Admiralty/NATO convention, source reliability (A–F) and
information credibility (1–6) are **independent**. A reliable source can relay
an implausible claim; an unreliable source can relay a true one. Collapsing
them into a single 0–1 "confidence" score destroys the distinction an analyst
needs.

Defaults are deliberately pessimistic: a new, untested source is **F** — "no
basis to judge", not "unreliable" — and an uncorroborated claim is **6**, not
"probably true".

### Confidence propagation

A derived assertion's grade is **not** the product of its inputs' grades.
Multiplying probabilities along a chain assumes independence, and in OSINT the
independence assumption is almost always false: three websites repeating one
press release are one source, not three.

The rules:

1. Carry the **weakest link** along a derivation chain, not a product.
2. Track a **count of genuinely independent sources** alongside the grade.
3. **Collapse sources sharing a common upstream origin to one** before
   counting. This requires tracking origin, not just immediate source, and is
   the single most common failure in automated OSINT aggregation — the illusion
   of corroboration produced by syndication.

*Implemented as:* the assertion writer **refuses** a derived assertion whose
grade is better than its weakest upstream (rule 1), the projector counts
`independent_sources` over the *root* origins of an assertion's derivation
chain, collapsing sources that share an `origin_key` (rules 2 and 3), and a
derived assertion therefore adds no independence of its own. Whether a given
`origin_key` is shared is the operator's or plugin author's declaration on the
source — the system cannot discover syndication by itself.

### Contradictions

Contradictory assertions coexist. Nothing is overwritten. A *resolution* is
itself an assertion, made by an analyst, with its own evidence and reasoning,
optionally linked to a stored **Analysis of Competing Hypotheses** matrix
(hypotheses across, evidence down) so the reasoning survives the analyst. For
contested identity merges and attribution questions, the matrix is data, not
prose — and evidence consistent with every hypothesis is correctly recorded as
having no diagnostic value.

## 4. Layer 2 — entities

**FollowTheMoney is the canonical internal vocabulary.** It is MIT-licensed,
purpose-built for investigations rather than for cyber-threat intelligence, and
ships ~71 schemata covering the entity types this project centres on: Person,
Company, Organization, LegalEntity, Ownership, Directorship, Address,
UserAccount, BankAccount, CryptoWallet, plus media types (Image, Video, Audio,
Document, Message, Call). Its relationships-as-entities design — an Ownership
is itself an entity with properties — is exactly what a provenance-first model
needs, because an edge can then carry its own assertions.

Where FtM is thin, Astral Lens adds custom schemata in the same YAML form:
**devices** (the clearest gap across *all* candidate standards), vehicles,
vessels, aircraft, infrastructure assets, and richer online-account types.

An entity is not a stored record so much as an identity: a cluster id, plus
every unretracted assertion whose subject is that cluster. Materialized entity
rows exist for query performance and are rebuilt from the log.

### Why not the alternatives

| Rejected as internal model | Reason |
|---|---|
| **STIX 2.1** | No Person type at all; people appear only as `Identity` or `Threat Actor`. Devices and vehicles unsupported. Verbose. Built for CTI, which is not this project's domain. Retained as an *export* format. |
| **MISP objects** | Attribute-centric and CTI-oriented; not a standards-body model; template quality varies. Its `person`, `organization` and `ftm-*` templates make it a good *import/export* target. |
| **schema.org / Wikidata** | Publication vocabularies, not investigation provenance models. Wikidata's statement/qualifier/reference/rank structure is nonetheless a direct influence on the design above, and Wikidata QIDs are used as stable external anchors. |
| **Fully custom** | Loses interoperability; every import and export needs a bespoke mapping layer, forever. |

Standards live **at the export boundary**, not in the core. STIX 2.1, MISP
objects, FtM-native and plain CSV/JSON exporters are adapters over layer 2.

## 5. Entity resolution — reversible by construction

Records are **never physically merged.** Merging is the one irreversible
operation in most investigative tools and the one most often wrong.

Instead, resolution follows the judgment model proven by OCCRP's
`nomenklatura`: a judgment is an append-only edge between two entity
references — `same`, `not_same`, or `undecided` — and clusters are the
connected components over `same` edges, with `not_same` acting as a hard
constraint. A stable mapping table keeps canonical ids from churning as
clusters change.

Undoing a merge is retracting a judgment and recomputing. That is all.

**Candidate generation** uses `Splink` (MIT) for scored probabilistic matching
via Fellegi-Sunter with EM estimation — fast enough for roughly a million
records on a laptop, which suits the small tier. Its conditional-independence
assumption is violated by correlated fields, so scores are treated as
*candidate rankings for human review*, never as automatic merges above a
threshold.

Deliberately excluded: **Zingg** (AGPL-3.0, JVM and Spark — far too heavy for
the reference box) and **Senzing** (proprietary; its free tier is limited to a
few hundred source records, which is unusable for self-hosting).

**Explainability is mandatory.** The survey found no open-source tool that
shows a reviewer *why* two records were proposed as the same, with per-field
match evidence and a visible merge and split history. That is a gap this
project should own: every judgment stores the field-level comparison vector
that produced it.

## 6. Names across scripts

Investigations are multilingual and name matching is where naive systems fail
first. The pipeline:

1. **Normalize per script** — Unicode normalization, script detection, script-
   appropriate casing and diacritic handling.
2. **Transliterate** with ICU (`Any-Latin` then `Latin-ASCII`), storing the
   romanized form as an *additional* FtM name value rather than replacing the
   original. FtM's multi-valued properties exist for exactly this. Note that
   transliteration output is standard-dependent, so the standard used is
   recorded.
3. **Score** with `rapidfuzz` token-based similarity (MIT, roughly twice as
   fast as `jellyfish` on Jaro-Winkler).
4. **Use phonetic codes as blocking keys only, within a single language.**
   Soundex, Metaphone and NYSIIS are English-oriented and actively harmful
   across languages — Soundex collapses *Christine*, *Christina* and
   *Christopher* to one code. They narrow candidate sets; they never decide a
   match.

Original-script and romanized forms are both always retained and searchable.

## 7. Cases, workspaces, principals

A **case** is simultaneously the compartmentalization boundary, the retention
boundary, the collection-identity boundary and the permission boundary. It is
the unit everything else hangs from.

```
Workspace ──┬── Principal (user, service, agent)
            ├── Role grant
            └── Case ──┬── Assertions
                       ├── Artifacts (via assertion evidence)
                       ├── Collection identity (egress persona)
                       ├── Retention policy
                       └── Audit log
```

In the solo profile these tables exist but collapse to a single implicit
workspace, one principal and no role checks — no login ceremony, no friction.
In the team profile the same schema carries real multi-tenancy. Designing the
seam now costs little; adding it later means rewriting every query in the
system. See [ADR-0004](adr/0004-tenancy-seams-from-day-one.md).

Per-case collection identity deserves emphasis: one shared egress IP across all
cases **links every investigation the operator has ever run**, from the
perspective of anyone observing. Egress identity therefore belongs to the case,
not to the installation.

## 8. Physical storage

**One Postgres at the small tier. No second database.**

- **Assertions and artifacts** — Postgres tables as above.
- **Graph traversal** — edges in plain relational tables, queried with
  recursive CTEs that are *bounded*: a depth cap, a cycle guard and a row cap,
  always. Investigation queries are overwhelmingly two to four hops with
  filters, which relational traversal serves well. Indexes on
  `(subject, predicate)` and `(object_entity, predicate)`.
- **Full-text search** — Postgres FTS at the small tier; a dedicated search
  service only at the cluster tier.
- **Vector search** — an embedded in-process index at the small tier; a
  dedicated service only when corpus size demands it.

Apache AGE (Apache-2.0) is supported as an *optional* query backend for
analysts who want Cypher, not as a dependency (`graphstore.py`, `ASTRAL_AGE_DSN`;
built and measured in M7, see the roadmap). Benchmark evidence comparing
recursive CTEs against AGE is genuinely mixed and mostly secondary, so neither
is assumed superior; the abstraction lets us measure on real data.

At the `server` and `cluster` tiers a dedicated graph database may be added as
a **rebuildable projection** — never the system of record, so it can be dropped
and rebuilt. The candidates carry traps that constrain this choice:

- **Neo4j Community** — GPLv3; one standard database; backup, clustering,
  failover and RBAC are Enterprise-only.
- **Memgraph Community** — BSL 1.1, whose Additional Use Grant permits internal
  business use but **prohibits embedding or distributing to third parties**.
  Since Astral Lens is software we ship to others, this is close to
  disqualifying. (Its stated Change Date is also inconsistent across sources
  and should be read from the licence text directly.)
- **Kuzu** — archived and read-only since October 2025. Not adoptable.
- **ArangoDB** — BSL 1.1 since 3.12, with a community production dataset-size
  limit.

The honest conclusion is that the licensing landscape for embeddable graph
databases is hostile to shipped software, which independently validates keeping
Postgres as the system of record.

## 9. What this model buys

Stated plainly, because these are the capabilities that justify the extra
complexity:

- **Explain any conclusion.** Walk `derived_from` to the originating artifact.
- **Reproduce a case** at a past state, including what was believed then.
- **Reverse any merge** by retracting a judgment.
- **Represent disagreement** between sources without choosing prematurely.
- **Retract a broken source.** When a plugin is found to have been wrong for a
  month, retract every assertion carrying that `derived_by` and recompute. No
  other data model makes this tractable.
- **Counterfactuals.** Ask what the case looks like without a given source.
- **Defensible exhibits.** Every claim resolves to a hashed artifact and a
  recorded acquisition context.
