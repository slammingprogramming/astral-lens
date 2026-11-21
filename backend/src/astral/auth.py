"""Team profile identity and roles.

A verified OIDC subject becomes a principal in the team workspace on first sight, with NO
roles: authentication says who you are, not what you may do. Roles come from `role_grant`
rows written by someone who holds `case.grant`. The only bootstrap is the operator naming
administrator subjects in configuration (ASTRAL_TEAM_ADMINS), who receive a workspace-wide
owner grant when they first appear. A workspace-wide grant covers every case; a case grant
covers one. Creating a case needs a workspace-wide investigator or owner grant, and its
creator becomes that case's owner.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg

from astral import audit
from astral.db import _NS, Actor
from astral.policy import ROLE_ACTIONS

TEAM_WORKSPACE_ID = uuid.uuid5(_NS, "workspace:team")
SYSTEM_PRINCIPAL_ID = uuid.uuid5(_NS, "principal:team-system")
SYSTEM_ACTOR = Actor(TEAM_WORKSPACE_ID, SYSTEM_PRINCIPAL_ID, "service")
ROLES = tuple(ROLE_ACTIONS)


def bootstrap_team(conn: psycopg.Connection) -> None:
    conn.execute("INSERT INTO workspace (id, name) VALUES (%s, 'team') ON CONFLICT DO NOTHING", (TEAM_WORKSPACE_ID,))
    conn.execute("INSERT INTO principal (id, workspace_id, kind, name) VALUES (%s,%s,'service','system')"
                 " ON CONFLICT DO NOTHING", (SYSTEM_PRINCIPAL_ID, TEAM_WORKSPACE_ID))


def actor_for(conn: psycopg.Connection, issuer: str, claims: dict[str, Any], admin_subjects: set[str]) -> Actor:
    """Find or create the principal for a verified token. New principals hold no roles."""
    sub = claims["sub"]
    row = conn.execute("SELECT principal_id FROM principal_identity WHERE issuer=%s AND subject=%s", (issuer, sub)).fetchone()
    if row:
        pid = row["principal_id"]
        conn.execute("UPDATE principal_identity SET last_seen_at = now() WHERE issuer=%s AND subject=%s", (issuer, sub))
    else:
        pid = uuid.uuid4()
        name = str(claims.get("name") or claims.get("preferred_username") or claims.get("email") or sub)[:200]
        conn.execute("INSERT INTO principal (id, workspace_id, kind, name) VALUES (%s,%s,'user',%s)",
                     (pid, TEAM_WORKSPACE_ID, name))
        conn.execute("INSERT INTO principal_identity (issuer, subject, principal_id, workspace_id, email, last_seen_at)"
                     " VALUES (%s,%s,%s,%s,%s,now())", (issuer, sub, pid, TEAM_WORKSPACE_ID, claims.get("email")))
        audit.record(conn, SYSTEM_ACTOR, "principal.provision", target_kind="principal", target_id=str(pid),
                     detail={"issuer": issuer, "admin": sub in admin_subjects})
    actor = Actor(TEAM_WORKSPACE_ID, pid, "user")
    if sub in admin_subjects:
        grant(conn, SYSTEM_ACTOR, None, pid, "owner")
    return actor


def grant(conn: psycopg.Connection, by: Actor, case_id: uuid.UUID | None, principal_id: uuid.UUID, role: str) -> bool:
    """Idempotent. Returns whether a row was added."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    if not conn.execute("SELECT 1 FROM principal WHERE id=%s AND workspace_id=%s", (principal_id, by.workspace_id)).fetchone():
        raise LookupError("no such principal in this workspace")
    have = conn.execute("SELECT 1 FROM role_grant WHERE principal_id=%s AND workspace_id=%s AND role=%s"
                        " AND case_id IS NOT DISTINCT FROM %s", (principal_id, by.workspace_id, role, case_id)).fetchone()
    if have:
        return False
    conn.execute("INSERT INTO role_grant (id, workspace_id, principal_id, case_id, role) VALUES (%s,%s,%s,%s,%s)",
                 (uuid.uuid4(), by.workspace_id, principal_id, case_id, role))
    audit.record(conn, by, "role.grant", case_id=case_id, target_kind="principal", target_id=str(principal_id),
                 detail={"role": role, "scope": "case" if case_id else "workspace"})
    return True


def revoke(conn: psycopg.Connection, by: Actor, case_id: uuid.UUID, principal_id: uuid.UUID) -> int:
    n = conn.execute("DELETE FROM role_grant WHERE principal_id=%s AND workspace_id=%s AND case_id=%s",
                     (principal_id, by.workspace_id, case_id)).rowcount
    if n:
        audit.record(conn, by, "role.revoke", case_id=case_id, target_kind="principal", target_id=str(principal_id),
                     detail={"grants_removed": n})
    return n


def visible_case_ids(conn: psycopg.Connection, actor: Actor) -> list[uuid.UUID] | None:
    """None means every case in the workspace (a workspace-wide grant); else the granted ones."""
    rows = conn.execute("SELECT case_id FROM role_grant WHERE principal_id=%s AND workspace_id=%s",
                        (actor.principal_id, actor.workspace_id)).fetchall()
    if any(r["case_id"] is None for r in rows):
        return None
    return [r["case_id"] for r in rows]
