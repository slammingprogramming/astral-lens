"""Face detection, embedding and search over a case's own media.

Scope and limits, deliberately plain:
- Output is RANKED CANDIDATES with a cosine score, the backend and model used and
  the model's licence class. It is never an identification, and nothing here
  produces a yes/no verdict. `REFERENCE_THRESHOLD` is OpenCV's published SFace
  operating point on LFW (an easy, frontal benchmark); on low-resolution, profile
  or occluded images it does not transfer, so it is shown as a reference, not a rule.
- The only gallery is the case's own media plus a watchlist the operator enrols
  by hand. There is no crawler and no general-public index (EU AI Act Art. 5(1)(e)).
- Embeddings are biometric data. Every row is tagged at write time and given an
  `expires_at`, and `purge_expired` deletes them. Searches are recorded in the
  audit log with ids, hashes and counts only (never names or labels).
- No gates or prompts: the platform records, it does not ask (docs/10 §5).
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg

from astral import audit
from astral.backends import module_available
from astral.db import Actor

REFERENCE_THRESHOLD = 0.363
DEFAULT_BIOMETRIC_DAYS = 365 * 3            # BIPA outer bound; a case policy may shorten it
_DET, _REC = "face_detection_yunet_2023mar.onnx", "face_recognition_sface_2021dec.onnx"
_lock = threading.Lock()
_cache: dict[str, Any] = {}


def models_dir() -> Path | None:
    for cand in (os.environ.get("ASTRAL_FACE_MODELS_DIR"),
                 str(Path(__file__).resolve().parents[3] / "models")):
        if cand and (Path(cand) / _DET).exists() and (Path(cand) / _REC).exists():
            return Path(cand)
    return None


class YuNetSFace:
    """Licence-clean default: YuNet (MIT) detects, SFace (Apache-2.0) embeds, both via OpenCV."""

    name, capability = "yunet-sface", "face.embed"
    model_id, detector_id = "sface", "yunet"
    dim = 128

    def available(self) -> tuple[bool, str]:
        if not module_available("cv2"):
            return False, "opencv is not installed (pip install astral-lens[faces])"
        if models_dir() is None:
            return False, f"{_DET} and {_REC} not found; set ASTRAL_FACE_MODELS_DIR"
        return True, ""

    def supports(self, **_: Any) -> bool:
        return True

    def run(self, *, data: bytes, max_faces: int = 20, min_score: float = 0.7, **_: Any) -> dict[str, Any]:
        import cv2
        import numpy as np

        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("not a decodable image")
        h, w = img.shape[:2]
        scale = min(1.0, 1600 / max(h, w))                       # detect on a bounded size
        small = cv2.resize(img, (int(w * scale), int(h * scale))) if scale < 1 else img
        d = models_dir()
        with _lock:                                              # OpenCV's detector is not thread-safe
            det = _cache.get("det") or _cache.setdefault(
                "det", cv2.FaceDetectorYN.create(str(d / _DET), "", (320, 320), min_score, 0.3, 5000))
            rec = _cache.get("rec") or _cache.setdefault("rec", cv2.FaceRecognizerSF.create(str(d / _REC), ""))
            det.setScoreThreshold(min_score)
            det.setInputSize((small.shape[1], small.shape[0]))
            _, found = det.detect(small)
            faces = []
            for f in (found if found is not None else []):
                feat = rec.feature(rec.alignCrop(small, f)).flatten().astype(np.float64)
                feat /= (np.linalg.norm(feat) or 1.0)
                lm = [[float(f[4 + 2 * i] / scale), float(f[5 + 2 * i] / scale)] for i in range(5)]
                faces.append({"box": {"x": int(f[0] / scale), "y": int(f[1] / scale),
                                      "w": int(f[2] / scale), "h": int(f[3] / scale)},
                              "landmarks": lm, "det_score": round(float(f[14]), 4),
                              "embedding": [round(float(x), 6) for x in feat]})
        faces.sort(key=lambda x: -x["det_score"])
        return {"faces": faces[:max_faces], "image": {"width": w, "height": h}}


class InsightFaceOptIn:
    """InsightFace buffalo_l. Its pretrained weights are NON-COMMERCIAL, so it is only
    chosen when the caller says so explicitly. NOT exercised: the library and weights
    are not installed here."""

    name, capability = "insightface", "face.embed"
    model_id, detector_id = "insightface-buffalo_l", "insightface-buffalo_l"
    dim = 512

    def available(self) -> tuple[bool, str]:
        if not module_available("insightface"):
            return False, "insightface is not installed"
        return (True, "") if os.environ.get("ASTRAL_INSIGHTFACE_DIR") else (False, "ASTRAL_INSIGHTFACE_DIR is not set")

    def supports(self, allow_noncommercial: bool = False, **_: Any) -> bool:
        return bool(allow_noncommercial)

    def run(self, *, data: bytes, max_faces: int = 20, min_score: float = 0.7, **_: Any) -> dict[str, Any]:  # pragma: no cover
        import cv2
        import numpy as np
        from insightface.app import FaceAnalysis

        app = FaceAnalysis(name="buffalo_l", root=os.environ["ASTRAL_INSIGHTFACE_DIR"])
        app.prepare(ctx_id=-1)
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        out = []
        for f in app.get(img):
            if float(f.det_score) < min_score:
                continue
            x1, y1, x2, y2 = (int(v) for v in f.bbox)
            out.append({"box": {"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1},
                        "landmarks": [[float(a), float(b)] for a, b in f.kps], "det_score": round(float(f.det_score), 4),
                        "embedding": [round(float(v), 6) for v in f.normed_embedding]})
        out.sort(key=lambda x: -x["det_score"])
        return {"faces": out[:max_faces], "image": {"width": img.shape[1], "height": img.shape[0]}}


# --------------------------------------------------------------------- storage
def biometric_expiry(policy: dict[str, Any] | None, now: datetime | None = None) -> datetime:
    days = int((policy or {}).get("biometric_days") or DEFAULT_BIOMETRIC_DAYS)
    return (now or datetime.now(timezone.utc)) + timedelta(days=min(days, DEFAULT_BIOMETRIC_DAYS))


def index_faces(conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID, *, artifact_sha: bytes,
                faces: list[dict[str, Any]], model_id: str, backend: str, frame_ms: int | None = None,
                watchlist: str | None = None, label: str | None = None, basis: str | None = None) -> int:
    """Insert embeddings, tagged biometric and given an expiry. Idempotent per entity."""
    pol = conn.execute("SELECT retention_policy FROM case_ WHERE id=%s", (case_id,)).fetchone()["retention_policy"]
    exp = biometric_expiry(pol)
    n = 0
    for f in faces:
        n += conn.execute(
            "INSERT INTO face (id, workspace_id, case_id, artifact_sha, frame_ms, entity, box, landmarks, det_score,"
            " model_id, backend, embedding, dim, watchlist, label, basis, biometric, expires_at, created_by)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::real[],%s,%s,%s,%s,true,%s,%s) ON CONFLICT (id) DO NOTHING",
            (uuid.UUID(f["entity"]), actor.workspace_id, case_id, artifact_sha, frame_ms, uuid.UUID(f["entity"]),
             psycopg.types.json.Jsonb(f["box"]), psycopg.types.json.Jsonb(f.get("landmarks")), f["det_score"],
             model_id, backend, f["embedding"], len(f["embedding"]), watchlist, label, basis, exp,
             actor.principal_id)).rowcount
    return n


def pgvector_available(conn: psycopg.Connection) -> bool:
    return conn.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone() is not None


def search(conn: psycopg.Connection, case_id: uuid.UUID, embedding: list[float], *, model_id: str,
           scope: str = "all", k: int = 10, min_score: float | None = None,
           exclude_artifact: bytes | None = None, exclude_face: uuid.UUID | None = None,
           use_pgvector: bool | None = None) -> list[dict[str, Any]]:
    """Cosine-ranked candidates. Exact (not approximate) either way; pgvector only moves
    the arithmetic into the database. Returns scores, never verdicts."""
    where = "case_id=%s AND model_id=%s AND dim=%s"
    args: list[Any] = [case_id, model_id, len(embedding)]
    if scope == "watchlist":
        where += " AND watchlist IS NOT NULL"
    elif scope == "case":
        where += " AND watchlist IS NULL"
    if exclude_artifact is not None:
        where += " AND artifact_sha <> %s"
        args.append(exclude_artifact)
    if exclude_face is not None:
        where += " AND id <> %s"
        args.append(exclude_face)
    pgv = pgvector_available(conn) if use_pgvector is None else use_pgvector
    if pgv:
        q = "[" + ",".join(repr(float(x)) for x in embedding) + "]"
        rows = conn.execute(
            f"SELECT id, artifact_sha, frame_ms, entity, box, det_score, backend, watchlist, label,"
            f" 1 - (embedding::vector <=> %s::vector) AS score FROM face WHERE {where}"
            f" ORDER BY embedding::vector <=> %s::vector LIMIT %s", [q, *args, q, k]).fetchall()
        scored = [dict(r) for r in rows]
    else:
        import numpy as np

        rows = conn.execute(f"SELECT id, artifact_sha, frame_ms, entity, box, det_score, backend, watchlist, label,"
                            f" embedding FROM face WHERE {where}", args).fetchall()
        if not rows:
            return []
        m = np.array([r["embedding"] for r in rows], dtype=np.float64)
        q = np.array(embedding, dtype=np.float64)
        m /= np.linalg.norm(m, axis=1, keepdims=True).clip(1e-12)
        sims = m @ (q / (np.linalg.norm(q) or 1.0))
        order = np.argsort(-sims)[:k]
        scored = [{**{kk: vv for kk, vv in rows[i].items() if kk != "embedding"}, "score": float(sims[i])} for i in order]
    out = []
    for r in scored:
        if min_score is not None and r["score"] < min_score:
            continue
        out.append({"face_id": str(r["id"]), "entity": str(r["entity"]), "artifact": bytes(r["artifact_sha"]).hex(),
                    "frame_ms": r["frame_ms"], "box": r["box"], "det_score": r["det_score"],
                    "score": round(float(r["score"]), 4), "backend": r["backend"], "model_id": model_id,
                    "watchlist": r["watchlist"], "label": r["label"],
                    "reference_threshold": REFERENCE_THRESHOLD if model_id == "sface" else None,
                    "above_reference": (r["score"] >= REFERENCE_THRESHOLD) if model_id == "sface" else None})
    return out


NOTICE = ("Ranked candidates by cosine similarity. A score is not an identification; the reference "
          "threshold comes from an easy frontal benchmark and does not transfer to poor images.")


class ProbeError(Exception):
    """A probe search that could not be made; `status` is the HTTP status the API answers with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def search_probe(conn: psycopg.Connection, store: Any, actor: Actor, case_id: uuid.UUID, backends: Any, services: Any | None,
                 *, artifact: str | None = None, face_id: str | None = None, face_index: int = 0, scope: str = "all",
                 k: int = 10, min_score: float | None = None, prefer: str | None = None,
                 allow_noncommercial: bool = False) -> dict[str, Any]:
    """One face search, from a probe image in the case or an indexed face, and its unconditional record.
    Shared by the API and by plan steps so both rank and record the same way. The caller authorises."""
    from astral.backends import NoBackend
    from astral.compute.models import load_registry

    if not artifact and not face_id:
        raise ProbeError(422, "give an artifact (probe image) or a face_id")
    exclude, exclude_face = None, (uuid.UUID(face_id) if face_id else None)
    if face_id:
        r = conn.execute("SELECT embedding, model_id, backend, artifact_sha FROM face WHERE id=%s AND case_id=%s",
                         (exclude_face, case_id)).fetchone()
        if not r:
            raise ProbeError(404, "no such face")
        emb, model_id, backend, qsha, n_faces = r["embedding"], r["model_id"], r["backend"], bytes(r["artifact_sha"]).hex(), 1
    else:
        try:
            res = backends.resolve("face.embed", prefer=prefer, allow_noncommercial=allow_noncommercial)
        except NoBackend as e:
            raise ProbeError(503, str(e)) from e
        data = store.read(conn, actor, case_id, bytes.fromhex(artifact))
        try:
            out = res.backend.run(data=data)
        except Exception as e:                                          # noqa: BLE001 - undecodable image, model failure
            raise ProbeError(422, f"{res.backend.name} failed: {type(e).__name__}: {e}") from e
        n_faces = len(out["faces"])
        if not n_faces:
            raise ProbeError(422, "no face was detected in the probe image (this is not proof there is none)")
        if face_index >= n_faces:
            raise ProbeError(422, f"face_index {face_index} out of range ({n_faces} faces found)")
        emb, model_id, backend, qsha = out["faces"][face_index]["embedding"], res.backend.model_id, res.backend.name, artifact
        exclude = bytes.fromhex(artifact)
    hits, degraded = (services.face_hits(conn, case_id, emb, model_id=model_id, scope=scope, k=k, min_score=min_score,
                                         exclude_artifact=exclude, exclude_face=exclude_face)
                      if services is not None else (None, None))
    engine = "qdrant (exact)" if hits is not None else "postgres"
    if hits is None:
        hits = search(conn, case_id, emb, model_id=model_id, scope=scope, k=k, min_score=min_score,
                      exclude_artifact=exclude, exclude_face=exclude_face)
    record_search(conn, actor, case_id, query_sha=qsha, model_id=model_id, backend=backend, scope=scope, k=k,
                  min_score=min_score, results=len(hits))
    lic = {m: {"licence": v.licence, "licence_class": v.licence_class, "commercial_use": v.commercial_use}
           for m, v in load_registry().items() if m in (model_id, "yunet")}
    return {"candidates": hits, "model_id": model_id, "backend": backend, "licences": lic, "faces_in_probe": n_faces,
            "engine": engine, "degraded": degraded, "notice": NOTICE}


