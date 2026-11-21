"""Investigation workflows: orchestration of plugins into a result a person can
read. M1 ships `name_to_network` (acceptance scenario 3) and `ingest_document`.

The report is built around COVERAGE. Every source consulted appears in it with
its outcome: hits, no_hits, or unknown. A reader can always tell "this source
said nothing" from "this source could not be asked" (invariant 3).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import psycopg

from astral import projector, resolution
from astral.db import Actor
from astral.plugins import registry
from astral.plugins.host import PluginHost, RateLimitExceeded
from astral.plugins.manifest import PluginManifest
from astral.policy import ApprovalRequired


def query_plugins(conn: psycopg.Connection) -> list[tuple[PluginManifest, str]]:
    """Enabled plugins that consume a Query (connectors), newest version each."""
    rows = conn.execute("SELECT id, version, manifest FROM plugin WHERE state = 'enabled'"
                        " ORDER BY id, version").fetchall()
    best: dict[str, tuple[PluginManifest, str]] = {}
    for r in rows:
        m = PluginManifest.model_validate({k: v for k, v in r["manifest"].items() if not k.startswith("_")})
        if any(c.type == "Query" for c in m.consumes) and m.stage == "ingest":
            best[m.id] = (m, r["version"])
    return list(best.values())


def _missing(m: PluginManifest, inp: dict[str, Any]) -> list[str]:
    need: list[str] = []
    for c in m.consumes:
        if c.type == "Query":
            need += [f for f in c.required if not inp.get(f)]
    return need


def name_to_network(
    host: PluginHost, conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID, *,
    name: str, country: str | None = None, schema: str | None = None,
    plugin_inputs: dict[str, dict[str, Any]] | None = None,
    plugin_configs: dict[str, dict[str, Any]] | None = None,
    only: list[str] | None = None, propose: bool = True,
) -> dict[str, Any]:
    """Query every enabled connector for a name, rebuild the projection and
    propose (never decide) entity matches. Returns the coverage-first report."""
    run_id = uuid.uuid4()
    base = {"name": name, "country": country, "schema": schema}
    sources: list[dict[str, Any]] = []
    for manifest, version in query_plugins(conn):
        if only and manifest.id not in only:
            continue
        inp = {**{k: v for k, v in base.items() if v}, **((plugin_inputs or {}).get(manifest.id) or {})}
        entry: dict[str, Any] = {"plugin": manifest.id, "version": version}
        miss = _missing(manifest, inp)
        if miss:
            entry.update(status="skipped", coverage=[{
                "source": manifest.title, "outcome": "unknown",
                "note": f"not queried: needs {', '.join(miss)}"}], gaps=[], assertions=0)
        elif manifest.footprint == "target_contact":
            entry.update(status="skipped", coverage=[{
                "source": manifest.title, "outcome": "unknown",
                "note": "not queried: target_contact needs approval at the moment of the call"}],
                gaps=[], assertions=0)
        else:
            try:
                with conn.transaction():
                    res = host.invoke(conn, actor, case_id, manifest.id, inp, version=version,
                                      config=(plugin_configs or {}).get(manifest.id))
                entry.update(status=res.status, coverage=res.coverage, gaps=res.gaps,
                             assertions=len(res.assertion_ids), stats=res.stats)
                if not res.coverage and res.status in ("error", "partial"):
                    entry["coverage"] = [{"source": manifest.title, "outcome": "unknown",
                                          "note": "; ".join(res.gaps) or "plugin failed"}]
            except (registry.PluginError, RateLimitExceeded, ApprovalRequired, ValueError) as e:
                entry.update(status="error", coverage=[{"source": manifest.title, "outcome": "unknown",
                                                        "note": f"{type(e).__name__}: {e}"}],
                             gaps=[str(e)], assertions=0)
        sources.append(entry)
        conn.execute(
            "INSERT INTO search_run (workspace_id, case_id, run_id, ran_at, principal_id, plugin_id,"
            " plugin_version, query, status, coverage, gaps, assertion_count, stats)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (actor.workspace_id, case_id, run_id, datetime.now(timezone.utc), actor.principal_id,
             manifest.id, version, json.dumps(base), entry["status"],
             json.dumps(entry.get("coverage", [])), json.dumps(entry.get("gaps", [])),
             entry.get("assertions", 0), json.dumps(entry.get("stats", {}), default=str)))

    projection = projector.rebuild_case(conn, case_id)
    proposed = 0
    report = resolution.find_candidates(conn, case_id)
    if propose and report.candidates:
        proposed = len(resolution.propose(conn, actor, case_id, report.candidates))
    outcomes = [c["outcome"] for s in sources for c in s.get("coverage", [])]
    return {
        "run_id": str(run_id), "query": base, "sources": sources, "projection": projection,
        "candidates_proposed": proposed, "skipped_blocks": report.skipped_blocks,
        "coverage_summary": {"hits": outcomes.count("hits"), "no_hits": outcomes.count("no_hits"),
                             "unknown": outcomes.count("unknown")},
        "note": ("'no_hits' means a source answered with nothing; 'unknown' means it could not be "
                 "asked. Neither is evidence that the subject is absent."),
    }


def ingest_document(
    host: PluginHost, conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID, sha256: str,
    *, config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Parse a document, then extract entities from its text. Both steps are
    plugins, so each is graded, attributed and retractable like any other."""
    parse = host.invoke(conn, actor, case_id, "core.doc-ingest", {"artifact": sha256}, config=config)
    out: dict[str, Any] = {"parse": _summ(parse), "extract": None}
    if "text_artifact" in parse.stats:
        ner = host.invoke(conn, actor, case_id, "core.ner",
                          {"artifact": sha256, "text_artifact": parse.stats["text_artifact"]})
        out["extract"] = _summ(ner)
        out["text_artifact"] = parse.stats["text_artifact"]
    return out


