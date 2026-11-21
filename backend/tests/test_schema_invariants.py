"""The invariants that must hold even if the Python layer is bypassed."""

from __future__ import annotations

import uuid

import psycopg
import pytest

from astral import audit
from astral.assertions import AssertionRejected


def _raw_insert(conn, kit, sha, source_id, **over):
    cols = dict(workspace_id=kit.actor.workspace_id, case_id=kit.case, subject=uuid.uuid4(),
                predicate="name", object_kind="literal", object_value='"x"', source_id=source_id,
                evidence=[bytes.fromhex(sha)], derived_by='{"plugin":"p","version":"1","backend":"b"}',
                recorded_at="2026-01-01", created_by=kit.actor.principal_id)
    cols.update(over)
    names = ",".join(cols)
    return conn.execute(
        f"INSERT INTO assertion ({names}) VALUES ({','.join(['%s'] * len(cols))})",
        list(cols.values()))


def test_direct_insert_outside_writer_is_refused(db, kit):
    with db.connection() as conn:
        sha, src = kit.artifact(conn, b"hello"), kit.source(conn)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with db.connection() as conn:
            _raw_insert(conn, kit, sha, src)          # invariant 1


def test_assertions_are_append_only(db, kit):
    with db.connection() as conn:
        sha, src = kit.artifact(conn, b"a"), kit.source(conn)
        aid = kit.write(conn, source_id=src, sha=sha)
    with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
        with db.connection() as conn:
            conn.execute("UPDATE assertion SET predicate='other' WHERE id=%s", (aid,))
    with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
        with db.connection() as conn:
            conn.execute("DELETE FROM assertion WHERE id=%s", (aid,))
    with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
        with db.connection() as conn:
            conn.execute("TRUNCATE assertion CASCADE")


def test_pessimistic_grade_defaults(db, kit):
    with db.connection() as conn:
        sha = kit.artifact(conn, b"a")
        src = kit.source(conn, name="fresh", grade="F")
        aid = kit.write(conn, source_id=src, sha=sha)
        row = conn.execute("SELECT source_grade, info_grade FROM assertion WHERE id=%s",
                           (aid,)).fetchone()
    assert (row["source_grade"], row["info_grade"]) == ("F", 6)   # "no basis to judge"


def test_writer_gate_closes_after_each_write(db, kit):
    """set_config(..., true) must not leave the gate open for the rest of the txn."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with db.connection() as conn:
            sha, src = kit.artifact(conn, b"g"), kit.source(conn)
            kit.write(conn, source_id=src, sha=sha)
            _raw_insert(conn, kit, sha, src)


def test_evidence_must_be_acquired_in_this_case(db, kit, case, actor):
    from astral import cases

    with db.connection() as conn:
        other = cases.create_case(conn, actor, "other")
        sha_other = kit.store.ingest(conn, actor, other, b"secret", acquired_by="t").hex
        src = kit.source(conn)
        with pytest.raises(AssertionRejected, match="no acquisition in case"):
            with conn.transaction():
                kit.write(conn, source_id=src, sha=sha_other)   # cross-case leak refused


def test_derived_by_and_evidence_are_mandatory(db, kit):
    with db.connection() as conn:
        sha, src = kit.artifact(conn, b"m"), kit.source(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.connection() as conn:
            conn.execute("SELECT set_config('astral.writer','1',true)")
            _raw_insert(conn, kit, sha, src, derived_by='{"plugin":"p"}')
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.connection() as conn:
            conn.execute("SELECT set_config('astral.writer','1',true)")
            _raw_insert(conn, kit, sha, src, evidence=[])


def test_audit_chain_verifies_and_detects_tampering(db, kit):
    with db.connection() as conn:
        sha, src = kit.artifact(conn, b"audit"), kit.source(conn)
        kit.write(conn, source_id=src, sha=sha)
    with db.connection() as conn:
        ok, bad = audit.verify_chain(conn, kit.actor.workspace_id)
    assert ok and bad is None

    # the log refuses mutation...
    with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
        with db.connection() as conn:
            conn.execute("UPDATE audit_log SET action='x' WHERE id=(SELECT max(id) FROM audit_log)")
    # ...and if someone with superuser rights disables the trigger and edits anyway,
    # the chain no longer verifies.
    with db.connection() as conn:
        conn.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only")
        conn.execute("UPDATE audit_log SET action='tampered'"
                     " WHERE id=(SELECT max(id) FROM audit_log)")
        conn.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_log_append_only")
    with db.connection() as conn:
        ok, bad = audit.verify_chain(conn, kit.actor.workspace_id)
    assert not ok and bad is not None
    # restore a valid chain for later tests: rebuild hashes from the tampered row onward
    with db.connection() as conn:
        conn.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only")
        conn.execute("UPDATE audit_log SET action='assertion.write' WHERE action='tampered'")
        conn.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_log_append_only")
    with db.connection() as conn:
        assert audit.verify_chain(conn, kit.actor.workspace_id)[0]


def test_artifact_dedup_keeps_per_case_acquisitions(db, kit, actor):
    from astral import cases

    with db.connection() as conn:
        c2 = cases.create_case(conn, actor, "second")
        a = kit.store.ingest(conn, actor, kit.case, b"same bytes", acquired_by="one")
        b = kit.store.ingest(conn, actor, c2, b"same bytes", acquired_by="two",
                             acquired_from="https://example.test/x")
        n = conn.execute("SELECT count(*) AS n FROM artifact WHERE sha256=%s",
                         (a.sha256,)).fetchone()["n"]
        acq = conn.execute("SELECT count(*) AS n FROM artifact_acquisition WHERE sha256=%s",
                           (a.sha256,)).fetchone()["n"]
    assert a.sha256 == b.sha256 and n == 1 and acq == 2
