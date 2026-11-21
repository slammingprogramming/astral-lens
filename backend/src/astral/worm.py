"""Write-once preservation of evidence in an S3 bucket with Object Lock.

What this gives. A byte-identical copy of an artifact (or an exhibit package) held by a storage
service that, in COMPLIANCE mode, refuses to delete or shorten it until the retention date, even to
the account owner. Every copy is written with a SHA-256 checksum the service verifies on receipt, and
`verify` reads it back and compares the bytes with the digest, so "still intact" is checked, not assumed.

What it does not give.
  * It does not make the platform's own database or local evidence directory immutable. They have
    their own append-only protections; this is an independent second copy that survives their loss.
  * It cannot be undone. A COMPLIANCE-mode object stays until its retention ends. That collides with a
    legally required erasure of a case, so the erase route refuses to proceed silently when a case has
    preserved copies: the operator must acknowledge that those copies remain, and the erasure record
    states until when.
  * Immutability here is the storage service's claim. Nothing in this repository can prove a given
    S3-compatible server honours it; a service you do not control can be asked, not forced.
  * Object Lock must be enabled when the bucket is created. A bucket without it is refused rather than
    quietly used as ordinary storage.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

from astral import audit
from astral.db import Actor
from astral.evidence import EvidenceStore

MAX_OBJECT = 512 * 1024 * 1024


class WormError(Exception):
    pass


@dataclass
class WormConfig:
    endpoint: str
    bucket: str
    access_key: str
    secret_key: str
    region: str = "us-east-1"
    mode: str = "COMPLIANCE"
    retain_days: int = 365


class WormStore:
    def __init__(self, cfg: WormConfig, client: Any | None = None) -> None:
        if cfg.mode not in ("COMPLIANCE", "GOVERNANCE"):
            raise WormError("mode must be COMPLIANCE or GOVERNANCE")
        if not 1 <= cfg.retain_days <= 36500:
            raise WormError("retain_days must be 1..36500")
        self.cfg, self._client = cfg, client

    @property
    def s3(self) -> Any:
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as e:
                raise WormError("WORM storage needs boto3 (pip install 'astral-lens[worm]')") from e
            self._client = boto3.client(
                "s3", endpoint_url=self.cfg.endpoint, aws_access_key_id=self.cfg.access_key, aws_secret_access_key=self.cfg.secret_key,
                region_name=self.cfg.region, config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                                                            retries={"max_attempts": 2}, connect_timeout=5, read_timeout=60))
        return self._client

    def _call(self, fn: str, **kw: Any) -> Any:
        try:
            return getattr(self.s3, fn)(**kw)
        except WormError:
            raise
        except Exception as e:                                       # botocore ClientError / connection errors
            code = getattr(e, "response", {}).get("Error", {}).get("Code") if hasattr(e, "response") else None
            raise WormError(f"{fn}: {code or type(e).__name__}: {str(e)[:200]}") from e

    # ----------------------------------------------------------------- state
    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"bucket": self.cfg.bucket, "endpoint": self.cfg.endpoint, "mode": self.cfg.mode, "retain_days": self.cfg.retain_days}
        try:
            self._call("head_bucket", Bucket=self.cfg.bucket)
        except WormError as e:
            return out | {"reachable": False, "object_lock": False, "error": str(e)}
        try:
            conf = self._call("get_object_lock_configuration", Bucket=self.cfg.bucket)
            out["object_lock"] = conf.get("ObjectLockConfiguration", {}).get("ObjectLockEnabled") == "Enabled"
        except WormError as e:
            out["object_lock"] = False
            out["error"] = str(e)
        out["reachable"] = True
        return out

    def require_lock(self) -> None:
        st = self.status()
        if not st.get("reachable"):
            raise WormError(f"the bucket cannot be reached: {st.get('error')}")
        if not st.get("object_lock"):
            raise WormError(f"bucket {self.cfg.bucket!r} was not created with Object Lock, so it cannot hold write-once copies "
                            "(Object Lock has to be enabled at creation); it was not used")

    # ----------------------------------------------------------------- objects
    def put(self, key: str, data: bytes, sha256_hex: str, *, retain_until: datetime) -> dict[str, Any]:
        if len(data) > MAX_OBJECT:
            raise WormError(f"objects over {MAX_OBJECT} bytes are not supported here")
        if hashlib.sha256(data).hexdigest() != sha256_hex:
            raise WormError("refusing to preserve bytes that do not match their digest")
        digest_b64 = base64.b64encode(bytes.fromhex(sha256_hex)).decode()
        r = self._call("put_object", Bucket=self.cfg.bucket, Key=key, Body=data, ChecksumSHA256=digest_b64,
                       ObjectLockMode=self.cfg.mode, ObjectLockRetainUntilDate=retain_until, Metadata={"sha256": sha256_hex})
        if not r.get("VersionId"):
            raise WormError("the service returned no version id: versioning (required by Object Lock) is not active")
        return {"version_id": r["VersionId"], "retain_until": retain_until, "mode": self.cfg.mode, "size": len(data)}

    def head(self, key: str, version_id: str) -> dict[str, Any]:
        return self._call("head_object", Bucket=self.cfg.bucket, Key=key, VersionId=version_id)

    def get(self, key: str, version_id: str) -> bytes:
        r = self._call("get_object", Bucket=self.cfg.bucket, Key=key, VersionId=version_id)
        data = r["Body"].read(MAX_OBJECT + 1)
        if len(data) > MAX_OBJECT:
            raise WormError("object is larger than the supported maximum")
        return data

    def verify(self, key: str, version_id: str, sha256_hex: str) -> dict[str, Any]:
        """Read the object back and compare its bytes with the digest, and check the lock is still in force."""
        problems = []
        h = self.head(key, version_id)
        mode, until = h.get("ObjectLockMode"), h.get("ObjectLockRetainUntilDate")
        if mode not in ("COMPLIANCE", "GOVERNANCE"):
            problems.append("no retention lock is set on this object")
        if until is not None and until < datetime.now(timezone.utc):
            problems.append(f"retention ended {until.isoformat()}: the object can now be deleted")
        if hashlib.sha256(self.get(key, version_id)).hexdigest() != sha256_hex:
            problems.append("the stored bytes no longer match the digest")
        return {"ok": not problems, "problems": problems, "mode": mode, "retain_until": until}

    def extend(self, key: str, version_id: str, retain_until: datetime) -> None:
        self._call("put_object_retention", Bucket=self.cfg.bucket, Key=key, VersionId=version_id,
                   Retention={"Mode": self.cfg.mode, "RetainUntilDate": retain_until})


# ------------------------------------------------------------------ platform operations
def _key(kind: str, sha_hex: str) -> str:
    return f"{kind}/{sha_hex[:2]}/{sha_hex}"


def _record(conn: psycopg.Connection, actor: Actor, kind: str, sha: bytes, key: str, cfg: WormConfig, put: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO worm_copy (workspace_id, kind, sha256, bucket, object_key, version_id, lock_mode, retain_until, size_bytes, mirrored_by)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (bucket, object_key, version_id) DO NOTHING",
        (actor.workspace_id, kind, sha, cfg.bucket, key, put["version_id"], put["mode"], put["retain_until"], put["size"], actor.principal_id))


def mirror_case(conn: psycopg.Connection, actor: Actor, local: EvidenceStore, worm: WormStore, case_id: uuid.UUID) -> dict[str, Any]:
    """Preserve every artifact the case holds. Already-preserved digests (under a still-running lock) are skipped."""
    worm.require_lock()
    shas = [bytes(r["sha256"]) for r in conn.execute("SELECT DISTINCT sha256 FROM artifact_acquisition WHERE case_id=%s", (case_id,))]
    until = datetime.now(timezone.utc) + timedelta(days=worm.cfg.retain_days)
    done, skipped, failed = 0, 0, []
    for sha in shas:
        hexd = sha.hex()
        existing = conn.execute("SELECT 1 FROM worm_copy WHERE sha256=%s AND bucket=%s AND kind='artifact' AND retain_until > now() + interval '1 day'",
                                (sha, worm.cfg.bucket)).fetchone()
        conn.execute("INSERT INTO worm_case (case_id, sha256) VALUES (%s,%s) ON CONFLICT DO NOTHING", (case_id, sha))
        if existing:
            skipped += 1
            continue
        try:
            data = local.verified_bytes(sha)                           # a corrupt local blob is never preserved as if it were good
            key = _key("artifact", hexd)
            _record(conn, actor, "artifact", sha, key, worm.cfg, worm.put(key, data, hexd, retain_until=until))
            done += 1
        except Exception as e:                                         # noqa: BLE001 - report each failure, keep going
            failed.append({"sha256": hexd, "error": str(e)[:200]})
    audit.record(conn, actor, "worm.mirror", case_id=case_id, target_kind="case", target_id=str(case_id),
                 detail={"preserved": done, "already": skipped, "failed": len(failed), "mode": worm.cfg.mode, "retain_until": until.isoformat()})
    return {"preserved": done, "already_preserved": skipped, "failed": failed, "retain_until": until.isoformat(), "mode": worm.cfg.mode,
            "note": ("These copies cannot be deleted before the retention date, including by a case erasure." if worm.cfg.mode == "COMPLIANCE"
                     else "GOVERNANCE mode: a privileged account can bypass the lock; this is weaker than COMPLIANCE.")}


def mirror_exhibit(conn: psycopg.Connection, actor: Actor, worm: WormStore, case_id: uuid.UUID, data: bytes) -> dict[str, Any]:
    worm.require_lock()
    hexd = hashlib.sha256(data).hexdigest()
    until = datetime.now(timezone.utc) + timedelta(days=worm.cfg.retain_days)
    key = f"exhibit/{case_id}/{hexd}.zip"
    put = worm.put(key, data, hexd, retain_until=until)
    _record(conn, actor, "exhibit", bytes.fromhex(hexd), key, worm.cfg, put)
    audit.record(conn, actor, "worm.exhibit", case_id=case_id, target_kind="exhibit", target_id=hexd, detail={"retain_until": until.isoformat()})
    return {"sha256": hexd, "version_id": put["version_id"], "retain_until": until.isoformat(), "mode": worm.cfg.mode}


def copies_for_case(conn: psycopg.Connection, case_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT c.sha256, c.kind, c.bucket, c.object_key, c.version_id, c.lock_mode, c.retain_until, c.size_bytes, c.last_verified_at, c.last_verified_ok"
        " FROM worm_copy c WHERE c.sha256 IN (SELECT sha256 FROM worm_case WHERE case_id=%s)"
        "   OR c.object_key LIKE %s ORDER BY c.mirrored_at", (case_id, f"exhibit/{case_id}/%")).fetchall()
    return [{"sha256": bytes(r["sha256"]).hex(), "kind": r["kind"], "bucket": r["bucket"], "key": r["object_key"], "version_id": r["version_id"],
             "mode": r["lock_mode"], "retain_until": r["retain_until"].isoformat(), "size": r["size_bytes"],
             "last_verified_ok": r["last_verified_ok"], "last_verified_at": r["last_verified_at"].isoformat() if r["last_verified_at"] else None}
            for r in rows]


def verify_case(conn: psycopg.Connection, actor: Actor, worm: WormStore, case_id: uuid.UUID) -> dict[str, Any]:
    out, bad = [], 0
    for c in copies_for_case(conn, case_id):
        try:
            v = worm.verify(c["key"], c["version_id"], c["sha256"])
        except WormError as e:
            v = {"ok": False, "problems": [str(e)], "mode": None, "retain_until": None}
        conn.execute("UPDATE worm_copy SET last_verified_at=now(), last_verified_ok=%s WHERE bucket=%s AND object_key=%s AND version_id=%s",
                     (v["ok"], c["bucket"], c["key"], c["version_id"]))
        bad += 0 if v["ok"] else 1
        out.append({"sha256": c["sha256"], "key": c["key"], "ok": v["ok"], "problems": v["problems"]})
    audit.record(conn, actor, "worm.verify", case_id=case_id, target_kind="case", target_id=str(case_id), detail={"checked": len(out), "failed": bad})
    return {"checked": len(out), "failed": bad, "results": out}


def restore_missing(conn: psycopg.Connection, actor: Actor, local: EvidenceStore, worm: WormStore, case_id: uuid.UUID) -> dict[str, Any]:
    """Re-create local blobs that are gone, from the preserved copy, after checking the digest. Records the restore as an acquisition."""
    restored, failed = 0, []
    for c in copies_for_case(conn, case_id):
        if c["kind"] != "artifact" or local._path(bytes.fromhex(c["sha256"])).exists():
            continue
        try:
            data = worm.get(c["key"], c["version_id"])
            if hashlib.sha256(data).hexdigest() != c["sha256"]:
                raise WormError("the preserved bytes do not match the digest")
            local.ingest(conn, actor, case_id, data, acquired_by="worm-restore", acquired_from=f"worm:{c['bucket']}/{c['key']}")
            restored += 1
        except Exception as e:                                         # noqa: BLE001
            failed.append({"sha256": c["sha256"], "error": str(e)[:200]})
    return {"restored": restored, "failed": failed}
