# 08 — AI Layer

The AI layer does three jobs: **plan** (turn a question into an inspectable
pipeline), **retrieve** (find relevant evidence across a heterogeneous corpus),
and **draft** (write findings that cite their sources). It is never the system
of record, and it is never permitted to introduce an uncited claim.

## 0. The constraint that decides the architecture

Hosted frontier models cannot be the default for person-targeted work — and
the binding reason is **provider usage policy, not OPSEC**.

- **Anthropic's usage policy** prohibits targeting or tracking a person without
  consent via physical location, biometrics, movements, "online activities,
  communications, or associations", and prohibits compiling dossiers or
  aggregating personal information to "harass, intimidate, surveil, or cause
  harm." Named carve-outs include journalism and "legal research and analysis
  by law enforcement agencies or courts." General OSINT due diligence is not
  named.
- **OpenAI's usage policy** prohibits aggregating or profiling private
  information and building facial-recognition databases; national-security and
  intelligence use requires review.

A platform whose central use case is "find everything about this person" cannot
assume hosted routing is available for that task. The OPSEC objection — that an
investigator may not want targets disclosed to a third-party log — points the
same way and is secondary.

**Therefore: person-targeted extraction and synthesis runs locally by default,
and the small tier must be genuinely useful without any hosted model.** That is
a hard requirement on everything below, not an aspiration.

### Egress modes, chosen per task and shown in the UI

| Mode | Use | Default for |
|---|---|---|
| `local-only` | Nothing leaves the box | **Any task touching a named private individual** |
| `hosted-redacted` | Entities replaced by opaque tokens before the call, restored locally after | Query planning, schema work, summarization of structure |
| `hosted-direct` | Full content to a hosted API | Explicit per-task consent; public-source and methodology work; logged |

The mode is a visible badge on every AI-produced result and is recorded in the
assertion's `derived_by`.

## 1. Local model selection

| Role | Choice | Licence | Footprint |
|---|---|---|---|
| Reasoning / extraction | **Qwen3.5-4B** at Q4, fully GPU-resident | Apache-2.0 | ~2.4 GB weights, 262k native context |
| Reasoning (non-thinking) | Qwen3-4B-Instruct-2507 | Apache-2.0 | ~2.4 GB |
| Step-up (if it fits) | Qwen3.5-9B Q4 at 8–16k context | Apache-2.0 | ~5–5.5 GB, leaves ~2–2.5 GB for KV — tight |
| Embeddings | **BGE-M3** | MIT | 1024-dim, 8192-token, dense + sparse + multi-vector from one model |
| Embeddings (light) | Qwen3-Embedding-0.6B | Apache-2.0 | ~610 MB Q8, Matryoshka 32–1024 dims |
| Embeddings (CPU) | multilingual-e5-small | MIT | ~118M params |
| Reranker | bge-reranker-v2-m3 | Apache-2.0 | 0.6B |

**Do not go below 4B.** Structured-output studies found Llama 3.2 3B parsed
only 47.8–56.5% of JSON successfully, and value accuracy trails JSON validity
by 15–30 points even in compliant models. Below 4B the extraction layer becomes
a liability.

**Multilingual retrieval numbers that justify BGE-M3:** MIRACL nDCG@10 across
18 languages — dense 67.8, sparse 53.9, multi-vector 69.0, **all modes 70.0**.
Getting all three scoring modes from one loaded model is decisive at 8 GB.
Compare multilingual-e5: small 60.8, base 62.3, large 66.5.

### Honest expectations at 4B

- **Schema-bound extraction** from Latin-script text with constrained decoding:
  dependable.
- **Cross-script transliteration and same-name disambiguation:** weak. Handled
  by rules plus BGE-M3, not by the LLM.
- **Query planning** over a typed connector registry (3–10 steps): adequate —
  this is tool selection, not reasoning.
- **Open-ended investigative reasoning and identity judgments:** poor. These go
  to a human, or to a hosted model where policy permits.

No published benchmark measures these models on investigative corpora. The
project must build a 50–200 item evaluation set from real cases and measure
extraction F1, citation pass rate and latency on the reference box before
committing. That eval is a deliverable, not an optional extra.

## 2. Serving

**llama.cpp server** is the small-tier choice, and its feature set maps onto
our requirements almost exactly:

- `--fit on` (default) with a 1024 MiB per-device margin, and `-ngl auto` — the
  automatic layer split the residency manager needs.
- `-ctk` / `-ctv` to quantize the KV cache (q8_0, q4_0) — the lever that makes
  a 9B model plausible at 8 GB.
- `-ncmoe` to keep MoE expert weights in CPU RAM, which is how a 30B-A3B MoE
  becomes conceivable on this hardware (~18 GB of experts in the 32 GB of
  system RAM, shared layers on the GPU). Speed unmeasured.
