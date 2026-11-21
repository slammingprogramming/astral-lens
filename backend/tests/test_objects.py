"""Object detection and tracking.

HONEST SCOPE. The tracker and the decoder are tested with constructed data: they prove the arithmetic and the rules (a
one-frame flicker is not a track, classes never merge, a briefly weak detection keeps its track alive), not that the
tracker is good on real footage. The plugin plumbing is tested with a scripted detector, so those tests prove what is
recorded and what is claimed, not what a model sees. The real-model tests run only when ASTRAL_DETECT_TEST_IMAGE points
at COCO val2017 image 000000039769 (two cats on a sofa with two remote controls; it is the model card's own example, and
is not in the repository) and the model files exist; they check the ONNX pipeline against the box positions the model
card publishes for that one image. They say nothing about CCTV, aerial, night or small-object footage.
"""

from __future__ import annotations

import itertools
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from astral import objects, tracking
from astral.backends import module_available

from conftest import REPO

need_cv = pytest.mark.skipif(not module_available("cv2"), reason="opencv not installed")
need_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
IMAGE = os.environ.get("ASTRAL_DETECT_TEST_IMAGE")
need_model = pytest.mark.skipif(
    not (module_available("cv2") and module_available("onnxruntime") and objects.models_dir() and IMAGE and Path(IMAGE).exists()),
    reason="needs opencv, onnxruntime, the RT-DETR model files and ASTRAL_DETECT_TEST_IMAGE")


# ------------------------------------------------------------------------------------------- decoding
def test_decode_scales_boxes_to_the_image_keeps_the_best_and_clips():
    logits = np.full((4, 3), -10.0)
    logits[0, 1] = 4.0                                    # query 0: class 1, sigmoid ~0.98
    logits[1, 2] = 0.0                                    # query 1: class 2, 0.5
    logits[2, 0] = -3.0                                   # query 2: 0.047, below the threshold
    logits[3, 1] = 3.0                                    # query 3: class 1 but runs off the right edge
    boxes = np.array([[0.5, 0.5, 0.5, 0.5], [0.25, 0.25, 0.1, 0.2], [0.5, 0.5, 0.1, 0.1], [0.95, 0.5, 0.2, 0.2]])
    got = objects.decode(logits, boxes, 200, 100, {0: "a", 1: "person", 2: "car"}, 0.3, 10)
    assert [o["label"] for o in got] == ["person", "person", "car"]                       # best first; the 0.047 one is gone
    assert got[0]["box"] == {"x": 50, "y": 25, "w": 100, "h": 50}
    assert got[1]["box"]["x"] + got[1]["box"]["w"] == 200                                # clipped to the image, not 210
    assert got[0]["score"] > got[1]["score"] > got[2]["score"] >= 0.3
    assert objects.decode(logits, boxes, 200, 100, {}, 0.3, 1)[0]["label"] == "class_1"   # an unknown id is labelled, not dropped
    assert len(objects.decode(logits, boxes, 200, 100, {}, 0.3, 2)) == 2                  # max_objects


def test_decode_drops_degenerate_boxes():
    logits = np.array([[5.0]])
    assert objects.decode(logits, np.array([[0.5, 0.5, 0.0, 0.0]]), 100, 100, {0: "x"}, 0.3, 5) == []


# ----------------------------------------------------------------------------------------- assignment
def test_hungarian_assignment_matches_brute_force_on_random_rectangular_matrices():
    rng = np.random.default_rng(7)
    for n, m in [(3, 3), (2, 5), (5, 2), (4, 4), (1, 3), (3, 1), (6, 4)]:
        for _ in range(8):
            c = rng.random((n, m))
            pairs = tracking.assign(c)
            assert len({i for i, _ in pairs}) == len(pairs) == min(n, m) and len({j for _, j in pairs}) == len(pairs)
            k = min(n, m)
            best = min(
                sum(c[i, j] for i, j in zip(rows, cols))
                for rows in itertools.combinations(range(n), k) for cols in itertools.permutations(range(m), k))
            assert abs(sum(c[i, j] for i, j in pairs) - best) < 1e-9
    assert tracking.assign(np.zeros((0, 3))) == [] and tracking.assign(np.zeros((3, 0))) == []


