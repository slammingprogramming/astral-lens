"""M6: lenses. A lens is data, carries no case content, is shared as a hash-checked (optionally
signed) package, and only ever PROPOSES a plan that a person must approve. Only the AI layer's
read-only step types exist, so only retrieval lenses are expressible here."""

from __future__ import annotations

import copy
import json

import pytest

from astral import lens, signing
from astral.plugins.trust import TrustStore

from test_ai import IBAN, make_client, seeded  # noqa: F401  (make_client is a fixture)

DEFN = {"schema": lens.SCHEMA, "lens": "company-payments", "version": 1, "description": "Where does a company's money go?",
        "inputs": ["company"], "question": "Which IBAN receives payments for {company}?",
        "steps": [{"id": "s1", "type": "search_text", "args": {"query": "payments for {company}", "k": 8}},
                  {"id": "s2", "type": "entity_facts", "args": {"name": "{company}"}}]}


def test_validation_rejects_what_would_be_unsafe_or_meaningless():
    lens.validate(copy.deepcopy(DEFN))
    bad = {
        "wrong schema": {**DEFN, "schema": "x"}, "bad name": {**DEFN, "lens": "Bad Name!"}, "no inputs": {**DEFN, "inputs": []},
        "unused input": {**DEFN, "inputs": ["company", "other"]}, "undeclared placeholder": {**DEFN, "question": "About {who}?"},
        "unknown step": {**DEFN, "steps": [{"id": "s1", "type": "browse_web", "args": {"url": "{company}"}}]},
        "too many steps": {**DEFN, "steps": [{"id": f"s{i}", "type": "search_text", "args": {"query": "{company}"}} for i in range(9)]},
        "version": {**DEFN, "version": 0},
    }
    for why, d in bad.items():
        with pytest.raises(lens.LensError):
            lens.validate(copy.deepcopy(d))


def test_instantiation_fills_parameters_and_insists_on_exactly_the_declared_inputs():
    q, plan = lens.instantiate(DEFN, {"company": "Acme Holdings Ltd"})
    assert q == "Which IBAN receives payments for Acme Holdings Ltd?"
    assert plan["steps"][0]["args"]["query"] == "payments for Acme Holdings Ltd" and plan["steps"][1]["args"]["name"] == "Acme Holdings Ltd"
    for bad in ({}, {"company": "x", "extra": "y"}, {"company": ""}, {"company": "a\nb"}, {"company": "z" * 201}):
        with pytest.raises(lens.LensError):
            lens.instantiate(DEFN, bad)


def test_a_plan_becomes_a_lens_only_if_the_cases_own_words_are_replaced():
    plan = {"steps": [{"id": "s1", "type": "search_text", "args": {"query": "Which IBAN receives payments for Acme Holdings?", "k": 8}},
                      {"id": "s2", "type": "entity_facts", "args": {"name": "Acme Holdings"}}]}
    d = lens.from_plan("Which IBAN receives payments for Acme Holdings?", plan, name="payments", version=1, description="d",
                       parameters={"company": "Acme Holdings"})
    assert "Acme" not in json.dumps(d) and d["inputs"] == ["company"] and "{company}" in d["question"]
    with pytest.raises(lens.LensError, match="at least one parameter"):
        lens.from_plan("q?", plan, name="p", version=1, description="", parameters={})
    with pytest.raises(lens.LensError, match="does not appear"):
        lens.from_plan("Which IBAN receives payments for Acme Holdings?", plan, name="p", version=1, description="",
                       parameters={"company": "Zeta Corp"})


def test_packages_are_hash_checked_and_signatures_decide_trust(tmp_path):
    priv, pub = signing.generate_keypair()
    store = TrustStore(tmp_path / "trusted")
    row = {"definition": DEFN}
    unsigned = lens.export(row)
    assert lens.verify_package(unsigned, store)[1]["status"] == "unsigned"
    signed = lens.export(row, (priv, None))
    assert lens.verify_package(signed, store)[1]["status"] == "signed-untrusted"
    store.add(pub, "acme-labs")
    assert lens.verify_package(signed, store)[1]["status"] == "trusted"
    altered = copy.deepcopy(signed)
    altered["definition"]["description"] = "something else"
    with pytest.raises(lens.LensError, match="altered"):
        lens.verify_package(altered, store)
    resigned = copy.deepcopy(altered)                                     # attacker fixes the hash but cannot forge the signature
    resigned["sha256"] = lens.digest(resigned["definition"])
    with pytest.raises(lens.LensError, match="does not verify"):
        lens.verify_package(resigned, store)
    with pytest.raises(lens.LensError):
        lens.verify_package({"schema": "nope"}, store)


