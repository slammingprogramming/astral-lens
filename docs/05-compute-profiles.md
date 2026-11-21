# 05 — Compute Profiles

> **Requirement.** Astral Lens must be usable on a system with **8 GB VRAM and
> 32 GB RAM**, must scale down to CPU-only, and must scale up to multi-GPU and
> multi-node — all from one codebase, selected by configuration. GPU execution
> with CPU fallback is designed in, not bolted on.

This is the most architecturally invasive requirement in the project, so it is
specified before the pipelines that depend on it.

## 1. Why this cannot be an afterthought

The naive design loads every model it needs when it needs it. On a 24 GB card
that mostly works. On 8 GB it fails immediately and in the worst way: a face
embedder, a speech-recognition model, a vision-language model and a reasoning
LLM cannot be co-resident. The process either OOMs mid-job, or thrashes, or —
worst — silently falls back to CPU and takes forty hours to do an overnight job
while reporting no error.

Three mechanisms prevent this, and all three are core services rather than
plugin concerns:

1. A **capability manifest** on every model-backed task declaring what it needs.
2. A **model residency manager** that owns the accelerator and accounts for it.
3. A **backend selection** layer that picks an implementation per profile.

## 2. Capability declarations

Every plugin stage that touches a model declares its requirements statically:

```python
@stage(
    name="face.embed",
    requires=Resources(
        vram_mb=1200,                 # at the default backend
        ram_mb=2048,
        accelerator=Accel.PREFERRED,  # CUDA/ROCm/MPS if present, else CPU
        exclusive=False,              # may share the device
    ),
    backends=["insightface-onnx-gpu", "insightface-onnx-cpu"],
)
```

`accelerator` takes one of four values, and the distinction matters:

| Value | Meaning |
|---|---|
| `REQUIRED` | Refuse to run without an accelerator. The job is rejected with a clear message, never silently queued forever. |
| `PREFERRED` | Use the accelerator if it fits; fall back to CPU automatically and **annotate the result** with the backend actually used. |
| `CPU_ONLY` | Never occupies the device — metadata extraction, hashing, parsers, registry lookups. |
| `REMOTE` | Executes against a hosted API. Consumes no local device, but consumes egress and discloses the query. |

The annotation requirement is not cosmetic. A face match produced by a small
CPU model has materially different reliability from one produced by the large
GPU model, and the assertion's confidence and `derived_by` record must say
which one ran. An investigator must never have to guess which model produced a
lead.

## 3. The model residency manager

A single service owns each accelerator and brokers access to it.

**Responsibilities**

- **VRAM accounting.** Tracks declared versus actual allocation per loaded
  model. Actual is measured after first load and cached, because declared
  figures are always optimistic.
- **LRU eviction with pinning.** Models load lazily and unload under device
  pressure. Hot models — the face embedder during a corpus-wide face sweep —
  can be pinned for a job's duration to prevent load/unload thrashing.
- **Admission control.** A job that cannot fit even alone on the device is
  routed to its CPU backend or rejected. This is decided before work starts,
  not discovered during it.
- **Device serialization.** At the small tier the device is effectively a
  critical section for large models. The manager holds a semaphore whose
  permits derive from measured free VRAM, so two 5 GB models cannot both
  conclude they fit.
- **Spill policy.** Where a backend supports partial residency — layer offload,
  device maps — it is used in preference to an all-or-nothing placement.

**Scheduling consequence.** The job queue must be *resource-aware*. A generic
work queue that pulls the next task regardless of what it needs will deadlock
or thrash on a constrained box. Workers subscribe to queues partitioned by
resource class — `cpu`, `gpu-small`, `gpu-large`, `remote` — and the number of
concurrent large-model GPU workers at the small tier is one.

This is a hard constraint on the orchestration choice: whatever executor is
selected must support either worker pools with independent per-queue
concurrency limits, or an external semaphore a task can acquire. Most do. It
must be verified rather than assumed, and it is an explicit criterion in the
orchestration decision record.

## 4. Backend interfaces

Every model-dependent capability is an interface with several implementations.
The *capability* is named in a pipeline; the *backend* is chosen by the profile.

```python
class ASRBackend(Protocol):
    def transcribe(self, audio: Path, *, language: str | None) -> Transcript: ...

# Registered implementations, each carrying its own Resources manifest:
#   asr.whisper-cpp-cpu      quantized, CPU, viable overnight
#   asr.faster-whisper-cuda  int8/fp16 on GPU
#   asr.hosted               remote API
```

A pipeline author writes `asr.transcribe`. They never construct a model class
directly. This is the single discipline that makes tier portability possible,
and it must be enforced in review: **no plugin imports a model library
directly.** A plugin that does has hard-coded a tier.

