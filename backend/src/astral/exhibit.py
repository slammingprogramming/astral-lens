"""Exhibit export (10 §8, 09 §9): assemble a case into a self-verifying package.

The package holds the evidence the case relies on (originals and derived artifacts,
named by their SHA-256), the assertions with their grades and retractions, the capture
log (how and when each artifact was acquired, what each search consulted and could not),
tool and model versions, the case's slice of the audit log, any approved AI answers, a
human-readable record with a source appendix, and the operator's own statement.

THE PLATFORM ASSEMBLES THE PACKAGE; IT NEVER ASSERTS ADMISSIBILITY. What the hashes show
is that these bytes are the bytes the system holds, unaltered since the moment it
acquired them. They say nothing about what happened before that, whether the content is
true, or whether a search was complete. LIMITS.md says so inside the package.

Nothing is exported if an artifact's bytes no longer match its hash or an audit entry no
longer recomputes, unless the caller explicitly allows it, in which case the failure is
written at the top of the manifest and the README.
"""

from __future__ import annotations

import hashlib
import io
import json
import platform
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg

from astral import __version__, audit, projector, signing
from astral.ai import SKIP_PREDICATES
from astral.db import Actor
from astral.evidence import EvidenceStore

SCHEMA = "astral.exhibit/v1"
_CHUNK = 1 << 20


class ExhibitError(Exception):
    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.problems = problems or []


def _j(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False)


def _iso(v: Any) -> Any:
    return v.astimezone(timezone.utc).isoformat() if isinstance(v, datetime) else v


def _hex_list(v: Any) -> list[str]:
    return [bytes(x).hex() for x in (v or [])]