def test_end_to_end_save_export_import_run_and_the_plan_still_needs_approval(make_client):
    c = make_client()
    case = seeded(c)
    assert c.post("/lenses", json=DEFN).status_code == 201
    assert c.post("/lenses", json=DEFN).status_code == 201                                # identical re-save is idempotent
    assert c.post("/lenses", json={**DEFN, "description": "changed"}).status_code == 422  # same version, different content
    assert next(l for l in c.get("/lenses").json() if l["name"] == DEFN["lens"])["trust"] == "local"
    pkg = c.get("/lenses/company-payments/1/export").json()
    assert pkg["sha256"] == lens.digest(DEFN) and pkg["signature"] is None
    pkg["definition"]["lens"] = "company-payments-copy"
    pkg["sha256"] = lens.digest(pkg["definition"])
    imp = c.post("/lenses/import", json=pkg)
    assert imp.status_code == 201 and imp.json()["trust"] == "unsigned"
    # running a lens proposes a plan; it does not run it
    p = c.post(f"/cases/{case}/lenses/company-payments-copy/run", json={"inputs": {"company": "Acme Holdings"}})
    assert p.status_code == 201 and p.json()["planner"] == "lens:company-payments-copy@1" and p.json()["plan"]["estimate"]["writes_assertions"] is False
    pid = p.json()["id"]
    assert c.post(f"/cases/{case}/plans/{pid}/run").status_code == 409                    # not approved: refused
    c.post(f"/cases/{case}/plans/{pid}/approve")
    out = c.post(f"/cases/{case}/plans/{pid}/run").json()
    assert any(IBAN in x["text"] for x in out["claims"])
    assert c.post(f"/cases/{case}/lenses/nope/run", json={"inputs": {"company": "x"}}).status_code == 404
    assert c.post(f"/cases/{case}/lenses/company-payments/run", json={"inputs": {}}).status_code == 422


def test_import_refuses_tampered_packages_and_conflicting_versions(make_client):
    c = make_client()
    pkg = lens.export({"definition": copy.deepcopy(DEFN)})
    pkg["definition"]["question"] = "Quietly different {company}?"
    assert c.post("/lenses/import", json=pkg).status_code == 422                          # hash no longer matches
    good = lens.export({"definition": {**copy.deepcopy(DEFN), "lens": "conflict-test"}})
    assert c.post("/lenses/import", json=good).status_code == 201
    other = {**copy.deepcopy(DEFN), "lens": "conflict-test", "description": "different content, same version"}
    assert c.post("/lenses/import", json=lens.export({"definition": other})).status_code == 422


def test_save_a_finished_plan_as_a_lens_through_the_api(make_client):
    c = make_client()
    case = seeded(c)
    p = c.post(f"/cases/{case}/ask", json={"question": "Which IBAN receives payments for Acme Holdings?"}).json()
    r = c.post(f"/cases/{case}/plans/{p['id']}/save-as-lens", json={"name": "from-plan", "parameters": {"company": "Acme Holdings"}})
    assert r.status_code == 201 and "Acme" not in json.dumps(r.json()["lens"]) and "read this before sharing" in r.json()["warning"]
    assert c.post(f"/cases/{case}/plans/{p['id']}/save-as-lens", json={"name": "bad", "parameters": {"company": "Not In Plan"}}).status_code == 422


def test_signed_export_over_the_api(make_client, tmp_path):
    priv, pub = signing.generate_keypair()
    k = tmp_path / "k.pem"
    k.write_bytes(priv)
    c = make_client(signing_key=str(k))
    c.post("/lenses", json={**DEFN, "lens": "signed-one"})
    pkg = c.get("/lenses/signed-one/1/export?sign=true").json()
    assert pkg["signature"]["key_fingerprint"] == signing.fingerprint(pub)
    assert make_client().get("/lenses/signed-one/1/export?sign=true").status_code in (400, 404)
