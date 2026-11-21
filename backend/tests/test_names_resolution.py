from __future__ import annotations

import uuid

import pytest

from astral import names, projector, resolution
from astral.assertions import AssertionRejected


# ------------------------------------------------------------------- names
def test_cyrillic_and_latin_spellings_match_through_the_romanised_form():
    s = names.score("Владимир Петров", "Vladimir Petrov")
    assert s.score == 1.0
    assert names.script_of("Владимир") == "CYRILLIC" and names.script_of("Vladimir") == "LATIN"


def test_forms_keep_the_original_and_record_the_standard():
    forms = names.name_forms("Владимир Петров")
    assert forms[0].kind == "original" and forms[0].form == "Владимир Петров"
    rom = next(f for f in forms if f.kind == "romanized")
    assert rom.form == "vladimir petrov" and rom.standard.startswith("anyascii")


def test_diacritics_and_case_do_not_matter():
    assert names.score("JOSÉ  Muñoz", "jose munoz").score == 1.0


def test_legal_suffixes_are_ignored_only_for_organisations():
    assert names.score("Acme Ltd", "ACME Limited", org=True).score == 1.0
    assert names.score("Acme Ltd", "ACME Limited", org=False).score < 1.0


def test_similar_given_names_are_not_equated_even_though_soundex_would():
    assert names.soundex("Christine") == names.soundex("Christopher")      # why it is only a block key
    assert names.score("Christine Lee", "Christopher Lee").score < 0.93


def test_a_short_name_inside_a_long_one_is_not_a_perfect_match():
    assert names.score("John Smith", "John Smith Holdings Group International").score < 0.93


def test_known_limit_vowelless_abjad_transliteration_scores_low():
    # anyascii renders Arabic محمد as "mhmd": it will not reach "muhammad" at the
    # auto-propose level. Documented limitation, not a bug to hide.
    s = names.score("محمد", "Muhammad").score
    assert 0.3 < s < 0.9


def test_blocking_keys_put_cross_script_duplicates_in_one_block():
    assert names.blocking_keys("Владимир Петров") & names.blocking_keys("Vladimir Petrov")


# -------------------------------------------------------------- resolution
def _person(kit, conn, sid, sha, name, **props):
    e = uuid.uuid4()
    kit.write(conn, source_id=sid, sha=sha, subject=e, predicate="schema", value="Person")
    kit.write(conn, source_id=sid, sha=sha, subject=e, predicate="name", value=name)
    for k, v in props.items():
        kit.write(conn, source_id=sid, sha=sha, subject=e, predicate=k, value=v)
    return e


def _org(kit, conn, sid, sha, name, **props):
    e = uuid.uuid4()
    kit.write(conn, source_id=sid, sha=sha, subject=e, predicate="schema", value="Company")
    kit.write(conn, source_id=sid, sha=sha, subject=e, predicate="name", value=name)
    for k, v in props.items():
        kit.write(conn, source_id=sid, sha=sha, subject=e, predicate=k, value=v)
    return e


