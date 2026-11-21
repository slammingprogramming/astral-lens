from __future__ import annotations

import datetime as dt
import uuid

import pytest
from pydantic import ValidationError

from astral.assertions import AssertionRejected, DerivedBy
from astral.explain import belief_as_of, explain_assertion, originating_artifacts, truth_as_of


def test_source_grade_defaults_to_the_sources_not_an_optimistic_one(db, kit):
    with db.connection() as conn:
        sha = kit.artifact(conn, b"x")
        aid = kit.write(conn, source_id=kit.source(conn, "dflt", grade="D"), sha=sha)
        row = conn.execute("SELECT source_grade FROM assertion WHERE id=%s", (aid,)).fetchone()
    assert row["source_grade"] == "D"


def test_derived_grade_is_weakest_link_never_an_upgrade(db, kit):
    with db.connection() as conn:
        sha = kit.artifact(conn, b"x")
        weak = kit.source(conn, "weak", grade="D")
        strong = kit.source(conn, "strong", grade="A")
        up = kit.write(conn, source_id=weak, sha=sha, info_grade=4)
        with pytest.raises(AssertionRejected, match="grade escalation"):
            with conn.transaction():
                kit.write(conn, source_id=strong, sha=sha, derived_from=[up], info_grade=4)
        with pytest.raises(AssertionRejected, match="grade escalation"):
            with conn.transaction():
                kit.write(conn, source_id=weak, sha=sha, derived_from=[up], info_grade=2)
        # equal or worse is fine
        kit.write(conn, source_id=weak, sha=sha, derived_from=[up], info_grade=4)
        kit.write(conn, source_id=weak, sha=sha, derived_from=[up], info_grade=5,
                  source_grade="E")


def test_derived_from_must_exist_in_this_case(db, kit, actor):
    with db.connection() as conn:
        sha, src = kit.artifact(conn, b"x"), kit.source(conn)
        with pytest.raises(AssertionRejected):
            with conn.transaction():
                kit.write(conn, source_id=src, sha=sha, derived_from=[999999999])


def test_degradation_must_name_what_it_degraded_from():
    with pytest.raises(ValidationError):
        DerivedBy(plugin="p", version="1", backend="cpu", degraded=True)
    DerivedBy(plugin="p", version="1", backend="cpu", degraded=True, degraded_from="gpu")


def test_object_is_entity_xor_literal(kit):
    from astral.assertions import AssertionDraft

    by = DerivedBy(plugin="p", version="1", backend="b")
    base = dict(subject=uuid.uuid4(), predicate="p", source_id=uuid.uuid4(),
                evidence=["0" * 64], derived_by=by)
    with pytest.raises(ValidationError):
        AssertionDraft(**base)
    with pytest.raises(ValidationError):
        AssertionDraft(**base, object_entity=uuid.uuid4(), object_value="x")


# ----------------------------------------------------------- AI-layer limits
def _llm(sha, start, end, text, **extra):
    return DerivedBy(plugin="ai", version="1", backend="llama", kind="llm",
                     params={"citations": [{"artifact": sha, "start": start, "end": end,
                                            "text": text}]}, **extra)


def test_ai_layer_limits(db, kit):
    doc = b"Acme Ltd, registration number 12345678, Leeds."
    with db.connection() as conn:
        sha, src = kit.artifact(conn, doc), kit.source(conn, "ai", grade="F")

        def attempt(**kw):
            with conn.transaction():
                return kit.write(conn, source_id=src, sha=sha, **kw)

        # uncited
        with pytest.raises(AssertionRejected, match="no citations"):
            attempt(derived_by=DerivedBy(plugin="ai", version="1", backend="l", kind="llm"),
                    info_grade=5)
        # citation that does not match the artifact
        with pytest.raises(AssertionRejected, match="does not match"):
            attempt(derived_by=_llm(sha, 0, 4, "Nope"), info_grade=5)
        # invented identifier: value not in any cited span
        with pytest.raises(AssertionRejected, match="invent an identifier"):
            attempt(predicate="registrationNumber", value="99999999",
                    derived_by=_llm(sha, 0, 8, "Acme Ltd"), info_grade=5)
        # identity is a judgment, not an assertion
        with pytest.raises(AssertionRejected, match="identity"):
            attempt(predicate="sameAs", value="x", derived_by=_llm(sha, 0, 8, "Acme Ltd"),
                    info_grade=5)
        # an LLM cannot grade itself better than 'doubtful'
        with pytest.raises(AssertionRejected, match="info_grade"):
            attempt(derived_by=_llm(sha, 0, 8, "Acme Ltd"), info_grade=2)
        # a properly cited, verified, low-graded assertion is accepted
        span = "registration number 12345678"
        start = doc.decode().index(span)
        attempt(predicate="registrationNumber", value="12345678",
                derived_by=_llm(sha, start, start + len(span), span), info_grade=5)


