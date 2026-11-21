"""Cases and sources. A case is the compartmentalization, retention,
collection-identity and permission boundary all at once (03 §7)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg

from astral import audit
from astral.db import Actor


def create_case(
    conn: psycopg.Connection,
    actor: Actor,
    name: str,
    *,
    retention_policy: dict[str, Any] | None = None,
    collection_identity: dict[str, Any] | None = None,
) -> uuid.UUID:
    case_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO case_ (id, workspace_id, name, retention_policy, collection_identity,"
        " created_by) VALUES (%s,%s,%s,%s,%s,%s)",
        (
            case_id, actor.workspace_id, name,
            json.dumps(retention_policy or {}), json.dumps(collection_identity or {}),
            actor.principal_id,
        ),
    )
    audit.record(conn, actor, "case.create", case_id=case_id, target_kind="case",
                 target_id=str(case_id), detail={"name": name})
    return case_id


def set_source_grade(conn: psycopg.Connection, actor: Actor, source_id: uuid.UUID,
                     grade: str) -> None:
    """The operator, not the plugin, decides how reliable a source is. New
    sources start at F ('no basis to judge'); raising one is an audited act."""
    if len(grade) != 1 or grade not in "ABCDEF":
        raise ValueError("grade must be one of A..F")
    cur = conn.execute(
        "UPDATE source SET default_source_grade = %s WHERE id = %s AND workspace_id = %s",
        (grade, source_id, actor.workspace_id))
    if cur.rowcount == 0:
        raise LookupError("source not found")
    audit.record(conn, actor, "source.grade", target_kind="source", target_id=str(source_id),
                 detail={"grade": grade})


def get_or_create_source(
    conn: psycopg.Connection,
    actor: Actor,
    *,
    kind: str,
    name: str,
    locator: str | None = None,
    origin_key: str | None = None,
    default_source_grade: str = "F",
) -> uuid.UUID:
    row = conn.execute(
        "SELECT id FROM source WHERE workspace_id = %s AND kind = %s AND name = %s",
        (actor.workspace_id, kind, name),
    ).fetchone()
    if row:
        return row["id"]
    source_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO source (id, workspace_id, kind, name, locator, origin_key,"
        " default_source_grade) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (source_id, actor.workspace_id, kind, name, locator, origin_key, default_source_grade),
    )
    return source_id