def test_iou_matrix_known_values():
    a = np.array([[0, 0, 10, 10]], float)
    b = np.array([[0, 0, 10, 10], [5, 0, 10, 10], [20, 20, 5, 5]], float)
    got = tracking.iou_matrix(a, b)[0]
    assert got[0] == pytest.approx(1.0) and got[1] == pytest.approx(50 / 150) and got[2] == 0.0
    assert tracking.iou_matrix(np.zeros((0, 4)), b).shape == (0, 3)


# ----------------------------------------------------------------------------------------- tracking
def det(x, y, label="person", score=0.9, w=40, h=80):
    return tracking.Det(x, y, w, h, score, label, 0)


def run(frames, **kw):
    tr = tracking.Tracker(**kw)
    for i, dets in enumerate(frames):
        tr.update(i * 250, dets)
    return tr.tracks()


def test_two_objects_moving_apart_keep_two_stable_tracks():
    frames = [[det(10 + 8 * i, 50), det(300 - 8 * i, 120, "car", w=90, h=50)] for i in range(12)]
    tracks = run(frames)
    assert [(t["label"], t["frames"]) for t in tracks] == [("person", 12), ("car", 12)]
    assert tracks[0]["start_ms"] == 0 and tracks[0]["end_ms"] == 11 * 250
    assert tracks[0]["boxes"][0]["x"] == 10 and tracks[0]["boxes"][-1]["x"] == 98


def test_a_one_frame_flicker_is_not_a_track():
    frames = [[det(10, 10)], [det(11, 10), det(200, 200, "dog")], [det(12, 10)], [det(13, 10)]]
    assert [t["label"] for t in run(frames)] == ["person"]


def test_a_briefly_weak_detection_keeps_the_track_instead_of_splitting_it():
    """ByteTrack's second pass: a 0.2-score box overlapping the track continues it; without it there would be a gap."""
    frames = [[det(10 + 6 * i, 40, score=0.9)] for i in range(4)] + [[det(10 + 6 * 4, 40, score=0.2)]] + \
             [[det(10 + 6 * i, 40, score=0.9)] for i in range(5, 9)]
    tracks = run(frames)
    assert len(tracks) == 1 and tracks[0]["frames"] == 9


def test_a_short_disappearance_resumes_the_same_track_but_a_long_one_does_not():
    short = [[det(10 + 5 * i, 40)] for i in range(4)] + [[]] * 2 + [[det(10 + 5 * i, 40)] for i in range(6, 10)]
    assert len(run(short)) == 1
    long = [[det(10, 40)] for _ in range(3)] + [[]] * 9 + [[det(10, 40)] for _ in range(3)]
    assert len(run(long)) == 2                                                          # not re-identified after a long gap


def test_classes_never_merge_even_when_boxes_coincide():
    frames = [[det(10, 10, "person")] for _ in range(4)] + [[det(10, 10, "dog", score=0.95)] for _ in range(4)]
    tracks = run(frames)
    assert sorted(t["label"] for t in tracks) == ["dog", "person"]


def test_the_label_is_a_score_weighted_vote_and_the_minority_is_reported():
    frames = [[det(10 + i, 10, "person", score=0.9)] for i in range(5)]
    tr = tracking.Tracker()
    for i, d in enumerate(frames):
        tr.update(i * 250, d)
    t = tr.tracks()[0]
    assert t["label"] == "person" and t["labels_seen"] == {"person": 4.5} and t["max_score"] == 0.9


def test_a_fast_mover_is_followed_by_the_motion_model():
    """16 px a frame on a 40 px box: consecutive boxes barely overlap, and the Kalman prediction carries the track."""
    frames = [[det(10 + 16 * i, 40)] for i in range(10)]
    assert len(run(frames)) == 1 and run(frames)[0]["frames"] == 10
    too_fast = [[det(10 + 60 * i, 40)] for i in range(6)]          # 60 px a frame: no overlap at all, so no honest link
    assert all(t["frames"] == 1 for t in run(too_fast, min_hits=1)) and len(run(too_fast, min_hits=1)) == 6


def test_no_detections_no_tracks_and_empty_frames_are_fine():
    assert run([[], [], []]) == []
    assert run([]) == []


