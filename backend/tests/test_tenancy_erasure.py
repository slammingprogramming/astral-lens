from __future__ import annotations

import uuid

import pytest

from astral import cases, projector
from astral.db import Actor
from astral.erasure import erase_case
from astral.evidence import IntegrityError
from astral.policy import PermissionDenied, SoloPolicy, TeamPolicy


# ------------------------------------------------------------------ tenancy
def _second_workspace(conn):
    ws, pr = uuid.uuid4(), uuid.uuid4()
    conn.execute("INSERT INTO workspace (id, name) VALUES (%s, 'other')", (ws,))
    conn.execute("INSERT INTO principal (id, workspace_id, kind, name) VALUES (%s,%s,'user','eve')",
                 (pr, ws))
    return Actor(ws, pr)


def test_another_workspace_cannot_see_or_write_into_a_case(db, kit):
    with db.connection() as conn:
        eve = _second_workspace(conn)
        for pol in (SoloPolicy(), TeamPolicy()):
            with pytest.raises(PermissionDenied, match="case not found"):
                pol.authorize(conn, eve, "case.read", kit.case)
        with pytest.raises(PermissionDenied):
            kit.store.read(conn, eve, kit.case, b"\x00" * 32)


def test_team_roles_are_enforced_and_scoped_to_the_case(db, kit):
    import uuid as _u

    with db.connection() as conn:
        carol = Actor(kit.actor.workspace_id, _u.uuid4())
        conn.execute("INSERT INTO principal (id, workspace_id, kind, name) VALUES (%s,%s,'user','c')",
                     (carol.principal_id, carol.workspace_id))
        pol = TeamPolicy()
        with pytest.raises(PermissionDenied):                 # no grant, no access
            pol.authorize(conn, carol, "case.read", kit.case)
        conn.execute("INSERT INTO role_grant (id, workspace_id, principal_id, case_id, role)"
                     " VALUES (%s,%s,%s,%s,'viewer')",
                     (_u.uuid4(), carol.workspace_id, carol.principal_id, kit.case))
        pol.authorize(conn, carol, "case.read", kit.case)
        with pytest.raises(PermissionDenied):                 # viewer cannot write
            pol.authorize(conn, carol, "assertion.write", kit.case)
        other = cases.create_case(conn, kit.actor, "unrelated")
        with pytest.raises(PermissionDenied):                 # grant is per-case
            pol.authorize(conn, carol, "case.read", other)


def test_entity_ids_are_case_scoped_so_cases_never_link(db, kit):
    from astral.plugins.host import entity_namespace

    with db.connection() as conn:
        other = cases.create_case(conn, kit.actor, "other")
    assert entity_namespace(kit.case) != entity_namespace(other)


# ------------------------------------------------------------------ erasure
def test_hard_delete_destroys_content_and_leaves_a_tombstone(db, kit, actor):
    with db.connection() as conn:
        c2 = cases.create_case(conn, actor, "keeper")
        shared = kit.store.ingest(conn, actor, kit.case, b"shared bytes", acquired_by="u")
        kit.store.ingest(conn, actor, c2, b"shared bytes", acquired_by="u")      # also held by c2
        raw = kit.store.ingest(conn, actor, kit.case, b"only here", acquired_by="u")
        kid = kit.store.ingest(conn, actor, kit.case, b"derived of only here", acquired_by="u",
                               parent_sha256=raw.sha256, derivation="x")
        src = kit.source(conn)
        e = uuid.uuid4()
        a = kit.write(conn, source_id=src, sha=kid.hex, subject=e)
        kit.writer.retract(conn, actor, kit.case, a, "r")
        projector.rebuild_case(conn, kit.case)
        plan = erase_case(conn, actor, kit.case, kit.store, legal_basis="GDPR Art. 17 request #42")
    assert plan.finish(kit.store) == 2                          # raw + derived blobs unlinked

    with db.connection() as conn:
        for t in ("assertion", "assertion_retraction", "artifact_acquisition", "proj_property"):
            assert conn.execute(f"SELECT count(*) AS n FROM {t} WHERE case_id=%s",
                                (kit.case,)).fetchone()["n"] == 0
        tomb = conn.execute("SELECT * FROM tombstone WHERE case_id=%s", (kit.case,)).fetchone()
        assert tomb["legal_basis"].startswith("GDPR") and tomb["counts"]["assertion"] == 1
        assert conn.execute("SELECT state FROM case_ WHERE id=%s", (kit.case,)).fetchone()[
            "state"] == "erased"
        # the other case still holds the shared blob; it must survive
        assert kit.store.read(conn, actor, c2, shared.sha256) == b"shared bytes"
        # and the audit trail records that the erasure happened
        assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE action='case.erase'"
                            " AND case_id=%s", (kit.case,)).fetchone()["n"] == 1


def test_erasure_requires_a_legal_basis_and_is_not_repeatable(db, kit, actor):
    with db.connection() as conn:
        with pytest.raises(ValueError, match="legal basis"):
            erase_case(conn, actor, kit.case, kit.store, legal_basis=" ")
        erase_case(conn, actor, kit.case, kit.store, legal_basis="warrant expired")
        with pytest.raises(ValueError, match="already erased"):
            erase_case(conn, actor, kit.case, kit.store, legal_basis="again")


def test_hard_delete_flag_does_not_leak_past_the_erasing_transaction(db, kit, actor):
    import psycopg

    with db.connection() as conn:
        sha, src = kit.artifact(conn, b"keep"), kit.source(conn)
        a = kit.write(conn, source_id=src, sha=sha)
    with db.connection() as conn:
        erase_case(conn, actor, cases_new(conn, actor), kit.store, legal_basis="x")
    with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
        with db.connection() as conn:
            conn.execute("DELETE FROM assertion WHERE id=%s", (a,))


def cases_new(conn, actor):
    return cases.create_case(conn, actor, "scratch")


# ----------------------------------------------------------------- evidence
def test_blob_corruption_is_detected_on_read(db, kit, actor, tmp_path):
    import os
    import stat

    with db.connection() as conn:
        ing = kit.store.ingest(conn, actor, kit.case, b"original", acquired_by="u")
        path = kit.store._path(ing.sha256)
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)   # on POSIX S_IWRITE alone is write-only, and the read below would fail
        path.write_bytes(b"tampered")
        with pytest.raises(IntegrityError):
            kit.store.read(conn, actor, kit.case, ing.sha256)


def test_blobs_are_write_once(db, kit, actor):
    with db.connection() as conn:
        ing = kit.store.ingest(conn, actor, kit.case, b"orig", acquired_by="u")
        again = kit.store.ingest(conn, actor, kit.case, b"orig", acquired_by="u2")
    assert ing.new_blob and not again.new_blob
    with pytest.raises(PermissionError):
        kit.store._path(ing.sha256).open("ab")
