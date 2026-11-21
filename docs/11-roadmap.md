# 11 — Roadmap

Ordered by **what is irreversible if deferred**, not by what demos well. The
assertion log, the plugin contract and the tenancy seam are all effectively
impossible to retrofit; a face-search UI is a weekend. So the unglamorous work
comes first.

Milestones are sized in scope, not dates.

## M0 — Foundations (irreversible decisions)

The only phase where getting it wrong is fatal.

- Postgres schema: artifacts, assertions, sources, cases, workspaces,
  principals, judgments. Bitemporal and graded from migration one.
- The **assertion writer** as the single write path to layer 1, enforcing
  evidence, `derived_by` and grading. No other code may insert.
- Content-addressed artifact store with acquisition context.
- Projector: rebuild layers 2–3 from layers 0–1.
- Plugin contract and manifest schema; in-process host.
- Resource-aware queues; compute-profile detection and preflight report.
- Model residency manager with measured-on-first-load VRAM accounting.
- FastAPI skeleton, policy layer, audit log.
- Tenancy seam present, collapsed in solo profile.

**Exit test:** ingest a file, write a graded assertion through a plugin, drop
and rebuild every projection, and explain the resulting conclusion back to the
originating artifact.

### M0 status (2026-11-05): implemented, exit test passing

Code is in [`backend/`](../backend/); `pytest` runs 75 tests, including the exit
test driven through the HTTP API (`tests/test_exit_m0.py`). What exists:

| Item | Where | Notes |
|---|---|---|
| Schema, append-only and writer-only guards | `migrations/0001`, `0002` | Enforced by triggers, not only by Python; direct INSERT, UPDATE, DELETE and TRUNCATE on layer 1 are refused |
| Assertion writer | `assertions.py` | Evidence, `derived_by`, weakest-link grading, AI-layer limits (invariant 10), cascading retraction, retract-by-plugin |
| Evidence store | `evidence.py` | Streamed SHA-256, write-once blobs, verified on read, per-case acquisition |
| Projector and resolver | `projector.py`, `resolver.py` | Deterministic rebuild, `same`/`not_same` judgments with conflicts kept visible, independence counted over root origins |
| Explain, bitemporal queries | `explain.py` | Walks `derived_from` to the originating artifact; truth-as-of and belief-as-of |
| Plugin contract | `plugins/` | Manifest schema, model-import lint, registry, in-process **and subprocess** runtimes over the same JSON protocol, quarantine → *unknown*, `target_contact` approval, scoped secrets, rate limit |
| Compute | `compute/` | Host probe, profiles, preflight, licence-aware model registry, residency manager |
| Queues | `queues.py` | DAG deps, cross-process per-class caps, retries, crash reaping ([ADR-0003](adr/0003-orchestration.md)) |
| API, audit, erasure | `api.py`, `audit.py`, `erasure.py` | Hash-chained audit log; case-scoped hard delete with tombstone; CSRF and Host-header guards |

**Not done, carried out of M0** — none of these is hidden by the exit test:

- **Secrets** use an environment-variable provider only; the encrypted store,
  passphrase/OS-keystore key and rotation of [10 §1](10-security-and-audit.md)
  do not exist, and neither does the log-redaction filter. *(Done in M1; the OS
  keystore variant is not — the key comes from a passphrase.)*
- **Model residency is exercised with fake loaders.** The accounting, LRU,
  pinning, admission and measured-footprint logic is tested, but no real model
  has been loaded, so "measured-on-first-load VRAM" has not been observed on a
  GPU. That arrives with the first backend (M2/M3).
- **Egress modes** (`local-only` etc.) and per-case egress enforcement are
  recorded in the audit log but not enforced; M1 for collection, M4 for AI.
  *(Collection egress enforced in M1; AI egress is M4.)*
- **No scheduler**: plugin self-checks run on demand, not on their interval.
  *(An in-process sweep exists as of M1.)*
- **No plugin signing, no weight-hash pinning, no container runtime** (M5).
- **The team profile is not exposed** over HTTP until real authentication
  exists; the role-checking policy is implemented and tested directly.
- Entity resolution **candidate generation** (Splink) is M1; only judgments and
  clustering exist.
- The projector loads a case into memory; a set-based SQL path is future work.

## M1 — Text investigation end to end

The first genuinely useful release, with no GPU required.

- Connectors tier 1A: GLEIF, OpenCorporates, Companies House, SEC EDGAR,
  OpenSanctions, OFAC, ICIJ Offshore Leaks.
- Web: SearXNG backend, Wayback CDX, **evidence-grade web capture** (MHTML or
  WARC, per-capture SHA-256) — the capability
  [09](09-ui-and-ux.md) identifies as absent from open source.
- Document parsing (Docling, Tika), NER (GLiNER multi-v2.1, spaCy).
- Entity resolution: Splink candidates, nomenklatura-style judgments, cluster
  recomputation, and the **merge-explanation view** with per-field evidence.
- Name matching across scripts ([03 §6](03-data-model.md)).
- UI: case workspace, entity profiles, graph with provenance-bearing edges,
  document review with hits in context, the global time axis.
- Health self-checks, auto-quarantine, and **"unknown ≠ negative"** everywhere.

**Exit test:** acceptance scenario 3 from [01](01-vision-and-scope.md) — name
to network — on a `cpu-only` box.

### M1 status (2026-11-06): implemented; exit test passing against local fixtures

`pytest` runs 123 tests; the exit test (`tests/test_exit_m1.py`) drives name →
coverage report → unmerged candidates → human `same` judgment → graph → edge
click-through to the raw API response, entirely through the HTTP API. The UI
(`frontend/`, built with `npm run build`, served at `/ui/`) was exercised in the
in-app browser against a seeded case (`python tests/demo_server.py`).

| Item | Where | Notes |
|---|---|---|
| Capability channel | `capabilities.py`, `plugins/host.py` | Plugins call `http.fetch`, `artifact.*`, `text.extract`, `ner.extract`, `web.capture` and must declare each; a plugin never opens a socket or loads a model |
| Egress policy | `egress.py` | `direct`, `proxy`, `local-only` per case; redirects re-checked; rate limits honoured by waiting; audit records hosts and counts, never URL paths |
| Connectors | `plugins/connectors/` | GLEIF, Companies House, SEC EDGAR, OpenCorporates, OpenSanctions (API or bulk), OFAC SDN, ICIJ Offshore Leaks, Wayback CDX, SearXNG. Each is a manifest and a function, with licence metadata and an `origin` so shared upstreams count once |
| Coverage ledger | `investigate.py`, migration `0003` | `hits` / `no_hits` / `unknown`; a failure or missing key is `unknown`; nothing ever asserts absence |
| Documents and NER | `docparse.py`, `ner.py`, `backends.py` | Backend registry resolves best-first and records `degraded_from` in `derived_by` when a better backend is missing. Identifier checks (IBAN mod-97, LEI, BTC, ETH) |
| Names | `names.py` | NFKC, case-folding, diacritics, transliteration (standard recorded), legal-suffix handling, fuzzy scoring |
| Resolution | `resolution.py` | Blocking, per-field comparison vector, ranked proposals stored as `undecided` judgments; only a person's judgment merges |
| Web capture | `capture.py` | WARC 1.1 with block and payload digests and a verify function; payload kept as a derived artifact |
| Views | `views.py`, `frontend/` | Graph from relationship-entities with weakest-link edge grades; timeline from date predicates; document hits in context; merge-explanation view |
| Secrets | `secrets.py` | Fernet store keyed by a scrypt-derived passphrase key, rotation, env fallback, redaction of keys in stored URLs and logs |
| Health | `plugins/host.py` | Sweep honours each plugin's interval; failures quarantine to `unknown` |
| Licence preflight | `compute/preflight.py` | A commercial deployment is told which enabled plugins read non-commercial (block) or unconfirmed (warn) data |