Interfaces required at minimum: ASR, diarization, speaker embedding, face
detection, face embedding, person re-identification, image embedding, OCR,
object detection, vision-language captioning, text embedding, reranking,
translation, NER, and LLM completion.

## 5. The profiles

Profiles are shipped presets that set backends, concurrency, model sizes and
index types. They are a starting point: every value is individually
overridable. The requirement is that all reasonably possible builds can run
this, so a profile is a default, never a cage.

### `cpu-only`

No accelerator. Stages marked `REQUIRED` are rejected with an explanation;
everything else runs on small CPU backends. Honest expectations: document,
text, registry and network OSINT run at full speed. Audio transcription is slow
but workable overnight. Video analysis is impractical beyond keyframe
sampling. Face search across a large corpus is a batch job measured in hours.
Reasoning routes to a hosted API, or to a small local model with modest
expectations.

### `small-gpu` — the reference target, 8 GB VRAM / 32 GB RAM

One consumer GPU. The governing rule is **one large model resident at a time**,
with small models — face, OCR, text embeddings, all comfortably under 2 GB —
permitted to co-reside. Media jobs are staged so heavy phases run
sequentially: transcribe the file, unload, run the vision passes, unload, run
extraction. Reasoning either uses a quantized local model in the gap or routes
to a hosted API. That is a per-task choice, because an investigator may be
unwilling to send case material off the box.

Vector search uses an embedded, in-process index — no separate cluster —
quantized where corpus size warrants. Graph and metadata share one Postgres.
The whole stack should come up with Docker Compose on one machine.

### `workstation` — one large GPU, 24–32 GB

Several large models co-resident: full-size face and ASR models, a usable local
vision-language model, and a local reasoning LLM concurrently. The comfortable
tier.

### `server` — multi-GPU, single node

Models pinned per device; the residency manager becomes a placement scheduler.
A strong local LLM served continuously alongside resident perception workers.

### `cluster` — multi-node

Workers scale horizontally per resource class. Vector search moves to a
dedicated service. Collection workers scale independently of GPU workers, since
collection is IO-bound and should never contend for a device.

### Profile summary

| | `cpu-only` | `small-gpu` **(ref)** | `workstation` | `server` | `cluster` |
|---|---|---|---|---|---|
| Accelerator | none | 1x ~8 GB | 1x 24–32 GB | 2–4x 48–80 GB | multi-node |
| System RAM | 16 GB+ | **32 GB** | 64 GB | 128 GB+ | per node |
| Concurrent GPU jobs | — | 1 large / N small | 2–3 | per device | scaled |
| Local LLM | no / tiny | quantized, swapped | resident | strong, resident | served |
| Local VLM | no | small, swapped in | yes | yes | yes |
| Vector index | embedded | embedded, quantized | embedded | embedded or service | service |
| Graph + metadata | 1x Postgres | 1x Postgres | 1x Postgres | Postgres | Postgres + replicas |
| Video analysis | keyframes only | practical, overnight | practical | near-realtime | scaled |
| Deployment | Compose | Compose | Compose | Compose | Kubernetes |

Specific model choices, their measured footprints and their licences are
deferred to [06 — Multimodal Pipelines](06-multimodal-pipelines.md) and
[08 — AI Layer](08-ai-layer.md), both backed by the hardware-focused research
pass.

## 6. Degradation must be visible

Three surfaces, all mandatory:

- **Preflight.** At startup the system probes the host — device presence, VRAM,
  driver version, RAM, disk, CPU instruction sets — selects a profile, and
  prints a capability report: what is available, what is degraded, what is
  unavailable, and why.
- **Per-job.** Before a job runs, the UI states which backends it will use and
  a rough time expectation. A two-hour video on the small tier should say so
  before the user walks away, not after.
- **Per-result.** Every assertion records `derived_by`, including backend
  identity and model version. A result's reliability is inseparable from what
  produced it, and this is also what lets a case be reproduced after a model
  upgrade.

## 7. Open questions for the research pass

- The smallest genuinely useful quantized reasoning model at roughly 5 GB, and
  whether structured-extraction quality at that size suffices or whether the
  small tier should route reasoning remotely by default.
- Whether any vision-language model is usable under 8 GB for frame
  understanding, or whether the small tier should rely on detection, OCR and
  classification instead.
- Realtime factor for quantized CPU speech recognition on a typical eight-core
  desktop, which decides whether `cpu-only` audio is an overnight job or a
  non-starter.
- Measured memory per million embedding vectors at the chosen dimensionalities,
  to size the small tier's corpus ceiling.
- ROCm and Apple Silicon coverage across the chosen backends, since "reasonably
  possible builds" includes both.
