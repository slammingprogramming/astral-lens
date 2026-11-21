"""TLS and authentication for the dedicated services.

The first group always runs: certificate verification is never switched off, and a private CA is trusted only by naming its file.
The second group needs a Qdrant server that has TLS and an API key switched on and skips otherwise:

    ASTRAL_TEST_QDRANT_TLS=https://127.0.0.1:56334   ASTRAL_TEST_QDRANT_TLS_CA=<path to the CA pem>   ASTRAL_TEST_QDRANT_TLS_KEY=<api key>

The third group needs an OpenSearch server with its security plugin on (HTTPS with a throwaway CA, Basic auth) and skips otherwise:

    ASTRAL_TEST_OPENSEARCH_SECURE=https://127.0.0.1:59200   ASTRAL_TEST_OPENSEARCH_SECURE_CA=<path to the CA pem>   ASTRAL_TEST_OPENSEARCH_SECURE_AUTH=admin:<password>

Verified against Qdrant (single node, TLS with a throwaway CA, API key) and against OpenSearch 2.17.1 (single node, security plugin on, HTTPS
with a throwaway CA, Basic auth). NOT verified: mutual TLS, certificate rotation, role-limited users, or a cluster.
"""

from __future__ import annotations

import datetime
import ipaddress
import os
import ssl
import uuid

import pytest

from astral import services

URL = os.environ.get("ASTRAL_TEST_QDRANT_TLS")
CA = os.environ.get("ASTRAL_TEST_QDRANT_TLS_CA")
KEY = os.environ.get("ASTRAL_TEST_QDRANT_TLS_KEY")
needs_tls = pytest.mark.skipif(not (URL and CA and KEY), reason="ASTRAL_TEST_QDRANT_TLS(_CA/_KEY) not set: no TLS Qdrant server available")
OS_URL = os.environ.get("ASTRAL_TEST_OPENSEARCH_SECURE")
OS_CA = os.environ.get("ASTRAL_TEST_OPENSEARCH_SECURE_CA")
OS_AUTH = os.environ.get("ASTRAL_TEST_OPENSEARCH_SECURE_AUTH")
needs_os_secure = pytest.mark.skipif(not (OS_URL and OS_CA and OS_AUTH), reason="ASTRAL_TEST_OPENSEARCH_SECURE(_CA/_AUTH) not set: no secured OpenSearch available")


def _ca_file(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    k = rsa.generate_private_key(65537, 2048)
    n = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "t")])
    now = datetime.datetime.now(datetime.timezone.utc)
    c = (x509.CertificateBuilder().subject_name(n).issuer_name(n).public_key(k.public_key()).serial_number(1)
         .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=1))
         .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(k, hashes.SHA256()))
    p = tmp_path / "ca.pem"
    p.write_bytes(c.public_bytes(serialization.Encoding.PEM))
    return str(p)


def test_verification_is_never_switched_off_with_or_without_a_private_ca(tmp_path):
    services._tls_context.cache_clear()
    for ctx in (services._tls_context(None), services._tls_context(_ca_file(tmp_path))):
        assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True


def test_a_private_ca_is_trusted_only_by_naming_its_file(tmp_path, monkeypatch):
    ca = _ca_file(tmp_path)
    monkeypatch.setenv("ASTRAL_SERVICES_CA_FILE", ca)
    services._tls_context.cache_clear()
    a = services._tls_context(ca)
    monkeypatch.delenv("ASTRAL_SERVICES_CA_FILE")
    b = services._tls_context(None)
    assert a is not b and len(a.get_ca_certs()) == 1            # only the named CA, not the system store
    assert len(b.get_ca_certs()) != 1 or os.name == "nt"        # the default context uses the system store


def test_an_unreachable_https_service_is_a_service_error_that_does_not_leak_the_key():
    svc = services.QdrantService("https://127.0.0.1:9", api_key="s3cret-key-value")
    with pytest.raises(services.ServiceError) as e:
        svc.ping()
    assert "s3cret-key-value" not in str(e.value)