def test_candidates_are_ranked_explained_and_never_merged(db, kit):
    with db.connection() as conn:
        sid, sha = kit.source(conn), kit.artifact(conn, b"records")
        a = _person(kit, conn, sid, sha, "Владимир Петров", birthDate="1970-03-02", nationality="ru")
        b = _person(kit, conn, sid, sha, "Vladimir Petrov", birthDate="1970-03-02", nationality="RU")
        c = _person(kit, conn, sid, sha, "Vladimir Petrov", birthDate="1985-06-01")      # same name, other man
        d = _org(kit, conn, sid, sha, "Acme Ltd", leiCode="5493001KJTIIGC8Y1R12")
        e = _org(kit, conn, sid, sha, "ACME Limited", leiCode="5493001KJTIIGC8Y1R12")
        f = _org(kit, conn, sid, sha, "Acme Ltd", leiCode="529900T8BM49AURSDO55")        # other LEI
        g = _person(kit, conn, sid, sha, "Dana Smith")
        h = _person(kit, conn, sid, sha, "Dana Smith")
        projector.rebuild_case(conn, kit.case)
        rep = resolution.find_candidates(conn, kit.case)
        pairs = {frozenset((x.a.member, x.b.member)): x for x in rep.candidates}

        assert frozenset((a, b)) in pairs and frozenset((d, e)) in pairs
        assert frozenset((b, c)) not in pairs              # name equal but birth dates conflict
        assert frozenset((a, c)) not in pairs
        assert frozenset((d, f)) not in pairs and frozenset((e, f)) not in pairs    # LEI conflict
        assert "leiCode_conflict" in resolution.compare(
            *[r for r in resolution.load_records(conn, kit.case).values() if r.member in (d, f)]
        ).flags
        dana = pairs[frozenset((g, h))]
        assert "name_only" in dana.flags and dana.score < pairs[frozenset((a, b))].score

        top = rep.candidates[0]
        assert {f["field"] for f in top.fields} >= {"name", "birthDate"} or top.score > 10
        # proposals are undecided judgments carrying the comparison vector; no cluster moves
        before = conn.execute("SELECT count(*) AS n FROM proj_entity WHERE case_id=%s",
                              (kit.case,)).fetchone()["n"]
        ids = resolution.propose(conn, kit.actor, kit.case, rep.candidates)
        assert len(ids) == len(rep.candidates)
        row = conn.execute("SELECT verdict, comparison FROM judgment WHERE id=%s", (ids[0],)).fetchone()
        assert row["verdict"] == "undecided" and row["comparison"]["fields"]
        projector.rebuild_case(conn, kit.case)
        assert conn.execute("SELECT count(*) AS n FROM proj_entity WHERE case_id=%s",
                            (kit.case,)).fetchone()["n"] == before
        # already-proposed pairs are not proposed again
        assert resolution.find_candidates(conn, kit.case).candidates == []


def test_missing_data_is_not_treated_as_a_mismatch(db, kit):
    with db.connection() as conn:
        sid, sha = kit.source(conn), kit.artifact(conn, b"r2")
        a = _person(kit, conn, sid, sha, "Boris Kuznetsov")
        b = _person(kit, conn, sid, sha, "Boris Kuznetsov", birthDate="1960-01-01")
        projector.rebuild_case(conn, kit.case)
        c = resolution.find_candidates(conn, kit.case).candidates[0]
    assert c.score == 6.0 and {f["field"] for f in c.fields} == {"name"}     # no penalty for no date
    assert "name_only" in c.flags


def test_explain_pair_names_the_assertion_and_evidence_behind_each_value(db, kit):
    with db.connection() as conn:
        sid, sha = kit.source(conn, grade="B"), kit.artifact(conn, b"r3")
        a = _person(kit, conn, sid, sha, "Irina Volkova", birthDate="1975-05-05")
        b = _person(kit, conn, sid, sha, "Irina Volkova", birthDate="1975-05-05")
        projector.rebuild_case(conn, kit.case)
        recs = resolution.load_records(conn, kit.case)
        ca = next(c for c, r in recs.items() if r.member == a)
        cb = next(c for c, r in recs.items() if r.member == b)
        ex = resolution.explain_pair(conn, kit.case, ca, cb)
    name = next(f for f in ex["fields"] if f["field"] == "name")
    assert name["a"][0]["source_grade"] == "B" and name["a"][0]["evidence"] == [sha]
    assert name["a"][0]["plugin"] == "t"
    assert "email" in ex["unseen_fields"] and "missing data" in ex["note"]


def test_an_agent_may_propose_but_not_decide(db, kit):
    from astral.db import Actor
    from astral.resolver import record_judgment

    agent = Actor(kit.actor.workspace_id, kit.actor.principal_id, "agent")
    with db.connection() as conn:
        with pytest.raises(AssertionRejected):
            record_judgment(conn, agent, kit.case, uuid.uuid4(), uuid.uuid4(), "same")