# ------------------------------------------------------------- retraction
def test_retraction_cascades_and_nothing_is_deleted(db, kit):
    with db.connection() as conn:
        sha, src = kit.artifact(conn, b"x"), kit.source(conn, grade="C")
        root = kit.write(conn, source_id=src, sha=sha)
        child = kit.write(conn, source_id=src, sha=sha, derived_from=[root], info_grade=6)
        grand = kit.write(conn, source_id=src, sha=sha, derived_from=[child], info_grade=6)
        other = kit.write(conn, source_id=src, sha=sha)
        done = kit.writer.retract(conn, kit.actor, kit.case, root, "source was broken")
        assert set(done) == {root, child, grand}
        total = conn.execute("SELECT count(*) AS n FROM assertion WHERE case_id=%s",
                             (kit.case,)).fetchone()["n"]
        assert total == 4                                   # invariant 2: nothing deleted
        tree = explain_assertion(conn, kit.case, grand)
        assert tree["retracted"]["reason"] == "source was broken"
        assert explain_assertion(conn, kit.case, other)["retracted"] is None
        # retracting again is a no-op, not an error and not a duplicate
        assert kit.writer.retract(conn, kit.actor, kit.case, root, "again") == []


def test_retract_requires_a_reason(db, kit):
    with db.connection() as conn:
        sha, src = kit.artifact(conn, b"x"), kit.source(conn)
        a = kit.write(conn, source_id=src, sha=sha)
        with pytest.raises(AssertionRejected, match="reason"):
            kit.writer.retract(conn, kit.actor, kit.case, a, "  ")


def test_retract_everything_a_plugin_asserted(db, kit):
    with db.connection() as conn:
        sha, src = kit.artifact(conn, b"x"), kit.source(conn)
        bad = DerivedBy(plugin="flaky", version="1.2.0", backend="native")
        good = DerivedBy(plugin="solid", version="1.0.0", backend="native")
        b1 = kit.write(conn, source_id=src, sha=sha, derived_by=bad)
        b2 = kit.write(conn, source_id=src, sha=sha, derived_by=bad)
        g1 = kit.write(conn, source_id=src, sha=sha, derived_by=good)
        batch, ids = kit.writer.retract_by_plugin(conn, kit.actor, kit.case, "flaky",
                                                  "broken for a month")
        assert set(ids) == {b1, b2} and g1 not in ids
        n = conn.execute("SELECT count(*) AS n FROM assertion_retraction WHERE batch_id=%s",
                         (batch,)).fetchone()["n"]
        assert n == 2


# ------------------------------------------------------- explain / bitemporal
def test_explain_walks_back_to_the_originating_artifact(db, kit):
    with db.connection() as conn:
        raw = kit.store.ingest(conn, kit.actor, kit.case, b"raw video bytes", acquired_by="upload",
                               media_type="video/mp4", acquired_from="/evidence/a.mp4")
        derived = kit.store.ingest(conn, kit.actor, kit.case, b"transcript text",
                                   acquired_by="asr@1", media_type="text/plain",
                                   parent_sha256=raw.sha256, derivation="transcribe")
        src = kit.source(conn, grade="B")
        a1 = kit.write(conn, source_id=src, sha=derived.hex, value="said hello")
        a2 = kit.write(conn, source_id=src, sha=derived.hex, derived_from=[a1], info_grade=6,
                       value="greeted")
        tree = explain_assertion(conn, kit.case, a2)
    assert tree["upstream"][0]["assertion_id"] == a1
    leaf = tree["upstream"][0]["artifacts"][0]
    assert leaf["sha256"] == derived.hex and leaf["parent_sha256"] == raw.hex
    assert leaf["acquisitions"][0]["acquired_by"] == "asr@1"
    assert originating_artifacts(tree) == [derived.hex]


def test_bitemporal_truth_and_belief_as_of(db, kit):
    t = lambda y, m=1: dt.datetime(y, m, 1, tzinfo=dt.timezone.utc)  # noqa: E731
    with db.connection() as conn:
        sha, src = kit.artifact(conn, b"x"), kit.source(conn)
        director = uuid.uuid4()
        a = kit.write(conn, source_id=src, sha=sha, subject=director, predicate="director",
                      value="J. Smith", valid_from=t(2019), valid_to=t(2022))
        before = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
        truth_2020 = [r["id"] for r in truth_as_of(conn, kit.case, t(2020))]
        truth_2023 = [r["id"] for r in truth_as_of(conn, kit.case, t(2023))]
        believed_before = [r["id"] for r in belief_as_of(conn, kit.case, before)]
        believed_now = [r["id"] for r in belief_as_of(
            conn, kit.case, dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1))]
        assert a in truth_2020 and a not in truth_2023
        # discovered today: we did not believe it before today, though it was true in 2020
        assert a not in believed_before and a in believed_now
