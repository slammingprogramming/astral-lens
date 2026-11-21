"""Object detection and tracking over a case's own media.

Scope and limits, deliberately plain:
- A detection is a BOX AND A CLASS GUESS from a fixed 80-class vocabulary (COCO: person, car, backpack, ...). The score is the
  model's confidence, not a probability that the label is right. Nothing outside that vocabulary can be reported: a drone,
  a particular uniform, a weapon type are not classes, and "none detected" never means "none present".
- A "person" box is a region, not an identity. No face is read here and no embedding is made; the face pipeline is separate
  and has its own controls (faces.py).
- A track groups boxes across sampled video frames (tracking.py); it is not an identity either, and the frames are
  sampled, so its start and end are bounded by the sampling interval.
- The detector is RT-DETR (ResNet-18 backbone), Apache-2.0, trained on COCO and run through ONNX Runtime on the CPU. COCO
  images are consumer photographs, so performance on CCTV, aerial, night, thermal or very small objects is expected to be
  worse and has not been measured here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

from astral import tracking
from astral.backends import module_available

_DIR = "rtdetr_r18vd"
SIZE = 640                                               # the model's fixed square input
_lock = threading.Lock()
_cache: dict[str, Any] = {}


def models_dir() -> Path | None:
    for cand in (os.environ.get("ASTRAL_DETECT_MODELS_DIR"), str(Path(__file__).resolve().parents[3] / "models" / "detect")):
        if cand and (Path(cand) / _DIR / "model.onnx").exists() and (Path(cand) / _DIR / "config.json").exists():
            return Path(cand) / _DIR
    return None


def decode(logits: Any, boxes: Any, width: int, height: int, labels: dict[int, str], min_score: float, max_objects: int) -> list[dict[str, Any]]:
    """RT-DETR's output -> boxes in the image's own pixels. `logits` (queries, classes), `boxes` (queries, 4) as normalised
    centre x, centre y, width, height. Every (query, class) pair is scored with a sigmoid and the best ones are kept, as the
    model's reference post-processing does; there is no non-maximum suppression because the model is trained to need none."""
    import numpy as np

    prob = 1.0 / (1.0 + np.exp(-np.asarray(logits, dtype=np.float64)))
    n_q, n_c = prob.shape
    flat = prob.reshape(-1)
    order = np.argsort(-flat)[: max(1, n_q)]
    out = []
    for k in order:
        s = float(flat[k])
        if s < min_score:
            break
        q, c = divmod(int(k), n_c)
        cx, cy, bw, bh = (float(v) for v in boxes[q])
        x1, y1 = max(0.0, (cx - bw / 2) * width), max(0.0, (cy - bh / 2) * height)
        x2, y2 = min(float(width), (cx + bw / 2) * width), min(float(height), (cy + bh / 2) * height)
        if x2 - x1 < 1 or y2 - y1 < 1:
            continue
        out.append({"label": labels.get(c, f"class_{c}"), "class_id": c, "score": round(s, 4),
                    "box": {"x": int(round(x1)), "y": int(round(y1)), "w": int(round(x2 - x1)), "h": int(round(y2 - y1))}})
    return out[:max_objects]


