"""The plugin registry and community repositories ("the hub").

A repository is a directory (or a URL serving the same files) holding `index.json` and the packages
it lists. It carries four kinds of thing: plugins (connectors and others), site packs, lenses and
machines. Publishing builds the index; installing verifies before anything touches the install.

What is checked, in order, and what is not.
  * The archive's size and sha256 must match the index. That detects a corrupted or swapped file; it
    cannot detect a malicious index, because whoever writes the index writes the hashes.
  * A publisher's Ed25519 signature over (kind, id, version, sha256) says WHO published it and that it
    is the file they signed. It is checked against the operator's trust store: trusted, signed by an
    unknown key, unsigned, or invalid (always refused). Signature never means the code is safe.
  * Plugin archives are extracted by a loader that refuses absolute paths, `..`, links and device
    files, and caps file count and size, then go through the normal plugin review (manifest, lint,
    in-package signature) and are installed DISABLED. A plugin still has to be enabled by a person.
  * A site pack is checked only for being JSON with a licence and a site name here; the site-pack
    plugin validates it for real (and names any pack it refuses) when it loads. Packs are stored in a
    directory per licence, so share-alike data never mixes into the original-work directory.
  * Remote repositories are read through the egress policy. With the default `local-only`, a URL is
    refused, so installing the hub does not by itself make the platform talk to the internet.

There is no public community repository operated by this project; the format and tooling exist so one
can be run. Nothing here vets a third party's intent.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import stat
import uuid
import zipfile
from pathlib import Path
from typing import Any

import psycopg

from astral import audit, egress, lens, machines, signing
from astral.db import Actor
from astral.plugins import registry
from astral.plugins.trust import TrustStore

INDEX = "index.json"
SCHEMA = "astral.repo/v1"
KINDS = ("plugin", "site-pack", "lens", "machine")
MAX_INDEX = 5 * 1024 * 1024
MAX_PACKAGE = 50 * 1024 * 1024
MAX_FILES, MAX_UNPACKED, MAX_FILE = 200, 20 * 1024 * 1024, 5 * 1024 * 1024
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{1,80}$")
_VER = re.compile(r"^[0-9]+(\.[0-9]+){0,3}$")


class HubError(Exception):
    pass


def attestation(kind: str, pid: str, version: str, sha256: str) -> bytes:
    return f"astral.repo-package/v1\n{kind}\n{pid}\n{version}\n{sha256}".encode()


# ------------------------------------------------------------------ building a repository
def _zip_dir(src: Path) -> bytes:
    """A reproducible archive: sorted names, fixed timestamps, plain permissions."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(p for p in src.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"):
            info = zipfile.ZipInfo(f.relative_to(src).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, f.read_bytes())
    return buf.getvalue()


def _entry(kind: str, pid: str, version: str, data: bytes, title: str, description: str, licence: str, extra: dict[str, Any],
           signing_key: tuple[bytes, str | None] | None) -> dict[str, Any]:
    if kind not in KINDS or not _ID.match(pid) or not _VER.match(version):
        raise HubError(f"cannot publish {kind} {pid!r} version {version!r}: ids are lowercase, versions numeric (1.2.3)")
    if not licence.strip():
        raise HubError(f"{pid}: a published package must state its licence")
    sha = hashlib.sha256(data).hexdigest()
    e: dict[str, Any] = {"kind": kind, "id": pid, "version": version, "title": title[:200], "description": description[:500],
                         "licence": licence[:200], "sha256": sha, "size": len(data), "signature": None, **extra}
    if signing_key:
        pub = signing.public_from_private(*signing_key)
        e["signature"] = {"algorithm": "ed25519", "public_key": pub.decode(), "key_fingerprint": signing.fingerprint(pub),
                          "signature": signing.sign(signing_key[0], attestation(kind, pid, version, sha), signing_key[1])}
    return e


