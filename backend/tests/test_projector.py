from __future__ import annotations

import uuid

import pytest

from astral import projector, resolver
from astral.assertions import AssertionRejected
from astral.db import Actor


def _entity(conn, kit, src, sha, name, schema="Person", **kw):
    e = uuid.uuid4()
    kit.write(conn, source_id=src, sha=sha, subject=e, predicate="schema", value=schema, **kw)
    kit.write(conn, source_id=src, sha=sha, subject=e, predicate="name", value=name, **kw)
    return e


def test_rebuild_is_deterministic_and_disposable(db, kit):
    with db.connection() as conn:
        sha, src = kit.artifact(conn, b"doc"), kit.source(conn, grade="B")
        a = _entity(conn, kit, src, sha, "Alice Doe")
        b = _entity(conn, kit, src, sha, "A. Doe")
        kit.write(conn, source_id=src, sha=sha, subject=a, predicate="knows",
                  value=None, object_entity=b)
        resolver.record_judgment(conn, kit.actor, kit.case, a, b, "same",
                                 comparison={"name": 0.9})
        projector.rebuild_case(conn, kit.case)
        first = projector.snapshot(conn, kit.case)
        projector.drop_projections(conn)                      # drop EVERY projection
        assert conn.execute("SELECT count(*) AS n FROM proj_property").fetchone()["n"] == 0
        projector.rebuild_all(conn)
        assert projector.snapshot(conn, kit.case) == first


def test_same_judgment_merges_and_retraction_splits(db, kit):
    with db.connection() as conn:
        sha, src = kit.artifact(conn, b"doc"), kit.source(conn)
        a, b = _entity(conn, kit, src, sha, "Alice"), _entity(conn, kit, src, sha, "Alicia")
        j = resolver.record_judgment(conn, kit.actor, kit.case, a, b, "same")
        projector.rebuild_case(conn, kit.case)
        n = conn.execute("SELECT count(*) AS n FROM proj_entity WHERE case_id=%s",
                         (kit.case,)).fetchone()["n"]
        merged = conn.execute("SELECT count(DISTINCT cluster_id) AS n FROM proj_entity_member"
                              " WHERE case_id=%s", (kit.case,)).fetchone()["n"]
        assert n == 1 and merged == 1
        # records are never physically merged: both entities' assertions still exist
        assert conn.execute("SELECT count(DISTINCT subject) AS n FROM assertion WHERE case_id=%s",
                            (kit.case,)).fetchone()["n"] == 2
        # undoing a merge is retracting the judgment and recomputing — that is all
        resolver.retract_judgment(conn, kit.actor, kit.case, j, "wrong person")
        projector.rebuild_case(conn, kit.case)
        assert conn.execute("SELECT count(*) AS n FROM proj_entity WHERE case_id=%s",
                            (kit.case,)).fetchone()["n"] == 2


def test_not_same_is_a_hard_constraint_and_conflicts_stay_visible(db, kit):
    with db.connection() as conn:
        sha, src = kit.artifact(conn, b"doc"), kit.source(conn)
        a, b, c = (_entity(conn, kit, src, sha, n) for n in ("A", "B", "C"))
        resolver.record_judgment(conn, kit.actor, kit.case, a, b, "same")
        resolver.record_judgment(conn, kit.actor, kit.case, b, c, "same")
        resolver.record_judgment(conn, kit.actor, kit.case, a, c, "not_same")
        projector.rebuild_case(conn, kit.case)
        clusters = conn.execute("SELECT count(*) AS n FROM proj_entity WHERE case_id=%s",
                                (kit.case,)).fetchone()["n"]
        conflicts = conn.execute("SELECT count(*) AS n FROM proj_conflict WHERE case_id=%s",
                                 (kit.case,)).fetchone()["n"]
        assert clusters == 2 and conflicts == 1     # refused, not silently dropped


def test_merging_redirects_old_canonical_ids(db, kit):
    with db.connection() as conn:
        sha, src = kit.artifact(conn, b"doc"), kit.source(conn)
        a, b = _entity(conn, kit, src, sha, "A"), _entity(conn, kit, src, sha, "B")
        resolver.record_judgment(conn, kit.actor, kit.case, a, b, "same")
        stats = projector.rebuild_case(conn, kit.case)
    assert stats["redirects"] == 1                  # the larger-id cluster's old id redirects


def test_independent_sources_collapse_shared_origin(db, kit):
    """Three websites repeating one press release are one source, not three."""
    with db.connection() as conn:
        sha = kit.artifact(conn, b"doc")
        s1 = kit.source(conn, "site1", origin="press-release-77")
        s2 = kit.source(conn, "site2", origin="press-release-77")
        s3 = kit.source(conn, "site3", origin="press-release-77")
        reg = kit.source(conn, "registry", origin="companies-house")
        co = uuid.uuid4()
        for s in (s1, s2, s3):
            kit.write(conn, source_id=s, sha=sha, subject=co, predicate="status", value="dissolved")
        kit.write(conn, source_id=reg, sha=sha, subject=co, predicate="status", value="dissolved")
        projector.rebuild_case(conn, kit.case)
        rows = conn.execute("SELECT independent_sources FROM proj_property"
                            " WHERE case_id=%s AND predicate='status'", (kit.case,)).fetchall()
    assert {r["independent_sources"] for r in rows} == {2}      # not 4


def test_a_derived_assertion_adds_no_independence(db, kit):
    with db.connection() as conn:
        sha, src = kit.artifact(conn, b"doc"), kit.source(conn, origin="o1")
        e = uuid.uuid4()
        root = kit.write(conn, source_id=src, sha=sha, subject=e, predicate="p", value="v")
        kit.write(conn, source_id=src, sha=sha, subject=e, predicate="p", value="v",
                  derived_from=[root], info_grade=6)
        projector.rebuild_case(conn, kit.case)
        n = conn.execute("SELECT max(independent_sources) AS n FROM proj_property"
                         " WHERE case_id=%s", (kit.case,)).fetchone()["n"]
    assert n == 1


def test_retracted_assertions_leave_the_projection(db, kit):
    with db.connection() as conn:
        sha, src = kit.artifact(conn, b"doc"), kit.source(conn)
        e = uuid.uuid4()
        a = kit.write(conn, source_id=src, sha=sha, subject=e, predicate="name", value="Gone")
        kit.writer.retract(conn, kit.actor, kit.case, a, "bad source")
        projector.rebuild_case(conn, kit.case)
        assert conn.execute("SELECT count(*) AS n FROM proj_property WHERE case_id=%s",
                            (kit.case,)).fetchone()["n"] == 0


def test_an_agent_may_propose_but_never_decide_a_merge(db, kit):
    agent = Actor(kit.actor.workspace_id, kit.actor.principal_id, "agent")
    with db.connection() as conn:
        a, b = uuid.uuid4(), uuid.uuid4()
        resolver.record_judgment(conn, agent, kit.case, a, b, "undecided",
                                 comparison={"name": 0.8})
        with pytest.raises(AssertionRejected, match="never decide"):
            resolver.record_judgment(conn, agent, kit.case, a, b, "same")