def build(conn: psycopg.Connection, store: EvidenceStore, actor: Actor, case_id: uuid.UUID, out_path: str | Path, *,
          operator_statement: str, plan_ids: list[uuid.UUID] | None = None, include_all_artifacts: bool = False,
          signing_key: tuple[bytes, str | None] | None = None, allow_integrity_failures: bool = False) -> dict[str, Any]:
    """Write the bundle to `out_path` and return a summary. `signing_key` = (private PEM, passphrase)."""
    if not (operator_statement or "").strip():
        raise ExhibitError("an operator statement is required: the package carries the operator's own account of what "
                           "was done, which the platform cannot write for them")
    case = conn.execute("SELECT id, name, state, created_at, created_by FROM case_ WHERE id=%s AND workspace_id=%s",
                        (case_id, actor.workspace_id)).fetchone()
    if not case:
        raise ExhibitError("no such case")
    projector.rebuild_case(conn, case_id)                      # the record is read from fresh projections
    now = datetime.now(timezone.utc)
    problems: list[str] = []

    # ---- assertions (all of them; retractions travel with them)
    arows = conn.execute(
        "SELECT a.*, s.kind AS source_kind, s.name AS source_name, s.locator AS source_locator, s.origin_key,"
        " r.retracted_at, r.reason AS retraction_reason FROM assertion a JOIN source s ON s.id = a.source_id"
        " LEFT JOIN assertion_retraction r ON r.assertion_id = a.id WHERE a.case_id=%s ORDER BY a.id", (case_id,)).fetchall()
    assertions = []
    for a in arows:
        assertions.append({
            "id": a["id"], "subject": str(a["subject"]), "predicate": a["predicate"], "object_kind": a["object_kind"],
            "object_entity": str(a["object_entity"]) if a["object_entity"] else None, "object_value": a["object_value"],
            "source": {"id": str(a["source_id"]), "kind": a["source_kind"], "name": a["source_name"],
                       "locator": a["source_locator"], "origin_key": a["origin_key"]},
            "source_grade": a["source_grade"], "info_grade": a["info_grade"], "evidence": _hex_list(a["evidence"]),
            "derived_from": list(a["derived_from"]), "derived_by": a["derived_by"], "recorded_at": _iso(a["recorded_at"]),
            "valid_from": _iso(a["valid_from"]), "valid_to": _iso(a["valid_to"]), "created_by": str(a["created_by"]),
            "retracted": ({"at": _iso(a["retracted_at"]), "reason": a["retraction_reason"]} if a["retracted_at"] else None)})
    active = [a for a in assertions if not a["retracted"]]

    # ---- AI answers the operator chose to include
    runs = []
    for pid in plan_ids or []:
        p = conn.execute("SELECT id, question, planner, plan, status, result, created_at, approved_at, finished_at"
                         " FROM ai_plan WHERE id=%s AND case_id=%s", (pid, case_id)).fetchone()
        if not p or p["status"] != "done" or not p["result"]:
            raise ExhibitError(f"plan {pid} is not a finished answer in this case")
        for c in p["result"].get("claims", []):
            if not c.get("anchors"):
                raise ExhibitError(f"plan {pid} has a claim with no citation; nothing is exported while a claim lacks one")
        runs.append({"id": str(p["id"]), "question": p["question"], "planner": p["planner"], "plan": p["plan"],
                     "created_at": _iso(p["created_at"]), "approved_at": _iso(p["approved_at"]),
                     "finished_at": _iso(p["finished_at"]), "result": p["result"]})

    # ---- which artifacts the package carries
    acq = conn.execute("SELECT * FROM artifact_acquisition WHERE case_id=%s ORDER BY id", (case_id,)).fetchall()
    in_case = {bytes(r["sha256"]).hex() for r in acq}
    relied: dict[str, set[int]] = {}
    for a in active:
        for e in a["evidence"]:
            relied.setdefault(e, set()).add(a["id"])
    for r in runs:
        for c in r["result"].get("claims", []):
            for an in c.get("anchors", []):
                if an.get("kind") == "span":
                    relied.setdefault(an["text_artifact"], set())
    wanted = set(in_case) if include_all_artifacts else set(relied) & in_case
    for sha in set(relied) - in_case:
        problems.append(f"evidence {sha[:12]} is cited but was not acquired in this case")
    frontier = list(wanted)
    while frontier:                                            # derived artifacts bring their parents
        sha = frontier.pop()
        row = conn.execute("SELECT parent_sha256 FROM artifact WHERE sha256=%s", (bytes.fromhex(sha),)).fetchone()
        if row and row["parent_sha256"]:
            parent = bytes(row["parent_sha256"]).hex()
            if parent in in_case and parent not in wanted:
                wanted.add(parent)
                frontier.append(parent)

    zpath = Path(out_path)
    tmp = zpath.with_suffix(zpath.suffix + ".partial")
    files: list[dict[str, Any]] = []
    manifest_artifacts: list[dict[str, Any]] = []
    with zipfile.ZipFile(tmp, "w", allowZip64=True) as zf:
        # ---- artifacts, streamed and re-hashed on the way out
        for sha in sorted(wanted):
            meta = conn.execute("SELECT * FROM artifact WHERE sha256=%s", (bytes.fromhex(sha),)).fetchone()
            src = store.path_for(conn, actor, case_id, bytes.fromhex(sha))
            h, size = hashlib.sha256(), 0
            info = zipfile.ZipInfo(f"artifacts/{sha}", date_time=now.timetuple()[:6])
            info.compress_type = zipfile.ZIP_STORED
            with zf.open(info, "w", force_zip64=True) as dst, src.open("rb") as f:
                while chunk := f.read(_CHUNK):
                    h.update(chunk)
                    size += len(chunk)
                    dst.write(chunk)
            if h.hexdigest() != sha:
                problems.append(f"artifact {sha[:12]}: stored bytes hash to {h.hexdigest()[:12]}")
            files.append({"path": f"artifacts/{sha}", "sha256": h.hexdigest(), "bytes": size})
            manifest_artifacts.append({
                "sha256": sha, "path": f"artifacts/{sha}", "included": True, "media_type": meta["media_type"],
                "bytes": int(meta["byte_size"]), "role": "derived" if meta["parent_sha256"] else "original",
                "parent_sha256": bytes(meta["parent_sha256"]).hex() if meta["parent_sha256"] else None,
                "derivation": meta["derivation"], "first_seen_at": _iso(meta["first_seen_at"]),
                "relied_on_by_assertions": sorted(relied.get(sha, set()))})
        for sha in sorted(set(relied) - wanted - set(in_case)):
            manifest_artifacts.append({"sha256": sha, "included": False, "note": "cited, not held by this case"})

        # ---- capture log: acquisitions and what each search consulted
        capture = [{"kind": "acquisition", "sha256": bytes(r["sha256"]).hex(), "acquired_by": r["acquired_by"],
                    "acquired_from": r["acquired_from"], "context": r["acquisition_ctx"], "acquired_at": _iso(r["acquired_at"]),
                    "principal": str(r["acquired_principal"])} for r in acq if bytes(r["sha256"]).hex() in wanted]
        for r in conn.execute("SELECT * FROM search_run WHERE case_id=%s ORDER BY id", (case_id,)):
            capture.append({"kind": "search_run", "run_id": str(r["run_id"]), "ran_at": _iso(r["ran_at"]),
                            "plugin": r["plugin_id"], "version": r["plugin_version"], "query": r["query"],
                            "status": r["status"], "coverage": r["coverage"], "gaps": r["gaps"],
                            "assertions": r["assertion_count"]})

        # ---- tools and models
        used = {(a["derived_by"].get("plugin"), a["derived_by"].get("version")) for a in assertions}
        used |= {(c["plugin"], c["version"]) for c in capture if c["kind"] == "search_run"}
        plugins = []
        for pid, ver in sorted((p, v) for p, v in used if p):
            row = conn.execute("SELECT manifest, runtime, state, signature FROM plugin WHERE id=%s AND version=%s",
                               (pid, ver)).fetchone()
            plugins.append({"id": pid, "version": ver, "installed": bool(row),
                            **({"runtime": row["runtime"], "state": row["state"], "signature": row["signature"],
                                "manifest_sha256": hashlib.sha256(_j(row["manifest"]).encode()).hexdigest(),
                                "licence": (row["manifest"] or {}).get("licence") or (row["manifest"] or {}).get("license")}
                               if row else {})})
        backends: dict[str, int] = {}
        for a in assertions:
            backends[_j(a["derived_by"])] = backends.get(_j(a["derived_by"]), 0) + 1
        tools = {"platform": {"astral_lens": __version__, "python": sys.version.split()[0], "os": platform.platform()},
                 "plugins": plugins,
                 "derived_by": [{"derived_by": json.loads(k), "assertions": n} for k, n in sorted(backends.items())],
                 "ai_composers": sorted({r["result"].get("composer", "?") for r in runs}),
                 "note": "plugin and backend versions are those recorded on each assertion when it was made"}

        # ---- audit slice, re-verified
        rows = conn.execute("SELECT * FROM audit_log WHERE workspace_id=%s AND case_id=%s ORDER BY id",
                            (actor.workspace_id, case_id)).fetchall()
        audit_lines = []
        for r in rows:
            fields = audit._row_fields(r["workspace_id"], r["case_id"], r["at"], r["principal_id"], r["action"],
                                       r["target_kind"], r["target_id"], r["detail"])
            if audit._digest(bytes(r["prev_hash"]), fields) != bytes(r["hash"]):
                problems.append(f"audit entry {r['id']} does not recompute")
            audit_lines.append({"id": r["id"], "fields": fields, "prev_hash": bytes(r["prev_hash"]).hex(),
                                "hash": bytes(r["hash"]).hex()})
        head = conn.execute("SELECT id, hash FROM audit_log WHERE workspace_id=%s ORDER BY id DESC LIMIT 1",
                            (actor.workspace_id,)).fetchone()
        chain_ok, bad = audit.verify_chain(conn, actor.workspace_id)
        if not chain_ok:
            problems.append(f"the workspace audit chain breaks at entry {bad}")

        if problems and not allow_integrity_failures:
            zf.close()
            tmp.unlink(missing_ok=True)
            raise ExhibitError(f"{len(problems)} integrity problem(s); nothing was exported", problems)

        principal = conn.execute("SELECT name FROM principal WHERE id=%s", (actor.principal_id,)).fetchone()
        operator = {"principal_id": str(actor.principal_id), "name": principal["name"] if principal else None}
        gaps = _gaps(capture)
        text_files: dict[str, bytes] = {
            "assertions.jsonl": _jsonl(assertions), "capture_log.jsonl": _jsonl(capture),
            "tools.json": json.dumps(tools, indent=2, default=str).encode(), "audit.jsonl": _jsonl(audit_lines),
            "operator_statement.md": _statement(operator_statement, operator, now, case).encode(),
            "report.md": _report(conn, case_id, case, now, manifest_artifacts, active, assertions, gaps, runs, problems).encode(),
            "LIMITS.md": _LIMITS.encode(), "verify.py": _verifier_source().encode(),
        }
        if runs:
            text_files["ai_runs.jsonl"] = _jsonl(runs)
        if signing_key:
            text_files["signer.pub"] = signing.public_from_private(*signing_key)
        text_files["README.txt"] = _readme(case, problems).encode()
        for name, data in text_files.items():
            zf.writestr(zipfile.ZipInfo(name, date_time=now.timetuple()[:6]), data, compress_type=zipfile.ZIP_DEFLATED)
            files.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})

        manifest = {
            "schema": SCHEMA, "generated_at": now.isoformat(), "platform": f"astral-lens {__version__}",
            "case": {"id": str(case["id"]), "name": case["name"], "state": case["state"],
                     "created_at": _iso(case["created_at"])},
            "operator": operator, "scope": "all artifacts in the case" if include_all_artifacts else
            "artifacts the case's unretracted assertions and included answers rely on, with their parents",
            "counts": {"artifacts_included": len(wanted), "assertions": len(assertions), "assertions_unretracted": len(active),
                       "audit_entries": len(audit_lines), "ai_answers": len(runs)},
            "audit": {"workspace_head_id": head["id"] if head else None,
                      "workspace_head_hash": bytes(head["hash"]).hex() if head else None,
                      "note": "the slice holds this case's entries only; compare the head hash with the live system to show "
                              "the log was not rewound after export. The export itself is recorded after this slice was taken."},
            "integrity": {"ok": not problems, "problems": problems},
            "artifacts": manifest_artifacts, "files": sorted(files, key=lambda f: f["path"]),
            "signed": bool(signing_key)}
        mbytes = json.dumps(manifest, indent=2, sort_keys=True, default=str).encode()
        zf.writestr(zipfile.ZipInfo("MANIFEST.json", date_time=now.timetuple()[:6]), mbytes, compress_type=zipfile.ZIP_DEFLATED)
        sums = "".join(f"{f['sha256']}  {f['path']}\n" for f in sorted(files, key=lambda f: f["path"])) + \
               f"{hashlib.sha256(mbytes).hexdigest()}  MANIFEST.json\n"
        zf.writestr(zipfile.ZipInfo("SHA256SUMS", date_time=now.timetuple()[:6]), sums.encode(), compress_type=zipfile.ZIP_DEFLATED)
        if signing_key:
            pub = text_files["signer.pub"]
            sig = {"algorithm": "ed25519", "signs": "MANIFEST.json", "signature": signing.sign(signing_key[0], mbytes, signing_key[1]),
                   "key_fingerprint": signing.fingerprint(pub)}
            zf.writestr(zipfile.ZipInfo("MANIFEST.sig", date_time=now.timetuple()[:6]), json.dumps(sig, indent=2).encode(),
                        compress_type=zipfile.ZIP_DEFLATED)
    tmp.replace(zpath)
    bundle_sha = hashlib.sha256(zpath.read_bytes()).hexdigest() if zpath.stat().st_size < (1 << 30) else None
    audit_id = audit.record(conn, actor, "exhibit.export", case_id=case_id, target_kind="case", target_id=str(case_id),
                            detail={"bundle_sha256": bundle_sha, "artifacts": len(wanted), "assertions": len(assertions),
                                    "ai_answers": len(runs), "signed": bool(signing_key), "integrity_ok": not problems})
    return {"path": str(zpath), "bytes": zpath.stat().st_size, "bundle_sha256": bundle_sha, "audit_id": audit_id,
            "counts": manifest["counts"], "integrity_ok": not problems, "problems": problems, "signed": bool(signing_key)}