def record_search(conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID, *, query_sha: str, model_id: str,
                  backend: str, scope: str, k: int, min_score: float | None, results: int) -> None:
    """The unconditional record. Ids, hashes and counts only: no names, no labels."""
    audit.record(conn, actor, "face.search", case_id=case_id, target_kind="artifact", target_id=query_sha,
                 detail={"model": model_id, "backend": backend, "scope": scope, "k": k,
                         "threshold": min_score, "results": results,
                         "pgvector": pgvector_available(conn)})


def purge_expired(conn: psycopg.Connection, actor: Actor, now: datetime | None = None) -> int:
    n = conn.execute("DELETE FROM face WHERE expires_at IS NOT NULL AND expires_at <= %s AND workspace_id=%s",
                     (now or datetime.now(timezone.utc), actor.workspace_id)).rowcount
    if n:
        audit.record(conn, actor, "face.purge_expired", detail={"deleted": n})
    return n


def crop_jpeg(data: bytes, box: dict[str, int], margin: float = 0.25, size: int = 160) -> bytes:
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    mx, my = int(box["w"] * margin), int(box["h"] * margin)
    x0, y0 = max(0, box["x"] - mx), max(0, box["y"] - my)
    x1, y1 = min(w, box["x"] + box["w"] + mx), min(h, box["y"] + box["h"] + my)
    crop = img[y0:y1, x0:x1]
    s = size / max(crop.shape[:2])
    crop = cv2.resize(crop, (max(1, int(crop.shape[1] * s)), max(1, int(crop.shape[0] * s))))
    ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes() if ok else b""


def register_face_backends(reg) -> None:
    reg.register(YuNetSFace())
    reg.register(InsightFaceOptIn())