@needs_tls
def test_a_private_ca_server_is_refused_until_its_ca_is_named(monkeypatch):
    monkeypatch.delenv("ASTRAL_SERVICES_CA_FILE", raising=False)
    services._tls_context.cache_clear()
    with pytest.raises(services.ServiceError) as e:
        services.QdrantService(URL, api_key=KEY).ping()
    assert "CERTIFICATE_VERIFY_FAILED" in str(e.value) or "certificate" in str(e.value).lower()
    assert KEY not in str(e.value)


@needs_tls
def test_the_server_refuses_a_missing_or_wrong_api_key_even_over_a_trusted_connection(monkeypatch):
    monkeypatch.setenv("ASTRAL_SERVICES_CA_FILE", CA)
    services._tls_context.cache_clear()
    for key in (None, "wrong-key-" + uuid.uuid4().hex):
        with pytest.raises(services.ServiceError) as e:
            services.QdrantService(URL, api_key=key).ping()
        assert "401" in str(e.value) or "403" in str(e.value)
        assert (key or "") not in str(e.value) or key is None


@needs_tls
def test_with_the_ca_and_the_key_vectors_round_trip_over_tls(monkeypatch):
    monkeypatch.setenv("ASTRAL_SERVICES_CA_FILE", CA)
    services._tls_context.cache_clear()
    svc = services.QdrantService(URL, api_key=KEY)
    assert svc.ping()
    model = "tlstest" + uuid.uuid4().hex[:6]
    name = svc.ensure_collection(model, 4)
    try:
        assert name in svc.collections()
    finally:
        svc._c("DELETE", f"/collections/{name}")
    assert name not in svc.collections()


# --------------------------------------------------------------------------- OpenSearch with security on
@needs_os_secure
def test_a_secured_opensearch_is_refused_until_its_ca_is_named_and_the_password_never_leaks(monkeypatch):
    monkeypatch.delenv("ASTRAL_SERVICES_CA_FILE", raising=False)
    services._tls_context.cache_clear()
    with pytest.raises(services.ServiceError) as e:
        services.OpenSearchService(OS_URL, auth=OS_AUTH).ping()
    assert "certificate" in str(e.value).lower()
    assert OS_AUTH.split(":", 1)[1] not in str(e.value)


@needs_os_secure
def test_a_missing_or_wrong_password_is_refused_with_a_clear_status(monkeypatch):
    monkeypatch.setenv("ASTRAL_SERVICES_CA_FILE", OS_CA)
    services._tls_context.cache_clear()
    for auth in (None, "admin:not-the-password"):
        with pytest.raises(services.ServiceError) as e:
            services.OpenSearchService(OS_URL, auth=auth).ping()
        assert "401" in str(e.value)
        assert "not-the-password" not in str(e.value)


@needs_os_secure
def test_with_the_ca_and_the_password_text_round_trips_and_stays_case_isolated(db, monkeypatch):
    from test_services import add_chunks, new_case

    from astral import retrieval

    monkeypatch.setenv("ASTRAL_SERVICES_CA_FILE", OS_CA)
    services._tls_context.cache_clear()
    ix = f"astral-test-{uuid.uuid4().hex[:8]}"
    text = services.OpenSearchService(OS_URL, index=ix, auth=OS_AUTH)
    info = text.ping()
    assert info["distribution"] == "opensearch"
    svc = services.Services(text, None)
    a, b = new_case(db), new_case(db)
    try:
        with db.connection() as conn:
            add_chunks(conn, a, ["The vessel Marlin docked at pier nine", "customs inspected the containers"])
            add_chunks(conn, b, ["The vessel Marlin appears only in the other case"])
            r = retrieval.search(conn, a, "vessel Marlin pier", k=5, services=svc)
            assert "opensearch" in r["rankers"]
            assert r["hits"][0]["text"].startswith("The vessel Marlin docked")
            assert all("other case" not in h["text"] for h in r["hits"])
            assert text.count(a) == 2
            svc.forget_case(conn, a)
        assert text.count(a) == 0
    finally:
        try:
            services._call("DELETE", f"{OS_URL}/{ix}", auth=OS_AUTH, ok=(200, 404))
        except services.ServiceError:
            pass
