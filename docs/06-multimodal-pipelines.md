# 06 — Multimodal Pipelines

Every model named here was checked for licence, footprint at 8 GB VRAM, and CPU
fallback viability. Where a figure is secondary or estimated it is marked, and
the measurement obligations are collected in §9.

## 0. Three findings that shape everything below

**1. The obvious face stack is not licence-clean, and the clean one has an
open caveat.** InsightFace's pretrained packs are **non-commercial research use
only**. InsightFace's official pages name `buffalo_l` and `antelopev2`
explicitly; `buffalo_m`, `buffalo_s` and `buffalo_sc` fall under the same
general policy but are not individually named (checked 2026-11-05). The
library code is MIT, which disguises the problem: the one-line
`FaceAnalysis(name='buffalo_l')` silently downloads research-only weights.
Commercial licences are available from InsightFace on request (a custom
"Model Licensing" tier; no published price). Astral Lens therefore defaults to
the OpenCV Zoo stack and treats InsightFace as an opt-in the operator knowingly
enables.

*The caveat on the default.* The **distributed licences** of the default pair
are permissive — YuNet is **MIT** (this document previously said Apache-2.0),
SFace is Apache-2.0 (both read from the model directories' LICENSE files). But
the **training data** is another matter. YuNet was trained on WIDER FACE,
whose dataset card on Hugging Face (a mirror; the official site failed TLS)
says CC-BY-NC-ND-4.0. The SFace paper reports CASIA-WebFace, VGGFace2 and
MS-Celeb-1M; the OpenCV README does not say which its weights used;
MS-Celeb-1M was withdrawn in 2019; and InsightFace itself treats models
trained on those sets as non-commercial. Whether a dataset licence binds
trained weights is a legal question no source answers. So: "licence-clean"
means *the licences attached to the weights are permissive*, not that upstream
data terms have been cleared. **That is unresolved**, and the model registry
says so in the `sface` and `yunet` rows.

**2. Deepfake detection does not work in the wild.** This is not a hedge; it is
the measured finding:

| Evidence | Result |
|---|---|
| DF26 (2026), nine detectors on 2026-era generators | AUROC falls from 75.6–94.3 on CelebDF++ to **44.0–69.7**. One detector scored **21.3** on Veo 3.1 — materially worse than chance |
| Same study, human baseline | **52.6%** accuracy on fakes |
| TrueVision (secondary) | EfficientNet-B7: 69.8% on Sora-2 → **15.4%** on Kling v2. **43–59% false-positive rates on real footage** |
| Deepfake-Eval-2024 | Best open-source AUC **0.58**; no commercial tool above 90% |
| Audio: AASIST in the wild | EER **~34.8%**, a 2026 reproduction reports **45.4%** |

A detector that flags real footage as fake half the time is worse than no
detector in an investigation, because it manufactures false leads with an air
of technical authority. Astral Lens ships these as **triage flags only**,
always reported with detector name, version and score, never as a verdict, and
never permitted to produce an assertion above the weakest credibility grade.
Provenance (C2PA where present, acquisition context, corroboration) carries far
more weight than any detector output.

**3. Weights are not the 8 GB constraint.** Activation memory, CUDA context
and co-resident workers are. No source provides measured per-model VRAM under
ONNX Runtime's CUDA execution provider, so the residency manager measures on
first load rather than trusting declarations. See
[05 — Compute Profiles](05-compute-profiles.md).

## 1. Images and faces

### Face detection and embedding

| Backend | Licence | Size | Notes |
|---|---|---|---|
| **YuNet + SFace** (OpenCV Zoo) — *default* | YuNet **MIT**, SFace Apache-2.0 (verified from the LICENSE files, 2026-11-05; **upstream training-data terms unresolved**, see §0.1) | tiny | i7-5930K: YuNet 1.45 ms @160x120, SFace 8.65 ms @112x112. Raspberry Pi 4B: 6.22 ms / 99.2 ms — genuinely viable CPU-only |
| **InsightFace** `buffalo_s` / `buffalo_l` — *opt-in* | Code MIT; **weights non-commercial research only** | 159 MB / 326 MB | `buffalo_l`: LFW 99.80, IJB-C(E4) 97.12. CPU ~102 ms/image (~10 img/s); ~12.8 ms on an 8 GB RTX 2070 Super |
| AdaFace / MagFace | Code MIT / Apache-2.0; **weights inherit non-commercial training-data terms** | — | AdaFace R101/WebFace12M IJB-C 97.66; best TinyFace rank-1 (72.02) for low-resolution work |
| YOLO-face (Ultralytics) | **AGPL-3.0, including trained weights** | — | Avoid by default; Enterprise licence required for non-AGPL distribution |

Detector scaling within InsightFace (WIDER FACE easy/medium/hard): SCRFD-500M
90.57/88.12/68.51 at 0.5 GFLOPs; SCRFD-10G 95.16/93.87/83.05 at 10 GFLOPs. The
hard-set gap is what matters for surveillance-quality imagery.

**Install trap:** `insightface` depends on the CPU `onnxruntime` package. If
`onnxruntime-gpu` is installed in the same environment the last install wins
and inference can silently run on CPU. Preflight must assert the active
execution providers at startup and report them.

### Quantization

ONNX Runtime: S8S8 with QDQ is the default balance; U8U8 when accuracy drops;
avoid S8S8 with QOperator on x86-64. Realized INT8 speedup depends on VNNI/AMX
availability. For ArcFace-style normalized embeddings, keeping the L2-normalize
op in FP32 (via NNCF `ignored_scopes`) preserved accuracy — ~4x speedup at
<0.16% loss in an analogous image-retrieval study, versus ~1.2% loss from naive
INT8. Benchmark execution-provider choice per machine: OpenVINO through ORT was
*slower* than the default CPU EP for ArcFace in one report.

### Person re-identification

OSNet x1.0 (2.2M params, 979M mult-adds at 256x128, Market-1501 94.8 R1 /
86.7 mAP) is the realistic CPU-viable choice; x0.5 drops to 245M mult-adds at
92.0/80.3. CLIP-ReID and TransReID are ViT-based and much heavier. Note that
`torchreid`'s licence is unconfirmed, and Market-1501 and MSMT17 are
research-only datasets — fine for internal evaluation, not for shipping as
training corpora.

### Reverse image search and near-duplicates

**Hashing beats embeddings for duplicates.** PDQ is 256-bit (32 bytes),
open-source, and a 2M-image DFRWS study found it the most robust with a tight
inter-image distance distribution, while dHash was fastest. Borders and
watermarks substantially degrade matching. PhotoDNA is not open and requires an
agreement.

Embeddings are for *semantic* and instance similarity, at ~512 bytes (int8) to
2 KB (fp32) per image. SigLIP (Apache-2.0; so400m vision tower ~0.8 GB fp16)
and DINOv2 (Apache-2.0) are the clean choices; OpenCLIP code is MIT but
checkpoint training-data terms need checking.

Run both: hash first (cheap, exact), embeddings second (expensive, semantic).

### Geolocation from imagery

| Model | Licence | Reality |
|---|---|---|
| **GeoCLIP** | MIT (code and weights) | The only licence-clean option. 100k-coordinate gallery |
| StreetCLIP | **CC-BY-NC-4.0** | Non-commercial. IM2GPS3K unseen-country 52.79% vs CLIP 41.51% |
| PIGEON / PIGEOTTO | Weights, geocells and datasets **withheld for dual-use reasons** | Not usable |

**Report region-level only.** No source supports street-level precision
claims, and a 2025 PETS study found CLIP-based models weaker than large VLMs in
complex urban and unfamiliar rural scenes. Output is a ranked region
hypothesis, graded accordingly — never a coordinate presented as a location.

### Image forensics

Error level analysis and JPEG ghost are **leads, not evidence**: ELA
false-positives on flat areas and high-contrast text, fails on PNG, on
multi-resave and above ~Q90, and AI-generated images have no JPEG history at
all. PRNU camera fingerprinting is stronger but a 2024/25 *J. Forensic Sci.*
analysis questions court-approved error rates across brightness levels.

Policy: run at least two independent methods, record method names and
parameters in `derived_by`, and always retain original bytes so results can be
recomputed.

## 2. Audio

### Speech recognition

| Backend | Licence | Footprint | CPU |
|---|---|---|---|
| **faster-whisper** large-v3-turbo int8 — *default* | MIT | **1,545 MB** | ~10x realtime for large-v3 int8 on a Ryzen 7 7700X (secondary); turbo ~6x faster again |
| faster-whisper distil-large-v3 int8 | MIT | 1,481 MB | LibriSpeech-clean WER 2.39 |
| faster-whisper large-v3 int8 | MIT | 2,953 MB | WER 4.59 int8 / 2.88 fp16 |
| **Parakeet-TDT-0.6B-v3** | CC-BY-4.0 (attribution required; card says ready for commercial use) | 1.31 GB fp16 | **INT8 ONNX artefacts exist and were verified at file level on 2026-11-06** (HF API listings, nothing downloaded): `istupakov/parakeet-tdt-0.6b-v3-onnx` ships `encoder-model.int8.onnx` (652 MB) + `decoder_joint-model.int8.onnx` (18 MB), licence tag cc-by-4.0, loadable with `onnx-asr` (MIT; ONNX Runtime + numpy, no NeMo/PyTorch); sherpa-onnx exports exist too (`csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8`, **no licence tag** — treat as inheriting CC-BY-4.0). **Speed is contested:** the earlier secondary figure of RTF 0.031–0.039 (25–30x realtime on 4 cores) is *not* supported by sherpa-onnx's own docs, which give RTF 0.325 (v3-int8) and 0.118 (v2-int8) on one wav with 2 threads, hardware unstated. No source gives a WER for the INT8 build. v2 is English-only; v3 covers 25 European languages. English avg WER 6.34% is NVIDIA's FP figure |
| Omnilingual ASR (Meta) | Apache-2.0 code/models (corpus terms conflict) | 300M–7B | **1,600+ languages**; CER <10 for 78% of them. The coverage option |
| whisper.cpp | MIT | large-v3 q5 ~1.6 GB | Conflicting reports: ~1.5–2.5x realtime on 8-core x86; much faster on Apple Metal |
| MMS, SeamlessM4T | **CC-BY-NC-4.0** | — | Non-commercial. Excluded |

**`cpu-only` is viable for audio**, which was an open question in doc 05.
Published speed figures for Parakeet INT8 ONNX disagree by an order of
magnitude: RTF ~0.035 would transcribe a two-hour recording in about four
minutes, but sherpa-onnx's documented RTF of 0.12–0.33 (2 threads, hardware
unstated) means roughly 14–39 minutes. Even the pessimistic end is workable for
a batch queue, so audio OSINT plausibly does not require a GPU — but the real
figure has **not yet been measured here** (§9 item 2) and the 4-minute number
should not be quoted.

