"""Explain any conclusion: walk derived_from to the originating artifact
(03 §9). Works from layer 1 alone, so it survives dropping every projection.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg


def _hex(b: bytes) -> str:
    return bytes(b).hex()


def explain_assertion(conn: psycopg.Connection, case_id: uuid.UUID, assertion_id: int,
                      *, _seen: set[int] | None = None) -> dict[str, Any]:
    """A tree: this assertion, its grades and derivation, the artifacts that
    support it with their acquisition records, and the same for every upstream
    assertion. `retracted` is reported rather than hidden."""
    seen = _seen if _seen is not None else set()
    a = conn.execute(
        "SELECT a.*, s.name AS source_name, s.kind AS source_kind, s.origin_key,"
        "       r.reason AS retract_reason, r.retracted_at"
        " FROM assertion a JOIN source s ON s.id = a.source_id"
        " LEFT JOIN assertion_retraction r ON r.assertion_id = a.id"
        " WHERE a.id = %s AND a.case_id = %s",
        (assertion_id, case_id),
    ).fetchone()
    if a is None:
        raise LookupError(f"assertion {assertion_id} not in case")
    seen.add(assertion_id)

    artifacts = []
    for sha in a["evidence"]:
        art = conn.execute("SELECT * FROM artifact WHERE sha256 = %s", (sha,)).fetchone()
        acqs = conn.execute(
            "SELECT acquired_by, acquired_from, acquisition_ctx, acquired_at"
            " FROM artifact_acquisition WHERE case_id = %s AND sha256 = %s ORDER BY id",
            (case_id, sha),
        ).fetchall()
        artifacts.append({
            "sha256": _hex(sha),
            "media_type": art["media_type"],
            "byte_size": art["byte_size"],
            "parent_sha256": _hex(art["parent_sha256"]) if art["parent_sha256"] else None,
            "derivation": art["derivation"],
            "acquisitions": [dict(x) for x in acqs],
        })

    upstream = [
        explain_assertion(conn, case_id, up, _seen=seen)
        for up in a["derived_from"] if up not in seen
    ]
    return {
        "assertion_id": a["id"],
        "subject": str(a["subject"]),
        "predicate": a["predicate"],
        "object": str(a["object_entity"]) if a["object_entity"] else a["object_value"],
        "source": {"name": a["source_name"], "kind": a["source_kind"], "origin": a["origin_key"]},
        "grade": f'{a["source_grade"]}{a["info_grade"]}',
        "derived_by": a["derived_by"],
        "recorded_at": a["recorded_at"],
        "valid_from": a["valid_from"],
        "valid_to": a["valid_to"],
        "retracted": None if a["retracted_at"] is None
        else {"at": a["retracted_at"], "reason": a["retract_reason"]},
        "artifacts": artifacts,
        "upstream": upstream,
    }


def originating_artifacts(tree: dict[str, Any]) -> list[str]:
    """The leaf artifacts of an explanation: where the claim ultimately came
    from. Follows derived artifacts (transcode, frame) up to their parents."""
    out: list[str] = []

    def walk(node: dict[str, Any]) -> None:
        for art in node["artifacts"]:
            out.append(art["sha256"])
        for up in node["upstream"]:
            walk(up)

    walk(tree)
    return list(dict.fromkeys(out))


def belief_as_of(conn: psycopg.Connection, case_id: uuid.UUID, at: Any) -> list[dict[str, Any]]:
    """Belief-as-of-T: what this case believed at time T (recorded_at <= T and
    not yet retracted by T) — the question single-timestamp systems cannot
    answer (03 'Bitemporality')."""
    return conn.execute(
        "SELECT a.id, a.subject, a.predicate, a.object_entity, a.object_value, a.valid_from,"
        "       a.valid_to, a.source_grade, a.info_grade"
        " FROM assertion a LEFT JOIN assertion_retraction r ON r.assertion_id = a.id"
        " WHERE a.case_id = %s AND a.recorded_at <= %s"
        "   AND (r.id IS NULL OR r.retracted_at > %s) ORDER BY a.id",
        (case_id, at, at),
    ).fetchall()


def truth_as_of(conn: psycopg.Connection, case_id: uuid.UUID, at: Any) -> list[dict[str, Any]]:
    """Truth-as-of-T: what was true in the world at T, per currently
    unretracted assertions."""
    return conn.execute(
        "SELECT a.id, a.subject, a.predicate, a.object_entity, a.object_value, a.valid_from,"
        "       a.valid_to, a.source_grade, a.info_grade"
        " FROM assertion a LEFT JOIN assertion_retraction r ON r.assertion_id = a.id"
        " WHERE a.case_id = %s AND r.id IS NULL"
        "   AND (a.valid_from IS NULL OR a.valid_from <= %s)"
        "   AND (a.valid_to IS NULL OR a.valid_to >= %s) ORDER BY a.id",
        (case_id, at, at),
    ).fetchall()
