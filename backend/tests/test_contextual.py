"""Contextual chunk headers. Model behaviour is played by a scripted local server that speaks the OpenAI chat protocol, so these
tests prove the ENFORCEMENT and the plumbing: a header is a search aid only, an invented name or number is rejected, a dead model
stops the run visibly, nothing about a passage's text or citation anchor changes, and erasure removes the headers. They say nothing
about how good a real model's headers are; see docs/11 for the one real-model run.
"""

from __future__ import annotations

import json

import pytest

from astral import contextual
from test_ai import DOCS, IBAN, make_client, reply, seeded  # noqa: F401  (fixtures and helpers shared with the AI tests)

DOC_SUBJECT = "Acme Holdings"


def model(server, header_for):
    def handler(req):
        body = json.loads(req["body"])
        assert body["response_format"]["json_schema"]["schema"]["required"] == ["header"]
        user = body["messages"][1]["content"]
        return reply({"header": header_for(user)})
    server.add_fn("/v1/chat/completions", handler)


def ctx_client(make_client, server):
    return make_client(llm_url=server.url, llm_egress="local-only")


LONG = ("Acme Holdings Ltd annual record.\n" + "The record continues with routine entries about the office and its staff. " * 14
        + f"\nPayments are made to IBAN {IBAN}.\n")


def test_a_header_lets_keyword_search_find_a_passage_that_never_names_its_subject(make_client, server, db):
    import uuid

    from astral import retrieval

    model(server, lambda u: "The payment details part of the Acme Holdings Ltd annual record.")
    c = ctx_client(make_client, server)
    case = c.post("/cases", json={"name": "long"}).json()["id"]
    sha = c.post(f"/cases/{case}/artifacts", content=LONG.encode(), headers={"content-type": "text/plain"}).json()["sha256"]
    assert c.post(f"/cases/{case}/artifacts/{sha}/ingest").status_code == 200
    with db.connection() as conn:
        retrieval.ensure_indexed(conn, c.app.state.store, __import__("astral.db", fromlist=["SOLO_ACTOR"]).SOLO_ACTOR, uuid.UUID(case))
        before = retrieval.search(conn, uuid.UUID(case), "Acme", k=10)
    last = [h for h in before["hits"] if IBAN in h["text"]]
    assert not last or "Acme" not in last[0]["text"]                      # the passage with the IBAN never names the company
    assert not any(IBAN in h["text"] for h in before["hits"])             # so a keyword search for the company misses it
    one = c.post(f"/cases/{case}/retrieval/context", json={"limit": 1}).json()
    assert one["written"] == 1 and one["remaining"] > 0 and one["stopped"] is None
    r = c.post(f"/cases/{case}/retrieval/context", json={"limit": 50}).json()
    assert r["remaining"] == 0 and r["stopped"] is None
    with db.connection() as conn:
        after = retrieval.search(conn, uuid.UUID(case), "Acme", k=10)
    found = [h for h in after["hits"] if IBAN in h["text"]]
    assert found and found[0]["fts_rank"] is None and found[0]["context_rank"] and "Acme" in found[0]["context_header"]
    assert any("model-written context header" in n for n in after["notes"])


def test_the_header_is_a_ranker_and_a_label_never_the_passage_text(make_client, server, db):
    model(server, lambda u: "Part of the Acme Holdings Ltd record.")
    c = ctx_client(make_client, server)
    case = seeded(c)
    c.post(f"/cases/{case}/retrieval/context", json={"limit": 50})
    with db.connection() as conn:
        from astral import retrieval
        res = retrieval.search(conn, __import__("uuid").UUID(case), "Acme")
        texts = {h["chunk_id"]: h["text"] for h in res["hits"]}
        raw = {r["id"]: r["body"] for r in conn.execute("SELECT id, body FROM text_chunk WHERE case_id=%s", (case,))}
    assert "context-headers" in res["rankers"]
    assert res["hits"] and all(h["text"] == raw[h["chunk_id"]] for h in res["hits"])          # the passage is the stored text, unchanged
    assert any(h["context_header"] and h["context_rank"] for h in res["hits"])
    assert all("Part of the Acme" not in t for t in texts.values())                           # the header is never spliced into the text


