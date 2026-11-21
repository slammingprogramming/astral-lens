"""Layer 0: the content-addressed evidence store (03 §2, 10 §8).

SHA-256 is computed while the bytes are streamed in, before any processing,
and is never recomputed from a mutated file. Blobs are write-once. Visibility
is granted only by an acquisition row in the case, so content-addressed dedup
cannot leak a blob across cases.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import stat
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

import psycopg

from astral import audit
from astral.db import Actor
from astral.policy import PermissionDenied

_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class Ingested:
    sha256: bytes
    new_blob: bool
    acquisition_id: int

    @property
    def hex(self) -> str:
        return self.sha256.hex()


class IntegrityError(Exception):
    """Stored bytes no longer match their digest."""


class EvidenceStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        (self.root / "tmp").mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------- paths
    def _path(self, sha256: bytes) -> Path:
        h = sha256.hex()
        return self.root / "blobs" / h[:2] / h[2:4] / h

    # ------------------------------------------------------------- ingest
    def ingest(
        self,
        conn: psycopg.Connection,
        actor: Actor,
        case_id: uuid.UUID,
        data: bytes | BinaryIO | Path,
        *,
        acquired_by: str,
        media_type: str | None = None,
        acquired_from: str | None = None,
        acquisition_ctx: dict[str, Any] | None = None,
        parent_sha256: bytes | None = None,
        derivation: str | None = None,
    ) -> Ingested:
        stream, close = _open(data)
        hasher = hashlib.sha256()
        size = 0
        fd, tmp_name = tempfile.mkstemp(dir=self.root / "tmp")
        try:
            with os.fdopen(fd, "wb") as out:
                while chunk := stream.read(_CHUNK):
                    hasher.update(chunk)
                    out.write(chunk)
                    size += len(chunk)
                out.flush()
                os.fsync(out.fileno())
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        finally:
            if close:
                stream.close()

        digest = hasher.digest()
        dest = self._path(digest)
        new_blob = not dest.exists()
        if new_blob:
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp_name, dest)
            os.chmod(dest, stat.S_IREAD)  # write-once
        else:
            Path(tmp_name).unlink(missing_ok=True)

        if media_type is None:
            media_type = _guess_media_type(data) or "application/octet-stream"
        now = datetime.now(timezone.utc)

        conn.execute(
            "INSERT INTO artifact (sha256, media_type, byte_size, storage_uri, first_seen_at,"
            " parent_sha256, derivation) VALUES (%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (sha256) DO NOTHING",
            (digest, media_type, size, f"file:{dest.relative_to(self.root).as_posix()}", now,
             parent_sha256, derivation),
        )
        row = conn.execute(
            "INSERT INTO artifact_acquisition (workspace_id, case_id, sha256, acquired_by,"
            " acquired_from, acquisition_ctx, acquired_at, acquired_principal)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (actor.workspace_id, case_id, digest, acquired_by, acquired_from,
             json.dumps(acquisition_ctx or {}), now, actor.principal_id),
        ).fetchone()
        audit.record(
            conn, actor, "artifact.ingest", case_id=case_id, target_kind="artifact",
            target_id=digest.hex(),
            # Deliberately no acquired_from: the audit log is immutable, and a
            # URL or path can identify a person. It lives in artifact_acquisition,
            # which a legally required erasure can remove.
            detail={"acquired_by": acquired_by, "byte_size": size, "new_blob": new_blob},
        )
        return Ingested(digest, new_blob, int(row["id"]))

    # --------------------------------------------------------------- read
    def read(self, conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID,
             sha256: bytes) -> bytes:
        """Return bytes for an artifact the case has acquired, verified on read."""
        row = conn.execute(
            "SELECT 1 FROM artifact_acquisition WHERE case_id = %s AND workspace_id = %s"
            " AND sha256 = %s LIMIT 1",
            (case_id, actor.workspace_id, sha256),
        ).fetchone()
        if row is None:
            raise PermissionDenied("artifact not found in case")
        return self.verified_bytes(sha256)

    def path_for(self, conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID,
                 sha256: bytes) -> Path:
        """Filesystem path of a blob the case has acquired, for streaming large
        datasets (a 2 GB sanctions file is not read into memory). The bytes are
        NOT re-hashed here; call verify_file() when integrity matters."""
        row = conn.execute(
            "SELECT 1 FROM artifact_acquisition WHERE case_id = %s AND workspace_id = %s"
            " AND sha256 = %s LIMIT 1", (case_id, actor.workspace_id, sha256)).fetchone()
        if row is None:
            raise PermissionDenied("artifact not found in case")
        return self._path(sha256)

    def verify_file(self, sha256: bytes) -> bool:
        h = hashlib.sha256()
        with self._path(sha256).open("rb") as f:
            while chunk := f.read(_CHUNK):
                h.update(chunk)
        return h.digest() == sha256

    def verified_bytes(self, sha256: bytes) -> bytes:
        data = self._path(sha256).read_bytes()
        if hashlib.sha256(data).digest() != sha256:
            raise IntegrityError(f"blob {sha256.hex()} does not match its digest")
        return data

    # -------------------------------------------------------------- purge
    def purge_unreferenced(self, conn: psycopg.Connection, shas: list[bytes]) -> list[bytes]:
        """Delete artifact rows no case still holds and return their digests.
        Hard-delete path only (caller has set astral.hard_delete). Files are
        NOT touched here: call delete_blobs() after the transaction commits,
        because a file unlink cannot be rolled back."""
        removed: list[bytes] = []
        for sha in shas:
            still = conn.execute(
                "SELECT 1 FROM artifact_acquisition WHERE sha256 = %s LIMIT 1", (sha,)
            ).fetchone()
            child = conn.execute(
                "SELECT 1 FROM artifact WHERE parent_sha256 = %s LIMIT 1", (sha,)
            ).fetchone()
            if still or child:
                continue
            conn.execute("DELETE FROM artifact WHERE sha256 = %s", (sha,))
            removed.append(sha)
        return removed

    def delete_blobs(self, shas: list[bytes]) -> int:
        n = 0
        for sha in shas:
            p = self._path(sha)
            if p.exists():
                os.chmod(p, stat.S_IWRITE)
                p.unlink()
                n += 1
        return n


def _open(data: bytes | BinaryIO | Path) -> tuple[BinaryIO, bool]:
    if isinstance(data, (bytes, bytearray)):
        import io

        return io.BytesIO(bytes(data)), False
    if isinstance(data, Path):
        return data.open("rb"), True
    return data, False


def _guess_media_type(data: object) -> str | None:
    if isinstance(data, Path):
        return mimetypes.guess_type(data.name)[0]
    return None
