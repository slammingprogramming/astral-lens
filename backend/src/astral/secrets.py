"""Encrypted secret store and log redaction (10 §1).

Secrets (API keys for connectors) live in one JSON file whose values are
Fernet-encrypted (AES-128-CBC + HMAC-SHA256) under a key derived from an
operator passphrase with scrypt. The passphrase comes from the environment
(`ASTRAL_SECRETS_PASSPHRASE`) or an OS keystore via the optional `keyring`
package; it is never written to disk. Rotation re-encrypts under a new
passphrase.

What this protects: the secret file at rest (a copied disk image or backup).
What it does NOT protect: a running process, whose memory holds the decrypted
values, or an attacker who can read the passphrase from the same environment.

`Redactor` removes known secret values (and credential-looking URL parameters)
from text before it is stored or returned, so a key cannot leak through a
plugin's log line, an error message, or an `acquired_from` URL.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.parse
from pathlib import Path
from typing import Iterable

from cryptography.fernet import Fernet, InvalidToken

SCRYPT = {"n": 2**15, "r": 8, "p": 1}
SENSITIVE_PARAMS = re.compile(r"^(api[_-]?key|apikey|api[_-]?token|access[_-]?token|token|key|secret|"
                              r"password|passwd|auth|authorization|signature|sig)$", re.I)


class SecretsError(Exception):
    pass


def _derive(passphrase: str, salt: bytes, params: dict) -> bytes:
    import hashlib

    raw = hashlib.scrypt(passphrase.encode(), salt=salt, n=params["n"], r=params["r"],
                         p=params["p"], dklen=32, maxmem=128 * 1024 * 1024)
    return base64.urlsafe_b64encode(raw)


class SecretStore:
    def __init__(self, path: str | Path, passphrase: str) -> None:
        if not passphrase:
            raise SecretsError("a passphrase is required")
        self.path = Path(path)
        self._passphrase = passphrase
        if self.path.exists():
            self._doc = json.loads(self.path.read_text(encoding="utf-8"))
            self._fernet = Fernet(_derive(passphrase, base64.b64decode(self._doc["salt"]), self._doc["kdf"]))
            self._verify()
        else:
            salt = os.urandom(16)
            self._doc = {"version": 1, "kdf": {"name": "scrypt", **SCRYPT},
                         "salt": base64.b64encode(salt).decode(), "secrets": {}}
            self._fernet = Fernet(_derive(passphrase, salt, SCRYPT))
            self._write()

    def _verify(self) -> None:
        try:
            for tok in self._doc["secrets"].values():
                self._fernet.decrypt(tok.encode())
                break
        except InvalidToken as e:
            raise SecretsError("wrong passphrase for this secret store") from e

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._doc, indent=1), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:                         # pragma: no cover - platform dependent
            pass
        os.replace(tmp, self.path)

    # SecretProvider interface
    def get(self, name: str) -> str | None:
        tok = self._doc["secrets"].get(name)
        if tok is None:
            return None
        try:
            return self._fernet.decrypt(tok.encode()).decode()
        except InvalidToken as e:               # pragma: no cover - file tampered with
            raise SecretsError(f"secret {name!r} cannot be decrypted") from e

    def set(self, name: str, value: str) -> None:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise SecretsError("secret names are UPPER_SNAKE_CASE")
        self._doc["secrets"][name] = self._fernet.encrypt(value.encode()).decode()
        self._write()

    def delete(self, name: str) -> bool:
        gone = self._doc["secrets"].pop(name, None) is not None
        if gone:
            self._write()
        return gone

    def names(self) -> list[str]:
        return sorted(self._doc["secrets"])

    def values(self) -> list[str]:
        return [v for n in self.names() if (v := self.get(n))]

    def rotate(self, new_passphrase: str) -> None:
        """Re-encrypt every secret under a new passphrase and a fresh salt."""
        if not new_passphrase:
            raise SecretsError("a passphrase is required")
        plain = {n: self.get(n) for n in self.names()}
        salt = os.urandom(16)
        self._fernet = Fernet(_derive(new_passphrase, salt, SCRYPT))
        self._doc.update(kdf={"name": "scrypt", **SCRYPT}, salt=base64.b64encode(salt).decode(),
                         secrets={n: self._fernet.encrypt(v.encode()).decode() for n, v in plain.items()})
        self._passphrase = new_passphrase
        self._write()


class ChainSecrets:
    """First provider with a value wins: encrypted store, then environment."""

    def __init__(self, *providers) -> None:
        self.providers = [p for p in providers if p is not None]

    def get(self, name: str) -> str | None:
        for p in self.providers:
            v = p.get(name)
            if v is not None:
                return v
        return None


def passphrase_from_environment() -> str | None:
    p = os.environ.get("ASTRAL_SECRETS_PASSPHRASE")
    if p:
        return p
    try:                                         # optional OS keystore
        import keyring

        return keyring.get_password("astral-lens", "secrets")
    except Exception:                            # noqa: BLE001
        return None


class Redactor:
    MASK = "[REDACTED]"

    def __init__(self, values: Iterable[str] = ()) -> None:
        # ignore short values: masking "1" would shred ordinary text
        self.values = sorted({v for v in values if v and len(v) >= 6}, key=len, reverse=True)

    def text(self, s: str) -> str:
        for v in self.values:
            s = s.replace(v, self.MASK)
            enc = urllib.parse.quote(v, safe="")
            if enc != v:
                s = s.replace(enc, self.MASK)
        return s

    def url(self, url: str) -> str:
        """Mask credential-looking query parameters and any known secret value."""
        parts = urllib.parse.urlsplit(url)
        if parts.query:
            pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            q = urllib.parse.urlencode([(k, self.MASK if SENSITIVE_PARAMS.match(k) else v)
                                        for k, v in pairs], safe="[]")
            parts = parts._replace(query=q)
        if parts.password or parts.username:
            host = parts.hostname or ""
            parts = parts._replace(netloc=(f"{self.MASK}@" + host) + (f":{parts.port}" if parts.port else ""))
        return self.text(urllib.parse.urlunsplit(parts))
