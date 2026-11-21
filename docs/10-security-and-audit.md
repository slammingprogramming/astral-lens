# 10 — Security, Audit & Operator Posture

Two distinct threat models, often conflated, with different mitigations:

1. **Protecting the investigation** — the operator's targets, methods and
   findings must not leak, and their collection must not be trivially
   attributable.
2. **Protecting the subject** — people appear in this system who are not
   suspects, often not even persons of interest, and the system must not
   quietly become a surveillance archive of them.

## 1. Secrets

API keys and credentials are never in plugin code, never in the assertion log,
and never in exported bundles.

- Stored encrypted at rest; the solo profile uses a key derived from an
  operator passphrase or the OS keystore; team profiles may use an external
  secret manager.
- **Injected into a plugin call at invocation time**, per the wire protocol in
  [04](04-plugin-system.md), scoped to the secrets that plugin's manifest
  declares. A plugin never reads the global secret store.
- Redacted from logs, error traces and job records by a filter applied at the
  logging boundary, not by convention.
- Rotation is a first-class operation; a rotated key invalidates cached
  credentials across workers.

## 2. Collection OPSEC

**Egress identity belongs to the case, not the installation.** This is the
single most consequential OPSEC decision in the design: a shared source IP
across all cases links every investigation the operator has ever run, to anyone
with visibility of the destinations.

- Per-case egress configuration: direct, proxy, VPN, or Tor where the source
  tolerates it.
- Per-case persona: User-Agent, `Accept-Language`, timezone, and any
  account identities used.
- The persona and egress that served each request are recorded in the
  artifact's `acquisition_ctx` ([03 §2](03-data-model.md)), so a capture can be
  explained later.
- Per-host token buckets; honour `Crawl-delay` although **RFC 9309 does not
  define it**; exponential backoff on 429.

**Footprint is declared and gated.** Every plugin declares a footprint class
([04](04-plugin-system.md)):

| Class | Meaning | Default |
|---|---|---|
| `none` | Local computation only | Always allowed |
| `third_party` | Traffic to an intermediary; target cannot see it | Allowed |
| `dns` | DNS resolution the target may observe in passive logs | Allowed, logged |
| `target_contact` | **The target can see this.** Direct HTTP, port contact, email, account interaction | **Requires explicit approval, every time** |

The planner may propose a `target_contact` node; it may never execute one
without a human approving it in that moment. This is not a configurable
default.

**The firm technical line: Astral Lens never bypasses authentication or
anti-bot controls.** This is drawn where the legal risk actually concentrates —
[07 §5](07-data-sources.md) shows that post-*Van Buren* the live exposure is
contract and DMCA anti-circumvention, not the CFAA — and it is why the line is
technical rather than a matter of taste.

## 3. Audit

The audit log is append-only and **hash-chained per workspace** (each entry
commits to the previous entry's hash, so edits or deletions are detectable by
re-verification; the database also refuses UPDATE, DELETE and TRUNCATE on it).
An entry is written in the same transaction as the action it records, so one
cannot commit without the other. It is queryable per case and covers:

- Every query, by whom, against which sources, with what arguments.
- Every plugin invocation: plugin, version, backend, footprint, egress
  identity, duration, outcome.
- Every assertion written, retracted and resolved.
- Every merge judgment and every split.
- Every export, with the exact artifact set and claim set included.
- Every model load, including which weights, which licence class, and whether a
  non-commercial model was used.
- Every hosted-API call: provider, model, egress mode, and what left the box.

**The solo profile keeps the full audit log.** Its value to a lone
investigator defending their own work is at least as high as its value to a
supervisor, so it is never disabled as a "single-user simplification."

## 4. Retention and deletion

A case is the retention boundary ([03 §7](03-data-model.md)). Each case carries
a retention policy: a clock, a trigger and a disposition.

- **Retraction, not deletion, is the default.** Nothing is overwritten; a
  retracted assertion remains in the log with a row in `assertion_retraction`
  carrying the time, the retracting principal and a mandatory reason, so the
  history of belief stays intact. Retracting an assertion also retracts every
  assertion derived from it.
- **Hard deletion exists** for legal obligation — a GDPR erasure request, a
  BIPA destruction schedule, an expiring warrant. It is a deliberate,
  logged, case-scoped operation that removes artifacts and assertions and
  records a tombstone. It is the one operation that genuinely destroys.
- **Implemented in M0** (`astral.erasure`): the operation requires a stated
  legal basis, runs in one transaction, deletes the case's assertions,
  retractions, judgments, acquisitions, jobs, grants and projections, removes
  artifacts that no other case holds (blob files are unlinked only *after*
  commit, since a file delete cannot be rolled back), marks the case `erased`
  and writes a tombstone plus an audit entry. The case row itself and its audit
  entries remain.
- **Tension, stated rather than hidden.** The audit log is immutable and
  hash-chained, yet erasure law may require removing data about a person. The
  resolution is to keep *content* out of the audit log: entries carry ids,
  hashes, counts and plugin names, never source URLs, file names or extracted
  values. Those live in `artifact_acquisition` and assertions, which erasure
  can remove. Any new audit `detail` field must be reviewed against this rule.
- **Biometric templates get their own clock.** BIPA requires destruction at
  purpose-satisfaction or within 3 years, whichever is first
  ([06 §6](06-multimodal-pipelines.md)). Face and voice embeddings are tagged as
  biometric at write time precisely so this timer can run without an operator
  remembering to set it.
- **Breach-corpus assertions carry a retention timer by default**
  ([07 §7](07-data-sources.md)). The connectors work without friction; what the
  platform does unconditionally is make later deletion possible.

