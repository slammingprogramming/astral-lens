#!/usr/bin/env python3
"""Verify an Astral Lens exhibit bundle. Standard library only (plus `cryptography`
if you want the signature checked). Ships inside every bundle as verify.py.

    python verify.py BUNDLE.zip            # or an unzipped folder
    python verify.py BUNDLE.zip --public-key signer.pub   # a key you pinned yourself
    python verify.py BUNDLE.zip --json

Exit status 0 only if every check passed. This script proves the package is internally
consistent and unaltered since it was written. It does NOT prove that the content is
true, that the collection was complete, or that the package is admissible anywhere.
A copy of this script inside a bundle is only as trustworthy as the bundle; use a copy
you obtained separately if that matters.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

SCHEMA = "astral.exhibit/v1"
GENESIS = b"\x00" * 32
UNLISTED_OK = {"MANIFEST.json", "SHA256SUMS", "MANIFEST.sig"}      # cannot list their own hash


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Source:
    def __init__(self, path: Path) -> None:
        self.zip = zipfile.ZipFile(path) if path.is_file() else None
        self.root = path
        if self.zip is None and not path.is_dir():
            raise SystemExit(f"{path}: not a file or folder")

    def names(self) -> list[str]:
        if self.zip:
            return [n for n in self.zip.namelist() if not n.endswith("/")]
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())

    def read(self, name: str) -> bytes:
        return self.zip.read(name) if self.zip else (self.root / name).read_bytes()


def verify(path: str | Path, public_key_pem: bytes | None = None) -> dict[str, Any]:
    src = _Source(Path(path))
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "") -> bool:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    names = set(src.names())
    if not check("MANIFEST.json present", "MANIFEST.json" in names):
        return _report(checks, None)
    manifest_bytes = src.read("MANIFEST.json")
    try:
        manifest = json.loads(manifest_bytes)
    except ValueError as e:
        check("MANIFEST.json is valid JSON", False, str(e))
        return _report(checks, None)
    check("schema", manifest.get("schema") == SCHEMA, f"{manifest.get('schema')!r}")
    integ = manifest.get("integrity", {})
    check("the export itself found no integrity problems", integ.get("ok") is True,
          "; ".join(integ.get("problems", [])[:5]) if integ.get("ok") is not True else "")

    # 1. every listed file exists and matches; nothing unlisted is present
    listed = {f["path"]: f for f in manifest.get("files", [])}
    bad = []
    for p, f in listed.items():
        if p not in names:
            bad.append(f"missing: {p}")
        else:
            data = src.read(p)
            if _sha(data) != f["sha256"] or len(data) != f["bytes"]:
                bad.append(f"altered: {p}")
    check("every file in the manifest is present and unaltered", not bad, "; ".join(bad[:10]) or f"{len(listed)} files")
    extra = sorted(names - set(listed) - UNLISTED_OK)
    check("no file outside the manifest", not extra, "; ".join(extra[:10]))

    # 2. SHA256SUMS agrees with the bytes (so `sha256sum -c SHA256SUMS` would pass too)
    if "SHA256SUMS" in names:
        wrong = []
        for line in src.read("SHA256SUMS").decode().splitlines():
            if not line.strip():
                continue
            h, _, p = line.partition("  ")
            if p not in names or _sha(src.read(p)) != h:
                wrong.append(p)
        check("SHA256SUMS matches the files", not wrong, "; ".join(wrong[:10]))
    else:
        check("SHA256SUMS present", False)

    # 3. artifacts: named by their own hash
    arts = manifest.get("artifacts", [])
    missing, misnamed = [], []
    included = set()
    for a in arts:
        if not a.get("included", True):
            continue
        included.add(a["sha256"])
        if a["path"] not in names:
            missing.append(a["sha256"][:12])
        elif _sha(src.read(a["path"])) != a["sha256"]:
            misnamed.append(a["sha256"][:12])
    check("every included artifact hashes to its own name", not missing and not misnamed,
          f"missing {missing[:5]} altered {misnamed[:5]}" if (missing or misnamed) else f"{len(included)} artifacts")

    # 4-6. content checks; anything malformed or missing fails a check instead of raising
    for label, fn in (("assertions", _check_assertions), ("audit slice", _check_audit), ("AI answers", _check_ai)):
        try:
            fn(src, names, included, check)
        except (ValueError, KeyError, TypeError, IndexError, UnicodeDecodeError) as e:
            check(f"{label} are well-formed", False, f"{type(e).__name__}: {e}")

    # 7. signature
    sig = _signature(src, names, manifest_bytes, public_key_pem, checks)
    return _report(checks, manifest, sig)


def _check_assertions(src, names, included, check) -> None:
    """Every unretracted assertion's evidence is in the package."""
    unresolved = []
    if "assertions.jsonl" in names:
        for line in src.read("assertions.jsonl").decode().splitlines():
            a = json.loads(line)
            if a.get("retracted"):
                continue
            for e in a["evidence"]:
                if e not in included:
                    unresolved.append(f"#{a['id']}:{e[:12]}")
    check("every unretracted assertion's evidence is in the package", not unresolved, "; ".join(unresolved[:8]))


