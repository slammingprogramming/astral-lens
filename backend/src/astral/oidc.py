"""OIDC bearer-token validation for the team profile (10 §9).

This makes the API a *resource server*: it accepts an ID/access token issued by the
operator's identity provider, checks it, and maps the subject to a principal. It does
NOT implement the browser login flow (authorization-code + PKCE, sessions, logout):
put the UI behind a proxy that does (oauth2-proxy and similar) or have clients obtain
tokens themselves.

Checks: the token is a JWS with a `kid` the provider's JWKS contains; `alg` is RS256 or
ES256 (never `none`, never an HMAC, so a public key cannot be abused as a secret);
signature; `iss` equals the configured issuer; `aud` contains the configured audience;
`exp` has not passed and `nbf` has begun (with a small leeway); `sub` is present.
Discovery and JWKS are fetched only from the configured issuer and must be https
unless the host is local (development).

Verified against a locally generated key pair and a local fake provider, not against a
real identity provider: provider quirks (opaque access tokens, unusual `aud` shapes,
key rollover timing) are untested here.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils

ALGORITHMS = ("RS256", "ES256")
_LOCAL = {"localhost", "127.0.0.1", "[::1]", "::1"}


class OidcError(Exception):
    """The token is not acceptable. The message is for logs, not for the caller."""


def _b64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):                              # a provider that redirects is not followed
        return None


def _default_get(url: str) -> bytes:
    handlers: list[Any] = [_NoRedirect()]
    if urllib.parse.urlparse(url).hostname in _LOCAL:
        handlers.append(urllib.request.ProxyHandler({}))             # never via a system proxy for local development
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.build_opener(*handlers).open(req, timeout=5) as r:      # scheme checked by the caller
        return r.read(1_000_000)


def _public_key(jwk: dict[str, Any]):
    kty = jwk.get("kty")
    if kty == "RSA":
        return rsa.RSAPublicNumbers(int.from_bytes(_b64(jwk["e"]), "big"), int.from_bytes(_b64(jwk["n"]), "big")).public_key()
    if kty == "EC" and jwk.get("crv") == "P-256":
        return ec.EllipticCurvePublicNumbers(int.from_bytes(_b64(jwk["x"]), "big"), int.from_bytes(_b64(jwk["y"]), "big"),
                                             ec.SECP256R1()).public_key()
    raise OidcError(f"unsupported key type {kty}/{jwk.get('crv')}")


class OidcValidator:
    def __init__(self, issuer: str, audience: str, *, leeway: int = 60, get: Callable[[str], bytes] | None = None,
                 clock: Callable[[], float] = time.time, jwks_ttl: int = 600) -> None:
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.leeway = leeway
        self._get = get or _default_get
        self._clock = clock
        self._ttl = jwks_ttl
        self._keys: dict[str, dict[str, Any]] = {}
        self._fetched = 0.0
        self._jwks_uri: str | None = None
        u = urllib.parse.urlparse(self.issuer)
        if u.scheme != "https" and not (u.scheme == "http" and u.hostname in _LOCAL):
            raise OidcError("the issuer must be https (http is allowed only for localhost development)")

    # --------------------------------------------------------------- keys
    def _load_keys(self) -> None:
        if self._jwks_uri is None:
            doc = json.loads(self._get(f"{self.issuer}/.well-known/openid-configuration"))
            if doc.get("issuer", "").rstrip("/") != self.issuer:
                raise OidcError("discovery document names a different issuer")
            self._jwks_uri = doc["jwks_uri"]
            host = urllib.parse.urlparse(self._jwks_uri).hostname
            if host != urllib.parse.urlparse(self.issuer).hostname:
                raise OidcError("jwks_uri is on a different host than the issuer")
        jwks = json.loads(self._get(self._jwks_uri))
        self._keys = {k["kid"]: k for k in jwks.get("keys", []) if "kid" in k and k.get("use", "sig") == "sig"}
        self._fetched = self._clock()

    def _key_for(self, kid: str):
        stale = self._clock() - self._fetched > self._ttl
        if kid not in self._keys and (stale or self._clock() - self._fetched > 30):   # one refresh for rollover, not a loop
            self._load_keys()
        elif stale:
            self._load_keys()
        if kid not in self._keys:
            raise OidcError("unknown signing key")
        return _public_key(self._keys[kid])

    # ----------------------------------------------------------- validate
    def validate(self, token: str) -> dict[str, Any]:
        parts = token.split(".")
        if len(parts) != 3:
            raise OidcError("not a JWS compact token")
        try:
            header, claims = json.loads(_b64(parts[0])), json.loads(_b64(parts[1]))
            sig = _b64(parts[2])
        except (ValueError, json.JSONDecodeError) as e:
            raise OidcError("malformed token") from e
        alg = header.get("alg")
        if alg not in ALGORITHMS:
            raise OidcError(f"algorithm {alg!r} is not accepted")
        if not header.get("kid"):
            raise OidcError("token has no kid")
        key = self._key_for(header["kid"])
        signed = f"{parts[0]}.{parts[1]}".encode()
        try:
            if alg == "RS256":
                if not isinstance(key, rsa.RSAPublicKey):
                    raise OidcError("key type does not match alg")
                key.verify(sig, signed, padding.PKCS1v15(), hashes.SHA256())
            else:
                if not isinstance(key, ec.EllipticCurvePublicKey) or len(sig) != 64:
                    raise OidcError("key type or signature size does not match alg")
                key.verify(utils.encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")),
                           signed, ec.ECDSA(hashes.SHA256()))
        except InvalidSignature as e:
            raise OidcError("signature does not verify") from e
        now = self._clock()
        if claims.get("iss", "").rstrip("/") != self.issuer:
            raise OidcError("wrong issuer")
        aud = claims.get("aud")
        if self.audience not in (aud if isinstance(aud, list) else [aud]):
            raise OidcError("wrong audience")
        if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] + self.leeway < now:
            raise OidcError("token expired")
        if isinstance(claims.get("nbf"), (int, float)) and claims["nbf"] - self.leeway > now:
            raise OidcError("token not yet valid")
        if not claims.get("sub"):
            raise OidcError("no subject")
        return claims
