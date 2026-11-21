"""M1 exit test (docs/11-roadmap.md): acceptance scenario 3, "name to network",
on a cpu-only box -- through the HTTP API.

HONEST SCOPE: the registries are a local HTTP server returning hand-written
responses in each service's documented shape. This proves the whole path
(connector -> http.fetch -> egress -> evidence -> assertions -> resolution ->
graph) and the handling of failures; it does NOT prove the live services still
answer in these shapes. See docs/11-roadmap.md "M1 status".
"""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from astral import cases
from astral.api import create_app
from astral.config import Settings

from conftest import REPO

CONNECTORS = REPO / "plugins" / "connectors"
ALL = ["gleif", "companies-house", "sec-edgar", "opencorporates", "opensanctions", "ofac-sdn",
       "icij-offshore-leaks", "wayback-cdx", "searxng"]
CORE = ["web-capture"]
LEI, PARENT_LEI = "5493001KJTIIGC8Y1R12", "529900T8BM49AURSDO55"


def fixtures(server):
    base = server.url
    server.add("/lei-records/" + PARENT_LEI, {"data": {
        "id": PARENT_LEI, "attributes": {"lei": PARENT_LEI, "entity": {
            "legalName": {"name": "ACME GROUP PLC"}, "jurisdiction": "GB", "registeredAs": "00998877",
            "legalAddress": {"city": "London", "country": "GB"}, "status": "ACTIVE"}}}})
    server.add("/lei-records", {"data": [{
        "type": "lei-records", "id": LEI,
        "attributes": {"lei": LEI, "entity": {
            "legalName": {"name": "ACME HOLDINGS LIMITED"},
            "otherNames": [{"name": "ACME HOLDINGS LTD", "type": "ALTERNATIVE_LANGUAGE_LEGAL_NAME"}],
            "legalAddress": {"addressLines": ["1 High Street"], "city": "London", "country": "GB",
                             "postalCode": "EC1A 1BB"},
            "registeredAs": "12345678", "jurisdiction": "GB", "status": "ACTIVE"},
            "registration": {"status": "ISSUED"}},
        "relationships": {"direct-parent": {"links": {"lei-record": f"{base}/lei-records/{PARENT_LEI}"}}}}]})

    server.add("/search/companies", {"items": [
        {"company_number": "12345678", "title": "ACME HOLDINGS LIMITED", "company_status": "active"}]})
    server.add("/company/12345678", {
        "company_name": "ACME HOLDINGS LIMITED", "company_number": "12345678", "type": "ltd",
        "company_status": "active", "date_of_creation": "2015-03-01",
        "registered_office_address": {"address_line_1": "1 High Street", "locality": "London",
                                      "postal_code": "EC1A 1BB", "country": "United Kingdom"}})
    server.add("/company/12345678/officers", {"items": [{
        "name": "DOE, Jane Q", "officer_role": "director", "appointed_on": "2015-04-01",
        "date_of_birth": {"month": 3, "year": 1970}, "nationality": "British",
        "country_of_residence": "United Kingdom",
        "links": {"officer": {"appointments": "/officers/abc123/appointments"}}}]})
    server.add("/company/12345678/persons-with-significant-control", {"items": [{
        "name": "Acme Group PLC", "kind": "corporate-entity-person-with-significant-control",
        "natures_of_control": ["ownership-of-shares-75-to-100-percent"], "notified_on": "2016-04-06",
        "identification": {"registration_number": "00998877"}}]})

    server.add("/search/default", {"results": [{
        "id": "NK-xyz", "caption": "Jane Doe", "schema": "Person",
        "properties": {"name": ["Jane Doe"], "birthDate": ["1970-03-02"], "nationality": ["gb"],
                       "topics": ["role.pep"]},
        "datasets": ["gb_parliament"]}], "total": {"value": 1}})

    server.add("/search", {"results": [
        {"url": "https://acme.example/about", "title": "About Acme Holdings",
         "content": "Acme Holdings Ltd is a London company.", "engines": ["duckduckgo"]}],
        "unresponsive_engines": [["google", "timeout"]]})

    server.add("/files/company_tickers.json", "upstream exploded", status=500, ctype="text/plain")