# ------------------------------------------------------------------ pieces
def _jsonl(rows: list[Any]) -> bytes:
    return ("".join(json.dumps(r, sort_keys=True, default=str, ensure_ascii=False) + "\n" for r in rows)).encode()


def _verifier_source() -> str:
    return (Path(__file__).with_name("exhibit_verify.py")).read_text(encoding="utf-8")


def _gaps(capture: list[dict[str, Any]]) -> list[str]:
    g = []
    for c in capture:
        if c["kind"] != "search_run":
            continue
        for cov in c["coverage"]:
            if cov["outcome"] == "unknown":
                g.append(f"{c['plugin']} ({c['ran_at'][:19]}): {cov['source']} could not be consulted — {cov['note']}")
        for x in c["gaps"]:
            g.append(f"{c['plugin']} ({c['ran_at'][:19]}): {x}")
    return g


def _statement(text: str, operator: dict[str, Any], now: datetime, case: dict[str, Any]) -> str:
    return (f"# Operator statement\n\nCase: {case['name']} ({case['id']})  \nExport made: {now.isoformat()}  \n"
            f"Exported by: {operator['name'] or 'unknown'} (principal {operator['principal_id']})\n\n"
            "The text below is the operator's own account, recorded verbatim. The platform did not write it and cannot "
            "verify it.\n\n---\n\n" + text.strip() + "\n\n---\n\nSignature of operator: ______________________  Date: __________\n")