**Not verified, not done — none of this is hidden by the exit test:**

- **No connector has talked to its real service.** Every connector was tested
  against a local server returning hand-written responses in the documented
  shape. That proves the mapping and the failure handling, not that the live
  APIs still answer this way. Live runs need keys (Companies House,
  OpenSanctions, OpenCorporates) and network access.
- **Docling, GLiNER and spaCy adapters are untested.** The libraries and model
  weights are not installed (weights need your approval to download). What runs
  today is the built-in parser and the rules extractor, which finds identifiers
  and organisations by suffix but **does not find person names**; the UI shows
  `rules · degraded` on those mentions.
- **Web capture does not execute JavaScript**, and response headers are
  reconstructed rather than recorded off the wire. It is evidence of what the
  server returned to this client, not of a rendered page.
- **Deviations from the plan:** resolution uses an in-process comparator with
  fixed weights rather than Splink (no training data exists to fit a model, and
  a scored ranking is all the UI claims); transliteration uses `anyascii` and
  `rapidfuzz`, not ICU. Cross-script matching is as good as transliteration is.
- **The UI has no automated tests**; it was checked by hand in one browser, on a
  case of a few dozen entities. Large graphs are untested.
- **Scheduler**: the health sweep runs inside the API process and stops with it.
- Plugin **signing and containers** remain M5; the team profile remains M5.

## M2 — Media

- ASR: faster-whisper (GPU) and **Parakeet-TDT INT8 ONNX (CPU)**. The research
  settles that audio does not need a GPU — RTF ~0.035 on four cores
  ([06 §2](06-multimodal-pipelines.md)).
- Diarization (pyannote community-1), speaker embeddings, speaker matching
  (WeSpeaker/CAM++), VAD (Silero), language ID.
- Video: PySceneDetect, RT-DETRv2, ByteTrack, keyframe OCR.
- VLM captioning on the GPU tier only, with a capped frame budget, clearly
  labelled as triage.
- Translation via MADLAD-400 (Apache-2.0) — **not** NLLB-200.
- EXIF/metadata, C2PA verification with the honest "absence proves nothing"
  framing.
- Timeline assembly from media with timecodes as citation anchors.

**Exit test:** acceptance scenario 2 — video to dossier, overnight, on the 8 GB
reference box — with published wall-clock numbers.

### M2 status (2026-11-06): pipeline built and verified; exit test NOT met as specified

`tests/test_media.py` (13 tests) drives video → probe → transcript → entities →
scenes → timeline through the HTTP API with real ffmpeg, ffprobe and Tesseract.

| Item | Where | State |
|---|---|---|
| Probe, EXIF, container tags | `media.py`, `plugins/core/media-probe` | **Verified.** Dates, device and GPS are recorded as *claims by the file* (information grade 4, `claimed_by_file`), never as facts about the event |
| C2PA | `media.py` | **Presence scan only** (JPEG, PNG, ISO-BMFF), verified on hand-built files. Validation needs `c2patool`, which is not installed; the adapter is untested. "None found" is reported as proving nothing |
| Audio extraction, VAD | `media.py`, `mediamodels.py` | **Verified.** The built-in VAD is an energy gate, not Silero: it cannot tell speech from music. It exists so silence is never sent to a recogniser |
| ASR (Parakeet ONNX, faster-whisper) | `mediamodels.py` | **Adapters only, never run.** No library or weights installed. The pipeline is tested with a stub recogniser |
| Diarization, translation | `mediamodels.py` | **Adapters only, never run.** Speakers are reported `unknown`, not "one speaker" |
| Transcript with timecodes | `plugins/core/transcribe` | **Verified** with the stub: transcript and a segments index are derived artifacts, so a mention is citable as (file, start_ms, end_ms) |
| Scenes, keyframes, keyframe OCR | `plugins/core/video-scenes`, `core/ocr` | **Verified.** Hard cuts only (ffmpeg scene score). Tesseract is real. Not built: VLM captions |
| Region guess from appearance (GeoCLIP) | `geo.py`, `plugins/core/geolocate`, `tests/test_geo.py` (7) | **Built, optional, run with the real model on three images; off unless the caller opts in.** PyTorch (CPU), `geoclip` 1.3.0 and a local OpenAI CLIP ViT-L/14 copy. Output is coarse grid cells (5 degrees), the model's share in each, graded 5, never a coordinate. Run through the project's backend: a Wikimedia Commons photograph of the Sydney Opera House came out as the single cell 35 to 30 degrees south, 150 to 155 east (all 100 predictions in it); one of the Eiffel Tower as the cell 45 to 50 north, 0 to 5 east; the indoor image of two cats, which has no place in it, came out diffuse (best cell 31% of the returned score, over the United Kingdom, with Canada, Hungary and Denmark among the others) and is flagged as such. About 9 s an image on this CPU after a first load of 65 to 320 s (the 1.7 GB backbone). **Not measured:** accuracy on any benchmark or on ordinary scenes; two famous landmarks prove the wiring, not the quality. **Licence:** the weights have no stated licence and the training photographs are of mixed licence; the backbone's card calls any deployed use out of scope. Both are registered `unknown`, the model is not a default, and an earlier doc line that called it MIT throughout was wrong and is corrected |
| Object detection and tracks | `objects.py`, `tracking.py`, `plugins/core/objects`, `core/video-tracks`, `tests/test_objects.py` (20) | **Built and run with the real model.** RT-DETR r18vd (Apache-2.0, COCO, 80 classes) through ONNX Runtime on the CPU; the tracker is a ByteTrack-style one written out here (Kalman prediction, Hungarian matching, a second pass for weak detections). Checked against the model card's own example image: the two cats and two remotes land on the published boxes (overlap above 0.9); the scores run lower than the card prints (the sofa 0.81 against 0.97), and whether that is the ONNX export or a stale card was not determined. A real image panned into a 6-second clip came out as 24 sampled frames, 145 detections and five tracks, four of them lasting the whole clip and the second cat joining at 1.5 s as it came into view; about 0.6 s a frame on this machine while other jobs ran. A person box is a region, never an identity; a track groups boxes, not identities; a video past the frame cap is not scanned and the coverage says so. **Not measured:** any tracking benchmark, any accuracy on CCTV, aerial, night or small-object footage; no GPU path; the larger RT-DETRv2 variants are not wired |
| UI | `frontend/src/viewsC.tsx` | Media tab with player, claim labels and a click-to-seek timeline, checked by hand in the in-app browser |

**Wall-clock (this machine: 16 logical CPUs, Windows 10, ffmpeg limited to 4
threads), `tests/bench_media.py 30 1280 4`.** A *synthetic* 30-minute 1280×720
video (ffmpeg `testsrc2`, 1.5 GB), **speech recognition not included**:

