# 09 — UI & UX

Stack: **React + TypeScript**. The UI's job is to make a provenance-first
assertion store (doc 03) legible to a working analyst, and to make the
platform's uncertainty visible rather than hiding it behind confident-looking
widgets.

## 1. The gap this UI exists to close

The research survey found that commercial tools (i2, Maltego, Videris,
Palantir, Paliscope) unify link analysis, timeline and geospatial views, while
**no open-source tool does**. The open tools each hold one piece: SpiderFoot
exports GEXF, Maigret exports D3 and Cypher, Creepy shows a map and exports
KML, Datashare does document search well, Aleph did entity-plus-document
search before its community edition went unsupported on **31 December 2025**.

Unifying **graph + timeline + map + document review over one evidence store,
in the open** is the single clearest product opportunity identified by the
research. It is the organizing goal of this document.

## 2. One global time axis

Borrowed from Palantir Foundry, whose docs describe the timeline control
setting the map's time, with the time range filtering map data **even when the
timeline is hidden**, plus playback.

In Astral Lens a single time scope is a first-class piece of application state
that filters **every** view simultaneously — graph, map, document list, search
results, entity profiles. Consequences:

- Point events render as ticks; durations render as bars. This falls directly
  out of the `valid_from`/`valid_to` columns in doc 03.
- A scrubber with playback animates the case forward: edges and map features
  appear and disappear as they become and cease to be true.
- **Two time axes, selectable.** Because assertions are bitemporal, the user
  chooses whether the axis means *valid time* ("what was true in March 2020")
  or *transaction time* ("what did this case believe in March 2020"). No other
  open investigative tool offers the second, and it is the one that matters
  when a conclusion is challenged.

## 3. Graph

- **Expand on demand with rollback.** OpenCTI's model — pivot from a node,
  with the ability to roll back the last N expand actions — is the correct
  interaction. Exploratory expansion is how analysts work, and without undo the
  canvas becomes unusable within a dozen clicks.
- **Pivots are generated, not hard-coded.** Because plugins declare their
  `consumes` types (doc 04), the context menu on a Person node is computed from
  the registry. Installing a connector adds pivots everywhere its input type
  appears, with no UI work. This is Maltego's input-constraint model and
  SpiderFoot's `watchedEvents`, applied to a generated menu.
- **Edges carry provenance.** Every edge shows its independent-source count and
  is clickable through to the underlying assertions and artifacts. An edge
  asserted by one blog and an edge asserted by three registries must not look
  identical.
- **Contradictions sit side by side.** Conflicting assertions render as
  parallel edges with a conflict marker, never silently reconciled. Resolving
  one opens the ACH matrix view (doc 03 §3).
- **Cluster membership is visible and reversible.** A resolved entity shows its
  constituent references, the field-level match evidence that proposed each
  merge, and a one-click split. The research found **no open-source tool that
  shows a reviewer why two records were proposed as the same** — this view is
  the answer to that gap.

Rendering: **Cytoscape.js** for analysis views (strongest layout and analysis
feature set, but sluggish above ~10k elements) and **Sigma.js + Graphology**
(WebGL) for large views. Both benchmarked on real case data before the split
point is fixed; licences need verifying.

## 4. Map

Time-linked, as above. Layers for case entities with locations, imagery
footprints (Copernicus), street-level coverage (Mapillary/KartaView), ADS-B and
AIS tracks, and OSM/Overture context.

**Geolocation hypotheses render as regions, never pins.** Doc 06 establishes
that image-geolocation models are region-level; a pin communicates a precision
that does not exist. A ranked set of shaded candidate regions with confidence
is the honest representation, and the UI should make the dishonest one
impossible to draw.

For comparison tasks, side-by-side synchronized maps rather than overlays.

## 5. Document and media review

- **Hits in context**, with filters by date, country, language, source and
  dataset — the Aleph pattern.
- **Bidirectional claim-to-span linking.** From a claim in a report, jump to the
  exact page, character span or **timecode** in the source. From a highlighted
  span, see every claim that cites it. The transcript chunking in doc 08 stores
  timecodes precisely so audio and video are first-class citation targets, not
  second-class.
