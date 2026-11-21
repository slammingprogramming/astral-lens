"""Append-only, hash-chained audit log (10 §3).

The solo profile keeps the full log: its value to a lone investigator
defending their own work is at least as high as to a supervisor.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

import psycopg

from astral.db import Actor

GENESIS = b"\x00" * 32


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def _digest(prev: bytes, row: dict[str, Any]) -> bytes:
    h = hashlib.sha256()
    h.update(prev)
    h.update(_canonical(row))
    return h.digest()


def _row_fields(
    workspace_id: uuid.UUID,
    case_id: uuid.UUID | None,
    at: datetime,
    principal_id: uuid.UUID,
    action: str,
    target_kind: str | None,
    target_id: str | None,
    detail: dict[str, Any],
) -> dict[str, Any]:
    return {
        "workspace_id": str(workspace_id),
        "case_id": str(case_id) if case_id else None,
        "at": at.astimezone(timezone.utc).isoformat(),
        "principal_id": str(principal_id),
        "action": action,
        "target_kind": target_kind,
        "target_id": target_id,
        "detail": json.loads(_canonical(detail)),
    }


def record(
    conn: psycopg.Connection,
    actor: Actor,
    action: str,
    *,
    case_id: uuid.UUID | None = None,
    target_kind: str | None = None,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> int:
    """Append one entry inside the caller's transaction.

    Writing the audit entry in the same transaction as the action it records
    means an action cannot commit without its record, or vice versa.
    """
    detail = detail or {}
    at = datetime.now(timezone.utc)
    # one chain per workspace; the lock serializes appenders
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"audit:{actor.workspace_id}",))
    prev = conn.execute(
        "SELECT hash FROM audit_log WHERE workspace_id = %s ORDER BY id DESC LIMIT 1",
        (actor.workspace_id,),
    ).fetchone()
    prev_hash = bytes(prev["hash"]) if prev else GENESIS
    fields = _row_fields(
        actor.workspace_id, case_id, at, actor.principal_id, action, target_kind, target_id, detail
    )
    h = _digest(prev_hash, fields)
    row = conn.execute(
        "INSERT INTO audit_log (workspace_id, case_id, at, principal_id, action, target_kind,"
        " target_id, detail, prev_hash, hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (
            actor.workspace_id, case_id, at, actor.principal_id, action, target_kind,
            target_id, json.dumps(fields["detail"]), prev_hash, h,
        ),
    ).fetchone()
    return int(row["id"])


def verify_chain(conn: psycopg.Connection, workspace_id: uuid.UUID) -> tuple[bool, int | None]:
    """Recompute the chain. Returns (ok, id of first bad entry)."""
    prev_hash = GENESIS
    for r in conn.execute(
        "SELECT * FROM audit_log WHERE workspace_id = %s ORDER BY id", (workspace_id,)
    ):
        fields = _row_fields(
            r["workspace_id"], r["case_id"], r["at"], r["principal_id"], r["action"],
            r["target_kind"], r["target_id"], r["detail"],
        )
        if bytes(r["prev_hash"]) != prev_hash or bytes(r["hash"]) != _digest(prev_hash, fields):
            return False, int(r["id"])
        prev_hash = bytes(r["hash"])
    return True, None