def _report(conn, case_id, case, now, arts, active, assertions, gaps, runs, problems) -> str:
    L = [f"# Case record: {case['name']}", "",
         f"Generated {now.isoformat()} by Astral Lens {__version__}. Case state: {case['state']}.", ""]
    if problems:
        L += ["> **INTEGRITY PROBLEMS WERE FOUND AND THIS PACKAGE WAS EXPORTED ANYWAY.** See MANIFEST.json.", ""]
        L += [f"> - {p}" for p in problems] + [""]
    L += ["## Basis", "",
          "Each statement below is something a *source said*, recorded with the evidence it came from. Grades follow the "
          "Admiralty system and are kept as two separate axes: **source reliability A–F** and **information credibility 1–6**; "
          "F and 6 mean \"cannot be judged\", not \"unreliable\". The platform does not use estimative-probability language and does "
          "not state how likely anything is. \"Independent sources\" counts distinct origins, so copies of one origin count once. "
          "Every statement lists the assertion number and the SHA-256 of its evidence, which are files in `artifacts/`.", ""]
    ents = conn.execute("SELECT cluster_id, schema_name, caption, member_count FROM proj_entity WHERE case_id=%s"
                        " ORDER BY member_count DESC, caption", (case_id,)).fetchall()
    cap = {e["cluster_id"]: e["caption"] or str(e["cluster_id"])[:8] for e in ents}
    L += ["## Entities and recorded facts", ""]
    if not ents:
        L.append("_No entities are recorded in this case._")
    for e in ents:
        props = conn.execute("SELECT predicate, value, assertion_id, source_grade, info_grade, independent_sources FROM proj_property"
                             " WHERE case_id=%s AND cluster_id=%s AND NOT (predicate = ANY(%s)) ORDER BY predicate, assertion_id",
                             (case_id, e["cluster_id"], list(SKIP_PREDICATES))).fetchall()
        if not props:
            continue
        L += [f"### {cap[e['cluster_id']]} _({e['schema_name'] or 'unknown type'}; {e['member_count']} record(s) judged the same)_", ""]
        for p in props:
            ev = next((a["evidence"] for a in active if a["id"] == p["assertion_id"]), [])
            v = p["value"] if not isinstance(p["value"], (dict, list)) else json.dumps(p["value"], ensure_ascii=False)
            L.append(f"- **{p['predicate']}**: {v}  \n  _source {p['source_grade']} / information {p['info_grade']}; "
                     f"{p['independent_sources']} independent source(s); assertion #{p['assertion_id']}; "
                     f"evidence {', '.join(dict.fromkeys(x[:12] for x in ev)) or 'none'}_")
        L.append("")
    edges = conn.execute("SELECT subject_cluster, predicate, object_cluster, assertion_id, source_grade, info_grade,"
                         " independent_sources FROM proj_edge WHERE case_id=%s ORDER BY assertion_id", (case_id,)).fetchall()
    L += ["## Relationships", ""]
    if not edges:
        L.append("_None recorded._")
    for x in edges:
        ev = next((a["evidence"] for a in active if a["id"] == x["assertion_id"]), [])
        L.append(f"- {cap.get(x['subject_cluster'], '?')} — **{x['predicate']}** → {cap.get(x['object_cluster'], '?')}  \n"
                 f"  _source {x['source_grade']} / information {x['info_grade']}; {x['independent_sources']} independent source(s); "
                 f"assertion #{x['assertion_id']}; evidence {', '.join(dict.fromkeys(y[:12] for y in ev)) or 'none'}_")
    conflicts = conn.execute("SELECT judgment_id, detail FROM proj_conflict WHERE case_id=%s", (case_id,)).fetchall()
    if conflicts:
        L += ["", "## Merges the analyst's own constraints refused", ""] + [f"- judgment {c['judgment_id']}: {c['detail']}" for c in conflicts]
    retracted = [a for a in assertions if a["retracted"]]
    L += ["", "## Gaps and limits of the collection", "",
          "A source that could not be consulted is **unknown**, which is different from \"nothing found\". "
          "Absence of a result is not evidence of absence.", ""]
    L += [f"- {g}" for g in gaps] or ["- No search in this case recorded a source it could not consult."]
    if retracted:
        L += ["", f"{len(retracted)} assertion(s) were later retracted and are excluded above; they remain in `assertions.jsonl` "
              "with the reason."]
    if runs:
        L += ["", "## Questions asked of the case and the checked answers", "",
              "Answers were produced from this case's evidence and each claim was checked against the stored text. A model, "
              "where one was used, is named; model output is not evidence and no assertion was written from it.", ""]
        for r in runs:
            body = ("\n" + (r["result"].get("report_md") or "")).replace("\n## ", "\n##### ").replace("\n# ", "\n#### ").lstrip("\n")
            L += [f"### Q: {r['question']}", "", f"_planned by {r['planner']}; approved {str(r['approved_at'])[:19]}_", "", body, ""]
    L += ["## Source appendix", "", "| sha256 | media type | bytes | role | acquired by | relied on by |", "|---|---|---|---|---|---|"]
    acqs = conn.execute("SELECT sha256, acquired_by, acquired_from FROM artifact_acquisition WHERE case_id=%s ORDER BY id",
                        (case_id,)).fetchall()
    by_sha: dict[str, list[str]] = {}
    for r in acqs:
        by_sha.setdefault(bytes(r["sha256"]).hex(), []).append(f"{r['acquired_by']}" + (f" from {r['acquired_from']}" if r["acquired_from"] else ""))
    for a in arts:
        if not a.get("included"):
            L.append(f"| {a['sha256']} | — | — | not included | — | — |")
            continue
        L.append(f"| {a['sha256']} | {a['media_type']} | {a['bytes']} | {a['role']} | {'; '.join(by_sha.get(a['sha256'], ['?']))[:90]} | "
                 f"{', '.join('#' + str(i) for i in a['relied_on_by_assertions'][:6]) or '—'} |")
    return "\n".join(L) + "\n"


