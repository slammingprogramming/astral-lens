"""Monitors (saved searches with diff views) and watch-term cross-referencing.

A monitor re-runs a name search. Each run records the facts its sources reported (keyed by
plugin, subject, predicate and object, so re-reporting an unchanged fact is not news) and
whether each source could be asked. The diff against the previous run keeps the project's
two rules about absence:

  * "no longer reported" is claimed ONLY for a source that answered both times. A source
    that could not be consulted this time is listed as NOT ASKED, never as "gone";
  * the append-only log means an old assertion is never deleted because a source stopped
    saying it; the diff is about what sources *reported*, not about what is true.

Watch terms are names and identifiers an analyst wants flagged wherever they occur in the
case. Name matching is fuzzy and a match is a CANDIDATE: similar is not the same person.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from rapidfuzz import fuzz

from astral import audit, investigate, retrieval
from astral.db import Actor
from astral.evidence import EvidenceStore

NAME_THRESHOLD = 90
QUERY_KEYS = {"name", "country", "schema", "only"}


class MonitorError(Exception):
    pass


# ---------------------------------------------------------------- monitors
def create(conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID, name: str, query: dict[str, Any],
           interval_minutes: int | None = None) -> uuid.UUID:
    q = {k: v for k, v in query.items() if k in QUERY_KEYS and v}
    if not q.get("name"):
        raise MonitorError("a monitor needs a name to search for")
    mid = uuid.uuid4()
    conn.execute("INSERT INTO monitor (id, workspace_id, case_id, name, query, interval_minutes, created_by)"
                 " VALUES (%s,%s,%s,%s,%s,%s,%s)",
                 (mid, actor.workspace_id, case_id, name, json.dumps(q), interval_minutes, actor.principal_id))
    audit.record(conn, actor, "monitor.create", case_id=case_id, target_kind="monitor", target_id=str(mid),
                 detail={"interval_minutes": interval_minutes})                     # the searched name stays out of the audit log
    return mid


def _fact_key(plugin: str, r: dict[str, Any]) -> str:
    obj = str(r["object_entity"]) if r["object_entity"] else json.dumps(r["object_value"], sort_keys=True, default=str)
    return f"{plugin}|{r['subject']}|{r['predicate']}|{obj}"


def run(host: Any, conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID, monitor_id: uuid.UUID,
        store: EvidenceStore | None = None) -> dict[str, Any]:
    mon = conn.execute("SELECT * FROM monitor WHERE id=%s AND case_id=%s", (monitor_id, case_id)).fetchone()
    if not mon:
        raise MonitorError("no such monitor")
    runs = conn.execute("SELECT facts, coverage, watch FROM monitor_run WHERE monitor_id=%s ORDER BY id DESC LIMIT 30",
                        (monitor_id,)).fetchall()
    prev = runs[0] if runs else None
    high = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM assertion WHERE case_id=%s", (case_id,)).fetchone()["m"]
    q = mon["query"]
    report = investigate.name_to_network(host, conn, actor, case_id, name=q["name"], country=q.get("country"),
                                         schema=q.get("schema"), only=q.get("only"), propose=True)

    coverage: dict[str, str] = {}
    for s in report["sources"]:
        asked = s["status"] in ("ok", "partial") and not any(c["outcome"] == "unknown" for c in s.get("coverage", []))
        coverage[s["plugin"]] = "answered" if asked else "not_asked"
    rows = conn.execute(
        "SELECT a.id, a.derived_by ->> 'plugin' AS plugin, a.subject, a.predicate, a.object_entity, a.object_value"
        " FROM assertion a WHERE a.case_id=%s AND a.id > %s AND NOT EXISTS"
        " (SELECT 1 FROM assertion_retraction r WHERE r.assertion_id = a.id) ORDER BY a.id", (case_id, high)).fetchall()
    caption = {r["entity_id"]: r["caption"] for r in conn.execute(
        "SELECT m.entity_id, e.caption FROM proj_entity_member m JOIN proj_entity e"
        " ON e.case_id=m.case_id AND e.cluster_id=m.cluster_id WHERE m.case_id=%s", (case_id,))}
    facts: dict[str, dict[str, str]] = {p: {} for p in coverage}
    for r in rows:
        if r["plugin"] not in facts:
            continue                                   # produced by a plugin this monitor did not run
        subj = caption.get(r["subject"]) or str(r["subject"])[:8]
        obj = (caption.get(r["object_entity"]) or str(r["object_entity"])[:8]) if r["object_entity"] else \
            (r["object_value"] if not isinstance(r["object_value"], (dict, list)) else json.dumps(r["object_value"]))
        facts[r["plugin"]][_fact_key(r["plugin"], r)] = f"{subj} — {r['predicate']}: {obj}"

    diff = _diff(runs, facts, coverage)
    watch_now = sorted(m["key"] for m in cross_reference(conn, actor, case_id, store)) if store else []
    prev_watch = set(prev["watch"]) if prev else set()
    diff["new_watch_matches"] = [k for k in watch_now if k not in prev_watch]
    conn.execute("INSERT INTO monitor_run (workspace_id, case_id, monitor_id, run_id, ran_at, facts, coverage, diff, watch)"
                 " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                 (actor.workspace_id, case_id, monitor_id, uuid.UUID(report["run_id"]), datetime.now(timezone.utc),
                  json.dumps(facts), json.dumps(coverage), json.dumps(diff), json.dumps(watch_now)))
    audit.record(conn, actor, "monitor.run", case_id=case_id, target_kind="monitor", target_id=str(monitor_id),
                 detail={"new": len(diff["new_facts"]), "no_longer_reported": len(diff["no_longer_reported"]),
                         "not_asked": len(diff["not_asked_this_time"])})
    return {"monitor_id": str(monitor_id), "run_id": report["run_id"], "coverage": coverage, "diff": diff,
            "coverage_summary": report["coverage_summary"], "first_run": diff["first_run"]}


def _baseline(runs: list[dict[str, Any]], plugin: str) -> dict[str, str] | None:
    """What this source reported the last time it ANSWERED. A run in which it could not be asked
    says nothing about its facts, so it is skipped, not treated as an empty answer."""
    for r in runs:
        if r["coverage"].get(plugin) == "answered":
            return r["facts"].get(plugin, {})
    return None


def _diff(runs: list[dict[str, Any]], facts: dict[str, dict[str, str]], coverage: dict[str, str]) -> dict[str, Any]:
    if not runs:
        return {"first_run": True, "new_facts": [{"plugin": p, "key": k, "label": v} for p, d in facts.items() for k, v in d.items()],
                "no_longer_reported": [], "not_asked_this_time": [p for p, c in coverage.items() if c == "not_asked"],
                "coverage_changes": [], "new_watch_matches": [],
                "note": "first run: everything reported is new by definition"}
    prev = runs[0]
    new, gone, not_asked, changes = [], [], [], []
    for plugin, now in facts.items():
        base = _baseline(runs, plugin)
        if coverage[plugin] == "answered":
            new += [{"plugin": plugin, "key": k, "label": v} for k, v in now.items() if base is None or k not in base]
            if base is not None:
                gone += [{"plugin": plugin, "key": k, "label": v} for k, v in base.items() if k not in now]
        else:
            not_asked.append(plugin)                   # unknown is not negative: nothing is gone, nothing is confirmed
        was = prev["coverage"].get(plugin)
        if was and was != coverage[plugin]:
            changes.append({"plugin": plugin, "was": was, "now": coverage[plugin]})
    for plugin in prev["facts"]:
        if plugin not in facts:
            changes.append({"plugin": plugin, "was": prev["coverage"].get(plugin), "now": "not_run"})
    return {"first_run": False, "new_facts": new, "no_longer_reported": gone, "not_asked_this_time": not_asked,
            "coverage_changes": changes, "new_watch_matches": [],
            "note": "'no longer reported' means a source that answered both times stopped reporting it; it is not evidence the "
                    "fact was false, and nothing was deleted from the record. A source that could not be asked is compared "
                    "with the last time it did answer."}


def due(conn: psycopg.Connection, now: datetime | None = None) -> list[dict[str, Any]]:
    """Active monitors with an interval whose last run is older than it (or that never ran)."""
    now = now or datetime.now(timezone.utc)
    out = []
    for m in conn.execute("SELECT id, case_id, workspace_id, interval_minutes, created_by FROM monitor"
                          " WHERE active AND interval_minutes IS NOT NULL").fetchall():
        last = conn.execute("SELECT MAX(ran_at) AS t FROM monitor_run WHERE monitor_id=%s", (m["id"],)).fetchone()["t"]
        if last is None or now - last >= timedelta(minutes=m["interval_minutes"]):
            out.append(m)
    return out


# ------------------------------------------------------------- watch terms
def add_term(conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID, kind: str, value: str, note: str = "") -> int:
    if kind not in ("name", "identifier"):
        raise MonitorError("kind must be 'name' or 'identifier'")
    try:
        tid = conn.execute("INSERT INTO watch_term (workspace_id, case_id, kind, value, note, created_by)"
                           " VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                           (actor.workspace_id, case_id, kind, value.strip(), note, actor.principal_id)).fetchone()["id"]
    except psycopg.errors.UniqueViolation as e:
        raise MonitorError("that term is already watched") from e
    audit.record(conn, actor, "watch.add", case_id=case_id, target_kind="watch_term", target_id=str(tid),
                 detail={"kind": kind})                                           # the term itself stays out of the audit log
    return tid


def _norm(s: str) -> str:
    return re.sub(r"[\s\-.]", "", s).lower()


def cross_reference(conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID, store: EvidenceStore) -> list[dict[str, Any]]:
    """Where each watched term appears in this case: in entities and in document text."""
    terms = conn.execute("SELECT id, kind, value, note FROM watch_term WHERE case_id=%s ORDER BY id", (case_id,)).fetchall()
    if not terms:
        return []
    retrieval.ensure_indexed(conn, store, actor, case_id)
    ents = conn.execute("SELECT cluster_id, caption FROM proj_entity WHERE case_id=%s", (case_id,)).fetchall()
    names: dict[Any, set[str]] = {}
    for p in conn.execute("SELECT cluster_id, value FROM proj_property WHERE case_id=%s AND predicate IN ('name','alias','previousName')",
                          (case_id,)):
        if isinstance(p["value"], str):
            names.setdefault(p["cluster_id"], set()).add(p["value"])
    props = conn.execute("SELECT cluster_id, predicate, value FROM proj_property WHERE case_id=%s", (case_id,)).fetchall()
    out: list[dict[str, Any]] = []
    for t in terms:
        hits: list[dict[str, Any]] = []
        if t["kind"] == "name":
            for e in ents:
                cands = names.get(e["cluster_id"], set()) | ({e["caption"]} if e["caption"] else set())
                best = max((fuzz.token_set_ratio(t["value"], c) for c in cands), default=0)
                if best >= NAME_THRESHOLD:
                    hits.append({"kind": "entity", "cluster_id": str(e["cluster_id"]), "caption": e["caption"], "similarity": best})
            like = f"%{t['value']}%"
        else:
            want = _norm(t["value"])
            for p in props:
                if isinstance(p["value"], str) and _norm(p["value"]) == want:
                    cap = next((e["caption"] for e in ents if e["cluster_id"] == p["cluster_id"]), None)
                    hits.append({"kind": "entity", "cluster_id": str(p["cluster_id"]), "caption": cap, "via": p["predicate"],
                                 "similarity": 100})
            like = f"%{t['value']}%"
        for c in conn.execute("SELECT text_sha, start_char, body FROM text_chunk WHERE case_id=%s AND body ILIKE %s LIMIT 20",
                              (case_id, like)):
            i = c["body"].lower().find(t["value"].lower())
            if i >= 0:
                s = c["start_char"] + i
                hits.append({"kind": "text", "text_artifact": bytes(c["text_sha"]).hex(), "start": s, "end": s + len(t["value"]),
                             "snippet": c["body"][max(0, i - 60): i + len(t["value"]) + 60]})
        seen: set[str] = set()
        for h in hits:
            key = f"{t['id']}|{h['kind']}|" + (h.get("cluster_id") or f"{h['text_artifact']}:{h['start']}")
            if key in seen:
                continue
            seen.add(key)
            out.append({"key": key, "term_id": t["id"], "term_kind": t["kind"], "term": t["value"], "note": t["note"], **h,
                        "caveat": "a candidate: a similar name or a shared string is not the same person"
                        if t["kind"] == "name" else "exact identifier match in the case's data or text"})
    return out
