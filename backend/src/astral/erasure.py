"""Hard deletion: the one operation that genuinely destroys (10 §4).

Exists for legal obligation (GDPR erasure, BIPA destruction schedule, an
expiring warrant). It is deliberate, logged, case-scoped, and always leaves a
tombstone. Everywhere else, retraction — not deletion — is the rule
(invariant 2).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg

from astral import audit
from astral.db import Actor
from astral.evidence import EvidenceStore
from astral.policy import Policy, SoloPolicy
from astral.projector import drop_projections


@dataclass
class ErasurePlan:
    tombstone_id: uuid.UUID
    counts: dict[str, int]
    blob_shas: list[bytes]

    def finish(self, store: EvidenceStore) -> int:
        """Unlink blob files. Call only AFTER the erasing transaction commits."""
        return store.delete_blobs(self.blob_shas)


def erase_case(
    conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID, store: EvidenceStore,
    *, legal_basis: str, policy: Policy | None = None,
) -> ErasurePlan:
    (policy or SoloPolicy()).authorize(conn, actor, "case.erase", case_id)
    if not legal_basis.strip():
        raise ValueError("erasure requires a stated legal basis")
    case = conn.execute("SELECT state FROM case_ WHERE id = %s", (case_id,)).fetchone()
    if case["state"] == "erased":
        raise ValueError("case already erased")

    shas = [bytes(r["sha256"]) for r in conn.execute(
        "SELECT DISTINCT sha256 FROM artifact_acquisition WHERE case_id = %s", (case_id,))]

    conn.execute("SELECT set_config('astral.hard_delete', 'on', true)")
    try:
        counts: dict[str, int] = {}
        for table in ("assertion_retraction", "judgment_retraction", "judgment", "assertion",
                      "artifact_acquisition", "search_run", "face", "image_hash", "text_chunk", "ai_plan", "comment", "assignment", "graph_annotation", "graph_view", "worm_case", "service_sync", "text_embedding", "machine_run", "machine_schedule", "monitor_run", "monitor", "watch_term", "job", "role_grant"):
            counts[table] = conn.execute(
                f"DELETE FROM {table} WHERE case_id = %s", (case_id,)).rowcount
        drop_projections(conn, case_id)
        # A derived artifact blocks deletion of its parent, so gather the whole
        # ancestor chain first, then purge repeatedly until no more progress.
        candidates = set(shas)
        frontier = list(shas)
        while frontier:
            parents = {bytes(r["parent_sha256"]) for r in conn.execute(
                "SELECT parent_sha256 FROM artifact WHERE sha256 = ANY(%s)"
                " AND parent_sha256 IS NOT NULL", (frontier,))}
            frontier = list(parents - candidates)
            candidates |= parents
        removed: list[bytes] = []
        remaining = list(candidates)
        while remaining:
            gone = store.purge_unreferenced(conn, remaining)
            if not gone:
                break
            removed += gone
            remaining = [s for s in remaining if s not in set(gone)]
        counts["artifact_rows_removed"] = len(removed)
    finally:
        conn.execute("SELECT set_config('astral.hard_delete', '', true)")

    conn.execute(
        "UPDATE case_ SET state='erased', name='[erased]', retention_policy='{}',"
        " collection_identity='{}' WHERE id = %s", (case_id,))
    tid = uuid.uuid4()
    conn.execute(
        "INSERT INTO tombstone (id, workspace_id, case_id, erased_at, erased_by, legal_basis, counts)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (tid, actor.workspace_id, case_id, datetime.now(timezone.utc), actor.principal_id,
         legal_basis, json.dumps(counts)),
    )
    audit.record(conn, actor, "case.erase", case_id=case_id, target_kind="case",
                 target_id=str(case_id), detail={"legal_basis": legal_basis, "counts": counts})
    return ErasurePlan(tid, counts, removed)