def _readme(case: dict[str, Any], problems: list[str]) -> str:
    warn = ("*** INTEGRITY PROBLEMS WERE FOUND BEFORE EXPORT — see MANIFEST.json ***\n\n" if problems else "")
    return (warn + f"Astral Lens exhibit for case: {case['name']}\n\n"
            "TO CHECK THIS PACKAGE (no Astral Lens install needed):\n"
            "  1. Unzip it, then in that folder:  sha256sum -c SHA256SUMS      (every file against its recorded hash)\n"
            "  2. python verify.py .                                          (deeper checks: manifest, artifacts, audit entries,\n"
            "                                                                   AI citations, signature if present)\n"
            "  Prefer a copy of verify.py you obtained separately from this package.\n\n"
            "CONTENTS\n"
            "  report.md             human-readable record: facts with grades and evidence, relationships, gaps, source appendix\n"
            "  operator_statement.md the operator's own account\n"
            "  MANIFEST.json        every file and artifact with its SHA-256, counts, scope, integrity result\n"
            "  artifacts/<sha256>   the evidence, named by the hash of its bytes\n"
            "  assertions.jsonl     every assertion with grades, evidence hashes, who/what derived it, and any retraction\n"
            "  capture_log.jsonl    how and when each artifact was acquired; what each search consulted and could not\n"
            "  tools.json           platform, plugin and backend versions recorded on the assertions\n"
            "  audit.jsonl          this case's entries from the hash-chained audit log\n"
            "  ai_runs.jsonl        (if present) approved questions and checked answers\n"
            "  LIMITS.md            what this package does not establish. READ IT.\n\n"
            "This package was assembled by software. It makes no claim that it is admissible in any proceeding;\n"
            "that is for counsel and the court.\n")


