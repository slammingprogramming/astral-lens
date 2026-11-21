"""Entity-resolution judgments (03 §5). Append-only edges between entity
references; records are never physically merged and undoing a merge is
retracting a judgment."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import psycopg

from astral import audit
from astral.assertions import AssertionRejected
from astral.db import Actor
from astral.policy import Policy, SoloPolicy

VERDICTS = ("same", "not_same", "undecided")


def record_judgment(
    conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID, entity_a: uuid.UUID,
    entity_b: uuid.UUID, verdict: str, *, comparison: dict[str, Any] | None = None,
    reasoning: str | None = None, policy: Policy | None = None,
) -> int:
    (policy or SoloPolicy()).authorize(conn, actor, "judgment.write", case_id)
    if verdict not in VERDICTS:
        raise AssertionRejected(f"verdict must be one of {VERDICTS}")
    if actor.kind == "agent" and verdict != "undecided":
        # invariant 10: the AI layer may never merge two identities. It may
        # propose a candidate pair; a human decides.
        raise AssertionRejected("an agent may propose (undecided) but never decide a judgment")
    if entity_a == entity_b:
        raise AssertionRejected("a judgment needs two distinct entities")
    row = conn.execute(
        "INSERT INTO judgment (workspace_id, case_id, entity_a, entity_b, verdict, comparison,"
        " reasoning, decided_by, decided_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (actor.workspace_id, case_id, entity_a, entity_b, verdict,
         json.dumps(comparison or {}), reasoning, actor.principal_id, datetime.now(timezone.utc)),
    ).fetchone()
    jid = int(row["id"])
    audit.record(conn, actor, "judgment.write", case_id=case_id, target_kind="judgment",
                 target_id=str(jid),
                 detail={"verdict": verdict, "a": str(entity_a), "b": str(entity_b)})
    return jid


def retract_judgment(
    conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID, judgment_id: int, reason: str,
    *, policy: Policy | None = None,
) -> None:
    """Undoing a merge is retracting the judgment and recomputing — that is all."""
    (policy or SoloPolicy()).authorize(conn, actor, "judgment.write", case_id)
    if not reason.strip():
        raise AssertionRejected("a retraction needs a reason")
    if conn.execute("SELECT 1 FROM judgment WHERE id=%s AND case_id=%s",
                    (judgment_id, case_id)).fetchone() is None:
        raise AssertionRejected("judgment not found in case")
    conn.execute(
        "INSERT INTO judgment_retraction (workspace_id, case_id, judgment_id, retracted_at,"
        " retracted_by, reason) VALUES (%s,%s,%s,%s,%s,%s)",
        (actor.workspace_id, case_id, judgment_id, datetime.now(timezone.utc),
         actor.principal_id, reason),
    )
    audit.record(conn, actor, "judgment.retract", case_id=case_id, target_kind="judgment",
                 target_id=str(judgment_id), detail={"reason": reason})