| Stage | Seconds | × realtime |
|---|---|---|
| ffprobe | 0.15 | 11,690 |
| Audio extract to 16 kHz mono | 0.76 | 2,383 |
| Energy VAD (pure Python) | 1.64 | 1,099 |
| Scene detection (full decode) | 9.64 | 187 |
| Upload, SHA-256, store | 3.31 | 545 |
| **Whole ingest through the API, stub ASR** | **16.84** | **107** |

How to read it: everything except recognition is minutes, not hours, for a
two-hour file. The number that decides "overnight on the reference box" is
**ASR real-time factor, which is still unmeasured**. The video is far simpler
than camera footage, so real decode and scene-detection times will be higher,
and the scene count (1) means the keyframe and OCR timings are single-frame.
This does not satisfy the exit test: no real recogniser has run, and nothing was
run on an 8 GB machine.

Known weaknesses seen while testing: the rules entity extractor finds identifiers
and `Ltd`-style organisations but misses person names and can absorb a
sentence-initial word ("Contact Acme Holdings Ltd"); that is the GLiNER/spaCy
gap noted under M1.

## M3 — Faces and vectors

- Vector index: pgvector at small tier, Qdrant at larger.
- **YuNet + SFace as the licence-clean default**; InsightFace as a knowing
  opt-in with the non-commercial weight licence surfaced at install
  ([06 §1](06-multimodal-pipelines.md)).
- Perceptual hashing (PDQ) for near-duplicates, run before embeddings.
- Face search over case media and operator-supplied watchlists.
- Reverse image search connectors (TinEye, FaceCheck.ID's documented REST API;
  PimEyes has no public API).
- Region-level geolocation via GeoCLIP, rendered as regions and never as pins.
- The **model registry** with licence, commercial-use flag and measured
  footprint per row.
- Biometric tagging at write time so the retention clock runs
  ([10 §4](10-security-and-audit.md)).

**Exit test:** acceptance scenario 1 — photograph to identity candidates, with
every result carrying its backend, score and licence class.

### M3 status (2026-11-07): core built and verified on real photographs; three items not built

`tests/test_faces.py` (10 tests; 9 run by default, the pgvector one needs the
extension) uses real photographs when `ASTRAL_FACE_TEST_DIR` points at an
extracted LFW `lfw_funneled` folder (the photos are copyrighted and are not in the
repository). Run with LFW, all 9 pass; the pgvector test passed separately against
a throw-away `pgvector/pgvector:pg16` container.

| Item | Where | State |
|---|---|---|
| Detect + embed, licence-clean default | `faces.py`, `plugins/core/faces` | **Verified.** YuNet (MIT) + SFace (Apache-2.0) through OpenCV. Needs the two ONNX files (`ASTRAL_FACE_MODELS_DIR`, or `models/`; never fetched automatically). On LFW photos: detector scores 0.92–0.94, same-person cosine 0.789 against OpenCV's reference threshold 0.363 |
| InsightFace opt-in | `faces.py` | **Adapter only, never run.** Chosen only when the caller sets `allow_noncommercial`; tested that it is never selected otherwise |
| Case-media and watchlist search | `faces.py`, `api.py` | **Verified.** Ranked candidates with score, backend, model and licence class; the UI offers no affordance for a verdict. Watchlist = photos the operator enrols (exactly one face each, a basis recorded but not judged). There is no crawler and no public-person index |
| Biometric tagging and retention | migration `0004`, `faces.py` | **Verified.** Every embedding is tagged and given `expires_at` at write time (case policy, never more than 3 years); `purge_expired` deletes; case erasure removes faces and hashes |
| Audit record of every search | `faces.record_search` | **Verified.** Model, backend, scope, threshold, count; the test asserts labels and watchlist reasons never reach the audit log |
| Vector search | `faces.search` | **Verified.** Exact cosine in NumPy over `real[]` (small tier), or pgvector's `<=>` when the extension exists; both give the same ranking and scores. Cost is linear in faces per query: fine to ~10⁵, then it is the M6 dedicated vector service |
| Perceptual hash | `pdq.py` | **Built, self-consistent, NOT reference-validated.** Called `pdq-like-1`; on generated images re-encodings sit at 0–4 of 256 bits and unrelated pictures far above 60. Do not compare with PDQ hashes from other systems |
| Model registry | `compute/models.py`, `data/models.yaml` | Entries carry licence, class and commercial-use flag. **Measured footprint per row is still missing**: no model was loaded on a GPU |
| UI | `frontend/src/viewsD.tsx` | Faces tab: probe search, faces in case, watchlist, near-duplicates |

**Not built, and why:**

- **Reverse-image connectors (TinEye, FaceCheck.ID).** They need paid API keys, and
  I could not verify either vendor's request and response shapes here. A connector
  written from memory would look like it worked and fail live.
- **Region-level geolocation (GeoCLIP).** Built later and run once with the real model; see the media and imagery table. The rule stands that it renders regions, never pins.
- **Face search quality on hard images.** LFW is easy and saturated. The default
  models are materially less reliable on CCTV, profile, low-resolution or
  occluded faces, and nothing here measures that.

Operational note: on this Windows machine the first `import cv2` in each process
took about two minutes (almost certainly antivirus scanning); afterwards calls
are milliseconds. Test runs with OpenCV therefore start slowly.

## M4 — The AI layer

- llama.cpp server with Qwen3.5-4B Q4, constrained JSON decoding.
- BGE-M3 embeddings, hybrid retrieval with RRF, bge-reranker-v2-m3.
- Contextual chunk headers generated at ingest.
- Planner producing a typed, bounded, **user-approved** DAG with cost, egress
  and footprint shown before execution.
- Citation enforcement: structured claims, span check, existence check,
  entailment check, two verifiers plus human sample.
- Report generation in ICD 203 form with mandatory gaps and indicators
  sections.
- Egress modes enforced in the policy layer.
- **The project's own 50–200 item evaluation set**, measuring extraction F1,
  citation pass rate and latency on the reference box. This gates the
  milestone; shipping the AI layer without it would be shipping an unmeasured
  hallucination surface.

**Exit test:** acceptance scenario 4 — question to answer with citations —
passing the citation verifier with a measured, published pass rate.

### M4 status (2026-11-07): the checked pipeline is built and verified WITHOUT a language model; the model-dependent half is not

No embedder or reranker is installed, and the measurements below were taken
without a language model, so they describe the model-free path. A model was
run later and measured on a small set (see "Run with a real model" in the M7 block):
a pass rate now exists, but on 16 questions, one run. **The milestone gate as written
("a measured, published citation pass rate") is met only in that thin sense**, and
nothing here should be read as a claim about answer quality with a model.

