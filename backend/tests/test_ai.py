"""M4: retrieval, citation enforcement, approved plans, egress rules, reports.

HONEST SCOPE. No language model exists on this machine. Model behaviour is played by
a scripted local server that speaks the OpenAI chat protocol, so these tests prove the
ENFORCEMENT (a fabricated identifier, an invented quote, an unknown evidence id or a
dissenting verifier all get a claim rejected; nothing runs before approval; egress
rules bite) and the plumbing. They say nothing about how often a real model fabricates,
or how well it plans or composes. That needs the measured evaluation in docs/11.
"""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from astral import ai, citations, retrieval
from astral.api import create_app
from astral.config import Settings

from conftest import REPO

IBAN = "GB82WEST12345698765432"
DOCS = [
    f"Acme Holdings Ltd was incorporated in London in 2015. Its director is Jane Q Doe. Payments are made to IBAN {IBAN}.\n"
    "The registered office is at 1 High Street.",
    "Contact Marta Novak at marta.novak@example.org about the Zurich shipment of 48213 units next week.",
    "Minutes of the quarterly meeting. Weather was discussed at length. Nothing else of note happened.",
]


def reply(obj):
    return (200, "application/json", {"choices": [{"message": {"content": json.dumps(obj)}}]}, {})


def evidence_id_for(prompt: str, needle: str) -> str:
    for m in re.finditer(r"\[(E\d+)\] \((?:passage|fact)\) (.*?)(?=\n\n\[E|\Z)", prompt, re.S):
        if needle in m.group(2):
            return m.group(1)
    return "E1"


@pytest.fixture()
def make_client(db, dsn, tmp_path):
    clients = []

    def make(**settings):
        app = create_app(Settings(dsn=dsn, data_dir=tmp_path / str(len(clients)), **settings), db=db)
        c = TestClient(app, headers={"x-astral-client": "1"})
        c.__enter__()
        clients.append(c)
        for n in ("doc-ingest", "ner"):
            r = c.post("/plugins", json={"path": str(REPO / "plugins" / "core" / n)}).json()
            c.post(f"/plugins/{r['id']}/{r['version']}/enabled")
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)


def seeded(client, name="ai", identity=None):
    case = client.post("/cases", json={"name": name, **({"collection_identity": identity} if identity else {})}).json()["id"]
    for d in DOCS:
        sha = client.post(f"/cases/{case}/artifacts", content=d.encode(), headers={"content-type": "text/plain"}).json()["sha256"]
        assert client.post(f"/cases/{case}/artifacts/{sha}/ingest").status_code == 200
    return case


def ask_run(client, case, q, approve=True):
    p = client.post(f"/cases/{case}/ask", json={"question": q}).json()
    if approve:
        assert client.post(f"/cases/{case}/plans/{p['id']}/approve").status_code == 200
    return p, client.post(f"/cases/{case}/plans/{p['id']}/run")


# ---------------------------------------------------------------- unit
def test_rrf_rewards_agreement_between_rankers():
    s = retrieval.rrf([[1, 2, 3], [1, 3, 2]])
    assert sorted(s, key=s.get, reverse=True) == [1, 2, 3] or sorted(s, key=s.get, reverse=True) == [1, 3, 2]
    assert s[1] > s[2] and s[1] > s[3]                       # first in both beats everything else
    assert retrieval.rrf([[7]]) == {7: 1 / 61}               # a single ranker degenerates to its own order


def test_fusion_weights_apply_only_when_the_dense_ranker_ran():
    assert retrieval.fusion(["fts", "fuzzy"]) == (60, None)
    assert retrieval.fusion(["fts", "fuzzy", "context-headers", "opensearch"]) == (60, None)
    k, w = retrieval.fusion(["fts", "fuzzy", "opensearch", "dense:intfloat/multilingual-e5-large@qdrant"])
    assert k == retrieval.HYBRID_K and w == [0.5, 0.1, 0.5, 1.0]
    # weighted: a chunk first for the dense ranker beats one first for full-text alone
    s = retrieval.rrf([[1, 2], [2, 1]], k, [0.5, 1.0])
    assert s[2] > s[1] and abs(s[2] - (0.5 / (k + 2) + 1.0 / (k + 1))) < 1e-12
    assert retrieval.rrf([[1, 2], [2, 1]]) == retrieval.rrf([[1, 2], [2, 1]], 60, [1.0, 1.0])


def test_chunks_are_verbatim_slices():
    text = ("Sentence one is here. " * 80) + "\n" + ("Another line follows. " * 60)
    ch = retrieval.chunk_text(text)
    assert len(ch) > 3 and all(text[s:e] == b for s, e, b in ch)
    assert ch[0][0] == 0 and ch[-1][1] == len(text)