- **Transliteration-aware search.** A query in Latin script matches
  original-script names and vice versa, per doc 03 §6, with both forms shown.
- **Side-by-side source review** with the original artifact rendered as
  captured — the stored request/response context from doc 03 §2, not a live
  re-fetch, because the page may have changed and the whole point is what it
  said when we saw it.
- **Evidence-grade capture is a headline feature.** Per-capture SHA-256,
  timestamp, MHTML or WARC storage. Hunchly does this well and is commercial,
  Chrome-only and single-user; the research found no open-source equivalent
  with multi-user support. Astral Lens should own this.

## 6. Monitoring and alerting

Saved searches re-run on a schedule with a **diff view** — what is new, what
changed, what disappeared. Disappearance is often the more interesting signal
and most tools ignore it. Watchlists cross-reference in bulk against new data
(the Aleph pattern). Alerts are per-case and per-user.

## 7. Degradation and health, in the interface

Doc 05 and doc 04 both require visible degradation. Concretely:

- A **capability panel** showing the active compute profile, which backends are
  loaded, what is degraded and why.
- **Pre-run job estimates** — backends to be used, rough wall time, OPSEC
  footprint, cost and data egress — shown before the user commits.
- **Per-result backend badges.** A face match from the small CPU model is
  labelled as such, inline, next to the result.
- **Quarantined sources are shown as unknown, never as negative.** The single
  most important rule in this document. A broken username checker reporting "no
  account found" is a false exculpatory finding, and the surveyed tools
  routinely conflate silence with absence. The source health panel shows each
  connector's last success, failure class and latency trend, and results from
  degraded sources are visually distinct everywhere they appear.
- **Egress mode badges** on every AI-produced result (doc 08 §0): local-only,
  hosted-redacted, hosted-direct.

## 8. The natural-language entry point

A single input accepting a question, a file, an image, a URL or a selector. It
compiles to a plan and **shows the plan** (doc 08 §3) before running anything:
the DAG of connector calls, the estimated cost, the data egress, the OPSEC
footprint of each node. The user edits nodes, removes sources, adjusts budgets,
then runs.

This is the non-negotiable design commitment from doc 01: **if the user cannot
see and edit what their question turned into, the feature is not finished.** A
chat box that silently fans out to forty sources is not an investigative tool,
it is a liability generator.

Plans are saveable as named, reusable **Lenses** — the pipeline templates of
doc 02 — so an ad-hoc investigation becomes repeatable methodology.

## 9. Reporting

Report composition from the case graph, following doc 08 §6: source assessment,
key judgments, alternatives, indicators, gaps.

- Every claim shows its citations inline and will not export while any claim
  lacks one.
- **Likelihood and confidence render in separate sentences**, enforced by the
  generator, with the ICD 203 band label shown alongside the term.
- The **"what would change this judgment"** field is a visible part of each
  judgment, not a footnote.
- The **gaps section is populated automatically** from quarantined sources and
  unanswered plan nodes, so incomplete collection is visible in the product
  rather than invisible by omission.
- **Exhibit export** produces the artifact bundle plus a hash manifest, capture
  log, tool and model versions and an operator statement — the elements that
  support authentication under FRE 901(b)(4) and self-authentication under
  902(13)/(14). The platform assembles the package; it never asserts
  admissibility, which is for counsel and the court.

## 10. Collaboration

Case roles, assigned tasks, per-case audit log, comments and annotations on
entities and artifacts, and templates. In the solo profile this collapses to a
single user with the audit log still running, because the log's value to a lone
investigator defending their own work is at least as high as its value to a
supervisor.

## 11. Open questions

- The element-count threshold where Cytoscape.js must hand off to Sigma, and
  whether that handoff can be seamless or needs to be an explicit mode.
- Whether the dual time axis (valid vs transaction) can be exposed without
  confusing users who have never encountered bitemporality — it may need a
  default and a disclosure rather than a prominent toggle.
- How to render a 5,000-node expand without either lying through aggregation or
  freezing the browser.
- Licence verification for Cytoscape.js, Sigma.js and Graphology.