| Item | Where | State |
|---|---|---|
| Planner: typed, bounded, approved before running | `ai.py`, `/cases/{id}/ask`, `/plans/{id}/approve`, `/run` | **Verified.** Steps limited to `search_text` and `entity_facts` (≤ 6 steps, k ≤ 20). The plan is stored and shown with an estimate (steps, model calls, egress, footprint, "writes assertions: no"); `/run` refuses a plan that is not approved. A rules planner is the default; a model planner exists and falls back to rules if it fails |
| Hybrid retrieval, RRF | `retrieval.py`, migration `0005` | **Verified for the lexical rankers.** Postgres full-text plus a fuzzy ranker, fused by RRF. The result lists the rankers that ran. **Dense retrieval and a reranker were added in M7** (see the M7 block); without them configured, paraphrases sharing no words with the question are missed |
| Contextual chunk headers | `contextual.py`, `retrieval._ctx_ids`, migration `0014`, `POST /cases/{id}/retrieval/context` | **Built; verified with a scripted model, not a real one.** On request (explicit, at most 500 passages a call) the configured model writes one situating sentence per passage; it is stored beside the chunk, matched by a `context-headers` ranker in the fusion, and never replaces or alters the passage text, is never offered as a quotation, and is never an assertion. A header is rejected if it contains a number or a capitalised word the document does not contain; a model that cannot be reached stops the run with a count. Same endpoint policy as every model call. `tests/test_contextual.py` (10), including a passage that a keyword search for the company misses before headers exist and finds afterwards. Headers feed the lexical ranker only: the dense embeddings are still computed from the passage alone, so the larger gain the literature reports for contextual embeddings is not taken, and no real model's headers have been judged for quality |
| Citation enforcement | `citations.py` | **Verified.** Every claim must cite evidence ids and quote text: existence check, verbatim span re-read from the stored bytes, no identifier absent from the quotes, then entailment verifiers. Without a model the only entailment check is lexical word overlap, so the best verdict is `weakly_verified` and confidence is capped at `low`; the answer says so. The two model verifiers (strict, contradiction) exist and are tested **only against a scripted server**, which proves the plumbing and the rejection rules, not the judgement of a real model. A deterministic 20 % of claims are flagged for human review |
| Report | `ai.render_report` | **Verified structurally.** ICD 203 shape with Key judgments, Gaps, Indicators, Method and limits; no estimative-probability language |
| Egress modes | `llm.py`, `api._llm` | **Verified by tests with a local server.** `local-only` (default) refuses a non-local endpoint; a case set to local-only overrides a wider operator setting; `hosted-redacted` is refused as not built; redirects are not followed |
| Language-model planner/composer/verifiers | `llm.py`, `ai.py` | Planner and composer **have now run against a real model** (see "Run with a real model" in the M7 block); the two model entailment verifiers ran in the same evaluation (same small model, two prompts; which one rejected which dropped claim was not broken out). Any model failure degrades visibly to the extractive composer |
| UI | `frontend/src/viewsE.tsx` | Ask tab: question, plan with estimate and an explicit approve button, answer with per-claim confidence, check status, anchors and quotes, gaps, dropped statements, downloadable report |

**What the model-free path does.** It quotes: each claim is a verbatim sentence
from the case, chosen by word overlap with the question, then verified like any
other claim. It cannot synthesise or infer. Sentence choice weights the words that
ask something above the words that name the subject, and with a named subject it
only quotes paragraphs that name that subject (lines are the paragraph unit; a
text without line breaks gets no such protection).

**Evaluation (`backend/eval/run_eval.py`, seeded synthetic corpus, 30 documents).**
The documents are templated, so these numbers are friendlier than real text would
give. They are a regression baseline for the plumbing, not an estimate of
real-world quality.

| Measure | Result |
|---|---|
| Rules entity extraction F1 (IBAN, e-mail, organisation) | 1.00 each. **Person names are not extracted at all** by the rules backend |
| Question "which IBAN receives payments for X" | correct IBAN quoted 30/30; another company's IBAN quoted 0/30 |
| Question "who is the director of X" | correct name quoted 30/30; another company's quoted 0/30 |
| Injected bad claims rejected (fabricated identifier, invented quote, unknown evidence id) | 20/20 |
| Latency per question, plan + approve + run | median ≈ 75 ms, p95 ≈ 90 ms (development machine, no model) |

The first run of this evaluation exposed two real defects in the extractive
composer, both fixed and covered by a regression test: it ranked the sentence that
merely repeated a company name above the sentence that answered, and it quoted a
different company's payment line because the two documents shared generic words
("Trading LLC"). The same run also exposed a bug in the evaluation harness itself
(it looked documents up by organisation name, which also appears in neighbouring
documents), which had made extraction recall look like 0.70.

**Not measured, and needed before this milestone can be called done:** the
citation pass rate of a real model's claims; extraction F1 on real text and for
person names; retrieval recall on a corpus that needs paraphrase matching; the
project's own 50–200 item set built from real-looking material; footprint and
latency on the reference box.

## M5 — Exhibits and teams

- Exhibit export: artifact bundle, hash manifest, capture log, tool and model
  versions, operator statement.
- WORM storage integration (S3 Object Lock in **compliance** mode).
- Multi-user: OIDC, roles, assignments, comments, per-case audit review.
- Container plugin runtime for untrusted and dependency-heavy plugins.
- Plugin signing and a registry.
- Monitoring, saved searches with diff views, watchlist cross-referencing.

**Exit test:** acceptance scenario 5 — case to exhibit — reviewed by someone
who did not run the investigation.

### M5 status (2026-11-07): most of it is built and tested; storage, a registry and the human review are not

| Item | State | Where / how it was checked |
|---|---|---|
| Exhibit export | Built. Bundle with SHA256SUMS, manifest, capture log, tool versions, per-case audit slice, operator statement, optional Ed25519 signature, and a stdlib-only `verify.py` that recomputes every hash and every audit entry. A missing or deleted artifact fails closed. The platform never asserts admissibility. | `exhibit.py`, `exhibit_verify.py`, `signing.py`, `tests/test_exhibit.py` (14) |
| Multi-user | Built. OIDC bearer validation (RS256/ES256 only; HS and `none` refused) and, in the browser, sign-in as a public client (authorization code + PKCE S256, `ASTRAL_OIDC_CLIENT_ID`; the API serves only the provider's endpoints at `GET /auth/config` and never sees a code or verifier; the token lives in sessionStorage, links to files are fetched with it), new principals hold no roles, workspace-wide vs case-level grants, assignments, comments, audit review, per-request actor. | `oidc.py`, `auth.py`, `policy.py` (`TeamPolicy`), `frontend/src/auth.ts`, `Gate.tsx`, `tests/test_team.py` (34) |
| Container plugin runtime | Built: `--network none` (all outside access goes through host capabilities), read-only root, all Linux capabilities dropped, no-new-privileges, non-root user, noexec tmpfs, memory/CPU/pid limits, and a watchdog that bounds plugin silence. Needs Docker and the runtime image; those tests skip without them. | `plugins/runtime.py`, `tests/test_container.py`, `tests/test_runtime_limits.py` |
| Plugin signing | Built: a trust store of key fingerprints; a signed package is re-checked at every invoke and quarantined if it stops verifying; an invalid signature is always refused. | `plugins/trust.py`, `tests/test_plugin_trust.py` (11) |
| Monitoring | Built: saved searches with diff views (new / no longer reported / not asked / recovered) and watch terms. A source that could not be asked is "not asked", never "gone"; the baseline is the last run where that source answered. | `monitoring.py`, `tests/test_monitoring.py` |

**Not built or not verified, stated plainly.**

- **WORM storage (S3 Object Lock, compliance mode): not built.** Nothing here claims immutability beyond the append-only database triggers and the hashes an exhibit carries.
- **A plugin registry or marketplace: not built.** Signing and trust exist; distribution does not.
- **The exit test is not met.** It needs a reviewer who did not run the investigation. `verify.py` is the mechanism for that review; no such review has happened.
- **OIDC was run against one real identity provider, Keycloak 26.8.2, and a local fake.** In the in-app browser, against Keycloak with a public client and PKCE S256: sign-in through its own login page, the browser's POST to its token endpoint (CORS accepted with the client's web origin set), an RS256 ID token validated by the API, a principal created from its claims with no roles, an admin named by `sub` receiving the owner grant and creating a case, single sign-on on a second sign-in, and sign-out. Earlier, a fake provider that enforces the exact redirect URI, single-use codes and the nonce covered a forged callback (refused, code stripped from the address bar, no token stored). The Keycloak run found two faults, both fixed: once a token expired the UI silently did nothing instead of returning to the sign-in prompt (and a failed file fetch in the evidence list could be reported as a hash mismatch), and `ASTRAL_TEAM_ADMINS` takes the token's `sub`, not an e-mail address, which nothing had documented (now [14](14-configuration.md)). Not tested: any other provider (Entra, Okta, Auth0, Google), opaque access tokens, token refresh (a session ends when the token expires; the UI returns to the sign-in prompt, one click with a live provider session), and provider-side logout (sign-out only clears the local token). The expiry fix has no automated test: the frontend has no test runner, and it was checked by hand against a 70-second token.
- **The container runtime was tested for the flags it sets and for a runaway plugin, not against a hostile one.** Treat it as hardening, not as a proven sandbox.
- A defect found while finishing M6 and fixed: an enabled connector missing a required setting (SEC EDGAR needs a contact) raised out of an entire investigation or monitor run. It is now reported as that source being unknown, with the reason.

