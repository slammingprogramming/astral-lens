from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from astral import secrets
from astral.api import create_app
from astral.config import Settings

from conftest import REPO


def test_secrets_are_encrypted_at_rest_and_need_the_passphrase(tmp_path):
    p = tmp_path / "s.json"
    st = secrets.SecretStore(p, "correct horse")
    st.set("COMPANIES_HOUSE_API_KEY", "key-abcdef-123456")
    raw = p.read_text()
    assert "key-abcdef-123456" not in raw and "COMPANIES_HOUSE_API_KEY" in raw   # names visible, values not
    again = secrets.SecretStore(p, "correct horse")
    assert again.get("COMPANIES_HOUSE_API_KEY") == "key-abcdef-123456" and again.get("NOPE") is None
    with pytest.raises(secrets.SecretsError, match="wrong passphrase"):
        secrets.SecretStore(p, "battery staple")


def test_rotation_reencrypts_under_a_new_passphrase_and_salt(tmp_path):
    p = tmp_path / "s.json"
    st = secrets.SecretStore(p, "one")
    st.set("A_KEY_NAME", "value-one-123")
    salt_before = json.loads(p.read_text())["salt"]
    st.rotate("two")
    assert json.loads(p.read_text())["salt"] != salt_before
    assert secrets.SecretStore(p, "two").get("A_KEY_NAME") == "value-one-123"
    with pytest.raises(secrets.SecretsError):
        secrets.SecretStore(p, "one")


def test_secret_names_are_constrained(tmp_path):
    with pytest.raises(secrets.SecretsError):
        secrets.SecretStore(tmp_path / "s.json", "pw").set("lower case", "x")


def test_redactor_masks_values_and_credential_url_parameters():
    r = secrets.Redactor(["tok-SECRET-123456"])
    assert r.text("boom: tok-SECRET-123456 was rejected") == "boom: [REDACTED] was rejected"
    u = r.url("https://api.example.org/v0.4/companies/search?q=acme&api_token=abc123&page=2")
    assert "abc123" not in u and "q=acme" in u and "page=2" in u
    assert "[REDACTED]" in r.url("https://user:pw@example.org/x")
    assert "pw@" not in r.url("https://user:pw@example.org/x")
    assert secrets.Redactor(["ab"]).text("ab cd") == "ab cd"                   # too short to mask safely


def test_a_token_in_a_query_string_never_reaches_the_evidence_record(db, dsn, tmp_path, server, monkeypatch):
    """OpenCorporates puts its token in the URL. It must not be stored as `acquired_from`."""
    monkeypatch.setenv("OPENCORPORATES_API_TOKEN", "oc-token-98765432")
    server.add("/companies/search", {"results": {"companies": [{"company": {
        "name": "Token Test Ltd", "company_number": "1", "jurisdiction_code": "gb"}}]}})
    app = create_app(Settings(dsn=dsn, data_dir=tmp_path), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        d = REPO / "plugins" / "connectors" / "opencorporates"
        c.post("/plugins", json={"path": str(d)})
        c.post("/plugins/connector.opencorporates/1.0.0/enabled")
        case = c.post("/cases", json={"name": "tok"}).json()["id"]
        r = c.post(f"/cases/{case}/plugins/connector.opencorporates/invoke",
                   json={"input": {"name": "Token Test"}, "config": {"base_url": server.url}}).json()
        assert r["status"] == "ok"
        arts = c.get(f"/cases/{case}/artifacts").json()
    assert server.requests[0]["query"]["api_token"] == "oc-token-98765432"      # it WAS sent to the service
    assert all("oc-token-98765432" not in (a["acquired_from"] or "") for a in arts)
    assert "api_token=%5BREDACTED%5D" in arts[0]["acquired_from"] or "[REDACTED]" in arts[0]["acquired_from"]
    with db.connection() as conn:
        dump = json.dumps([dict(r) for r in conn.execute("SELECT acquisition_ctx FROM artifact_acquisition")],
                          default=str)
    assert "oc-token-98765432" not in dump


def test_api_secret_store_endpoints(db, dsn, tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRAL_SECRETS_PASSPHRASE", "api-pass")
    app = create_app(Settings(dsn=dsn, data_dir=tmp_path, secrets_enabled=True), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        assert c.get("/secrets").json()["enabled"] is True
        assert c.put("/secrets/OPENSANCTIONS_API_KEY", json={"value": "os-key-123456"}).status_code == 200
        names = c.get("/secrets").json()
        assert names["names"] == ["OPENSANCTIONS_API_KEY"]
        assert "os-key-123456" not in json.dumps(names)                   # names only, never values
        assert c.put("/secrets/bad name", json={"value": "x"}).status_code == 422


def test_without_a_passphrase_the_api_says_so_and_env_vars_still_work(db, dsn, tmp_path, monkeypatch):
    monkeypatch.delenv("ASTRAL_SECRETS_PASSPHRASE", raising=False)
    app = create_app(Settings(dsn=dsn, data_dir=tmp_path), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        s = c.get("/secrets").json()
        assert s["enabled"] is False and "ASTRAL_SECRETS_PASSPHRASE" in s["note"]
        assert c.put("/secrets/SOME_KEY", json={"value": "x"}).status_code == 409
