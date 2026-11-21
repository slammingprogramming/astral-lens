"""Machines: multi-step transform chains over graph entities (the Maltego idea, with this project's brakes).

A machine is data: an ordered list of stages, each naming a connector (a "transform"), which entity
types it applies to and where its inputs come from (the seeds, or what the previous stage found). It
does not run when selected. Running one first PROPOSES a run that shows the most it could do (stages,
connectors, a ceiling on calls); a person approves it, and decides at that moment whether connectors
that touch the target are allowed. A machine definition cannot grant itself that permission, so a
machine received from someone else is never able to contact a subject on its own say-so.

Scheduling is the same idea over time: creating a schedule IS the approval, and scheduled runs never
use target_contact connectors, because nobody is present to approve them when they fire.

Limits are enforced here and not left to the definition: at most 8 stages, 100 calls per run, 25
entities per stage. A source that cannot be asked, or lacks configuration, is recorded as unknown in
the trace and the run continues; it is never reported as an empty result.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

from astral import audit, lens, workbench
from astral.db import Actor
from astral.plugins.host import PluginHost
from astral.plugins.trust import TrustStore
from astral.policy import ApprovalRequired

SCHEMA = "astral.machine/v1"
PACKAGE = "astral.machine-package/v1"
MAX_STAGES, MAX_CALLS, MAX_PER_STAGE = 8, 100, 25
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SCHEMA_NAME = re.compile(r"^[A-Z][A-Za-z]{2,40}$")
_PLUGIN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,80}$")


class MachineError(ValueError):
    pass


def validate(defn: Any) -> dict[str, Any]:
    if not isinstance(defn, dict) or defn.get("schema") != SCHEMA:
        raise MachineError(f"not a {SCHEMA} definition")
    if not _NAME.match(str(defn.get("machine", ""))):
        raise MachineError("machine name must be lowercase letters, digits, '.', '_' or '-' (max 64)")
    if not isinstance(defn.get("version"), int) or defn["version"] < 1:
        raise MachineError("version must be an integer >= 1")
    if len(str(defn.get("description", ""))) > 500:
        raise MachineError("description is limited to 500 characters")
    seed = defn.get("seed") or {}
    if not isinstance(seed.get("schemas"), list) or not seed["schemas"] or not all(_SCHEMA_NAME.match(str(s)) for s in seed["schemas"]):
        raise MachineError("seed.schemas must list one or more schema names, e.g. [\"Company\"]")
    stages = defn.get("stages")
    if not isinstance(stages, list) or not 1 <= len(stages) <= MAX_STAGES:
        raise MachineError(f"a machine has 1-{MAX_STAGES} stages")
    ids = set()
    for i, s in enumerate(stages):
        if not isinstance(s, dict) or not re.fullmatch(r"[a-z0-9_]{1,20}", str(s.get("id", ""))) or s["id"] in ids:
            raise MachineError(f"stage {i}: needs a unique short id")
        ids.add(s["id"])
        if not _PLUGIN.match(str(s.get("transform", ""))):
            raise MachineError(f"stage {s['id']}: transform must be a connector id")
        on = s.get("on")
        if not isinstance(on, list) or not on or not all(_SCHEMA_NAME.match(str(x)) for x in on):
            raise MachineError(f"stage {s['id']}: 'on' must list schema names")
        if s.get("from", "seed" if i == 0 else "new") not in ("seed", "new", "all"):
            raise MachineError(f"stage {s['id']}: 'from' must be seed, new or all")
        if i == 0 and s.get("from") == "new":
            raise MachineError("the first stage has no previous stage to take new entities from")
        mx = s.get("max_entities", 10)
        if not isinstance(mx, int) or not 1 <= mx <= MAX_PER_STAGE:
            raise MachineError(f"stage {s['id']}: max_entities is 1-{MAX_PER_STAGE}")
        cfg = s.get("config", {})
        if not isinstance(cfg, dict) or any(not isinstance(v, (str, int, float, bool)) for v in cfg.values()):
            raise MachineError(f"stage {s['id']}: config values must be plain scalars")
        if "approved" in s or "allow_target_contact" in s:
            raise MachineError(f"stage {s['id']}: a machine cannot grant itself approval")
    mc = (defn.get("limits") or {}).get("max_calls", 30)
    if not isinstance(mc, int) or not 1 <= mc <= MAX_CALLS:
        raise MachineError(f"limits.max_calls is 1-{MAX_CALLS}")
    return defn


def digest(defn: dict[str, Any]) -> str:
    return lens.digest(defn)


def save(conn: psycopg.Connection, actor: Actor, defn: dict[str, Any], *, origin: str = "local",
         trust: dict[str, Any] | None = None) -> uuid.UUID:
    validate(defn)
    h = digest(defn)
    row = conn.execute("SELECT id, sha256 FROM machine WHERE workspace_id=%s AND name=%s AND version=%s",
                       (actor.workspace_id, defn["machine"], defn["version"])).fetchone()
    if row:
        if row["sha256"] == h:
            return row["id"]
        raise MachineError(f"{defn['machine']} v{defn['version']} already exists with different content; publish a new version")
    mid = uuid.uuid4()
    conn.execute("INSERT INTO machine (id, workspace_id, name, version, definition, sha256, origin, trust, created_by)"
                 " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                 (mid, actor.workspace_id, defn["machine"], defn["version"], json.dumps(defn), h, origin,
                  json.dumps(trust or {}), actor.principal_id))
    audit.record(conn, actor, "machine.save", target_kind="machine", target_id=f"{defn['machine']}@{defn['version']}",
                 detail={"origin": origin, "sha256": h, "trust": (trust or {}).get("status")})
    return mid


def get(conn: psycopg.Connection, actor: Actor, name: str, version: int | None = None) -> dict[str, Any]:
    rows = conn.execute("SELECT * FROM machine WHERE workspace_id=%s AND name=%s AND (%s::int IS NULL OR version=%s)"
                        " ORDER BY version DESC LIMIT 1", (actor.workspace_id, name, version, version)).fetchall()
    if not rows:
        raise workbench.NotFound(f"no machine {name}" + (f" v{version}" if version else ""))
    return rows[0]


def export(row: dict[str, Any], signing_key: tuple[bytes, str | None] | None = None) -> dict[str, Any]:
    return lens.export(row, signing_key, package=PACKAGE)


def verify_package(pkg: Any, trust: TrustStore | None) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        return lens.verify_package(pkg, trust, package=PACKAGE, validator=lambda d: _as_lens_error(d))
    except lens.LensError as e:
        raise MachineError(str(e)) from e


def _as_lens_error(d: dict[str, Any]) -> dict[str, Any]:
    try:
        return validate(d)
    except MachineError as e:
        raise lens.LensError(str(e)) from e


# ------------------------------------------------------------------ runs
def propose(conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID, name: str, seeds: list[uuid.UUID],
            version: int | None = None) -> dict[str, Any]:
    row = get(conn, actor, name, version)
    defn = row["definition"]
    if not seeds or len(seeds) > MAX_PER_STAGE:
        raise MachineError(f"give 1-{MAX_PER_STAGE} seed entities")
    seed_schemas = set(defn["seed"]["schemas"])
    for s in seeds:
        ent = conn.execute("SELECT schema_name FROM proj_entity WHERE case_id=%s AND cluster_id=%s", (case_id, s)).fetchone()
        if not ent:
            raise workbench.NotFound(f"no such entity {s}")
        if ent["schema_name"] not in seed_schemas:
            raise MachineError(f"{s} is a {ent['schema_name']}; this machine starts from {sorted(seed_schemas)}")
    cap = min(defn.get("limits", {}).get("max_calls", 30), MAX_CALLS)
    stages = [{"id": s["id"], "transform": s["transform"], "on": s["on"], "from": s.get("from", "seed" if i == 0 else "new"),
               "max_entities": s.get("max_entities", 10)} for i, s in enumerate(defn["stages"])]
    ceiling = min(cap, sum(s["max_entities"] for s in stages))
    plan = {"stages": stages, "max_calls": ceiling, "touches_target": [], "note":
            "this is the most the machine can do; it may do less. Connectors that contact the subject run only if you allow it when approving."}
    run_id = uuid.uuid4()
    conn.execute("INSERT INTO machine_run (id, workspace_id, case_id, machine_name, machine_version, seeds, status, trace, created_by)"
                 " VALUES (%s,%s,%s,%s,%s,%s,'proposed',%s,%s)",
                 (run_id, actor.workspace_id, case_id, name, row["version"], json.dumps([str(s) for s in seeds]),
                  json.dumps([{"plan": plan}]), actor.principal_id))
    audit.record(conn, actor, "machine.propose", case_id=case_id, target_kind="machine_run", target_id=str(run_id),
                 detail={"machine": name, "version": row["version"], "stages": len(stages)})
    return {"id": str(run_id), "status": "proposed", "machine": name, "version": row["version"], "plan": plan}


def approve(conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID, run_id: uuid.UUID, *, allow_target_contact: bool = False) -> None:
    n = conn.execute("UPDATE machine_run SET status='approved', approved_by=%s, allow_target_contact=%s"
                     " WHERE id=%s AND case_id=%s AND status='proposed'",
                     (actor.principal_id, allow_target_contact, run_id, case_id)).rowcount
    if not n:
        raise MachineError("no such proposed run (it may already have been approved)")
    audit.record(conn, actor, "machine.approve", case_id=case_id, target_kind="machine_run", target_id=str(run_id),
                 detail={"allow_target_contact": allow_target_contact})


def run(host: PluginHost, conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM machine_run WHERE id=%s AND case_id=%s", (run_id, case_id)).fetchone()
    if not row:
        raise workbench.NotFound("no such run")
    if row["status"] != "approved":
        raise MachineError(f"run is {row['status']}; only an approved run can start")
    defn = get(conn, actor, row["machine_name"], row["machine_version"])["definition"]
    conn.execute("UPDATE machine_run SET status='running' WHERE id=%s", (run_id,))
    allow = row["allow_target_contact"]
    budget = min(defn.get("limits", {}).get("max_calls", 30), MAX_CALLS)
    seeds = [uuid.UUID(s) for s in row["seeds"]]
    trace: list[dict[str, Any]] = [row["trace"][0]] if row["trace"] else []
    done: set[tuple[str, str]] = set()
    previous_new: list[uuid.UUID] = []
    calls = 0
    try:
        for i, st in enumerate(defn["stages"]):
            source = st.get("from", "seed" if i == 0 else "new")
            pool = seeds if source == "seed" else previous_new if source == "new" else [
                r["cluster_id"] for r in conn.execute("SELECT cluster_id FROM proj_entity WHERE case_id=%s", (case_id,))]
            chosen = []
            for cid in pool:
                ent = conn.execute("SELECT schema_name FROM proj_entity WHERE case_id=%s AND cluster_id=%s", (case_id, cid)).fetchone()
                if ent and ent["schema_name"] in st["on"] and cid not in chosen:
                    chosen.append(cid)
            chosen = chosen[: st.get("max_entities", 10)]
            found_here: list[uuid.UUID] = []
            if not chosen:
                trace.append({"stage": st["id"], "transform": st["transform"], "event": "no matching entities",
                              "note": "nothing to ask; this says nothing about the subject"})
            for cid in chosen:
                if calls >= budget:
                    trace.append({"stage": st["id"], "transform": st["transform"], "entity": str(cid), "outcome": "unknown",
                                  "note": f"not run: the call limit ({budget}) was reached"})
                    continue
                try:
                    res = workbench.run_transform(host, conn, actor, case_id, cid, st["transform"], config=st.get("config") or None,
                                                  approved=allow)
                except ApprovalRequired:
                    trace.append({"stage": st["id"], "transform": st["transform"], "entity": str(cid), "outcome": "unknown",
                                  "note": "not run: this connector contacts the subject and the run was not approved to do that"})
                    continue
                except (LookupError, workbench.WorkbenchError) as e:
                    trace.append({"stage": st["id"], "transform": st["transform"], "entity": str(cid), "outcome": "unknown", "note": str(e)})
                    continue
                key = (st["transform"], json.dumps(res["input"], sort_keys=True))
                calls += 1
                done.add(key)
                found_here += [uuid.UUID(n["id"]) for n in res["new_entities"]]
                trace.append({"stage": st["id"], "transform": st["transform"], "entity": str(cid), "status": res["status"],
                              "input": res["input"], "coverage": res["coverage"], "new_entities": len(res["new_entities"]),
                              "new_edges": res["new_edges"]})
            previous_new = found_here
        status = "done"
    except Exception as e:                                          # the trace so far is kept
        trace.append({"event": "failed", "error": f"{type(e).__name__}: {e}"})
        status = "failed"
    conn.execute("UPDATE machine_run SET status=%s, trace=%s, finished_at=now() WHERE id=%s", (status, json.dumps(trace, default=str), run_id))
    audit.record(conn, actor, "machine.run", case_id=case_id, target_kind="machine_run", target_id=str(run_id),
                 detail={"status": status, "calls": calls})
    outcomes = [c["outcome"] for t in trace for c in t.get("coverage", [])] + [t["outcome"] for t in trace if t.get("outcome")]
    return {"id": str(run_id), "status": status, "calls": calls, "trace": trace,
            "coverage_summary": {k: outcomes.count(k) for k in ("hits", "no_hits", "unknown")}}


def get_run(conn: psycopg.Connection, case_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any]:
    r = conn.execute("SELECT * FROM machine_run WHERE id=%s AND case_id=%s", (run_id, case_id)).fetchone()
    if not r:
        raise workbench.NotFound("no such run")
    return {"id": str(r["id"]), "machine": r["machine_name"], "version": r["machine_version"], "status": r["status"],
            "seeds": r["seeds"], "allow_target_contact": r["allow_target_contact"], "trace": r["trace"],
            "created_at": r["created_at"].isoformat(), "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None}


# ------------------------------------------------------------------ schedules
def schedule(conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID, name: str, seeds: list[uuid.UUID], interval_minutes: int) -> uuid.UUID:
    get(conn, actor, name)
    if interval_minutes < 5:
        raise MachineError("the shortest interval is 5 minutes")
    sid = uuid.uuid4()
    conn.execute("INSERT INTO machine_schedule (id, workspace_id, case_id, machine_name, seeds, interval_minutes, created_by)"
                 " VALUES (%s,%s,%s,%s,%s,%s,%s)",
                 (sid, actor.workspace_id, case_id, name, json.dumps([str(s) for s in seeds]), interval_minutes, actor.principal_id))
    audit.record(conn, actor, "machine.schedule", case_id=case_id, target_kind="machine_schedule", target_id=str(sid),
                 detail={"machine": name, "interval_minutes": interval_minutes, "approves_passive_runs": True})
    return sid


def list_schedules(conn: psycopg.Connection, case_id: uuid.UUID) -> list[dict[str, Any]]:
    return [{"id": str(r["id"]), "machine": r["machine_name"], "seeds": r["seeds"], "interval_minutes": r["interval_minutes"],
             "enabled": r["enabled"], "last_run_at": r["last_run_at"].isoformat() if r["last_run_at"] else None}
            for r in conn.execute("SELECT * FROM machine_schedule WHERE case_id=%s ORDER BY created_at", (case_id,))]


def set_schedule_enabled(conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID, sid: uuid.UUID, enabled: bool) -> None:
    """Disable (or re-enable) a schedule. It is never silently deleted: the row stays as the record of what was scheduled."""
    n = conn.execute("UPDATE machine_schedule SET enabled=%s WHERE id=%s AND case_id=%s", (enabled, sid, case_id)).rowcount
    if not n:
        raise workbench.NotFound("no such schedule in this case")
    audit.record(conn, actor, "machine.schedule_enable" if enabled else "machine.schedule_disable", case_id=case_id,
                 target_kind="machine_schedule", target_id=str(sid))


def run_due(host: PluginHost, conn: psycopg.Connection, now: datetime | None = None) -> list[dict[str, Any]]:
    """Run every due schedule, passive connectors only, as the person who created it (so their rights apply).
    Creating the schedule was the approval."""
    now = now or datetime.now(timezone.utc)
    out = []
    for s in conn.execute("SELECT * FROM machine_schedule WHERE enabled").fetchall():
        actor = Actor(s["workspace_id"], s["created_by"])
        if s["last_run_at"] and s["last_run_at"] + timedelta(minutes=s["interval_minutes"]) > now:
            continue
        try:
            p = propose(conn, actor, s["case_id"], s["machine_name"], [uuid.UUID(x) for x in s["seeds"]])
            approve(conn, actor, s["case_id"], uuid.UUID(p["id"]), allow_target_contact=False)
            res = run(host, conn, actor, s["case_id"], uuid.UUID(p["id"]))
            out.append({"schedule": str(s["id"]), "run": res["id"], "status": res["status"]})
        except (MachineError, LookupError, PermissionError) as e:
            out.append({"schedule": str(s["id"]), "error": str(e)})
        conn.execute("UPDATE machine_schedule SET last_run_at=%s WHERE id=%s", (now, s["id"]))
    return out
