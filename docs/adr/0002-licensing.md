# ADR-0002: Licensing posture

**Status:** Accepted

## Context

The research passes surfaced an unusual density of licence traps, most of which
only bite at distribution time — after the code is written:

- **Model weights.** InsightFace's pretrained packs are non-commercial
  research only (official pages name `buffalo_l` and `antelopev2`; the other
  `buffalo_*` packs fall under the same general policy), while the library code
  is MIT — so the one-line default API call silently downloads research-only
  weights. Qwen2.5-VL-3B is under the Qwen Research License (non-commercial),
  confirmed from the model's LICENSE on 2026-11-05.
  NLLB-200, SeamlessM4T, MMS, StreetCLIP and GLiNER v0/v1 are CC-BY-NC.
  Ultralytics YOLO is AGPL-3.0 **including trained weights**. Marker and Surya
  weights are modified OpenRAIL-M with a $5M revenue cap.
- **Data.** OpenSanctions is CC-BY-NC — commercial use, *including compliance
  screening*, requires a paid licence. VirusTotal's public API "must not be
  used in commercial products or services." MaxMind GeoLite2 restricts
  redistribution, and open-source projects are actively removing bundled
  copies. OSM and Overture carry ODbL share-alike that reaches derivative
  *databases*, which our entity store is.
- **Infrastructure.** Memgraph's BSL 1.1 Additional Use Grant prohibits
  distribution to third parties, which is disqualifying for shipped software.
  Neo4j Community is GPLv3 with Enterprise-only backup and RBAC. ArangoDB is
  BSL. Kuzu was archived in October 2025.

An operator cannot be expected to discover these. A platform that silently
downloads a non-commercial model on first run has created an exposure its
operator never agreed to.

## Decision

1. **The project is licensed AGPL-3.0-or-later** (owner decision, 2026-11-05;
   see "Project licence" below). The default install path depends only on code
   whose licence is AGPL-compatible and, for *commercial operators*, on
   permissive code — see decision 2.
2. **Default backends and default connectors must be licence-clean for
   commercial use.** Where the obvious choice is not — InsightFace for faces,
   NLLB-200 for translation, Ultralytics for detection — a clean alternative is
   the default and the obvious choice becomes a knowing opt-in.
3. **A model registry and a source registry** record licence, SPDX identifier
   where applicable, commercial-use flag, attribution requirement, gating and
   source URL for every model and every connector.
4. **Preflight reports licence posture** before anything is loaded, and warns
   when a deployment that has declared itself commercial is about to use a
   non-commercial weight or dataset.
5. **Copyleft tools may be invoked as subprocesses or run as separate
   services** — ExifTool (GPL/Artistic), SearXNG (AGPL), mempool.space (AGPL) —
   but are not linked into the core.

## Project licence

**AGPL-3.0-or-later**, chosen by the project owner on 2026-11-05 from
Apache-2.0, MIT, AGPL-3.0 and "decide later". An earlier draft of this ADR
assumed a permissive core; that assumption is superseded.

What AGPL does and does not change:

- **Commercial use stays permitted.** AGPL restricts how derivatives are
  distributed, not who may use the software. Section 13 requires that anyone
  offering a *modified* version to users over a network provide them the
  corresponding source. That fits a self-hosted investigative platform and
  deters closed hosted forks. **Consequently the licence-clean defaults in
  decision 2 are unchanged**: an operator may be commercial under AGPL, and
  the earlier note that "several model substitutions relax" under AGPL does
  *not* apply, because the AGPL does not forbid commercial operators.
- **Model weights and datasets are licensed separately** from the code. An
  AGPL core does nothing about InsightFace, Qwen Research-licensed weights,
  OpenSanctions or any other non-commercial asset.
- **Dependencies must be AGPL-compatible.** MIT, BSD, Apache-2.0 (compatible
  with GPLv3-family licences, *not* with GPLv2-only), LGPL (psycopg) and
  MPL-2.0 are fine. **GPL-2.0-only code cannot be combined** — theHarvester
  (GPL-2.0-only) is the example already on record. Add each dependency's
  licence to the relevant table before merging.
- **The plugin boundary matters more.** An in-process plugin is loaded into
  the AGPL core's process and is arguably a combined work, so first-party
  in-process plugins are AGPL-3.0-or-later too. A plugin that exchanges only
  serialized messages with the core in a separate process (ADR-0005) has a
  stronger case for being a separate work and may carry its own licence;
  proprietary third-party plugins should therefore be run out-of-process. This
  is the author's reading, **not legal advice**, and should be confirmed with a
  lawyer before a plugin marketplace is opened.
- **Reference material that people copy is licensed separately.** The example
  plugin under `plugins/examples/` is MIT so it can seed any plugin. The
  published wire contract (the `astral.plugin/v1` manifest schema) is a
  candidate for a permissive licence too, so plugin authors are not forced
  into AGPL by writing against it; that is a follow-up decision, not made here.
- **Share-alike data.** Checked 2026-11-06 against Creative Commons' compatible-
  licences page and announcement, the FSF licence list and the 4.0 legal code
  (read through fetch tools that return paraphrase, so confirm the wording
  against the raw legal code). CC BY-SA 4.0 is declared one-way compatible with
  **GPLv3 only**; **AGPLv3 is not on either list**, and the legal code only
  lets an adapter use a licence CC has approved as "BY-SA Compatible". So
  CC BY-SA data cannot be relicensed as AGPL. Rules that follow:
  third-party site packs (WhatsMyName's `wmn-data.json`, whose upstream
  `LICENSE.md` is CC BY-SA 4.0) live unmodified in their own directory, e.g.
  `data/wmn/`, with the CC BY-SA 4.0 text, an attribution file and a note of any
  changes; they are loaded at runtime as data and never converted into AGPL
  source files; a modified copy stays BY-SA 4.0; project-written site entries
  are original rather than derived from the pack; the WhatsMyName name and logo
  are not used as branding. Whether runtime loading leaves the AGPL code
  untouched is the author's inference, not a verified legal position.
- **`LICENSE` file.** Added on 2026-11-06: the unmodified FSF text of the GNU
  AGPL v3, fetched from `https://www.gnu.org/licenses/agpl-3.0.txt`. The
  "or later" grant is stated in the README and `backend/pyproject.toml`
  (`license = "AGPL-3.0-or-later"`), not in the licence text itself.

## Consequences

- **"Licence-clean" has a precise, limited meaning here:** the licence attached
  to the weights is permissive. It does not mean upstream *training-data* terms
  are cleared. YuNet (MIT) was trained on WIDER FACE, whose dataset card says
  CC-BY-NC-ND-4.0; SFace (Apache-2.0) reports CASIA-WebFace, VGGFace2 and
  MS-Celeb-1M, the last withdrawn in 2019. Whether a dataset licence binds
  trained weights is an open legal question. The model registry records this in
  the `yunet` and `sface` rows rather than hiding it, and an operator who needs
  certainty must take legal advice or obtain a commercial InsightFace licence.
- Default accuracy is lower than it could be: YuNet+SFace rather than
  `buffalo_l`. Whether that cost is acceptable is an explicit measurement
  obligation ([06 §9](../06-multimodal-pipelines.md)). If it is not, the answer
  is to obtain a commercial licence from InsightFace, not to quietly ship
  research weights.
- The two registries are real engineering work that produces no visible
  feature. They are nonetheless the only honest way to answer "is my deployment
  licence-clean?" — a question no existing OSINT platform answers.
- Some capabilities ship degraded or opt-in rather than not at all.