_LIMITS = """# What this package does and does not establish

**It shows** that each file in `artifacts/` has the content hashed in the manifest; that the manifest and every file
are unchanged since the package was written (and, if a signature is present, that the holder of the signing key
produced exactly this manifest); and that each audit entry listed recomputes from its recorded predecessor.

**It does not show**

- that anything an artifact *says* is true. A recorded statement is what a source said, graded for source reliability
  and information credibility, not a finding of fact;
- what happened to an artifact before the system acquired it. A hash proves the bytes are unchanged *since*
  acquisition. Authentication of origin (for example, who captured a page and how) rests on the operator's statement
  and the capture log;
- that the search was complete. A source that could not be consulted is *unknown*. The report lists those gaps;
  absence of a result is not evidence of absence;
- continuity of the audit log across the whole workspace. Only this case's entries are exported. Entries of other
  cases sit between them, so entries are checked one by one, and the workspace head hash recorded in the manifest
  can be compared with the live system;
- authorship, unless the package is signed **and** you hold the signer's public key from a source you already
  trust. A key embedded in the package proves only that the package agrees with itself;
- that any model-assisted answer is correct. Where a model was used it is named; its output is not evidence, and each
  claim is shown with the stored text it was checked against. Without a model verifier the strongest check available
  is that the quoted text exists and shares its words with the claim;
- admissibility, authenticity under any rule of evidence, or fitness for any proceeding. The platform assembles the
  package and asserts nothing about admissibility; that is for counsel and the court.
"""