def test_plans_are_typed_and_bounded():
    with pytest.raises(ai.PlanError):
        ai.validate_plan({"steps": [{"id": "a", "type": "browse_web", "args": {"url": "x"}}]})
    with pytest.raises(ai.PlanError):
        ai.validate_plan({"steps": [{"id": f"s{i}", "type": "search_text", "args": {"query": "q"}} for i in range(9)]})
    p = ai.validate_plan({"steps": [{"id": "a", "type": "search_text", "args": {"query": "q", "k": 999}}]})
    assert p["steps"][0]["args"]["k"] == ai.MAX_K
    assert [s["type"] for s in ai.rules_plan("What does Jane Q Doe do at Acme Holdings?")["steps"]] == [
        "search_text", "entity_facts", "entity_facts"]


def test_human_review_sample_is_deterministic():
    assert citations._needs_review("same text") == citations._needs_review("same text")
    share = sum(citations._needs_review(f"claim number {i}") for i in range(2000)) / 2000
    assert 0.15 < share < 0.25


# ------------------------------------------------------- no model configured
def test_nothing_runs_before_approval_and_the_layer_writes_no_assertions(make_client, db):
    c = make_client()
    case = seeded(c)
    p = c.post(f"/cases/{case}/ask", json={"question": f"Which IBAN receives payments for Acme Holdings?"}).json()
    assert p["planner"] == "rules" and p["plan"]["estimate"]["egress"] == "none"
    assert p["plan"]["estimate"]["footprint"] == "none" and p["plan"]["estimate"]["writes_assertions"] is False
    r = c.post(f"/cases/{case}/plans/{p['id']}/run")
    assert r.status_code == 409 and "approved" in r.json()["detail"]
    c.post(f"/cases/{case}/plans/{p['id']}/approve")
    with db.connection() as conn:
        before = conn.execute("SELECT count(*) AS n FROM assertion WHERE case_id=%s", (case,)).fetchone()["n"]
    out = c.post(f"/cases/{case}/plans/{p['id']}/run").json()
    with db.connection() as conn:
        after = conn.execute("SELECT count(*) AS n FROM assertion WHERE case_id=%s", (case,)).fetchone()["n"]
    assert before == after
    assert c.post(f"/cases/{case}/plans/{p['id']}/run").status_code == 409         # a plan runs once


def test_extractive_answer_is_verified_against_stored_bytes_and_says_how_weak_it_is(make_client):
    c = make_client()
    case = seeded(c)
    _, r = ask_run(c, case, "Which IBAN receives payments for Acme Holdings?")
    out = r.json()
    assert out["composer"].startswith("extractive")
    top = next(x for x in out["claims"] if IBAN in x["text"])
    assert top["status"] == "weakly_verified" and top["confidence"] == "low"
    a = top["anchors"][0]
    stored = c.get(f"/cases/{case}/artifacts/{a['text_artifact']}").text
    assert IBAN in stored[a["start"]:a["end"]]                                   # the anchor points at real bytes
    assert any("no model verifier" in n for n in out["notes"])
    md = out["report_md"]
    assert all(h in md for h in ("## Key judgments", "## Gaps", "## Indicators", "## Method and limits"))
    assert "writes no assertions" in md and "not model certainty" in md
    # a question the case cannot answer yields an explicit gap, not an invented answer
    _, r2 = ask_run(c, case, "What is the submarine fleet size of Atlantis?")
    assert r2.json()["claims"] == [] and any("not evidence that nothing exists" in g for g in r2.json()["gaps"])


def test_audit_holds_ids_and_counts_never_the_question(make_client, db):
    c = make_client()
    case = seeded(c)
    ask_run(c, case, "Where does SECRETNAME Zephyrine live?")
    with db.connection() as conn:
        rows = conn.execute("SELECT action, detail FROM audit_log WHERE case_id=%s AND action LIKE 'ai.%%'", (case,)).fetchall()
    assert {r["action"] for r in rows} == {"ai.plan", "ai.approve", "ai.run"}
    assert "SECRETNAME" not in str(rows)
    assert c.get("/audit/verify").json()["ok"] is True


# ------------------------------------------------------ egress policy (in code)
@pytest.mark.parametrize("settings,identity,url", [
    ({"llm_egress": "local-only"}, None, "http://93.184.216.34:8080"),               # hosted endpoint, local-only deployment
    ({"llm_egress": "hosted-direct"}, {"egress": "local-only"}, "http://93.184.216.34:8080"),   # local-only CASE wins
    ({"llm_egress": "hosted-redacted"}, None, "http://127.0.0.1:9"),                 # not built: refused, not faked
])
def test_egress_rules_refuse_before_any_request(make_client, settings, identity, url):
    c = make_client(llm_url=url, **settings)
    case = seeded(c, identity=identity)
    r = c.post(f"/cases/{case}/ask", json={"question": "anything at all"})
    assert r.status_code == 403
    assert any(w in r.json()["detail"] for w in ("local-only", "not implemented"))


# ----------------------------------------------- scripted model (enforcement)
def model_server(server, *, claim_for, verdict=True, plan=None):
    """claim_for(evidence_prompt) -> composer JSON. Verifiers answer `verdict`."""
    def handler(req):
        body = json.loads(req["body"])
        system, user = body["messages"][0]["content"], body["messages"][1]["content"]
        schema = body["response_format"]["json_schema"]["schema"]
        if "supported" in schema["properties"]:
            return reply({"supported": verdict, "reason": "scripted"})
        if "steps" in schema["properties"]:
            return reply(plan or {"steps": [{"id": "s1", "type": "search_text", "args": {"query": user, "k": 5}}]})
        return reply(claim_for(user))
    server.add_fn("/v1/chat/completions", handler)