def ingest_media(
    host: PluginHost, conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID, sha256: str,
    *, config: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Describe a media file, then do what its kind allows: transcribe speech and
    find entities in it, cut video into scenes with keyframes and follow detected
    objects through it, or read text and find objects in a still. Each step is a plugin, so each is graded and retractable. A plugin that
    is not installed, or a step that fails, is reported as skipped/error with its
    reason; the others still run."""
    config = config or {}
    steps: dict[str, Any] = {}

    def run(step: str, plugin: str, inp: dict[str, Any]):
        try:
            r = host.invoke(conn, actor, case_id, plugin, inp, config=config.get(plugin))
        except registry.PluginError as e:
            steps[step] = {"status": "skipped", "reason": str(e), "coverage": [], "stats": {}}
            return None
        steps[step] = _summ(r)
        return r

    probe = run("probe", "core.media-probe", {"artifact": sha256})
    kind = (probe.stats.get("kind") if probe else None)
    if kind in ("audio", "video"):
        tr = run("transcribe", "core.transcribe", {"artifact": sha256})
        if tr and "text_artifact" in tr.stats:
            run("extract", "core.ner", {"artifact": sha256, "text_artifact": tr.stats["text_artifact"]})
    if kind == "video":
        sc = run("scenes", "core.video-scenes", {"artifact": sha256})
        if sc:
            frames = sc.stats.get("keyframe_artifacts", [])[:50]
            agg = {"status": "ok", "faces": 0, "frames": len(frames), "coverage": [], "stats": {}}
            for kf, t_ms in frames:
                try:
                    r = host.invoke(conn, actor, case_id, "core.faces",
                                    {"artifact": kf, "frame_of": sha256, "frame_ms": t_ms}, config=config.get("core.faces"))
                except registry.PluginError as e:
                    steps["faces"] = {"status": "skipped", "reason": str(e), "coverage": [], "stats": {}}
                    agg = None
                    break
                agg["faces"] += r.stats.get("faces", 0)
                if r.status in ("error", "unknown"):
                    agg["status"] = "partial"
                    agg["coverage"] = agg["coverage"] or r.coverage
            if agg:
                steps["faces"] = {**agg, "stats": {"faces": agg["faces"], "keyframes_scanned": agg["frames"]}}
        run("tracks", "core.video-tracks", {"artifact": sha256})
    if kind == "image":
        run("faces", "core.faces", {"artifact": sha256})
        run("objects", "core.objects", {"artifact": sha256})
        oc = run("ocr", "core.ocr", {"artifact": sha256})
        if oc and "text_artifact" in oc.stats:
            run("extract", "core.ner", {"artifact": sha256, "text_artifact": oc.stats["text_artifact"]})
    return {"kind": kind, "steps": steps}


def _summ(r) -> dict[str, Any]:
    return {"status": r.status, "assertions": len(r.assertion_ids), "coverage": r.coverage,
            "gaps": r.gaps, "stats": r.stats, "logs": r.logs}