def build(out: Path, *, plugins: list[Path] = (), packs: list[Path] = (), packages: list[Path] = (),
          name: str = "repository", signing_key: tuple[bytes, str | None] | None = None) -> dict[str, Any]:
    """Write `out/index.json` and `out/packages/*` for these inputs. `packages` are exported lens or machine packages."""
    out = Path(out)
    (out / "packages").mkdir(parents=True, exist_ok=True)
    entries = []
    for d in plugins:
        m = registry.load_manifest(Path(d))
        data = _zip_dir(Path(d))
        e = _entry("plugin", m.id, m.version, data, m.title, f"{m.stage} plugin, footprint {m.footprint}", m.licence.code if m.licence else "",
                   {"footprint": m.footprint, "path": f"packages/{m.id}-{m.version}.zip"}, signing_key)
        (out / e["path"]).write_bytes(data)
        entries.append(e)
    for f in packs:
        raw = Path(f).read_bytes()
        try:
            p = json.loads(raw)
        except ValueError as ex:
            raise HubError(f"{f}: not JSON") from ex
        site = str(p.get("site", ""))
        e = _entry("site-pack", site, str(p.get("pack_version", "1")), raw, f"{site} site pack", str(p.get("attribution", "")), str(p.get("licence", "")),
                   {"path": f"packages/site-pack-{site}-{p.get('pack_version', 1)}.json"}, signing_key)
        (out / e["path"]).write_bytes(raw)
        entries.append(e)
    for f in packages:
        raw = Path(f).read_bytes()
        pkg = json.loads(raw)
        d = pkg.get("definition") or {}
        kind = "lens" if pkg.get("schema") == lens.PACKAGE else "machine" if pkg.get("schema") == machines.PACKAGE else None
        if not kind:
            raise HubError(f"{f}: not a lens or machine package")
        pid = d.get("lens") or d.get("machine")
        e = _entry(kind, pid, f"{d.get('version')}", raw, pid, str(d.get("description", "")), "see package", {"path": f"packages/{kind}-{pid}-{d.get('version')}.json"}, signing_key)
        (out / e["path"]).write_bytes(raw)
        entries.append(e)
    index = {"schema": SCHEMA, "name": name, "packages": sorted(entries, key=lambda e: (e["kind"], e["id"], e["version"]))}
    (out / INDEX).write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")
    return index


# ------------------------------------------------------------------ reading one
class Repository:
    def __init__(self, location: str, *, remote_policy: egress.EgressPolicy | None = None, fetcher: egress.Fetcher | None = None) -> None:
        self.location = location
        self.remote = location.startswith(("http://", "https://"))
        self.policy, self.fetcher = remote_policy, fetcher or egress.Fetcher()
        self._index: dict[str, Any] | None = None

    def read(self, rel: str, limit: int) -> bytes:
        if rel.startswith(("/", "\\")) or ".." in Path(rel.replace("\\", "/")).parts or ":" in rel:
            raise HubError(f"refusing path {rel!r}")
        if self.remote:
            if self.policy is None:
                raise HubError("remote repositories are disabled (set ASTRAL_HUB_EGRESS=direct to allow them)")
            try:
                res = self.fetcher.fetch(self.policy, self.location.rstrip("/") + "/" + rel, max_bytes=limit)
            except (egress.EgressDenied, OSError) as e:
                raise HubError(f"cannot read {rel}: {e}") from e
            if res.status != 200 or res.truncated:
                raise HubError(f"cannot read {rel}: HTTP {res.status}{' (too large)' if res.truncated else ''}")
            return res.body
        root = Path(self.location).resolve()
        p = (root / rel).resolve()
        if root not in p.parents and p != root:
            raise HubError(f"refusing path {rel!r}")
        if not p.is_file():
            raise HubError(f"{rel} not found in {self.location}")
        if p.stat().st_size > limit:
            raise HubError(f"{rel} is larger than {limit} bytes")
        return p.read_bytes()

    @property
    def index(self) -> dict[str, Any]:
        if self._index is None:
            try:
                idx = json.loads(self.read(INDEX, MAX_INDEX))
            except ValueError as e:
                raise HubError("index.json is not valid JSON") from e
            if not isinstance(idx, dict) or idx.get("schema") != SCHEMA or not isinstance(idx.get("packages"), list):
                raise HubError(f"not an {SCHEMA} repository")
            self._index = idx
        return self._index

    def search(self, query: str = "", kind: str | None = None) -> list[dict[str, Any]]:
        q = query.lower().strip()
        out = []
        for e in self.index["packages"]:
            if kind and e.get("kind") != kind:
                continue
            if q and q not in f"{e.get('id')} {e.get('title')} {e.get('description')}".lower():
                continue
            out.append({k: e.get(k) for k in ("kind", "id", "version", "title", "description", "licence", "footprint", "size")} |
                       {"signed": bool(e.get("signature")), "fingerprint": (e.get("signature") or {}).get("key_fingerprint"), "repository": self.location})
        return out

    def find(self, kind: str, pid: str, version: str | None = None) -> dict[str, Any]:
        cands = [e for e in self.index["packages"] if e.get("kind") == kind and e.get("id") == pid and (version is None or e.get("version") == version)]
        if not cands:
            raise HubError(f"{kind} {pid}{' ' + version if version else ''} is not in {self.location}")
        return max(cands, key=lambda e: _vkey(e["version"]))

    def fetch(self, entry: dict[str, Any]) -> bytes:
        data = self.read(str(entry.get("path", "")), MAX_PACKAGE)
        if len(data) != entry.get("size") or hashlib.sha256(data).hexdigest() != entry.get("sha256"):
            raise HubError(f"{entry['id']}: the file does not match the size/hash in the index")
        return data