- `--json-schema` / per-request `json_schema` for **constrained decoding** —
  the mechanism that makes claim-level citation enforceable (§4).
- OpenAI-compatible `/v1/chat/completions`, `/v1/embeddings` **and
  `/v1/rerank`** — one server for all three roles, which matters when VRAM is
  the scarce resource.

**Avoid spilling.** One measurement found spilling just four layers on an 8 GB
card cut decode throughput ~70%, to 17.4 tok/s. Better to use a smaller model
fully resident than a larger one partly spilled; the residency manager should
enforce this rather than let the server quietly degrade.

Ollama is a convenience wrapper over llama.cpp and is supported for users who
prefer it. vLLM belongs on a 16 GB+ GPU with concurrent users, not on the small
tier (its CPU backend exists but is experimental, and it offers no layer-level
weight offload — only KV-cache sizing).

## 3. Planning: typed plans, not autonomous agents

**The planner compiles a question into a DAG of calls against the plugin
registry, shows it to the user, and executes only after approval.** It is not
an open-ended research loop.

```
question
  → parse to typed constraints (JSON-schema output: names, variants,
    locations, sectors, languages, time bounds)
  → IDENTITY GATE: if disambiguation confidence is low, ask the user.
    Never silently merge two people.
  → compile DAG over the registry (each node: plugin, args, footprint,
    estimated cost, estimated egress)
  → SHOW the plan with total cost, data egress and OPSEC footprint
  → execute with per-branch caps, checkpointing each node
  → single evaluator pass against a fixed rubric, hard iteration cap
```

The evidence for this shape, and against the alternative:

- Anthropic's orchestrator-worker research system outperformed a single agent
  by **90.2%** on their internal eval — but used **~15x the tokens** of chat,
  and their own guidance is that multi-agent is "not a good fit" for tasks
  needing shared context or heavy inter-agent dependencies. Investigation is
  exactly such a task: every finding changes what the next query should be.
- Their effort-scaling rule is worth encoding directly: simple fact-finding =
  1 agent, 3–10 tool calls; comparisons = 2–4 subagents, 10–15 calls each.
  Documented failure modes include spawning 50 subagents for a simple query and
  duplicating work from vague delegation.
- The **MAST** study (1,600+ annotated traces, 7 frameworks, κ=0.88) groups 14
  failure modes into system design, inter-agent misalignment and **task
  verification** — naming verification as the weakest link. We therefore spend
  disproportionate effort there (§4).
- **STORM**'s own README states its output "cannot produce publication-ready
  articles." Taking that at face value is the correct posture for any
  auto-generated intelligence product.

Guardrails, following OWASP LLM06 (Excessive Agency) and LLM10 (Unbounded
Consumption):

- A **deterministic policy layer** validates every proposed call against a tool
  allowlist, argument schemas and an egress domain allowlist **before**
  execution. The LLM proposes; the policy layer disposes.
- **Per-case budgets** with hard stops: calls per source, spend, wall time,
  new entities created.
- **Read is free; reach-out requires approval.** Any action that contacts a
  person, creates an account, or sends traffic to a `target_contact`-class
  source needs explicit human approval.
- Checkpoint every node so a long run resumes rather than restarts.

## 4. Retrieval

Hybrid, reranked, and measured:

```
BM25 (Postgres FTS)  ─┐
BGE-M3 dense         ─┼─ reciprocal rank fusion → top 100 → rerank → top 10
BGE-M3 sparse        ─┘
```

Anthropic's contextual-retrieval results give the expected shape of the gains:
contextual embeddings alone cut top-20 retrieval failures by **35%**; adding
contextual BM25 reached **49%**; adding reranking reached **67%** (their
pipeline took the top 150 and reranked to 20). Their embedder and reranker were
different from ours, so the magnitude will not transfer — but the ordering of
value will. Contextual chunk headers are generated by the local 4B model at
ingest, so the cost is ingest time rather than money.

Chunking by modality:

- **Transcripts:** by speaker turn, capped at ~60–120 seconds, one turn of
  overlap, **storing start and end timecodes as citation anchors**. No
  benchmark supports the specific window; it is a starting point to be tuned
  against the project's own eval set.
- **Documents:** structural chunks from Docling, with contextual headers.
- **Scanned/layout-heavy documents:** OCR-plus-text at the small tier. ColPali
  and ColQwen are strong on ViDoRe but storage-heavy — an opt-in on the GPU
  tier only.

**Skip LLM-built GraphRAG community summaries at the small tier.** Build the
entity and co-occurrence graph *deterministically* from NER output instead — we
already maintain a real entity graph (doc 03), so the expensive part of
GraphRAG is redundant here. One AWS benchmark put LightRAG hybrid at $42.29 per
1,000 queries against GraphRAG local at $5.23; neither is justified when the
graph is already a first-class object.

## 5. Citation enforcement

