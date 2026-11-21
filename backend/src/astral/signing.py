"""Detached Ed25519 signatures, used for exhibit manifests and plugin packages.

A signature proves that the holder of a private key signed these exact bytes. It does
NOT prove that the key belongs to who you think: that needs a key you already trust
(pinned out of band). Nothing here talks to a certificate authority or a registry.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


class SigningError(Exception):
    pass


def generate_keypair(passphrase: str | None = None) -> tuple[bytes, bytes]:
    """(private PEM, public PEM). The private key is encrypted when a passphrase is given."""
    key = Ed25519PrivateKey.generate()
    enc = (serialization.BestAvailableEncryption(passphrase.encode()) if passphrase
           else serialization.NoEncryption())
    return (key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc),
            key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))


def _load_private(pem: bytes, passphrase: str | None) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(pem, passphrase.encode() if passphrase else None)
    except (ValueError, TypeError) as e:
        raise SigningError(f"cannot load private key: {e}") from e
    if not isinstance(key, Ed25519PrivateKey):
        raise SigningError("only Ed25519 keys are supported")
    return key


def load_public(pem: bytes) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(pem)
    except ValueError as e:
        raise SigningError(f"cannot load public key: {e}") from e
    if not isinstance(key, Ed25519PublicKey):
        raise SigningError("only Ed25519 keys are supported")
    return key


def public_from_private(pem: bytes, passphrase: str | None = None) -> bytes:
    return _load_private(pem, passphrase).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def fingerprint(public_pem: bytes) -> str:
    """SHA-256 of the raw 32-byte public key: what an operator pins and compares."""
    raw = load_public(public_pem).public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()


def sign(private_pem: bytes, data: bytes, passphrase: str | None = None) -> str:
    """Base64 signature over exactly these bytes."""
    return base64.b64encode(_load_private(private_pem, passphrase).sign(data)).decode()


def verify(public_pem: bytes, data: bytes, signature_b64: str) -> bool:
    try:
        load_public(public_pem).verify(base64.b64decode(signature_b64, validate=True), data)
        return True
    except (InvalidSignature, ValueError):
        return False