def test_without_headers_the_ranker_does_not_run_and_nothing_changes(make_client, server, db):
    c = make_client()
    case = seeded(c)
    with db.connection() as conn:
        from astral import retrieval
        res = retrieval.search(conn, __import__("uuid").UUID(case), "Acme")
    assert "context-headers" not in res["rankers"] and all(h["context_header"] is None and h["context_rank"] is None for h in res["hits"])


@pytest.mark.parametrize("header", [
    "The Zurich Holdings account was opened in 1999.",       # a name and a number the document does not contain
    "Payments of 999999 units are described here.",          # an invented number
    "",                                                       # empty
])
def test_an_invented_name_or_number_or_an_empty_header_is_rejected(make_client, server, db, header):
    model(server, lambda u: header)
    c = ctx_client(make_client, server)
    case = seeded(c)
    r = c.post(f"/cases/{case}/retrieval/context", json={"limit": 50}).json()
    assert r["written"] == 0 and r["ungrounded"] + r["unusable"] > 0
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM text_chunk_context WHERE case_id=%s", (case,)).fetchone()["n"] == 0


def test_a_dead_model_stops_the_run_visibly_and_writes_nothing_half_done(make_client, server, db):
    server.add("/v1/chat/completions", "boom", status=500, ctype="text/plain")
    c = ctx_client(make_client, server)
    case = seeded(c)
    r = c.post(f"/cases/{case}/retrieval/context", json={"limit": 50}).json()
    assert r["written"] == 0 and r["stopped"] and "model" in r["stopped"] and r["remaining"] > 0


def test_no_model_configured_is_a_clear_400_and_a_hosted_endpoint_for_a_local_only_case_is_refused(make_client, server):
    c = make_client()
    case = seeded(c)
    r = c.post(f"/cases/{case}/retrieval/context", json={"limit": 5})
    assert r.status_code == 400 and "ASTRAL_LLM_URL" in r.text
    c2 = make_client(llm_url="https://api.example.com", llm_egress="local-only")
    case2 = seeded(c2, name="hosted")
    assert c2.post(f"/cases/{case2}/retrieval/context", json={"limit": 5}).status_code == 403


def test_headers_are_erased_with_the_case_and_isolated_between_cases(make_client, server, db):
    model(server, lambda u: "Part of the Acme Holdings Ltd record.")
    c = ctx_client(make_client, server)
    a, b = seeded(c, name="a"), seeded(c, name="b")
    c.post(f"/cases/{a}/retrieval/context", json={"limit": 50})
    with db.connection() as conn:
        from astral import retrieval
        assert retrieval._ctx_ids(conn, __import__("uuid").UUID(b), "Acme", 10) is None       # b has none: the ranker did not run for it
        assert conn.execute("SELECT count(*) AS n FROM text_chunk_context WHERE case_id=%s", (a,)).fetchone()["n"] > 0
    r = c.post(f"/cases/{a}/erase", json={"legal_basis": "test: erase the case and its headers"})
    assert r.status_code == 200 and r.json()["counts"]["text_chunk_context"] > 0
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM text_chunk_context WHERE case_id=%s", (a,)).fetchone()["n"] == 0


def test_grounding_and_excerpt_rules():
    doc = "Acme Holdings Ltd was incorporated in London in 2015."
    assert contextual.grounded("A note about Acme Holdings in London.", doc)
    assert not contextual.grounded("A note about Zurich.", doc)
    assert not contextual.grounded("Founded in 1999.", doc)
    assert contextual.grounded("Founded in 2015.", doc)
    long = "x" * 20000
    ex = contextual.excerpt(long, 10000, 10800)
    assert len(ex) < 6000 and ex.startswith("x" * 100)
    assert contextual.excerpt(doc, 0, 5) == doc
    assert contextual.clean({"header": "  two   words \n"}) == "two words" and contextual.clean({"header": "y" * 301}) is None and contextual.clean([]) is None