# --------------------------------------------------------------------------------- plugin plumbing
class Scripted:
    """A detector that follows a script: it returns what it is told, so the tests prove recording, not seeing."""

    name, capability = "scripted-detector", "object.detect"
    model_id = detector_id = "rt-detr-r18vd"

    def __init__(self, per_frame=None):
        self.calls = 0
        self.per_frame = per_frame or (lambda i: [{"label": "person", "class_id": 0, "score": 0.9,
                                                    "box": {"x": 20 + 6 * i, "y": 40, "w": 50, "h": 100}}])

    def available(self):
        return True, ""

    def supports(self, **_):
        return True

    def detect_array(self, img, *, min_score=0.4, max_objects=100):
        objs = [o for o in self.per_frame(self.calls) if o["score"] >= min_score]
        self.calls += 1
        return objs

    def run(self, *, data, min_score=0.4, max_objects=100, **_):
        return {"objects": self.detect_array(None, min_score=min_score), "image": {"width": 320, "height": 240}}


@pytest.fixture()
def client(db, dsn, tmp_path):
    from fastapi.testclient import TestClient

    from astral.api import create_app
    from astral.config import Settings

    app = create_app(Settings(dsn=dsn, data_dir=tmp_path), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        for n in ("media-probe", "objects", "video-tracks"):
            r = c.post("/plugins", json={"path": str(REPO / "plugins" / "core" / n)})
            assert r.status_code == 201, r.text
            c.post(f"/plugins/{r.json()['id']}/{r.json()['version']}/enabled")
        yield c


def use(client, backend):
    client.app.state.host.backends._by_cap["object.detect"] = [backend]


def upload(client, case, data: bytes, mime: str) -> str:
    r = client.post(f"/cases/{case}/artifacts", content=data, headers={"content-type": mime})
    assert r.status_code == 201, r.text
    return r.json()["sha256"]


def png() -> bytes:
    import cv2

    img = np.full((240, 320, 3), 60, np.uint8)
    cv2.rectangle(img, (40, 40), (200, 180), (200, 120, 40), -1)
    return cv2.imencode(".png", img)[1].tobytes()


@need_cv
def test_an_image_is_ingested_into_object_regions_with_provenance_and_honest_coverage(client):
    use(client, Scripted(lambda i: [
        {"label": "car", "class_id": 2, "score": 0.93, "box": {"x": 10, "y": 20, "w": 100, "h": 50}},
        {"label": "person", "class_id": 0, "score": 0.61, "box": {"x": 150, "y": 30, "w": 40, "h": 120}}]))
    case = client.post("/cases", json={"name": "objects"}).json()["id"]
    sha = upload(client, case, png(), "image/png")
    rep = client.post(f"/cases/{case}/artifacts/{sha}/ingest").json()
    step = rep["steps"]["objects"]
    assert step["status"] == "ok" and step["assertions"] == 12 and step["stats"]["objects"] == 2
    cov = {c["source"]: c for c in step["coverage"]}["object detection"]
    assert cov["outcome"] == "hits" and "no identification" in cov["note"] and "80-class" in cov["note"]


@need_cv
def test_zero_detections_say_none_detected_not_nothing_there(client):
    use(client, Scripted(lambda i: []))
    case = client.post("/cases", json={"name": "empty"}).json()["id"]
    rep = client.post(f"/cases/{case}/artifacts/{upload(client, case, png(), 'image/png')}/ingest").json()
    cov = {c["source"]: c for c in rep["steps"]["objects"]["coverage"]}["object detection"]
    assert cov["outcome"] == "no_hits" and "not proof" in cov["note"]


@need_cv
def test_without_the_model_the_step_is_unknown_not_empty(client, monkeypatch):
    monkeypatch.setattr(objects, "models_dir", lambda: None)
    client.app.state.host.backends._by_cap["object.detect"] = [objects.RtDetrOnnx()]
    case = client.post("/cases", json={"name": "nomodel"}).json()["id"]
    rep = client.post(f"/cases/{case}/artifacts/{upload(client, case, png(), 'image/png')}/ingest").json()
    cov = {c["source"]: c for c in rep["steps"]["objects"]["coverage"]}["object detection"]
    assert cov["outcome"] == "unknown" and "not found" in cov["note"]


def make_video(path: Path, seconds: int = 3) -> bytes:
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=10", "-t", str(seconds),
                    "-pix_fmt", "yuv420p", str(path)], check=True, timeout=120)
    return path.read_bytes()