class RtDetrOnnx:
    """Licence-clean default detector: RT-DETR r18vd (Apache-2.0) through ONNX Runtime."""

    name, capability = "rtdetr-onnx", "object.detect"
    model_id = "rt-detr-r18vd"
    detector_id = "rt-detr-r18vd"

    def available(self) -> tuple[bool, str]:
        for mod, hint in (("onnxruntime", "pip install onnxruntime"), ("cv2", "pip install astral-lens[faces]"), ("numpy", "pip install numpy")):
            if not module_available(mod):
                return False, f"{mod} is not installed ({hint})"
        if models_dir() is None:
            return False, f"models/detect/{_DIR}/model.onnx and config.json not found; set ASTRAL_DETECT_MODELS_DIR"
        return True, ""

    def supports(self, **_: Any) -> bool:
        return True

    def _session(self):
        with _lock:
            if "sess" not in _cache:
                import onnxruntime as ort

                d = models_dir()
                opts = ort.SessionOptions()
                opts.intra_op_num_threads = int(os.environ.get("ASTRAL_DETECT_THREADS", "4"))
                _cache["sess"] = ort.InferenceSession(str(d / "model.onnx"), opts, providers=["CPUExecutionProvider"])
                cfg = json.loads((d / "config.json").read_text(encoding="utf-8"))
                _cache["labels"] = {int(k): v for k, v in cfg["id2label"].items()}
        return _cache["sess"], _cache["labels"]

    def detect_array(self, img: Any, *, min_score: float = 0.4, max_objects: int = 100) -> list[dict[str, Any]]:
        """`img`: a BGR image as OpenCV reads it."""
        import cv2
        import numpy as np

        sess, labels = self._session()
        h, w = img.shape[:2]
        x = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2RGB), (SIZE, SIZE), interpolation=cv2.INTER_LINEAR)
        x = np.ascontiguousarray(x.astype(np.float32).transpose(2, 0, 1)[None] / 255.0)      # rescale only: the model takes no mean/std
        with _lock:                                                                          # one inference at a time
            logits, boxes = sess.run(["logits", "pred_boxes"], {"pixel_values": x})
        return decode(logits[0], boxes[0], w, h, labels, min_score, max_objects)

    def run(self, *, data: bytes, min_score: float = 0.4, max_objects: int = 100, **_: Any) -> dict[str, Any]:
        import cv2
        import numpy as np

        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("not a decodable image")
        objs = self.detect_array(img, min_score=min_score, max_objects=max_objects)
        return {"objects": objs, "image": {"width": img.shape[1], "height": img.shape[0]}}


def register_object_backends(reg) -> None:
    reg.register(RtDetrOnnx())


# ----------------------------------------------------------------------------------------------- video
def sample_frames(src: Path, dst: Path, fps: float, max_frames: int) -> tuple[list[Path], bool]:
    """Extract frames at `fps` (at most `max_frames`) as JPEGs no wider than 960 px. Returns the files and whether the
    video had more frames than the cap (so the tail was NOT scanned)."""
    ff = shutil.which("ffmpeg")
    if not ff:
        raise RuntimeError("ffmpeg not found on PATH")
    r = subprocess.run([ff, "-v", "error", "-i", str(src), "-vf", f"fps={fps},scale='min(960,iw)':-2", "-frames:v", str(max_frames + 1),
                        "-q:v", "3", str(dst / "f%05d.jpg")], capture_output=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg could not read the video: {r.stderr.decode('utf-8', 'replace')[:200]}")
    files = sorted(dst.glob("f*.jpg"))
    return files[:max_frames], len(files) > max_frames


def track_video(src: Path, backend: Any, *, fps: float = 4.0, max_frames: int = 600, min_score: float = 0.3,
                tracker: tracking.Tracker | None = None) -> dict[str, Any]:
    """Detect on sampled frames and link the detections into tracks."""
    import cv2

    tr = tracker or tracking.Tracker()
    with tempfile.TemporaryDirectory() as d:
        files, truncated = sample_frames(src, Path(d), fps, max_frames)
        n_det = 0
        size: tuple[int, int] | None = None
        for i, f in enumerate(files):
            img = cv2.imread(str(f))
            if img is None:
                continue
            objs = backend.detect_array(img, min_score=min_score)
            n_det += len(objs)
            t_ms = int(round(i * 1000 / fps))
            tr.update(t_ms, [tracking.Det(o["box"]["x"], o["box"]["y"], o["box"]["w"], o["box"]["h"], o["score"], o["label"], o["class_id"])
                             for o in objs])
            size = (img.shape[1], img.shape[0])
    tracks = tr.tracks()
    return {"tracks": tracks, "frames_sampled": len(files), "fps": fps, "truncated": truncated, "detections": n_det,
            "covered_ms": int(round(len(files) * 1000 / fps)), "frame": {"width": size[0], "height": size[1]} if size else None,
            "note": "boxes are in the pixels of the sampled frames (at most 960 px wide), not the original video"}