## M6 — Scale and breadth

- Dedicated search and vector services; optional graph projection.
- Remaining connectors ([07 §8](07-data-sources.md) build order).
- Crypto: Esplora, mempool.space, Etherscan, Blockchair, GraphSense.
- Geospatial: Copernicus, FIRMS, Overpass/Overture, adsb.lol, AISStream,
  Mapillary/KartaView.
- Site packs as data; community pack repository.
- MCP server exposing cases to external clients under the same policy layer.
- Lens sharing between installs.

### M6 status (2026-11-07): four items built to a limited depth; the rest not started

| Item | State | Where / how it was checked |
|---|---|---|
| MCP server | Built, **read-only**, stdio JSON-RPC 2.0, under the same policy layer. Results are marked as untrusted content, carry a budget, and the audit records sizes only. | `mcp.py`, `tests/test_mcp.py` (16). Tested by speaking the protocol in-process; **not tried with a real MCP client**, and there is no HTTP transport. |
| Lens sharing | Built: a lens is a parameterised, bounded pipeline stored as data; export is hashed and optionally Ed25519-signed; import verifies against the trust store; running a lens only **proposes** a plan that must be approved. | `lens.py`, migration `0008`, `tests/test_lens.py`. **Only retrieval stages exist** (`search_text`, `entity_facts`); face, reverse-image and geolocation stages, and the five acceptance scenarios as shipped lenses, are not built. No UI. A lens built from a case plan has its parameters checked for leaks, but a person must still read it before sharing. |
| Site packs | Interpreter built. Three outcomes only (present / absent / unknown); a pack with no absent rule can never say absent; 403, 429 and 5xx are always unknown; a control probe withholds an absence if the pack no longer recognises a known account; a pack must pass its own fixtures to load. Third-party packs go in a separate licensed directory and the licence is reported per run. | `plugins/connectors/site-pack/`, `tests/test_site_pack.py`. **Two original packs (github.com, gitlab.com) ship with `verified_live: null`: they have never been run against the real sites.** JSON, not YAML, and extraction is by regex, not CSS. No WhatsMyName data is included (CC BY-SA 4.0). |
| Crypto: Esplora | Connector built for the Esplora API (wallet plus per-transaction flows). Counterparties are recorded as values, never linked wallets; amounts stay in satoshis; truncated history is reported as unknown. | `plugins/connectors/esplora/`, `tests/test_esplora.py`. Written from Esplora's API.md, which gives field names but no examples and no rate limit. **Fixture-verified only.** Address checksums are not verified. |

**Not built at that date** (later work is in the M7 block below): dedicated search and vector services and the optional graph projection (one Postgres remains the design until a measured need appears); mempool.space, Etherscan, Blockchair and GraphSense connectors; every geospatial source; the community pack repository.

## M7 — Workbench, distribution, storage, services and breadth

Added on the owner's request to close the items M5/M6 listed as unbuilt and to reach feature parity with
Maltego's analyst workflow. Nothing here changes an invariant: the assertion log remains the system of
record, and everything in the workbench is either a view over it or analyst workspace data.

### M7 status (2026-11-07)

