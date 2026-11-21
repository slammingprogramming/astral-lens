"""Projector: rebuild layers 2 and 3 from layers 0 and 1 (03 §1).

Everything it writes is disposable. A rebuild is deterministic: dropping every
projection and rebuilding yields identical content. That property is what the
M0 exit test checks.

M0 implementation loads a case into memory; a set-based SQL path can replace
it later without changing the contract.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import psycopg

PROJECTOR_VERSION = "m0-1"
_CLUSTER_NS = uuid.UUID("a3f2c0de-5c7a-4e0b-8a43-0c1d4b6a9e21")

PROJECTION_TABLES = (
    "proj_conflict", "proj_edge", "proj_property", "proj_entity",
    "proj_redirect", "proj_entity_member", "proj_state",
)


def cluster_id_for(min_member: uuid.UUID) -> uuid.UUID:
    """Canonical cluster id: deterministic from the smallest member id, so a
    rebuild never churns ids."""
    return uuid.uuid5(_CLUSTER_NS, str(min_member))


def drop_projections(conn: psycopg.Connection, case_id: uuid.UUID | None = None) -> None:
    """Drop layers 2-3 (all cases, or one). Always safe."""
    for t in PROJECTION_TABLES:
        if case_id is None:
            conn.execute(f"DELETE FROM {t}")
        else:
            conn.execute(f"DELETE FROM {t} WHERE case_id = %s", (case_id,))


# --------------------------------------------------------------- clustering
class _Clusters:
    def __init__(self) -> None:
        self.parent: dict[uuid.UUID, uuid.UUID] = {}
        self.members: dict[uuid.UUID, set[uuid.UUID]] = {}

    def add(self, e: uuid.UUID) -> None:
        if e not in self.parent:
            self.parent[e] = e
            self.members[e] = {e}

    def find(self, e: uuid.UUID) -> uuid.UUID:
        self.add(e)
        while self.parent[e] != e:
            self.parent[e] = self.parent[self.parent[e]]
            e = self.parent[e]
        return e

    def union(self, a: uuid.UUID, b: uuid.UUID) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if len(self.members[ra]) < len(self.members[rb]):
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.members[ra] |= self.members.pop(rb)

    def canonical(self, e: uuid.UUID) -> uuid.UUID:
        return min(self.members[self.find(e)])


def _resolve(
    judgments: list[dict[str, Any]], entities: set[uuid.UUID]
) -> tuple[_Clusters, dict[uuid.UUID, uuid.UUID], list[tuple[int, str]]]:
    """Connected components over `same` edges, `not_same` as a hard constraint.

    Returns (clusters, redirects, conflicts). A merge that would join a
    not_same pair is refused and reported — never silently dropped.
    """
    cl = _Clusters()
    for e in entities:
        cl.add(e)
    not_same = [(j["entity_a"], j["entity_b"]) for j in judgments if j["verdict"] == "not_same"]
    ever_canon: dict[uuid.UUID, uuid.UUID] = {}      # historical canonical member -> itself
    conflicts: list[tuple[int, str]] = []

    for j in judgments:
        if j["verdict"] != "same":
            continue
        a, b = j["entity_a"], j["entity_b"]
        ra, rb = cl.find(a), cl.find(b)
        if ra == rb:
            continue
        ma, mb = cl.members[ra], cl.members[rb]
        blocked = next(
            ((x, y) for x, y in not_same if (x in ma and y in mb) or (x in mb and y in ma)),
            None,
        )
        if blocked:
            conflicts.append((j["id"], f"refused: {blocked[0]} and {blocked[1]} are not_same"))
            continue
        ever_canon[min(ma)] = min(ma)
        ever_canon[min(mb)] = min(mb)
        cl.union(a, b)

    redirects = {
        cluster_id_for(old): cluster_id_for(cl.canonical(old))
        for old in ever_canon
        if cluster_id_for(old) != cluster_id_for(cl.canonical(old))
    }
    return cl, redirects, conflicts


# ------------------------------------------------------------------ rebuild
def rebuild_case(conn: psycopg.Connection, case_id: uuid.UUID) -> dict[str, int]:
    """Drop and rebuild every projection row for one case."""
    drop_projections(conn, case_id)

    assertions = conn.execute(
        "SELECT a.*, s.origin_key, s.id AS sid FROM assertion a JOIN source s ON s.id = a.source_id"
        " LEFT JOIN assertion_retraction r ON r.assertion_id = a.id"
        " WHERE a.case_id = %s AND r.id IS NULL ORDER BY a.id",
        (case_id,),
    ).fetchall()
    judgments = conn.execute(
        "SELECT j.* FROM judgment j LEFT JOIN judgment_retraction r ON r.judgment_id = j.id"
        " WHERE j.case_id = %s AND r.id IS NULL ORDER BY j.id",
        (case_id,),
    ).fetchall()
    high_a = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS m FROM assertion WHERE case_id = %s", (case_id,)
    ).fetchone()["m"]
    high_j = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS m FROM judgment WHERE case_id = %s", (case_id,)
    ).fetchone()["m"]

    entities: set[uuid.UUID] = set()
    for a in assertions:
        entities.add(a["subject"])
        if a["object_entity"]:
            entities.add(a["object_entity"])
    for j in judgments:
        entities.update((j["entity_a"], j["entity_b"]))

    cl, redirects, conflicts = _resolve(judgments, entities)
    cid = {e: cluster_id_for(cl.canonical(e)) for e in entities}

    # -- independence: collapse shared upstream origin before counting ------
    by_id = {a["id"]: a for a in assertions}
    memo: dict[int, frozenset[str]] = {}

    def origins(aid: int, stack: tuple[int, ...] = ()) -> frozenset[str]:
        if aid in memo:
            return memo[aid]
        a = by_id.get(aid)
        if a is None or aid in stack:               # retracted/unknown upstream: no origin
            return frozenset()
        ups = [u for u in a["derived_from"] if u in by_id]
        if not ups:
            res = frozenset({a["origin_key"] or f"source:{a['sid']}"})
        else:
            res = frozenset().union(*(origins(u, stack + (aid,)) for u in ups))
        memo[aid] = res
        return res

    groups: dict[tuple[uuid.UUID, str, str], set[str]] = {}
    for a in assertions:
        if a["object_entity"] is not None:
            key = (cid[a["subject"]], a["predicate"], str(cid[a["object_entity"]]))
        else:
            key = (cid[a["subject"]], a["predicate"], json.dumps(a["object_value"], sort_keys=True))
        groups.setdefault(key, set()).update(origins(a["id"]))

    # -- write ---------------------------------------------------------------
    for e, c in sorted(cid.items(), key=lambda kv: str(kv[0])):
        conn.execute(
            "INSERT INTO proj_entity_member (case_id, entity_id, cluster_id) VALUES (%s,%s,%s)",
            (case_id, e, c),
        )
    for old, cur in sorted(redirects.items(), key=lambda kv: str(kv[0])):
        conn.execute(
            "INSERT INTO proj_redirect (case_id, old_id, current_id) VALUES (%s,%s,%s)",
            (case_id, old, cur),
        )
    for jid, detail in conflicts:
        conn.execute(
            "INSERT INTO proj_conflict (case_id, judgment_id, detail) VALUES (%s,%s,%s)",
            (case_id, jid, detail),
        )

    props = edges = 0
    schema_of: dict[uuid.UUID, str] = {}
    name_of: dict[uuid.UUID, tuple[tuple[int, str, int], str]] = {}
    for a in assertions:
        subj = cid[a["subject"]]
        if a["object_entity"] is not None:
            obj = cid[a["object_entity"]]
            n = len(groups[(subj, a["predicate"], str(obj))])
            conn.execute(
                "INSERT INTO proj_edge (case_id, subject_cluster, predicate, object_cluster,"
                " assertion_id, source_grade, info_grade, independent_sources)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (case_id, subj, a["predicate"], obj, a["id"], a["source_grade"],
                 a["info_grade"], n),
            )
            edges += 1
            continue
        val = a["object_value"]
        n = len(groups[(subj, a["predicate"], json.dumps(val, sort_keys=True))])
        conn.execute(
            "INSERT INTO proj_property (case_id, cluster_id, predicate, value, assertion_id,"
            " source_grade, info_grade, independent_sources, valid_from, valid_to)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (case_id, subj, a["predicate"], json.dumps(val), a["id"], a["source_grade"],
             a["info_grade"], n, a["valid_from"], a["valid_to"]),
        )
        props += 1
        if a["predicate"] == "schema" and isinstance(val, str):
            schema_of.setdefault(subj, val)
        if a["predicate"] == "name" and isinstance(val, str):
            rank = (a["info_grade"], a["source_grade"], a["id"])
            if subj not in name_of or rank < name_of[subj][0]:
                name_of[subj] = (rank, val)

    for c in sorted({v for v in cid.values()}, key=str):
        members = sum(1 for v in cid.values() if v == c)
        conn.execute(
            "INSERT INTO proj_entity (case_id, cluster_id, schema_name, caption, member_count)"
            " VALUES (%s,%s,%s,%s,%s)",
            (case_id, c, schema_of.get(c), name_of.get(c, (None, None))[1], members),
        )

    conn.execute(
        "INSERT INTO proj_state (case_id, rebuilt_at, assertion_high, judgment_high,"
        " projector_version) VALUES (%s,%s,%s,%s,%s)",
        (case_id, datetime.now(timezone.utc), high_a, high_j, PROJECTOR_VERSION),
    )
    return {"entities": len(set(cid.values())), "properties": props, "edges": edges,
            "conflicts": len(conflicts), "redirects": len(redirects)}


def rebuild_all(conn: psycopg.Connection) -> dict[str, dict[str, int]]:
    """Drop every projection and rebuild every case."""
    drop_projections(conn)
    out = {}
    for r in conn.execute("SELECT id FROM case_ WHERE state <> 'erased' ORDER BY id").fetchall():
        out[str(r["id"])] = rebuild_case(conn, r["id"])
    return out


def snapshot(conn: psycopg.Connection, case_id: uuid.UUID) -> dict[str, list[tuple[Any, ...]]]:
    """Stable dump of a case's projections (excludes proj_state, whose
    rebuilt_at legitimately differs). Used to prove rebuild determinism."""
    out: dict[str, list[tuple[Any, ...]]] = {}
    for t in PROJECTION_TABLES:
        if t == "proj_state":
            continue
        rows = conn.execute(f"SELECT * FROM {t} WHERE case_id = %s", (case_id,)).fetchall()
        out[t] = sorted((tuple(sorted((k, str(v)) for k, v in r.items())) for r in rows))
    return out