### Diarization and speaker identification

- **pyannote `speaker-diarization-community-1`** — CC-BY-4.0 but **gated**
  (requires accepting conditions and a token, which an air-gapped install must
  plan for). DER: AMI IHM 17.0, DIHARD 3 full 20.2, VoxConverse 11.2. Runs on
  CPU by default.
- **NVIDIA Nemotron-3-Diarization** — OpenMDW-1.1, explicitly commercial-ok,
  materially better (DIHARD III DER 12.73, CALLHOME 9.10) but **requires an
  NVIDIA GPU**, so it cannot serve the CPU tier.
- **Speaker matching** ("who is this voice") uses WeSpeaker ResNet293-LM
  (VoxCeleb1-O EER **0.447%**) or CAM++ (0.73% at 7.18M params) or SpeechBrain
  ECAPA (Apache-2.0). **Do not use Resemblyzer** — 6.2% EER, ranked below ECAPA
  and WavLM-TDNN; it is a demo, not a matcher.

Voice embeddings are biometric data under several regimes; see §6.

### Events, VAD, language ID

Silero VAD (MIT, ~2 MB, <1 ms per 30 ms chunk on one CPU thread) for
segmentation. YAMNet (521 AudioSet classes, mAP 0.306, 3.7M weights) for cheap
CPU event detection; BEATs (MIT, 90M params, AudioSet mAP 48.6, ESC-50 98.1)
when a GPU is available. SpeechBrain VoxLingua107 ECAPA (Apache-2.0) for
language ID across 107 languages at 93.3% test accuracy.