| Item | State | Where / how it was checked |
|---|---|---|
| **Workbench (Maltego-style graph work)** | Built. Entity transforms are ordinary plugin invocations offered per entity (`footprint: target_contact` still needs in-the-moment approval); the graph view, neighbourhood expansion, path finding, common neighbours, link analysis and communities (deterministic CNM greedy modularity); annotations, bookmarks and saved views (workspace data, never evidence); CSV/table import (stored as evidence, grades default to F/6); export to GraphML, JSON and node/edge CSV; an HTML report and a PDF report (`/cases/{id}/report.pdf`, reportlab, optional extra `pdf`). Above 1,500 nodes betweenness and closeness are **estimated from a deterministic sample** (20 to 300 source nodes, fewer as the graph grows) and the result says so in an `approximate` field; below that they are exact. | `workbench.py`, `analysis.py`, migration `0009`, `tests/test_workbench.py` (19). Link analysis is descriptive: it reports recorded links and never infers one. The PDF test extracts the text layer with pypdf; the layout was not inspected by eye. |
| **Machines** | Built. A machine is data (`astral.machine/v1`): a bounded sequence of transforms. It must be proposed, then approved by a person (target-contact is decided at approval; a definition cannot approve itself), then run with call and entity caps. Schedules are passive-only. Machines share as signed packages (`astral.machine-package/v1`). | `machines.py`, `tests/test_workbench.py`. The Machines panel has a form editor (steps, caps, resulting `astral.machine/v1` shown) and a schedule list with Enable/Disable. Saving a machine and the PDF endpoint were exercised in a browser; entity-selection flows in the graph view were not clicked through. |
| **Crypto connectors** | Etherscan (v2 API), GraphSense and Blockchair built; Esplora gained pagination. Blockchair was written defensively from a 2021 copy of its documentation, because the live documentation answered 403 to this environment and no bot wall was circumvented; the keyless mode and the limit codes (402, 429, 430, 434 to 437, 503, all reported as unknown) are unconfirmed against the current service. mempool.space is served by the Esplora connector (same API). | `plugins/connectors/etherscan`, `graphsense`, `blockchair`, `tests/test_chain_connectors.py` (8). **Fixture-verified only**; never run against the live services or with real keys. |
| **Geospatial connectors** | Overpass (OSM), adsb.lol, NASA FIRMS, Copernicus CDSE catalogue search and Mapillary, Digitraffic AIS (Finnish waters, plain HTTP, gzip) and AISStream built; a Map tab plots entities that carry coordinates. AISStream uses a new bounded `ws.collect` host capability (ws/wss only, egress policy applies, refused in proxy mode, 30 s / 5,000 messages / 8 MB caps, the subscription frame that carries the API key is never stored, messages are kept as one NDJSON artifact). The egress fetcher now decodes `Content-Encoding: gzip` within the byte cap. Overture Maps (places, buildings, addresses) is built on a new bounded `parquet.query` host capability (below). KartaView is built from client code but **does not work against the public service**: on 2026-11-08 its 1.0 endpoint answered 404 and its 2.0 host's TLS certificate did not match the host name (verification is never switched off), so it reports unknown; only `base_url` needs changing if an endpoint is repaired. Mapillary thumbnails are never fetched. | `plugins/connectors/*`, `views.build_geo`, `tests/test_geo_connectors.py` (24, AISStream against a local WebSocket server). Live checks are in "Live checks" below; AISStream needs a key we do not have and was not run. |
| **Plugin registry and community repository** | Format, tooling, verification and install built (see [12](12-community-repository.md)). **No public repository exists**; this project does not run one. | `hub.py`, `astral repo build/verify/search`, `tests/test_hub.py` (12) |
| **Write-once storage** | Built against the S3 Object Lock API (boto3, COMPLIANCE or GOVERNANCE): checksum on put, read-back verification, restore after a digest check, exhibit preservation. Erasure of a case that has write-once copies is refused (409) until the operator acknowledges that those copies cannot be deleted; they are listed with their retention dates. | `worm.py`, migration `0010`, `tests/test_worm.py` (12). **Run against a real SeaweedFS server** (the owner chose it; MinIO's images could not be pulled): deleting a locked version was refused (AccessDenied), shortening COMPLIANCE retention was refused, and our code refuses a bucket without Object Lock. The server ran **without authentication**, so credential handling and the real-cluster behaviours (replication, legal hold, governance bypass) were not tested. Two real-server tests run when `ASTRAL_TEST_S3`, `_KEY` and `_SECRET` are set; the rest use an in-process fake. |
| **Dedicated search and vector services** | Built as rebuildable projections (docs/02 cluster tier). OpenSearch adds a BM25 ranker fused by RRF; Qdrant answers face search with `exact: true`, so it ranks like the Postgres path and every returned row is re-read from Postgres. If a service is down the answer comes from Postgres and says so. Face vectors leave Qdrant on expiry, purge, watchlist removal and case erasure; a deletion that fails is recorded (`service_pending_delete`), reported to the operator, and retryable. | `services.py`, migration `0011`, `tests/test_services.py` (12): **12 pass against OpenSearch 2.17.1 and Qdrant 1.20** in single-node, security-disabled test containers; 5 pass and 7 skip without servers. Not tested: clusters, TLS, authentication, corpora large enough for approximate indexes to matter. Dense text vectors can be served from Qdrant too (exact search, collection `chunks_<model>_<dim>`, point id = chunk id, case id only in the payload; ids are re-checked against Postgres, a vector for a deleted chunk is dropped and removed by the next sync; erased with the case); if Qdrant is down the same vectors are ranked from Postgres and the answer says so. `tests/test_dense.py` (3 tests: one always runs, two need a server; all pass against Qdrant 1.20 in a single-node container). Qdrant was also run with TLS (a private CA named by `ASTRAL_SERVICES_CA_FILE`; verification is never switched off) and an API key: a server whose CA is unnamed is refused, a missing or wrong key is refused, and vectors round-trip with both (`tests/test_services_tls.py`, 6, 3 of which need that server). That run found one real gap, now fixed: Qdrant serves `/` without a key, so `ping()` reported a server healthy even with a wrong key; it now makes an authenticated read too. Not tested: clusters, OpenSearch with its security plugin on (its Basic auth and TLS go through the same code, but the image is 2.3 GB and heavy for this machine's shared Docker), mutual TLS, certificate rotation, approximate indexes. |
| **Dense retrieval and reranker** | Built, optional (`pip install .[dense]`, `ASTRAL_EMBED_MODEL`, `ASTRAL_RERANK_MODEL`, cache in `ASTRAL_EMBED_CACHE`). `intfloat/multilingual-e5-large` (MIT, 1,024 dimensions, `query:`/`passage:` prefixes added by `embed.py`) embeds chunks in-process through ONNX on CPU; vectors live in Postgres (`text_embedding`, migration `0012`) and are ranked by exact cosine, or by Qdrant when configured (migration `0013`); the result joins the lexical rankers in RRF and the top candidates go through the cross-encoder `BAAI/bge-reranker-v2-m3-int8` (Apache-2.0). BGE-M3 itself is not supported by fastembed and was not used. If either model cannot load the answer comes from the lexical rankers and says so. Vectors are erased with the case. | `embed.py`, `retrieval.py`, `tests/test_dense.py`: 6 pass against a stand-in embedder; the real-model test passed with both models downloaded (a German and a French question each retrieved the matching English passage that shares no words with it). The two-query real-model test was later joined by a measured run on a real corpus (below). |
| **Bounded Parquet reads (`parquet.query`)** | New host capability: a plugin names a remote Parquet file, columns, a bbox and a limit; the host reads the footer, skips every row group whose bbox statistics rule it out, reads only the requested columns of the rest through HTTP range requests (same egress policy and throttle as `http.fetch`, never a download of the whole file), and stores the rows returned as one NDJSON artifact whose acquisition context records the bytes, requests and row groups read. Hard budgets the plugin cannot raise: 48 MiB, 120 requests, 40 row groups, 500 rows; hitting one stops the read and the answer says so (`stopped_by`, `truncated`), and the connector reports the area as not fully read, never as empty. A server that ignores `Range` is refused. Needs pyarrow (extra `parquet`). | `parquet_scan.py`, `capabilities._parquet_query`, `tests/test_parquet.py` (9). **Run live against Overture's public files** (below). |
| **Overture Maps connector** | `connector.overture`: reads the catalogue's latest release, finds the files that intersect the box in that release's `collections.parquet` index (the method the `overturemaps` package uses), and asks `parquet.query` for places, buildings or addresses in the box. | `plugins/connectors/overture`, 3 tests in `tests/test_geo_connectors.py`; **live run succeeded** (below). Licences differ by theme and source (places: CDLA-Permissive-2.0, Apache-2.0, CC0; buildings: ODbL); the manifest carries them. |
| **Graph projection (Apache AGE)** | Built as an optional, rebuildable Cypher view of a case's graph (docs/03 §8: AGE is the one licence-clean option; Kuzu is archived, Memgraph, Neo4j Community and ArangoDB have licence traps). `ASTRAL_AGE_DSN` points at a separate database with the `age` extension. The projection is dropped and rebuilt whenever the case graph's fingerprint changes, so a query never sees an older graph; it holds ids only (vertex label = schema, edge label = predicate, plus grades and independent-source count) and every name shown comes from Postgres, with a vertex Postgres no longer holds marked stale. Analyst queries (`POST /cases/{id}/graph/cypher`) are read-only twice over: a keyword check (no `$`, `;`, CREATE/MERGE/SET/DELETE/CALL/UNION, and so on) and a `READ ONLY` transaction the database enforces; a 15 s timeout and a 500-row cap bound the cost. The graph is erased with the case. Cypher `shortestPath` does not exist in AGE; the built-in path, neighbourhood and analysis endpoints stay relational because that was measured faster. | `graphstore.py`, migration `0013`, `tests/test_graphstore.py`: 24 pass (18 always run; 6 against Apache AGE 1.8.0 on PostgreSQL 18.6, image `apache/age`, single node, no TLS or authentication tuning). Measured on that container, synthetic hub-skewed graphs: 2,000 nodes / 8,000 edges rebuild 2.1 s; 20,000 / 80,000 rebuild 24.5 s. Queries after that took 0.02 to 0.7 s (a 3-hop `DISTINCT` with `LIMIT 500` 83 ms; a grouped hub count 0.7 s). A rebuild is a full copy and happens at the next query after any change, so a busy case pays it often. |
| **Language model** | Run, measured on a tiny set, and **below the standard the extractive path sets**. Phi-3.5-mini-instruct (Q4_K_M GGUF, MIT) served by llama.cpp b12196 on CPU, on loopback only. Planner, composer and both entailment verifiers ran through the existing `llm.py` client. | `backend/eval/run_eval.py` with `ASTRAL_EVAL_LLM`; numbers below. The weights and server are in the git-ignored `models/`. |

**Run with a real model (2026-11-07; 8 synthetic documents, 16 questions, seed 7; one run).**

| Measure | Result |
|---|---|
| Questions whose answer the model composed | 15/16 (1 fell back to extractive quoting, visibly) |
| Questions planned by the model | 15/16 (the other used the rules planner) |
| Claims the model wrote that passed citation checking | 15/18 = **83 %** (IBAN question 6/8, director question 9/10) |
| Correct answer present in the accepted claims | 12/16 = **75 %**, against 100 % for the extractive path on the same questions |
| A different company's answer offered | **1/16** (director question). It passed every check: the quote was real, its identifiers were in it, and both model entailment prompts accepted it. Checking that a quote supports a claim does not check that the claim answers about the right subject |
| Latency per question (plan + approve + run) | median 43.6 s, p95 56.2 s on an 8-core / 16-thread desktop CPU, CPU only, while the full test suite and a browser were running |

What this does and does not show. It closes the gap that a model had never been run at all, and
confirms that the egress, plan-approval and rejection plumbing behaves with a real model: unverifiable
quotes were dropped, not shown. It does **not** support a quality claim: the sample is 16 questions on
templated text, one run, one 3.8 B-parameter model at 4-bit that also served as its own entailment verifier, and no GPU. The
extractive composer is currently more accurate on this corpus. A model composer should be enabled for
paraphrase and synthesis, not because it is better at quotation.

llama.cpp's grammar-constrained JSON mode failed with this model's default Jinja chat template
(`Unexpected empty grammar stack`), for every schema, and worked with `--no-jinja`. Anyone serving this
model needs that flag. Another model may behave differently.

**Graph projection: measured, then built as an optional Cypher view.** The relational path was weighed first on synthetic graphs (hub-skewed, 4 edges per node), Postgres in a container, 8-core / 16-thread desktop CPU. At 20,000 nodes and 80,000 edges: building the graph from assertions 0.79 s, adjacency 0.07 s, shortest path 2 ms, a 3-hop recursive query 55 ms. At 100,000 nodes and 400,000 edges: 5.25 s, 0.53 s, 62 ms and 263 ms. Those are adequate for interactive use, so the built-in path, neighbourhood and analysis endpoints stay relational and the design stays on one Postgres; the AGE projection is for analysts who want Cypher, not a speed-up. What was slow was whole-graph analysis: exact betweenness at 20,000 nodes did not finish in 33 minutes. It is now sampled above 1,500 nodes, tied to edge count (2.4 s at 20,000 nodes and 20.8 s at 100,000, from 35 s and 290 s with a fixed 300-source sample), at the price of a rough estimate from about 20 source nodes that the result labels. Synthetic graphs are not real investigations; revisit with a real one.

**Live checks (2026-11-08, with the owner's approval; one or two tiny requests each, default hosts, the connectors' own rate limits).** Worked: Digitraffic AIS (5 vessels near Helsinki, 63 assertions), adsb.lol (70 aircraft near Heathrow), Wayback CDX (50 snapshots), Esplora (25 of 67,465 transactions, correctly reported as a partial history), Copernicus CDSE (20 Sentinel-2 scenes, limit reported), GLEIF (8 records, after the fix below), Overture (STAC catalogue, a 149 KB index, then one places file of 756 MB with 512 row groups and 5.45 M rows: the footer cost 3.4 MB, statistics left 3 of 512 row groups for a 300 m box in Manhattan, and 10 real places came back after 4.1 MB in total). Did not work: KartaView (404 on 1.0; TLS name mismatch on 2.0; see above) and Overpass (two attempts: a 30 s read timeout, and a TLS handshake failure from this machine's curl; reported as unknown, not confirmed either way). **The live run found a real bug the fixtures could not:** GLEIF answers `application/vnd.api+json`, which the host did not treat as text, so a valid answer was reported as "not JSON"; `+json` and `+xml` types are now text and a regression test covers them. Not run live: AISStream, Blockchair, Etherscan, GraphSense, FIRMS, Mapillary and every other keyed service; the two bundled site packs; SearXNG; OFAC; ICIJ. Overture's row-group ordering was measured on one file near one box only.

**Query builder (built, run against Apache AGE 1.8.0).** The Cypher panel has a "Build a query" section: pick the vertex types, the relationship, a worst source grade, a minimum number of independent sources and a limit, and it writes the Cypher. Its choices come from `GET /cases/{id}/graph/schema`, which reads what the case holds from Postgres (no graph server needed), so nothing can be chosen that does not exist; the server still refuses anything that writes. Run in the browser on a three-person case: the generated query returned the right three rows, and the grade and source-count conditions are valid Cypher in AGE (an A/B filter correctly returned nothing for edges graded F). `tests/test_graphstore.py` covers the schema endpoint; the TypeScript has no unit tests.

**Continuous integration (GitHub Actions; run on GitHub, passing on the public branch).** The first run failed on one test that had only ever passed on Windows (it set a blob's mode to write-only, which on Linux also removed read permission); found by reproducing the run in Linux containers, fixed, and green on Python 3.11 and 3.12. Dependabot then opened nine pull requests, whose runs all failed on that same test; they were handled by hand rather than merged on GitHub, because the public branch is generated from a private one and a merge there would split the two. Applied: the three GitHub Actions bumps, cytoscape 3.35, TypeScript 7 (with a `vite-env.d.ts` for the stylesheet import it now checks). The bundler and React packages (vite 8 with its React plugin, React 19 with its renderer and typings) cannot resolve their peer dependencies one at a time, so they were tried together: the build and type-check pass, and the demo case loaded in the browser with its 18-node graph drawn; they were then applied, and Dependabot is set to group them from now on. The frontend has no automated UI tests, so that check was by eye on one case. Declined: the OpenCV 5 range (the code and its tests ran under 5.1, but the face models were not re-checked on real faces under it). The original paragraph follows. **Continuous integration (as first set up).** `.github/workflows/ci.yml` runs the backend tests on Python 3.11 and 3.12 against a Postgres service and builds the frontend. The same install (`pip install -e ".[dev,parquet,streams]"`) was run locally in a clean virtual environment on Python 3.13: the full suite passed with 28 tests skipped for lack of optional services. Doing that found two real problems, now fixed: tests that needed numpy and reportlab, which only optional extras installed.

**Retrieval quality on a real corpus (SciFact, run once).** `bench/retrieval/scifact_eval.py` puts BEIR's SciFact abstracts (one chunk each) into a throwaway case and scores the 300 test claims through the project's own retrieval code, against the published relevance judgements. **The corpus was cut to 1,000 documents** (all 339 judged-relevant ones, about 300 distinct, plus 700 others), because embedding on this CPU took 24 minutes for 1,000 and the full 5,183 would have taken over two hours; a smaller corpus is easier, so these numbers are higher than a full-corpus run would give and are not comparable with a leaderboard.

| Ranker | nDCG@10 | Recall@10 | MRR@10 | Per query |
|---|---|---|---|---|
| Full-text only (Postgres, no stemming) | 0.689 | 0.772 | 0.672 | 0.01 s |
| Full-text + fuzzy (the no-model default) | 0.689 | 0.772 | 0.671 | 0.08 s |
| Dense only (multilingual-e5-large) | 0.823 | 0.913 | 0.799 | 1.3 s |
| Hybrid, rank fusion of the three | 0.797 | 0.899 | 0.775 | 0.5 s |
| Hybrid + bge-reranker-v2-m3 (int8) on the top 30 | **0.860** | **0.942** | **0.835** | 16.7 s |

What it shows: the embedder lifts retrieval clearly over keywords on this kind of text (about 13 points of nDCG), and the reranker adds about 4 more; the fuzzy ranker adds nothing here; and **the fusion scored a little below dense alone**, because the equal-weight rank fusion lets the weaker keyword rankers pull good dense hits down. That is a finding, not a fix: the fusion was not re-tuned, and the reranker hides the gap in the full pipeline. The reranker costs 16.7 s a query on this CPU, which is too slow for interactive use without a GPU. What it does not show: scanned, noisy, multilingual or investigative text (SciFact is clean English scientific abstracts), the contextual-header ranker (single-chunk documents give it nothing to do), or how any of this scales. The corpus is CC BY-NC 4.0 and was used for the check only, not shipped.

**Still not built.** A retrieval evaluation on investigative-style text, a re-tuned fusion; a larger or second run of the language-model evaluation; live runs of the keyed and unchecked connectors listed above; a working KartaView endpoint (the connector is ready, the service was not reachable); Qdrant or AGE at cluster scale, OpenSearch with security on, AGE over TLS; a hosted-model egress mode with redaction (refused as not built, deliberately); VLM captions for video (needs a model that has not been downloaded); reverse-image connectors (paid keys, request shapes unverified); a real sign-off review of an exhibit by a second person (the exit test).

## Deliberately deferred, with reasons

| Deferred | Why |
|---|---|
| **Deepfake detection** | Measured AUROC of 44–70% on current generators, with 43–59% false positives on real footage ([06 §0](06-multimodal-pipelines.md)). If it ships at all it ships as a triage flag with its numbers attached, never as a verdict |
| **Street-level geolocation** | No model supports the precision; region-level is the honest ceiling |
| **Autonomous research agents** | [08 §3](08-ai-layer.md). Typed approved plans instead |
| **GraphRAG community summaries** | Redundant when a real entity graph already exists, and expensive |
| **Mass-scraped face index** | Prohibited by EU AI Act Art. 5(1)(e) and a stated non-goal |
| **Graph database as system of record** | Licence landscape is hostile to shipped software ([03 §8](03-data-model.md)) |
| **Audio splice detection** | No maintained open-source tool found. A real gap, not a decision |

## Research debt status

Carried forward from the research passes, because each could change a decision.
Status as of 2026-11-05. "Closed" means the question was answered from primary
sources or by measurement and the docs were updated; it does not mean no
caveat remains, and each caveat is stated.

| # | Item | Status | Outcome |
|---|---|---|---|
| 1 | Measure the five items in [06 §9](06-multimodal-pipelines.md) | **Partly closed** | Measured: face CPU throughput (133 images/s on 16 threads), YuNet+SFace on LFW (98.03% ± 0.70%), pgvector sizing (≈ 1.35 GB per million 128-d vectors, **confirmed by an actual 1M-row run**: 209 s HNSW build, but synthetic recall@10 fell to 0.64/0.82 at `ef_search` 40/100). **Still open:** real VRAM per model, ASR RTF, OCR pages/s, two-hour-video wall time, 512-d 1M-row run, `ef_search` tuning on real embeddings. Whether the default is *good enough on hard images* is unanswered |
| 2 | Verify OpenCV Zoo licences | **Closed with a caveat** | YuNet is **MIT** (docs said Apache-2.0), SFace Apache-2.0, repo Apache-2.0 — read from the LICENSE files. Upstream training-data terms (WIDER FACE, MS-Celeb-1M, VGGFace2, CASIA-WebFace) are **unresolved** |
| 3 | Check ICD 203 bands against the primary text | **Closed** | Bands match; the term pairings and a missing alias were corrected. Confidence levels have no numeric definition in the directive ([08 §6](08-ai-layer.md)) |
| 4 | Close the [07 §9](07-data-sources.md) source gaps | **Mostly closed** | Researched and recorded, including that several free sources are non-commercial. Not researched: land registries, VK/4chan, podcast indexes, satellite providers, rail. Several pages were gated or unreachable |
| 5 | EU and UK scraping law | **Closed as research, not as legal advice** | [07 §5.1](07-data-sources.md); contested points and an unverified list are stated |
| 6 | Hatchet versus procrastinate | **Closed** | [ADR-0003](adr/0003-orchestration.md) — Accepted; Hatchet fails the no-container invariant as documented; an in-repo queue won on measurement. Veto-able |
| 7 | Qwen2.5-VL-3B licence | **Closed** | Qwen Research License, non-commercial. Default VLM is now Qwen3-VL-2B (Apache-2.0) |
| 8 | InsightFace commercial position | **Closed** | A custom "Model Licensing" tier with no published price; contact recognition-oss-pack@insightface.ai. Official pages name `buffalo_l` and `antelopev2` explicitly |
| 9 | Project licence ([ADR-0002](adr/0002-licensing.md)) | **Closed** | AGPL-3.0-or-later, owner's choice. The canonical `LICENSE` text was added on 2026-11-06 |

**New debt created by this pass**

- ~~DBOS outbound connections~~ — **source-audited 2026-11-06**: none by default
  in the open-source code ([ADR-0003](adr/0003-orchestration.md)). Still owed
  before adoption: a deny-all-egress run, since the Conductor client is closed
  source.
- ~~CC BY-SA 4.0 vs AGPLv3~~ — **closed 2026-11-06**: not compatible; keep the
  data as unmodified separate files ([ADR-0002](adr/0002-licensing.md)).
- ~~Parakeet INT8 ONNX artefact~~ — **verified at file level 2026-11-06**
  (CC-BY-4.0, 652 MB encoder); published speed figures disagree ~10x, so ASR
  RTF is still to be measured ([06 §2](06-multimodal-pipelines.md)).
- Settle whether upstream training-data terms bind the YuNet/SFace weights.
- ~~Re-run the 1M-row vector benchmark~~ — **done for 128-d, 2026-11-06**
  ([06 §9.3](06-multimodal-pipelines.md)). Still owed: tune `ef_search` on real
  embeddings (synthetic recall fell sharply at 1M) and run 512-d if that
  embedding class is ever used.