def _vkey(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split(".")) if _VER.match(v or "") else (0,)


def verdict(entry: dict[str, Any], trust: TrustStore | None) -> dict[str, Any]:
    """Who published this? Raises HubError for a signature that does not verify."""
    sig = entry.get("signature")
    if not sig:
        return {"status": "unsigned", "detail": "no publisher signature"}
    try:
        pub = sig["public_key"].encode()
        ok = signing.verify(pub, attestation(entry["kind"], entry["id"], entry["version"], entry["sha256"]), sig["signature"])
        fp = signing.fingerprint(pub)
    except (KeyError, AttributeError, signing.SigningError) as e:
        raise HubError(f"unreadable publisher signature: {e}") from e
    if not ok:
        raise HubError(f"{entry['id']}: the publisher signature does not verify")
    if trust and fp in trust.keys:
        return {"status": "trusted", "fingerprint": fp, "detail": f"signed by trusted key '{trust.keys[fp]}'"}
    return {"status": "signed-untrusted", "fingerprint": fp, "detail": "valid signature by a key not in the trust store"}


# ------------------------------------------------------------------ installing
def safe_extract(data: bytes, dest: Path) -> list[str]:
    """Extract a zip into an EMPTY directory, refusing anything that is not a plain, in-tree file."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise HubError("the package is not a valid zip") from e
    infos = z.infolist()
    if not infos or len(infos) > MAX_FILES:
        raise HubError(f"a package holds 1-{MAX_FILES} files")
    total, seen = 0, set()
    root = dest.resolve()
    for i in infos:
        name = i.filename
        parts = Path(name.replace("\\", "/")).parts
        if i.is_dir():
            continue
        if name.startswith(("/", "\\")) or ".." in parts or (parts and re.match(r"^[A-Za-z]:", parts[0])) or "\x00" in name:
            raise HubError(f"unsafe path in package: {name!r}")
        mode = (i.external_attr >> 16) & 0xFFFF
        if mode and not stat.S_ISREG(mode):
            raise HubError(f"{name!r} is not a regular file (links and devices are refused)")
        if name.lower() in seen:
            raise HubError(f"duplicate entry {name!r}")
        seen.add(name.lower())
        if i.file_size > MAX_FILE:
            raise HubError(f"{name!r} is larger than {MAX_FILE} bytes")
        total += i.file_size
        if total > MAX_UNPACKED:
            raise HubError("the package unpacks to more than the allowed size")
        target = (root / Path(*parts)).resolve()
        if root not in target.parents:
            raise HubError(f"unsafe path in package: {name!r}")
    for i in infos:
        if i.is_dir():
            continue
        target = root / Path(*Path(i.filename.replace("\\", "/")).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with z.open(i) as src, open(target, "wb") as out:
            written = 0
            while chunk := src.read(65536):                     # trust the bytes read, not the header's claimed size
                written += len(chunk)
                if written > MAX_FILE:
                    raise HubError(f"{i.filename!r} expands past the allowed size")
                out.write(chunk)
    return sorted(seen)


def install(conn: psycopg.Connection, actor: Actor, repo: Repository, kind: str, pid: str, version: str | None, *, data_dir: Path,
            trust: TrustStore | None, require_trusted: bool = False) -> dict[str, Any]:
    entry = repo.find(kind, pid, version)
    data = repo.fetch(entry)
    v = verdict(entry, trust)
    if require_trusted and v["status"] != "trusted":
        raise HubError(f"this install requires a trusted publisher key; this package is {v['status']} ({v['detail']})")
    out: dict[str, Any] = {"kind": kind, "id": pid, "version": entry["version"], "publisher": v, "licence": entry["licence"]}
    if kind == "plugin":
        dest = data_dir / "plugins" / entry["id"] / entry["version"]
        marker = dest / ".astral-package-sha256"
        if dest.exists():
            if marker.exists() and marker.read_text() == entry["sha256"]:
                pass                                            # same bytes already installed: re-register
            else:
                raise HubError(f"{pid} {entry['version']} is already installed with different content; publish a new version")
        else:
            stage = data_dir / "hub-staging" / uuid.uuid4().hex
            stage.mkdir(parents=True)
            try:
                safe_extract(data, stage)
                (stage / ".astral-package-sha256").write_text(entry["sha256"])
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(stage), str(dest))
            finally:
                shutil.rmtree(stage, ignore_errors=True)
        try:
            m = registry.install_from_dir(conn, actor, dest, trust=trust, require_trusted=False)   # the publisher check above is the gate
        except registry.PluginError:
            shutil.rmtree(dest, ignore_errors=True)             # a rejected plugin leaves nothing behind
            raise
        if (m.id, m.version) != (entry["id"], entry["version"]):
            shutil.rmtree(dest, ignore_errors=True)
            raise HubError(f"the index says {entry['id']} {entry['version']} but the manifest says {m.id} {m.version}")
        out |= {"state": "disabled", "installed_to": str(dest), "note": "installed disabled: review it, then enable it"}
    elif kind == "site-pack":
        try:
            p = json.loads(data)
        except ValueError as e:
            raise HubError("the site pack is not JSON") from e
        lic = str(p.get("licence", "")).strip()
        if not lic or p.get("site") != entry["id"]:
            raise HubError("the pack must state its licence and match its index entry")
        d = data_dir / "packs" / (re.sub(r"[^A-Za-z0-9._-]", "_", lic)[:60] or "unlicensed")        # one directory per licence
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{entry['id']}.json").write_bytes(data)
        out |= {"installed_to": str(d / f"{entry['id']}.json"), "packs_dir": str(d),
                "note": "point the site-pack plugin's packs_dir at this directory; it validates the pack and names it if refused"}
    else:
        pkg = json.loads(data)
        mod = lens if kind == "lens" else machines
        defn, tv = mod.verify_package(pkg, trust) if kind == "lens" else machines.verify_package(pkg, trust)
        (lens if kind == "lens" else machines).save(conn, actor, defn, origin="imported", trust=tv)
        out |= {"state": "saved", "package_trust": tv}
    audit.record(conn, actor, "hub.install", target_kind=kind, target_id=f"{pid}@{entry['version']}",
                 detail={"repository": repo.location, "sha256": entry["sha256"], "publisher": v["status"]})
    return out


def updates(conn: psycopg.Connection, repos: list[Repository]) -> list[dict[str, Any]]:
    """Installed plugins for which a repository lists a newer version."""
    have: dict[str, str] = {}
    for r in conn.execute("SELECT id, version FROM plugin"):
        if _vkey(r["version"]) >= _vkey(have.get(r["id"], "0")):
            have[r["id"]] = r["version"]
    out = []
    for repo in repos:
        try:
            for e in repo.index["packages"]:
                if e.get("kind") == "plugin" and e["id"] in have and _vkey(e["version"]) > _vkey(have[e["id"]]):
                    out.append({"id": e["id"], "installed": have[e["id"]], "available": e["version"], "repository": repo.location,
                                "signed": bool(e.get("signature"))})
        except HubError as ex:
            out.append({"repository": repo.location, "error": str(ex)})
    return out