@need_cv
@need_ffmpeg
def test_a_video_becomes_tracks_with_a_stated_sampling_and_no_identity_claim(client, tmp_path):
    use(client, Scripted())
    case = client.post("/cases", json={"name": "video"}).json()["id"]
    sha = upload(client, case, make_video(tmp_path / "v.mp4"), "video/mp4")
    rep = client.post(f"/cases/{case}/artifacts/{sha}/ingest").json()
    assert rep["kind"] == "video"
    step = rep["steps"]["tracks"]
    assert step["status"] == "ok" and step["stats"]["tracks"] == 1 and step["stats"]["truncated"] is False
    assert step["stats"]["frames_sampled"] in (12, 13)
    assert step["assertions"] == 9
    note = {c["source"]: c for c in step["coverage"]}["object tracking"]["note"]
    assert "sampled" in note and "not identities" in note


@need_cv
@need_ffmpeg
def test_a_video_longer_than_the_frame_cap_reports_that_the_rest_was_not_scanned(tmp_path):
    clip = tmp_path / "v.mp4"
    make_video(clip)
    capped = objects.track_video(clip, Scripted(), fps=4, max_frames=5)
    assert capped["truncated"] is True and capped["frames_sampled"] == 5 and capped["covered_ms"] == 1250
    full = objects.track_video(clip, Scripted(), fps=4, max_frames=600)
    assert full["truncated"] is False and full["frames_sampled"] in (12, 13)
    assert full["frame"] == {"width": 320, "height": 240}


@need_cv
@need_ffmpeg
def test_a_clip_with_no_readable_video_is_an_error_not_an_empty_result(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video at all")
    with pytest.raises(RuntimeError):
        objects.track_video(bad, Scripted(), fps=4)


# --------------------------------------------------------------------------------------- real model
@need_model
def test_real_model_boxes_match_the_model_cards_published_output_for_its_example_image():
    published = {"cat": [(343.38, 24.28, 640.14, 371.5), (13.23, 54.18, 318.98, 472.22)],
                 "remote": [(40.11, 73.44, 175.96, 118.48), (333.73, 76.58, 369.97, 186.99)]}
    res = objects.RtDetrOnnx().run(data=Path(IMAGE).read_bytes(), min_score=0.5)
    assert res["image"] == {"width": 640, "height": 480}
    for label, boxes in published.items():
        found = [o for o in res["objects"] if o["label"] == label]
        assert len(found) == len(boxes), (label, res["objects"])
        for x1, y1, x2, y2 in boxes:
            best = max(found, key=lambda o: tracking.iou_matrix(
                np.array([[x1, y1, x2 - x1, y2 - y1]]), np.array([[o["box"]["x"], o["box"]["y"], o["box"]["w"], o["box"]["h"]]]))[0, 0])
            iou = tracking.iou_matrix(np.array([[x1, y1, x2 - x1, y2 - y1]]),
                                      np.array([[best["box"]["x"], best["box"]["y"], best["box"]["w"], best["box"]["h"]]]))[0, 0]
            assert iou > 0.9, (label, best, iou)


@need_model
@need_ffmpeg
def test_real_model_follows_two_cats_through_a_panning_clip(tmp_path):
    """The still image is panned across a window so everything in it moves 20 px a second; the cats must come out as
    two tracks that last, not a swarm of fragments."""
    clip = tmp_path / "pan.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-loop", "1", "-framerate", "12", "-i", IMAGE, "-vf",
                    "crop=420:360:x='t*20':y=40", "-t", "6", "-pix_fmt", "yuv420p", str(clip)], check=True, timeout=180)
    out = objects.track_video(clip, objects.RtDetrOnnx(), fps=4, min_score=0.3)
    cats = [t for t in out["tracks"] if t["label"] == "cat"]
    assert out["frames_sampled"] >= 23 and not out["truncated"]
    assert len(cats) == 2 and all(t["frames"] >= 18 for t in cats), [(t["label"], t["frames"]) for t in out["tracks"]]