@pytest.fixture()
def client(db, dsn, tmp_path, server, monkeypatch):
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "test-key")
    monkeypatch.setenv("OPENSANCTIONS_API_KEY", "test-key")
    monkeypatch.delenv("OPENCORPORATES_API_TOKEN", raising=False)
    fixtures(server)
    app = create_app(Settings(dsn=dsn, data_dir=tmp_path), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        yield c


def install_all(client, db):
    for d in [CONNECTORS / n for n in ALL] + [REPO / "plugins" / "core" / n for n in CORE]:
        r = client.post("/plugins", json={"path": str(d)})
        assert r.status_code == 201, r.text
        client.post(f"/plugins/{r.json()['id']}/{r.json()['version']}/enabled")


def vouch_sources(db, actor):
    """The operator grades sources up front. Origin keys come from manifests."""
    grades = {"connector.gleif": ("gleif", "B"), "connector.companies-house": ("uk-companies-house", "A"),
              "connector.opensanctions": ("opensanctions", "B"), "connector.searxng": ("searxng", "E")}
    with db.connection() as conn:
        for name, (origin, g) in grades.items():
            cases.get_or_create_source(conn, actor, kind="plugin", name=name, origin_key=origin,
                                       default_source_grade=g)


def test_m1_exit_name_to_network(client, db, actor, server):
    install_all(client, db)
    vouch_sources(db, actor)
    case = client.post("/cases", json={"name": "m1-exit"}).json()["id"]
    cfg = {"connector.sec-edgar": {"contact": "ops@example.org", "www_url": server.url, "data_url": server.url},
           "connector.gleif": {"base_url": server.url}, "connector.companies-house": {"base_url": server.url},
           "connector.opencorporates": {"base_url": server.url},
           "connector.opensanctions": {"base_url": server.url},
           "connector.searxng": {"base_url": server.url}, "connector.wayback-cdx": {"base_url": server.url}}

    # ---- 1. name + country in, coverage-first report out
    r = client.post(f"/cases/{case}/investigate/name",
                    json={"name": "Acme Holdings Ltd", "country": "GB", "plugin_configs": cfg})
    assert r.status_code == 200, r.text
    rep = r.json()
    by = {s["plugin"]: s for s in rep["sources"]}
    outcome = lambda p: [c["outcome"] for c in by[p]["coverage"]]            # noqa: E731
    assert outcome("connector.gleif") == ["hits"] and outcome("connector.companies-house") == ["hits"]
    assert outcome("connector.opensanctions") == ["hits"] and "hits" in outcome("connector.searxng")
    # sources that could not be asked are UNKNOWN, with a reason, never "no results"
    assert outcome("connector.opencorporates") == ["unknown"]
    assert "OPENCORPORATES_API_TOKEN" in by["connector.opencorporates"]["coverage"][0]["note"]
    assert outcome("connector.sec-edgar") == ["unknown"] and "HTTP 500" in by["connector.sec-edgar"]["coverage"][0]["note"]
    assert outcome("connector.ofac-sdn") == ["unknown"] and by["connector.ofac-sdn"]["status"] == "skipped"
    assert outcome("connector.wayback-cdx") == ["unknown"]
    engines = [c for c in by["connector.searxng"]["coverage"] if c["source"] == "SearXNG engines"]
    assert engines and engines[0]["outcome"] == "unknown" and "google" in engines[0]["note"]
    assert rep["coverage_summary"]["unknown"] >= 5 and rep["coverage_summary"]["no_hits"] == 0

    # ---- 2. entities arrive unmerged; the resolver PROPOSES, a person decides
    ents = client.get(f"/cases/{case}/entities").json()
    acme = [e for e in ents if e["schema_name"] == "Company" and "ACME HOLDINGS" in (e["caption"] or "").upper()]
    assert len(acme) == 2                                   # GLEIF record and CH record, not auto-merged
    props = client.get(f"/cases/{case}/proposals").json()
    pair = next(p for p in props if {p["comparison"]["a_caption"], p["comparison"]["b_caption"]}
                == {"ACME HOLDINGS LIMITED"} and p["comparison"]["score"] >= 15)
    # the PEP record and the company director are proposed too, but ranked lower
    assert {c["a_caption"] for c in (p["comparison"] for p in props)} >= {"Jane Doe"} or \
        {c["b_caption"] for c in (p["comparison"] for p in props)} >= {"Jane Doe"}
    fields = {f["field"]: f for f in pair["comparison"]["fields"]}
    assert fields["registrationNumber"]["level"] == "shared" and fields["name"]["level"] == "exact"

    # merge-explanation view: per-field evidence with grades and plugin
    ex = client.get(f"/cases/{case}/merge-explanation", params={"a": pair["a_cluster"], "b": pair["b_cluster"]}).json()
    reg = next(f for f in ex["fields"] if f["field"] == "registrationNumber")
    assert {x["plugin"] for x in reg["a"] + reg["b"]} == {"connector.gleif", "connector.companies-house"}
    assert {x["source_grade"] for x in reg["a"] + reg["b"]} == {"B", "A"}

    # ---- 3. the human decides: same. Provenance is preserved, corroboration counted once per origin
    j = client.post(f"/cases/{case}/judgments", json={
        "entity_a": pair["a_entity"], "entity_b": pair["b_entity"], "verdict": "same",
        "reasoning": "same registration number and legal name"})
    assert j.status_code == 201
    client.post(f"/cases/{case}/rebuild")
    merged = [e for e in client.get(f"/cases/{case}/entities").json()
              if e["schema_name"] == "Company" and "ACME HOLDINGS" in (e["caption"] or "").upper()]
    assert len(merged) == 1 and merged[0]["member_count"] == 2
    detail = client.get(f"/cases/{case}/entities/{merged[0]['cluster_id']}").json()
    regno = [p for p in detail["properties"] if p["predicate"] == "registrationNumber"]
    assert len(regno) == 2 and {p["independent_sources"] for p in regno} == {2}   # two origins agree

    # ---- 4. the graph: ownership + directorship edges, each clickable to its record
    g = client.get(f"/cases/{case}/graph", params={"documents": "false"}).json()
    node = {n["id"]: n for n in g["nodes"]}
    kinds = {(node[e["source"]]["caption"], e["label"], node[e["target"]]["caption"]) for e in g["edges"]}
    assert ("Jane Q Doe", "director of", "ACME HOLDINGS LIMITED") in kinds
    assert any(lab == "owns" and tgt == "ACME HOLDINGS LIMITED" and "ACME GROUP" in src.upper()
               or lab == "owns" and tgt == "ACME HOLDINGS LIMITED" and "Acme Group" in src
               for src, lab, tgt in kinds)
    d_edge = next(e for e in g["edges"] if e["label"] == "director of")
    assert d_edge["source_grade"] == "A" and d_edge["info_grade"] == 2          # weakest link of its assertions
    assert d_edge["properties"]["role"] == "director" and d_edge["properties"]["startDate"] == "2015-04-01"

    aid = d_edge["assertions"][0]
    ex = client.get(f"/cases/{case}/assertions/{aid}/explain").json()
    sha = ex["originating_artifacts"][0]
    raw = client.get(f"/cases/{case}/artifacts/{sha}")
    assert raw.status_code == 200 and hashlib.sha256(raw.content).hexdigest() == sha   # the actual API response
    assert "DOE, Jane Q" in raw.content.decode()
    leaf = ex["tree"]["artifacts"][0]["acquisitions"][0]
    assert leaf["acquired_by"] == "connector.companies-house@1.0.0" and "/officers" in leaf["acquired_from"]

    # ---- 5. unknown is persisted, not forgotten
    cov = client.get(f"/cases/{case}/coverage").json()
    assert any(c["plugin_id"] == "connector.opencorporates" and c["coverage"][0]["outcome"] == "unknown" for c in cov)
    with db.connection() as conn:
        n = conn.execute("SELECT count(*) AS n FROM assertion WHERE case_id=%s AND predicate IN"
                         " ('absent','notFound','noRecord')", (case,)).fetchone()["n"]
    assert n == 0                                                   # nothing asserts absence

    # ---- 6. the timeline and the audit trail
    tl = client.get(f"/cases/{case}/timeline").json()
    assert any(e["date"] == "2015-03-01" and e["what"] == "incorporated" for e in tl)
    assert client.get("/audit/verify").json() == {"ok": True, "first_bad_entry": None}
    # egress was summarised by host, never by path
    with db.connection() as conn:
        d = conn.execute("SELECT detail FROM audit_log WHERE action='plugin.invoke' AND case_id=%s"
                         " AND target_id LIKE 'connector.gleif%%'", (case,)).fetchone()["detail"]
    assert d["egress_mode"] == "direct" and "127.0.0.1" in d["hosts_contacted"] and "lei-records" not in str(d)


def test_a_sanctions_file_hit_and_miss_are_reported_differently(client, db, actor, server, tmp_path):
    install_all(client, db)
    case = client.post("/cases", json={"name": "ofac"}).json()["id"]
    xml = ("""<?xml version="1.0"?><sdnList xmlns="https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/XML">
    <publshInformation><Publish_Date>11/04/2026</Publish_Date><Record_Count>2</Record_Count></publshInformation>
    <sdnEntry><uid>36</uid><lastName>ACME TRADING LLC</lastName><sdnType>Entity</sdnType>
      <programList><program>RUSSIA-EO14024</program></programList>
      <akaList><aka><uid>1</uid><type>a.k.a.</type><category>strong</category><lastName>AKME TREYDING</lastName></aka></akaList>
      <addressList><address><uid>9</uid><address1>5 Sample St</address1><city>Dubai</city><country>United Arab Emirates</country></address></addressList>
      <idList><id><uid>3</uid><idType>Digital Currency Address - XBT</idType><idNumber>1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa</idNumber></id></idList></sdnEntry>
    <sdnEntry><uid>99</uid><firstName>Ivan</firstName><lastName>PETROV</lastName><sdnType>Individual</sdnType>
      <dateOfBirthList><dateOfBirthItem><dateOfBirth>2 Mar 1970</dateOfBirth><mainEntry>true</mainEntry></dateOfBirthItem></dateOfBirthList></sdnEntry>
    </sdnList>""").encode()
    up = client.post(f"/cases/{case}/artifacts", content=xml, headers={"content-type": "application/xml"}).json()
    ds = {"connector.ofac-sdn": {"dataset_artifact": up["sha256"]}}

    hit = client.post(f"/cases/{case}/investigate/name", json={
        "name": "Acme Trading", "only": ["connector.ofac-sdn"], "plugin_inputs": ds}).json()
    s = hit["sources"][0]
    assert s["coverage"][0]["outcome"] == "hits" and "published 11/04/2026" in s["coverage"][0]["note"]
    detail = [e for e in client.get(f"/cases/{case}/entities").json() if e["schema_name"] == "Company"]
    ent = client.get(f"/cases/{case}/entities/{detail[0]['cluster_id']}").json()
    preds = {p["predicate"] for p in ent["properties"]}
    assert {"listedIn", "sanctionProgram", "cryptoAddress", "alias", "address"} <= preds

    miss = client.post(f"/cases/{case}/investigate/name", json={
        "name": "Totally Different Name", "only": ["connector.ofac-sdn"], "plugin_inputs": ds}).json()
    c = miss["sources"][0]["coverage"][0]
    assert c["outcome"] == "no_hits" and "scanned 2 entries" in c["note"]
    assert miss["coverage_summary"] == {"hits": 0, "no_hits": 1, "unknown": 0}

    bad = client.post(f"/cases/{case}/investigate/name", json={
        "name": "x", "only": ["connector.ofac-sdn"],
        "plugin_inputs": {"connector.ofac-sdn": {"dataset_artifact": "ab" * 32}}}).json()
    assert bad["sources"][0]["coverage"][0]["outcome"] == "unknown"       # missing file: unknown, not clean


def test_icij_bulk_files_map_to_ownership_and_directorship(client, db, actor, server):
    install_all(client, db)
    case = client.post("/cases", json={"name": "icij"}).json()["id"]

    def up(text: str) -> str:
        return client.post(f"/cases/{case}/artifacts", content=text.encode(),
                           headers={"content-type": "text/csv"}).json()["sha256"]

    ents = up("node_id,name,original_name,former_name,jurisdiction,country_codes,status,address,"
              "incorporation_date,inactivation_date,sourceID\n"
              "10000001,ACME OFFSHORE LTD,,,BVI,VGB,Active,,12-JAN-2001,,Panama Papers\n")
    offs = up("node_id,name,countries,country_codes,sourceID\n20000001,John Q Public,Cyprus,CYP,Panama Papers\n")
    rels = up("node_id_start,node_id_end,rel_type,link,status,start_date,end_date,sourceID\n"
              "20000001,10000001,officer_of,shareholder of,Active,05-MAR-2003,,Panama Papers\n"
              "20000001,10000001,officer_of,director of,Active,,,Panama Papers\n")
    r = client.post(f"/cases/{case}/investigate/name", json={
        "name": "Acme Offshore", "only": ["connector.icij-offshore-leaks"],
        "plugin_inputs": {"connector.icij-offshore-leaks": {
            "nodes": {"entities": ents, "officers": offs}, "relationships": rels}}}).json()
    assert r["sources"][0]["coverage"][0]["outcome"] == "hits" and "not an accusation" in r["sources"][0]["coverage"][0]["note"]
    g = client.get(f"/cases/{case}/graph").json()
    node = {n["id"]: n["caption"] for n in g["nodes"]}
    labels = {(node[e["source"]], e["label"], node[e["target"]]) for e in g["edges"]}
    assert ("John Q Public", "owns", "ACME OFFSHORE LTD") in labels
    assert ("John Q Public", "director of", "ACME OFFSHORE LTD") in labels
    own = next(e for e in g["edges"] if e["label"] == "owns")
    assert own["properties"]["startDate"] == "2003-03-05"


def test_opensanctions_bulk_file_mode(client, db, actor, server):
    install_all(client, db)
    case = client.post("/cases", json={"name": "os-bulk"}).json()["id"]
    lines = [json.dumps({"id": "Q1", "schema": "Person", "caption": "Dmitri Volkov",
                         "properties": {"name": ["Дмитрий Волков"], "alias": ["Dmitri Volkov"],
                                        "birthDate": ["1966-04-11"], "topics": ["sanction"]},
                         "datasets": ["us_ofac_sdn"]}),
             json.dumps({"id": "Q2", "schema": "Company", "properties": {"name": ["Unrelated Corp"]}})]
    sha = client.post(f"/cases/{case}/artifacts", content="\n".join(lines).encode(),
                      headers={"content-type": "application/json"}).json()["sha256"]
    r = client.post(f"/cases/{case}/investigate/name", json={
        "name": "Dmitry Volkov", "schema": "Person", "only": ["connector.opensanctions"],
        "plugin_inputs": {"connector.opensanctions": {"dataset_artifact": sha}}}).json()
    c = r["sources"][0]["coverage"][0]
    assert c["outcome"] == "hits", r["sources"][0]            # Cyrillic + spelling variant still found
    ents = client.get(f"/cases/{case}/entities").json()
    assert [e["caption"] for e in ents if e["schema_name"] == "Person"] == ["Дмитрий Волков"]


def test_commercial_preflight_flags_noncommercial_and_unconfirmed_plugins(db, dsn, tmp_path):
    app = create_app(Settings(dsn=dsn, data_dir=tmp_path, commercial=True), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        for n in ("opensanctions", "gleif", "ofac-sdn"):
            r = c.post("/plugins", json={"path": str(CONNECTORS / n)}).json()
            c.post(f"/plugins/{r['id']}/{r['version']}/enabled")
        found = {(f["model_id"], f["level"]) for f in c.get("/preflight").json()["licence_findings"]}
    assert ("connector.opensanctions", "block-commercial") in found     # CC BY-NC: needs a paid licence
    assert not any(m == "connector.ofac-sdn" for m, _ in found)         # reads a local file, no third party
    # unconfirmed is a warning, never silently fine
    assert all(lvl in ("block-commercial", "warn") for _, lvl in found)


def test_local_only_case_cannot_reach_the_registries(client, db, actor, server):
    install_all(client, db)
    case = client.post("/cases", json={"name": "airgap",
                                       "collection_identity": {"egress": "local-only"}}).json()["id"]
    r = client.post(f"/cases/{case}/investigate/name", json={
        "name": "Acme", "only": ["connector.gleif"],
        "plugin_configs": {"connector.gleif": {"base_url": "https://api.gleif.org/api/v1"}}}).json()
    c = r["sources"][0]["coverage"][0]
    assert c["outcome"] == "unknown" and "local-only" in c["note"]