def _check_audit(src, names, included, check) -> None:
    """Each entry's hash recomputes from its own fields and recorded predecessor."""
    if "audit.jsonl" not in names:
        return
    entries = [json.loads(x) for x in src.read("audit.jsonl").decode().splitlines() if x.strip()]
    bad_rows, linked, prev = [], 0, None
    for e in entries:
        expect = hashlib.sha256(bytes.fromhex(e["prev_hash"]) + _canonical(e["fields"])).hexdigest()
        if expect != e["hash"]:
            bad_rows.append(e["id"])
        if prev is not None and e["prev_hash"] == prev:
            linked += 1
        prev = e["hash"]
    check("each audit entry's hash recomputes", not bad_rows,
          f"{len(entries)} entries; bad ids {bad_rows[:8]}" if bad_rows else
          f"{len(entries)} entries ({linked} directly follow another listed entry; the rest follow entries of other cases, "
          "which are not exported)")


def _check_ai(src, names, included, check) -> None:
    """Every claim cites, and every cited text is in the package and long enough for the span."""
    if "ai_runs.jsonl" not in names:
        return
    problems = []
    for line in src.read("ai_runs.jsonl").decode().splitlines():
        run = json.loads(line)
        for c in (run.get("result") or {}).get("claims", []):
            if not c.get("anchors"):
                problems.append("claim without a citation")
            for a in c.get("anchors", []):
                if a.get("kind") != "span":
                    continue
                path = f"artifacts/{a['text_artifact']}"
                if a["text_artifact"] not in included or path not in names:
                    problems.append(f"cited text {a['text_artifact'][:12]} is not in the package")
                    continue
                text = src.read(path).decode("utf-8", "replace")
                if not (0 <= a["start"] < a["end"] <= len(text)):
                    problems.append(f"span {a['start']}-{a['end']} outside its document")
    check("every AI claim cites text that is in the package", not problems, "; ".join(problems[:6]))


def _signature(src, names, manifest_bytes, public_key_pem, checks) -> dict[str, Any]:
    if "MANIFEST.sig" not in names:
        return {"present": False, "note": "the bundle is unsigned: integrity is checked, authorship is not"}
    sig = json.loads(src.read("MANIFEST.sig"))
    pub = public_key_pem
    source = "supplied by you"
    if pub is None and "signer.pub" in names:
        pub, source = src.read("signer.pub"), "embedded in the bundle (proves only self-consistency; pin the fingerprint)"
    if pub is None:
        checks.append({"check": "signature", "ok": False, "detail": "no public key to check against"})
        return {"present": True, "checked": False}
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        checks.append({"check": "signature", "ok": False, "detail": "install `cryptography` to check the signature"})
        return {"present": True, "checked": False}
    key = serialization.load_pem_public_key(pub)
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    fp = hashlib.sha256(raw).hexdigest()
    try:
        assert isinstance(key, Ed25519PublicKey)
        key.verify(base64.b64decode(sig["signature"]), manifest_bytes)
        ok = True
    except (InvalidSignature, AssertionError, ValueError):
        ok = False
    checks.append({"check": "manifest signature", "ok": ok, "detail": f"key {fp[:16]}… {source}"})
    return {"present": True, "checked": True, "valid": ok, "key_fingerprint": fp, "key_source": source}


def _report(checks, manifest, sig=None) -> dict[str, Any]:
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "signature": sig,
            "case": (manifest or {}).get("case"), "generated_at": (manifest or {}).get("generated_at")}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args
    pub = None
    if "--public-key" in args:
        i = args.index("--public-key")
        pub = Path(args[i + 1]).read_bytes()
        del args[i:i + 2]
    args = [a for a in args if a != "--json"]
    if len(args) != 1:
        print(__doc__)
        return 2
    rep = verify(args[0], pub)
    if as_json:
        print(json.dumps(rep, indent=2))
    else:
        for c in rep["checks"]:
            print(f"[{'ok' if c['ok'] else 'FAIL'}] {c['check']}" + (f" — {c['detail']}" if c["detail"] else ""))
        print("\n" + ("ALL CHECKS PASSED" if rep["ok"] else "VERIFICATION FAILED"))
        if rep.get("signature") and not rep["signature"].get("present"):
            print("Note: " + rep["signature"]["note"])
        print("This checks integrity only. It does not establish that the content is true, complete or admissible.")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
