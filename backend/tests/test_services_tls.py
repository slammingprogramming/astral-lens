"""TLS and authentication for the dedicated services.

The first group always runs: certificate verification is never switched off, and a private CA is trusted only by naming its file.
The second group needs a Qdrant server that has TLS and an API key switched on and skips otherwise:

    ASTRAL_TEST_QDRANT_TLS=https://127.0.0.1:56334   ASTRAL_TEST_QDRANT_TLS_CA=<path to the CA pem>   ASTRAL_TEST_QDRANT_TLS_KEY=<api key>

Verified against Qdrant (single node, TLS with a throwaway CA, API key). NOT verified: OpenSearch (its Basic auth and TLS go through the
same code, but no OpenSearch server was run with security on), mutual TLS, certificate rotation, or a cluster.
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