Honest limit on gunshot detection: fireworks and bursts dominate false
positives, and YAMNet fell to 63.8% accuracy with five overlapping sources
versus 93.2% for a fine-tuned AST.

### Audio forensics

ENF (electrical network frequency) analysis is the most promising —
Multi-HCNet reports 98.84% peak accuracy under filtering — but it **requires a
regional grid reference database**, and anti-forensic ENF re-synthesis has been
demonstrated, so a clean trace is not proof of authenticity. No maintained
open-source splice-detection tool was found; this is a genuine gap.

## 3. Video

Pipeline: PySceneDetect (BSD-3) for shot boundaries using `AdaptiveDetector`
(rolling threshold, better under camera motion) → keyframe extraction →
per-frame analysis → track association → timeline assembly.

| Component | Choice | Licence | Why |
|---|---|---|---|
| Detection | **RT-DETRv2-S/M** | Apache-2.0 | S: 20M params, 48.1 AP, 217 FPS T4 fp16. The clean alternative to AGPL YOLO |
| Open-vocabulary | Grounding DINO Swin-T | Apache-2.0 | ~700 MB fp32; 1.5/1.6 are paid API only |
| Segmentation | SAM 2.1 Hiera-Tiny | Apache-2.0 | 38.9M params, 91 FPS A100; 1024x1024 input so activations dominate; GPU only |
| Tracking | **ByteTrack** | MIT | Motion-only, no ReID, 29.6 FPS MOT17 — the cheap default |
| Tracking (hard) | BoT-SORT-ReID | MIT | +2 HOTA under occlusion/camera motion at ~5x the cost (4.5 FPS) |