def run_with_model(make_client, server, q="Which IBAN receives payments for Acme Holdings?", **kw):
    c = make_client(llm_url=server.url, llm_egress="local-only")
    case = seeded(c)
    _, r = ask_run(c, case, q)
    assert r.status_code == 200, r.text
    return c, case, r.json()


def test_a_well_cited_claim_is_verified_by_two_model_verifiers(make_client, server):
    model_server(server, claim_for=lambda u: {"claims": [{"text": f"Payments go to IBAN {IBAN}.", "cites": [
        {"evidence_id": evidence_id_for(u, IBAN), "quote": f"Payments are made to IBAN {IBAN}"}]}]})
    _, _, out = run_with_model(make_client, server)
    assert out["composer"].startswith("model via")
    cl = out["claims"][0]
    assert cl["status"] == "verified" and {k["verifier"] for k in cl["checks"]} == {
        "lexical-support", "llm-verifier:strict", "llm-verifier:contradiction"}
    assert not any("no model verifier" in n for n in out["notes"])


def test_a_fabricated_identifier_is_rejected_even_with_a_real_quote(make_client, server):
    model_server(server, claim_for=lambda u: {"claims": [{"text": "Payments go to IBAN DE89370400440532013000.", "cites": [
        {"evidence_id": evidence_id_for(u, IBAN), "quote": f"Payments are made to IBAN {IBAN}"}]}]})
    _, _, out = run_with_model(make_client, server)
    assert out["claims"] == [] and "identifier(s) not in any cited text" in out["rejected"][0]["reasons"][0]
    assert "DE89370400440532013000" in out["rejected"][0]["reasons"][0]


def test_an_invented_quote_and_an_unknown_evidence_id_are_rejected(make_client, server):
    model_server(server, claim_for=lambda u: {"claims": [
        {"text": "Acme was founded in 1999.", "cites": [{"evidence_id": evidence_id_for(u, "Acme"), "quote": "Acme was founded in 1999"}]},
        {"text": "Something.", "cites": [{"evidence_id": "E99", "quote": "Something"}]},
        {"text": "No citation at all."}]})
    _, _, out = run_with_model(make_client, server)
    assert out["claims"] == []
    reasons = " | ".join(r for x in out["rejected"] for r in x["reasons"])
    assert "quote not found" in reasons and "unknown evidence id" in reasons and "no citation" in reasons


def test_a_dissenting_verifier_rejects_the_claim(make_client, server):
    model_server(server, verdict=False, claim_for=lambda u: {"claims": [{"text": f"Payments go to IBAN {IBAN}.", "cites": [
        {"evidence_id": evidence_id_for(u, IBAN), "quote": f"Payments are made to IBAN {IBAN}"}]}]})
    _, _, out = run_with_model(make_client, server)
    assert out["claims"] == [] and "verifier judged" in out["rejected"][0]["reasons"][0]


def test_when_the_model_is_down_everything_degrades_visibly(make_client, server):
    server.add("/v1/chat/completions", "boom", status=500, ctype="text/plain")
    c = make_client(llm_url=server.url, llm_egress="local-only")
    case = seeded(c)
    _, r = ask_run(c, case, "Which IBAN receives payments for Acme Holdings?")
    out = r.json()
    assert out["planner" if "planner" in out else "composer"]
    assert out["composer"].startswith("extractive") and any("fell back" in n for n in out["notes"])
    top = next(x for x in out["claims"] if IBAN in x["text"])
    assert top["status"] == "weakly_verified"                                    # verifier unreachable is not 'supported'
    assert any(k["result"] is None for k in top["checks"] if k["verifier"].startswith("llm-verifier"))


def test_extractive_composer_answers_with_the_asked_word_and_stays_on_the_named_subject():
    own = ("Cobalt Partners LLP was registered in Lisbon in 2003. Its director is Ingrid Costa. "
           "Payments for Cobalt Partners LLP are made to IBAN GB60UQMK32319487574911.\nVolume was 40000 units.")
    other = ("Nimbus Trading LLC was registered in Dublin in 2001. Payments for Nimbus Trading LLC are made to IBAN GB48GKQS24504000883873.\n"
             "The firm trades with Cobalt Partners LLP, a separate company.")
    ev = {"E1": {"id": "E1", "kind": "passage", "text": own}, "E2": {"id": "E2", "kind": "passage", "text": other}}
    who = [c["text"] for c in ai.compose_extractive("Who is the director of Cobalt Partners LLP?", ev)]
    assert any("Ingrid Costa" in t for t in who)                      # the asked word outranks repeating the name
    iban = " ".join(c["text"] for c in ai.compose_extractive("Which IBAN receives payments for Cobalt Partners LLP?", ev))
    assert "GB60UQMK32319487574911" in iban and "GB48GKQS24504000883873" not in iban   # another company's line is not quoted