This is where the AI layer earns the right to exist, and the published failure
rates show why it needs teeth:

| Measurement | Result |
|---|---|
| DeepTRACE | Unsupported-statement rates **up to 97.5%** for one engine; citation accuracy 40–80% across systems |
| DeepResearch Bench (FACT) | Citation accuracy 78–94% |
| Verifier disagreement (2026) | Unsupported-claim rates measured between **3% and 18% for the same output** depending on verifier |
| Survey of benchmarks | Most do not even check whether cited URLs exist |

The enforcement stack:

1. **Reports are structured, not prose.** The generator emits a list of claims,
   each with `text` and `evidence_ids`, under a JSON schema enforced by
   constrained decoding. There is no free-text assertion channel. A claim
   without evidence ids is structurally impossible to express.
2. **Deterministic span check.** Every cited span must contain the quoted
   string after normalization. Cheap, and it catches fabricated quotes
   outright.
3. **Existence check.** Every `evidence_id` must resolve to a real assertion,
   and through it to a real artifact. This is the check most systems skip.
4. **Semantic entailment check** via NLI or an LLM judge — 100% of
   person-level claims, sampled elsewhere.
5. **Two verifiers plus a human sample**, given the 3–18% spread above. A
   single judge is not a control.
6. **Never generate identifiers.** Dates, numbers, registration numbers, names
   and addresses are *copied* from records, never produced by the model. A
   hallucinated company number is far more damaging than a vague sentence.

Evaluation uses RAGAS faithfulness (supported claims / total claims against
retrieved context) and a fixed rubric covering factual accuracy, citation
accuracy, completeness, source quality and tool efficiency — a single judge
returning 0.0–1.0 plus pass/fail proved most consistent in Anthropic's work.
Note FActScore's sobering baseline: ChatGPT scored ~58% atomic-fact precision
on biographies, which is precisely our use case.

## 6. Analytic language

Generated products follow ICD 203 conventions. Likelihood bands, **verified
against the primary directive text** (ICD 203 *Analytic Standards*, signed
2 January 2015, with technical amendments, one signed 21 January 2022; read
from the PDF served by archive.dni.gov, Standard (2)(a)). The directive
publishes two parallel term sets (set A, set B) over seven shared bands:

| Band | Set A | Set B |
|---|---|---|
| 01–05% | almost no chance | remote |
| 05–20% | very unlikely | highly improbable |
| 20–45% | unlikely | improbable (improbably) |
| 45–55% | roughly even chance | roughly even odds |
| 55–80% | likely | probable (probably) |
| 80–95% | very likely | highly probable |
| 95–99% | almost certain(ly) | nearly certain |

The numeric bands in our earlier draft were right. Its "remote / almost no
chance" pairing came from a secondary transcription and its table omitted
"roughly even odds"; both are corrected above.

Rules the generator enforces mechanically:

- **Likelihood and confidence never appear in the same sentence.** This is
  Standard (2)(b): a product that uses a "confidence level" must not combine
  it with a degree of likelihood in one sentence. Likelihood is about the event
  or development; confidence is about the basis for the judgment.
- Pick one term set and stay in it. The directive only *strongly encourages*
  not mixing sets; a product that does mix them **must** carry a disclaimer
  that the terms denote the same probability. The generator never mixes.
- Every judgment carries a **"what would change this"** indicators field — the
  single most useful discipline in the standard, and the one most often
  omitted.
- Confidence High/Moderate/Low is derived from independent-source count and
  primary-source status, and is **labelled a house convention**, because ICD
  203 does not define numeric confidence rules (verified: the directive gives
  "high confidence" only as an example of a confidence level and defines no
  scale).

Report structure: source assessment → key judgments → alternatives → indicators
→ gaps. The "gaps" section is mandatory and is populated automatically from
quarantined sources and unanswered plan nodes, so an incomplete collection is
visible rather than silently absent.

## 7. MCP

MCP is the **connector-to-agent** surface, not the plugin runtime. Astral Lens
exposes a read-mostly MCP server so external LLM clients can query a case under
the same policy layer and budgets as the internal planner. Plugins themselves
run over the contract in [04 — Plugin System](04-plugin-system.md).

Current spec revision is 2026-07-28; requests declare protocol version in
`_meta` and `server/discover` is optional, with older handshake-based versions
still supported. HTTP transports require OAuth 2.1 per the 2025-11-25
authorization spec. Governance now sits with the Agentic AI Foundation under
the Linux Foundation.

## 8. What the AI layer must never do

- Produce an assertion without `evidence_ids`.
- Merge two identities. It may *propose* a merge with evidence; a human or an
  explicit resolution rule decides.
- Invent an identifier, date or quantity.
- Escalate a confidence grade. It may lower one.
- Execute a `target_contact` plugin without approval.
- Silently substitute a different model or backend than the one recorded.
