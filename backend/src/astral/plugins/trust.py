"""Plugin signing and the trust store (10 §7, 04 §6).

A signature binds a publisher's key to *exactly these files* of *this plugin id and
version*. Verification yields one of:

  trusted          valid signature, and the key's fingerprint is in the trust store
  signed-untrusted valid signature by a key nobody here has chosen to trust
  unsigned         no plugin.sig
  invalid          a signature that does not verify, files that changed since signing,
                   or a signature for a different plugin id/version  -> never installed

"Trusted" means the operator put that key in the trust store (a directory of .pub
files); nothing is fetched and no authority vouches for the publisher. A signature does
not make code safe. It says who published it and that it has not been altered since.
Unsigned third-party plugins can still be installed with a warning, because forbidding
them would defeat an open plugin system; operators who want to forbid them set
ASTRAL_PLUGIN_TRUST=require.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astral import signing

SIG_FILE = "plugin.sig"
SCHEMA = "astral.plugin-signature/v1"
_SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache"}
_SKIP_SUFFIX = {".pyc", ".pyo"}


class TrustError(Exception):
    pass


@dataclass
class Verdict:
    status: str                       # trusted | signed-untrusted | unsigned | invalid
    fingerprint: str | None = None
    detail: str = ""
    digest: str | None = None         # SHA-256 over the signed payload, to detect later change

    def as_record(self) -> dict[str, Any]:
        return {"status": self.status, "fingerprint": self.fingerprint, "digest": self.digest, "detail": self.detail}


def package_files(plugin_dir: Path) -> dict[str, str]:
    """{relative path: sha256} for every file that ships, in a stable order."""
    out: dict[str, str] = {}
    for p in sorted(plugin_dir.rglob("*")):
        rel = p.relative_to(plugin_dir)
        if not p.is_file() or rel.as_posix() == SIG_FILE:
            continue
        if any(part in _SKIP_DIRS for part in rel.parts) or p.suffix in _SKIP_SUFFIX:
            continue
        out[rel.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _payload(plugin_id: str, version: str, files: dict[str, str]) -> bytes:
    return json.dumps({"schema": SCHEMA, "id": plugin_id, "version": version, "files": files},
                      sort_keys=True, separators=(",", ":")).encode()


def sign_package(plugin_dir: Path, plugin_id: str, version: str, private_pem: bytes,
                 passphrase: str | None = None) -> dict[str, Any]:
    """Write plugin.sig next to the plugin.yaml. The public key travels with it for
    convenience; trust comes only from the operator's trust store, never from that copy."""
    files = package_files(plugin_dir)
    payload = _payload(plugin_id, version, files)
    pub = signing.public_from_private(private_pem, passphrase)
    rec = {"schema": SCHEMA, "algorithm": "ed25519", "id": plugin_id, "version": version, "files": files,
           "signature": signing.sign(private_pem, payload, passphrase), "public_key": pub.decode(),
           "key_fingerprint": signing.fingerprint(pub)}
    (plugin_dir / SIG_FILE).write_text(json.dumps(rec, indent=2), encoding="utf-8")
    return rec


class TrustStore:
    """Fingerprint -> name, from a directory of public-key PEM files."""

    def __init__(self, directory: Path | None) -> None:
        self.dir = directory
        self.keys: dict[str, str] = {}
        if directory and directory.is_dir():
            for f in sorted(directory.glob("*.pub")):
                try:
                    self.keys[signing.fingerprint(f.read_bytes())] = f.stem
                except signing.SigningError:
                    continue                      # a malformed key file trusts nothing

    def add(self, public_pem: bytes, name: str) -> str:
        if not self.dir:
            raise TrustError("no trust directory configured")
        if not name.replace("-", "").replace("_", "").replace(".", "").isalnum():
            raise TrustError("key name must be letters, digits, '-', '_' or '.'")
        fp = signing.fingerprint(public_pem)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{name}.pub").write_bytes(public_pem)
        self.keys[fp] = name
        return fp


def verify_package(plugin_dir: Path, plugin_id: str, version: str, trust: TrustStore | None) -> Verdict:
    sig_path = plugin_dir / SIG_FILE
    if not sig_path.is_file():
        return Verdict("unsigned", detail="no plugin.sig: the publisher is unknown and the files are unchecked")
    try:
        rec = json.loads(sig_path.read_text(encoding="utf-8"))
        pub = rec["public_key"].encode()
        fp = signing.fingerprint(pub)
        if rec.get("schema") != SCHEMA or rec.get("algorithm") != "ed25519":
            return Verdict("invalid", fp, "unsupported signature format")
        if rec["id"] != plugin_id or rec["version"] != version:
            return Verdict("invalid", fp, f"signed for {rec['id']}@{rec['version']}, not {plugin_id}@{version}")
        payload = _payload(rec["id"], rec["version"], rec["files"])
        if not signing.verify(pub, payload, rec["signature"]):
            return Verdict("invalid", fp, "the signature does not verify")
    except (KeyError, ValueError, TypeError, signing.SigningError) as e:
        return Verdict("invalid", None, f"unreadable signature: {type(e).__name__}: {e}")
    now = package_files(plugin_dir)
    if now != rec["files"]:
        changed = sorted({k for k in set(now) | set(rec["files"]) if now.get(k) != rec["files"].get(k)})
        return Verdict("invalid", fp, "files differ from what was signed: " + ", ".join(changed[:6]))
    digest = hashlib.sha256(payload).hexdigest()
    if trust and fp in trust.keys:
        return Verdict("trusted", fp, f"signed by trusted key '{trust.keys[fp]}'", digest)
    return Verdict("signed-untrusted", fp, "valid signature, but this key is not in the trust store", digest)


def still_matches(plugin_dir: Path, plugin_id: str, version: str, recorded: dict[str, Any]) -> tuple[bool, str]:
    """Recheck a signed install at load time: the files must still be the signed ones."""
    v = verify_package(plugin_dir, plugin_id, version, None)
    if v.status == "invalid":
        return False, v.detail
    if v.digest != recorded.get("digest"):
        return False, "the signed package is not the one that was installed"
    return True, ""