## 5. The record the platform keeps unconditionally

This is the design's answer to the user's "core feature, minimal friction"
decision on sensitive capabilities, and it is worth stating explicitly because
it is a choice, not a default.

Face search, voice matching, breach lookup and people-search connectors are
**available without gates, prompts, justification fields or policy toggles**.
What the platform does instead, unconditionally and invisibly, is **record**:
purpose (the case), operator, timestamp, model, model version, backend,
threshold, source and egress.

The reasoning: a gate the operator must click through protects nobody — they
click it — while costing them friction on every legitimate query. A complete
record costs them nothing at the moment of use and is the only artifact that
makes their use defensible afterwards, whether to a court, an editor, a client
or a regulator. The optional authorization fields (case number, warrant
reference, requesting authority) exist for team deployments that need them and
are **absent from the solo profile entirely.**

What the platform does *not* do, and will not be configured to do:

- Ship or build a scraped face index of the general public. **EU AI Act
  Art. 5(1)(e)** has prohibited creating or expanding facial-recognition
  databases through untargeted scraping since 2 Feb 2025, with no
  law-enforcement exception ([06 §6](06-multimodal-pipelines.md)). This is the
  non-goal in [01](01-vision-and-scope.md) and it is load-bearing.
- Infer race, political opinion, union membership, religion or sexual
  orientation from biometrics (Art. 5(1)(g)).
- Present a model's output as an identification. Face search returns **ranked
  candidates with scores and the backend used**, and the UI has no
  affordance for rendering a match as a conclusion.

## 6. Hosted model egress

The three egress modes from [08 §0](08-ai-layer.md) — `local-only`,
`hosted-redacted`, `hosted-direct` — are enforced in the policy layer, not in
prompts. `local-only` is the default for any task touching a named private
individual, because **provider usage policies prohibit compiling dossiers on
people**, which makes this a correctness constraint rather than a preference.

Every AI-produced result carries its egress mode as a visible badge and in its
`derived_by`.

## 7. Supply chain

- **Plugin signing.** Manifests are signed; the registry records publisher and
  signature. Unsigned third-party plugins can be installed, with a clear
  warning, because forbidding them would defeat the point of an open plugin
  system.
- **Container isolation where it earns its cost.** Third-party,
  dependency-heavy or untrusted plugins run containerized at the profiles where
  that is affordable, in-process at the small tier where it is not
  ([04 §2](04-plugin-system.md)). This is a deliberate accepted risk, stated
  rather than hidden.
- **Model weights are pinned by hash** and verified on load. A silently
  substituted model is a silently invalidated case.
- **Licence metadata is enforced at load.** The model registry
  ([06 §7](06-multimodal-pipelines.md)) and source registry
  ([07 §6](07-data-sources.md)) let preflight warn before a non-commercial
  weight or a non-commercial dataset is used in a deployment that has declared
  itself commercial. Given how many such traps the research found — InsightFace
  weights, NLLB-200, Ultralytics YOLO, OpenSanctions, VirusTotal, MaxMind — this
  is a genuine feature.
- **Untrusted input handling.** Every artifact entering the system is
  adversarial: malformed media targeting parser bugs, prompt injection in
  document text aimed at the AI layer, zip bombs, SSRF via plugin-supplied
  URLs. Parsers run with resource limits; retrieved document text is never
  treated as instructions by the planner; plugin-supplied URLs are validated
  against the egress allowlist before fetch.

## 8. Chain of custody

Per NIST SP 800-86 and ISO/IEC 27037 ([06 §5](06-multimodal-pipelines.md)):

- SHA-256 at acquisition, before any processing. Note SP 800-86 names MD5 and
  SHA-1; we do not use them.
- Nothing is modified in place; derived artifacts carry `parent_sha256`.
- Originals in WORM storage. With S3 Object Lock, **compliance mode, not
  governance mode** — governance mode can be bypassed by any principal holding
  `s3:BypassGovernanceRetention`. Delete markers are not WORM-protected, and
  default retention applies only to objects created after it is enabled.
- Exhibit export bundles artifacts, a hash manifest, the capture log, tool and
  model versions and an operator statement — the elements supporting
  authentication under FRE 901(b)(4) and self-authentication under 902(13) and
  902(14). **The platform assembles the package; it never asserts
  admissibility.**

## 9. Hardening checklist

- Authentication: local accounts with strong password hashing in the solo
  profile; OIDC in team profiles. MFA available in both.
- Authorization at the case boundary, enforced in the policy layer, with no
  query path that bypasses it.
- TLS everywhere, including between internal services at the team tier.
- CSP, SameSite cookies, CSRF protection on state-changing endpoints. *M0
  status:* the solo API has no login, so it binds to localhost only, allows
  only `localhost`/`127.0.0.1`/`[::1]` Host headers (against DNS rebinding) and
  requires a custom `X-Astral-Client` header on every state-changing request,
  which a web page the operator visits cannot send cross-origin without a CORS
  preflight the API never grants. The **team profile refuses to start** until
  real authentication (OIDC, roadmap M5) exists — there is deliberately no
  header-based stand-in for it.
- Rate limiting on the API, independent of per-source rate limiting.
- Database encryption at rest; encrypted backups with tested restores.
- An offline/air-gapped install path: pre-downloaded models and site packs, no
  outbound calls at startup. Note that **pyannote's community model is gated**
  ([06 §2](06-multimodal-pipelines.md)), so air-gapped installs need the token
  flow handled at image-build time.
