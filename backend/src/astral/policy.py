"""Policy layer: authorization at the case boundary (10 §9).

The solo profile collapses the seam (ADR-0004): no role checks, no login. The
team profile enforces roles from role_grant. Both go through the same
interface, so no query path can skip the check by being "the solo path".

Footprint gating also lives here: `target_contact` plugin calls need approval
every time and this is not a configurable default (10 §2).
"""

from __future__ import annotations

import uuid
from typing import Protocol

import psycopg

from astral.db import Actor


class PermissionDenied(Exception):
    pass


class ApprovalRequired(Exception):
    """A target_contact call was attempted without in-the-moment approval."""


READ = {"case.read", "artifact.read", "assertion.read", "entity.read"}
COMMENT = READ | {"comment.write", "case.audit"}
WRITE = COMMENT | {"artifact.write", "assertion.write", "assertion.retract",
                   "judgment.write", "plugin.invoke", "job.submit", "case.export", "case.create", "lens.write",
                   "assignment.write", "workbench.write", "machine.write"}
ADMIN = WRITE | {"case.close", "case.erase", "case.grant", "plugin.manage"}

ROLE_ACTIONS: dict[str, set[str]] = {
    "viewer": READ,
    "reviewer": COMMENT,
    "investigator": WRITE,
    "owner": ADMIN,
}


class Policy(Protocol):
    def authorize(
        self, conn: psycopg.Connection, actor: Actor, action: str, case_id: uuid.UUID | None
    ) -> None: ...


class SoloPolicy:
    """One implicit principal, no role checks. Still verifies the case belongs
    to the actor's workspace so a mis-routed id cannot cross workspaces."""

    def authorize(
        self, conn: psycopg.Connection, actor: Actor, action: str, case_id: uuid.UUID | None
    ) -> None:
        if case_id is not None:
            _require_case_in_workspace(conn, actor, case_id)


class TeamPolicy:
    def authorize(
        self, conn: psycopg.Connection, actor: Actor, action: str, case_id: uuid.UUID | None
    ) -> None:
        if case_id is not None:
            _require_case_in_workspace(conn, actor, case_id)
        rows = conn.execute(
            "SELECT role FROM role_grant WHERE principal_id = %s AND workspace_id = %s"
            " AND (case_id IS NULL OR case_id = %s)",
            (actor.principal_id, actor.workspace_id, case_id),
        ).fetchall()
        allowed: set[str] = set()
        for r in rows:
            allowed |= ROLE_ACTIONS[r["role"]]
        if action not in allowed:
            raise PermissionDenied(f"{action} not permitted for principal {actor.principal_id}")


def _require_case_in_workspace(conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID) -> None:
    row = conn.execute(
        "SELECT workspace_id, state FROM case_ WHERE id = %s", (case_id,)
    ).fetchone()
    # Same message for "missing" and "someone else's": do not reveal existence.
    if row is None or row["workspace_id"] != actor.workspace_id:
        raise PermissionDenied("case not found")


def require_footprint_approval(footprint: str, approved: bool) -> None:
    """`target_contact` requires explicit approval at the moment of the call."""
    if footprint == "target_contact" and not approved:
        raise ApprovalRequired("target_contact requires explicit approval every time")