### Vision-language models under 8 GB

Viable, but only just: **Qwen3-VL-2B** (Apache-2.0; GGUF Q4_K_M ≈ 1.11 GB,
Q8_0 ≈ 1.83 GB, with a separate `mmproj` vision file — model page read
2026-11-05) is the default; **Qwen3-VL-4B** (Apache-2.0, Q4_K_M ≈ 2.5 GB) is
the larger option; **InternVL3-1B/2B** (MIT, 1B ≈ 1.9 GB bf16) is the
alternative. **Qwen2.5-VL-3B is excluded as a default: its checkpoint is under
the Qwen Research License, which permits non-commercial use only** (confirmed
from the model's LICENSE; GGUF derivatives presumably inherit it). Qwen2.5-VL-7B
is Apache-2.0 but too large for the 8 GB tier except heavily quantized.
Requires a capped frame budget (~8–16 frames) and capped `max_pixels` per
frame, with GPU jobs serialized. Footprints above are file sizes from model
pages, not measured VRAM — see §9.

Stated plainly: **a 2B VLM is a triage captioner, not a forensic witness.** It
is useful for "does this clip contain a vehicle, a crowd, an interior" and for
routing frames to specialist models. It is not evidence.

## 4. Documents and text

- **Parsing:** Docling (MIT, ~3.1 s/page CPU, ~0.49 s/page on an L4) as
  default; SmolDocling-256M (Apache-2.0, <500 MB VRAM) for the GPU path;
  Apache Tika (Apache-2.0, ~1,000 formats, JVM, CPU) for the long format tail.
  **Marker and Surya weights are modified OpenRAIL-M, free only below $5M
  revenue/funding** — a redistribution trap. MinerU uses a custom
  Apache-derived licence with extra conditions.
- **Translation:** **MADLAD-400** (Apache-2.0, 450+ languages, 3B ≈ 6 GB fp16 /
  ~3 GB int8) is the only clean large option. Opus-MT per-pair models are
  CC-BY-4.0 and small. **NLLB-200 is CC-BY-NC-4.0 and that contaminates every
  quantized port of it** — excluded despite being the obvious choice.
- **NER:** GLiNER `multi-v2.1` (209M, Apache-2.0) for zero-shot typed
  extraction — note **v0 and v1 are CC-BY-NC**, so version pinning matters.
  spaCy `xx_ent_wiki_sm` (MIT, 11.6 MB, F≈83) as the cheap baseline, with the
  caveat that WikiNER is Europe-weighted and CJK needs language-specific
  pipelines. Cross-script name handling is specified in
  [03 — Data Model](03-data-model.md#6-names-across-scripts).

## 5. Provenance and integrity

**C2PA is worth checking and worth nothing as a negative.** The Interim Trust
List froze on 1 Jan 2026; only ~10 organizations have conforming products and
no dedicated camera had completed conformance as of 2026. WhatsApp, iMessage,
Facebook, Instagram, X, LinkedIn and TikTok all strip manifests on upload. So:
a valid manifest is positive evidence; its absence proves nothing, and the UI
must say so rather than showing a red "unverified" badge that analysts will
over-read.

**Chain of custody** follows NIST SP 800-86 (collection, examination, analysis,
reporting; hash before and after acquisition — though note it names the now-weak
MD5 and SHA-1) and ISO/IEC 27037 (auditability, repeatability,
reproducibility). Practice:

- SHA-256 at ingest, before any processing, and before every transform.
- Derived artifacts reference `parent_sha256`; nothing is modified in place.
- Every model output logs model name, weights hash, version and threshold.
- Signed hash manifests; originals in WORM storage. If S3 Object Lock is used,
  **compliance mode, not governance mode** — governance can be bypassed by a
  principal holding `s3:BypassGovernanceRetention`. Delete markers are not
  WORM-protected, and default retention applies only to objects created after
  it is enabled.
- ExifTool is the metadata workhorse but is **GPL-1.0+/Artistic dual-licensed**,
  so it is invoked as a subprocess rather than linked, and it reads but cannot
  write C2PA.

## 6. Biometric processing: the legal landscape

Reported as fact, because it bears on what the software can ship and where it
can be deployed. This is not legal advice.

- **EU AI Act Art. 5(1)(e)** prohibits placing on the market, putting into
  service, or using AI systems that *create or expand* facial-recognition
  databases through **untargeted scraping** of facial images from the internet
  or CCTV. In force since **2 Feb 2025**, with **no law-enforcement exception
  and no authorization route in the text.** Commission guidance indicates
  pre-existing databases are excluded unless expanded this way, that
  train/test-only databases are excluded where individuals are not identified,
  and that scraping region-by-region to evade the rule is still caught. This is
  the legal basis for the non-goal in
  [01 — Vision & Scope](01-vision-and-scope.md): Astral Lens searches media the
  operator lawfully holds, and does not build a scraped index.
- **EU AI Act Art. 5(1)(h)** restricts real-time remote biometric
  identification in public spaces for law enforcement to narrow cases
  (abduction/trafficking/missing persons; imminent threat to life; locating a
  suspect for an Annex II offence carrying ≥4 years), each requiring prior
  judicial or independent administrative authorization, an Art. 27
  fundamental-rights impact assessment and Art. 49 registration, with urgent
  use filed within 24 hours and **no adverse decision based solely on system
  output**. Art. 5(1)(g) prohibits biometric categorization inferring race,
  political opinion, union membership, religion or sexual orientation.
- **Annex III(1)** makes remote biometric identification, sensitive-attribute
  categorization and emotion recognition high-risk. One-to-one *verification*
  is excluded. Regulation (EU) 2026/1744 (the Digital Omnibus, in force
  27 Jul 2026) **moved Annex III obligations from 2 Aug 2026 to 2 Dec 2027**;
  it did not change Art. 5(1)(e).
- **GDPR** Art. 4(14) makes a face *template* biometric data where processing
  is for unique identification — a stored photograph is not, on this reading, but
  the embedding we compute from it is. Art. 9(1) prohibits such processing
  absent an Art. 9(2) condition (explicit consent; data manifestly made public
  by the subject; legal claims; substantial public interest under member-state
  law with specific safeguards).
- **US state law** varies in who can sue, which drives real exposure: **BIPA**
  (Illinois) requires a written release before collection, a public retention
  and destruction schedule, destruction at purpose-satisfaction or 3 years, and
  bars profiting from identifiers — and it carries a **private right of
  action**. **CUBI** (Texas) allows up to $25,000 per violation but **Attorney
  General only**; Washington RCW 19.375 up to $7,500, AG only; Colorado
  HB 24-1130 (effective 1 Jul 2025) applies regardless of volume thresholds,
  AG and DAs only.
- **Enforcement reality:** Clearview AI drew ~€90–100M across EU decisions
  (Italy €20M, Greece €20M, France €20M, Netherlands €30.5M) plus a
  £7.55M UK ICO fine whose jurisdictional question reached the Court of Appeal;
  reporting indicates none were paid. Its US BIPA settlement — a 23% equity
  stake — was **vacated and remanded by the Seventh Circuit on 13 Jul 2026**
  (*Weissman v. Clearview AI*) on Rule 23 representation grounds. Texas
  separately settled with Meta for $1.4B and Google for $1.375B under CUBI.

**Design consequence, not a gate:** face and voice capability is available
without friction. What the platform does unconditionally is *record* — purpose,
operator, case, model, backend and timestamp — because that record costs the
operator nothing and is the only thing that makes their use defensible. The
optional authorization fields exist for team deployments that need them and are
absent from the solo profile.

## 7. Model registry

Every model is a registry row, not a hard-coded import: id, task, backend,
licence, SPDX identifier, commercial-use flag, gated flag, weights hash,
measured VRAM, measured CPU throughput, source URL. The UI can then answer "is
my deployment licence-clean?" and the preflight report can warn before a
non-commercial weight is ever loaded. Given how many traps §1–§4 surfaced,
this is a core feature, not bookkeeping.

## 8. Pipeline sketch: video to dossier on 8 GB

Satisfying acceptance scenario 2 from doc 01, with phases ordered so one large
model is resident at a time:

```
1  ingest        hash, ExifTool, container/codec probe, C2PA check      CPU
2  segment       PySceneDetect adaptive; keyframes; VAD                 CPU
3  transcribe    Parakeet int8 (CPU) or faster-whisper-turbo (GPU)      ~1.5 GB
4  diarize       pyannote community-1; speaker embeddings → vectors     CPU
   ── unload ──
5  detect        RT-DETRv2-S on keyframes; ByteTrack association        ~1.5 GB
6  faces         YuNet detect → SFace embed → vector search             ~0.3 GB
7  text          OCR keyframes; logos                                   ~1 GB
   ── unload ──
8  caption       Qwen3-VL-2B on representative frames, capped budget    ~3 GB
   ── unload ──
9  extract       GLiNER + rules over transcript and OCR                 ~0.5 GB
10 assemble      assertions with derived_by; timeline; entity links     CPU
```

Phases 1–7 and 9–10 run on `cpu-only` too, slowly but completely. Phase 8 is
the only one that genuinely needs the GPU, and it is the one producing the
least evidentiary output — which is a convenient alignment.

## 9. Measurement obligations

No source provided these; the project must measure them on reference hardware
and publish the numbers. **Reference machine for every figure below:** a
mid-range desktop (8-core / 16-thread CPU, 32 GB RAM, a consumer GPU with 8 GB
VRAM), Windows 10, measured 2026-11-05 on a machine that was also running other
workloads. Scripts are in [`bench/`](../bench/); re-run them rather than
trusting the numbers.

| # | Obligation | Status |
|---|---|---|
| 1 | VRAM per model under ONNX Runtime CUDA EP and llama.cpp | **Not measured.** Needs a CUDA/cuDNN install and the model files; no GPU backend exists yet (M2/M3) |
| 2 | CPU throughput per backend | **Face: measured** (below). ASR RTF and OCR pages/s **not measured** (Parakeet ONNX is ~650 MB; OCR stack not chosen) |
| 3 | Memory per million vectors | **Measured**: 128-d at 1M rows; 512-d at 100k–300k rows, extrapolated for size (below) |
| 4 | Is YuNet+SFace accuracy acceptable? | **Measured on LFW** (below) — a floor check only |
| 5 | End-to-end wall time, two-hour video | **Not measured.** Depends on items 1–2 |

### 9.1 Face detection and embedding on CPU (item 2)

YuNet 2023mar + SFace 2021dec through OpenCV 5.0.0 DNN on CPU, 250×250 images,
300 images after warm-up (`bench/faces/lfw_eval.py`):

| Threads | Detect | Align + embed | Images/s end to end |
|---|---|---|---|
| 1 | 3.7 ms | 24.3 ms | **35.7** |
| 16 | 2.1 ms | 5.4 ms | **133** |

So a face sweep of a **100,000-image corpus takes roughly 13 minutes** on this
CPU with all threads and **about 47 minutes single-threaded**. This is the
OpenCV DNN CPU path; GPU execution was not measured. The earlier doc figure for
SFace (8.65 ms on an i7-5930K) is the same order as the single-thread embed
time here.

### 9.2 YuNet + SFace accuracy on LFW (item 4)

Standard LFW 10-fold pairs (6,000 pairs), deep-funneled full images, threshold
chosen on the nine training folds, cosine similarity:

| Metric | Result |
|---|---|
| Accuracy | **98.03% ± 0.70%** (10 folds) |
| ROC AUC | 0.9823 |
| True-accept rate at false-accept 1% / 0.1% | 96.8% / 95.8% |
| Images with no face detected | 0 of 12,000 |

**How to read it.** LFW is easy and saturated. This number says the pipeline is
wired correctly and not broken; it does **not** say the stack will find the
right person in low-resolution CCTV, profile or occluded images, which is what
investigative use looks like. InsightFace `buffalo_l` is cited elsewhere in
this repo at 99.80% on LFW; that is a published figure from a different
source and protocol, not something measured here, and `buffalo_l`'s weights are
non-commercial research only, so they were **not downloaded and not compared
like for like**. A ~1.8-point gap on the easiest benchmark suggests a larger gap
on hard images, but that is inference. The licence-clean default is therefore
*usable* on clean frontal faces; whether it is *good enough* for an
investigator's real material needs a harder benchmark (IJB-C-class, which was
not available here). Because the face stage returns ranked candidates with
scores and the backend named, a weaker default degrades recall, never
silently.

### 9.3 Vector index size and latency (item 3)

pgvector 0.8.7 HNSW (`m=16`, `ef_construction=64`) in Postgres 16.15,
`shared_buffers=1GB`, `maintenance_work_mem=1GB`, 3 parallel build workers
(`bench/vectors/pgvector_bench.py`). **Synthetic, identity-clustered data**
(ten members per identity); queries are fresh members of existing identities.

| Case | Heap + TOAST | HNSW index | Per vector (index) | **Total per million** | Build |
|---|---|---|---|---|---|
| 512-d float32, 300k rows | 809 MB | 781 MB | 2,731 B | **≈ 5.3 GB** | 97–103 s |
| 512-d **halfvec**, 300k rows | 335 MB | 391 MB | 1,366 B | **≈ 2.4 GB** | 83 s |
| **128-d float32** (SFace's real output size), 300k rows | 168 MB | 238 MB | 832 B | **≈ 1.35 GB** | 48 s |

- **Sizes are reliable and scale linearly with rows.** The 128-d row's "per
  million" figure was a linear extrapolation until it was run at 1M (below);
  the 512-d rows are still extrapolated, and **512-d 1M was not run**.
- **128-d float32 at 1M rows (run 2026-11-06, 4 GB-capped container,
  `shared_buffers=1GB`, `maintenance_work_mem=1500MB`, 3 build workers):**
  heap + TOAST 558.7 MB, HNSW 793.7 MB (832 B/vector), **1,352 MB per million —
  identical to the extrapolation**. Load 70 s; **HNSW build 209 s** (300k took
  48 s: 4.3x the time for 3.3x the rows, mildly superlinear). Recall@10 on the
  synthetic data **fell to 0.64 at `ef_search=40` and 0.82 at 100** (it was 0.96
  and 0.99 at 300k), p50 1.9 ms and 3.1 ms. More rows means more distractors
  for the graph, so `ef_search` must grow with the corpus; higher values were
  not measured at 1M. Same synthetic-data caveat as below.
- **Operational trap:** a parallel HNSW build allocates shared memory about the
  size of `maintenance_work_mem`; with Docker's default 64 MB `/dev/shm` or a
  too-small `--shm-size` it fails with `could not resize shared memory segment
  ... No space left on device` after the data has already loaded.
- **SFace produces 128-d embeddings, not 512-d.** The 512-d case is the
  ArcFace/InsightFace class. The licence-clean default stack therefore costs
  about a quarter of the memory the earlier sizing assumed.
- **Float32 512-d vectors exceed Postgres' ~2 KB TOAST threshold** (2,056 bytes
  per row), so they are stored out of line and the table is ~32% larger than
  the raw 2,048 bytes. `halfvec` (1,032 bytes) stays inline and halves total
  storage. `pg_relation_size` alone reports a misleadingly tiny heap; use
  `pg_table_size`.
- **Recall depends heavily on the data and cannot be read from this run.**
  At 100k rows, 512-d recall@10 was 0.97 (`ef_search=40`) and 0.995 (`100`). At
  300k rows the same synthetic generator gave 0.66 and 0.86, while the 128-d
  case held 0.96 and 0.99. Random 512-d points are nearly equidistant, which is
  the hardest case for graph navigation, so this is a **pessimistic** result
  for real embeddings, not a verdict on pgvector. The knob that buys recall is
  `ef_search`, at a price in latency (512-d, 300k rows):

  | `ef_search` | 40 | 100 | 200 | 400 | 800 |
  |---|---|---|---|---|---|
  | recall@10 | 0.66 | 0.86 | 0.92 | 0.95 | **0.98** |
  | p50 latency | 1.9 ms | 3.6 ms | 6.7 ms | 11.4 ms | 21.6 ms |

  Even at `ef_search=800` a query takes ~22 ms. **Tune `ef_search` against real
  embeddings of the operator's own data before trusting any recall figure.**
- **How the 1M run became possible.** The first attempt (512-d, large buffers)
  overran the 16 GB Docker VM on a host with ~7 GB free; Docker Desktop
  restarted and took every container on the machine down with it, including two
  unrelated to this project. The successful run used only the 128-d case, a
  4 GB container cap and a watchdog that stops the container if host free RAM
  falls below 1.5 GB (host free RAM was 4.6–8 GB throughout). The default case
  list in the script still stops at 300k rows; 512-d at 1M remains unrun.

### 9.4 What the corpus ceiling looks like

On the 32 GB reference box, with the whole index kept in RAM and half of the
RAM left for everything else, the measured per-vector costs give a rough
ceiling of **~10 million 128-d SFace vectors** (≈ 13.5 GB) or **~3 million
512-d float32 vectors** (≈ 16 GB), or **~6 million 512-d `halfvec`**. These are
sizing estimates built on a linear extrapolation, not tested limits.
