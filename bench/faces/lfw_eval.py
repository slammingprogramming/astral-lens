"""YuNet + SFace on LFW: accuracy (06 §9 item 4) and CPU throughput (item 2).

    python lfw_eval.py MODELS_DIR DATA_HOME [--timing-only]

Protocol: the standard LFW 10-fold pairs (6,000 pairs, 300 same / 300 different
per fold). For each held-out fold the decision threshold is chosen on the other
nine, as in the 'restricted' protocol. Faces are found with YuNet, aligned from
YuNet's landmarks with SFace's alignCrop, embedded with SFace, and scored by
cosine similarity. Images are the full 250x250 deep-funneled LFW images, read
one pair at a time (scikit-learn is used only to download and cache the data).

LFW is easy and saturated. A good number here is a FLOOR CHECK: it says the
pipeline is wired correctly and is not broken, not that it will identify
people in low-resolution CCTV, profile or occluded images. It says nothing
about InsightFace, whose weights are non-commercial and were not downloaded.
"""

import json
import os
import statistics
import sys
import time

import cv2
import numpy as np
from sklearn.datasets import fetch_lfw_pairs
from sklearn.metrics import roc_auc_score

models, data_home = sys.argv[1], sys.argv[2]
det_path = os.path.join(models, "face_detection_yunet_2023mar.onnx")
rec_path = os.path.join(models, "face_recognition_sface_2021dec.onnx")

# Trigger scikit-learn's download/cache with a tiny in-memory load, then read files ourselves.
fetch_lfw_pairs(subset="10_folds", color=False, resize=0.1, funneled=True, data_home=data_home)
home = os.path.join(data_home, "lfw_home")
img_root = os.path.join(home, "lfw_funneled")


def img_path(name, k):
    return os.path.join(img_root, name, f"{name}_{int(k):04d}.jpg")


def read_pairs():
    """-> list of (path_a, path_b, same, fold) from the official pairs.txt."""
    lines = open(os.path.join(home, "pairs.txt"), encoding="utf-8").read().split("\n")
    n_folds, per = (int(v) for v in lines[0].split())
    out, i = [], 1
    for fold in range(n_folds):
        for _ in range(per):                          # same-person pairs
            n, a, b = lines[i].split(); i += 1
            out.append((img_path(n, a), img_path(n, b), 1, fold))
        for _ in range(per):                          # different-person pairs
            n1, a, n2, b = lines[i].split(); i += 1
            out.append((img_path(n1, a), img_path(n2, b), 0, fold))
    return out


pairs = read_pairs()
y = np.array([p[2] for p in pairs])
folds = np.array([p[3] for p in pairs])
print("pairs", len(pairs), "same", int(y.sum()), flush=True)

det = cv2.FaceDetectorYN.create(det_path, "", (250, 250), 0.6, 0.3, 5000)
rec = cv2.FaceRecognizerSF.create(rec_path, "")


def load(path):
    im = cv2.imread(path)
    if im is None:
        raise FileNotFoundError(path)
    return im


def embed(img):
    """-> (feature, detected). Falls back to a centre crop when no face is found."""
    det.setInputSize((img.shape[1], img.shape[0]))
    _, faces = det.detect(img)
    if faces is not None and len(faces):
        f = faces[np.argmax(faces[:, -1])]
        return rec.feature(rec.alignCrop(img, f)).ravel(), True
    c = cv2.resize(img[60:190, 60:190], (112, 112))
    return rec.feature(c).ravel(), False


# ---- throughput (single-threaded and default threads), after a warm-up -------------
sample = [load(pairs[i][0]) for i in range(0, 600, 2)]      # 300 images
default_threads = cv2.getNumThreads()
for n_threads in (1, default_threads):
    cv2.setNumThreads(n_threads)
    for im in sample[:20]:
        embed(im)
    t_det = t_rec = 0.0
    for im in sample:
        det.setInputSize((im.shape[1], im.shape[0]))
        t0 = time.perf_counter()
        _, faces = det.detect(im)
        t1 = time.perf_counter()
        if faces is not None and len(faces):
            rec.feature(rec.alignCrop(im, faces[np.argmax(faces[:, -1])]))
        t2 = time.perf_counter()
        t_det += t1 - t0
        t_rec += t2 - t1
    print(json.dumps({"threads": n_threads, "image": "250x250",
                      "detect_ms": round(t_det / len(sample) * 1000, 2),
                      "align_embed_ms": round(t_rec / len(sample) * 1000, 2),
                      "images_per_s": round(len(sample) / (t_det + t_rec), 1)}), flush=True)
cv2.setNumThreads(default_threads)
if "--timing-only" in sys.argv:          # rerun on an otherwise idle machine
    sys.exit(0)

# ---- accuracy -------------------------------------------------------------------------
feats, detected = np.zeros((len(pairs), 2, 128), np.float32), np.zeros((len(pairs), 2), bool)
t0 = time.time()
for i, (pa, pb, _, _) in enumerate(pairs):
    feats[i, 0], detected[i, 0] = embed(load(pa))
    feats[i, 1], detected[i, 1] = embed(load(pb))
print("embedded %d images in %.0fs" % (detected.size, time.time() - t0), flush=True)
feats /= np.linalg.norm(feats, axis=2, keepdims=True)
scores = (feats[:, 0] * feats[:, 1]).sum(1)

accs, thr = [], []
for k in range(10):
    tr, te = folds != k, folds == k
    cand = np.linspace(scores[tr].min(), scores[tr].max(), 400)
    best = max(cand, key=lambda t: ((scores[tr] > t) == y[tr]).mean())
    accs.append(((scores[te] > best) == y[te]).mean())
    thr.append(float(best))

neg, pos = np.sort(scores[y == 0]), scores[y == 1]
t_far = {far: float(neg[int(len(neg) * (1 - far))]) for far in (1e-2, 1e-3)}
res = {
    "model": "YuNet 2023mar + SFace 2021dec (OpenCV %s)" % cv2.__version__,
    "dataset": "LFW 10-fold pairs, deep-funneled, full 250x250",
    "accuracy_mean": round(float(np.mean(accs)), 4), "accuracy_std": round(float(np.std(accs)), 4),
    "auc": round(float(roc_auc_score(y, scores)), 4),
    "tar_at_far_1e-2": round(float((pos > t_far[1e-2]).mean()), 4),
    "tar_at_far_1e-3": round(float((pos > t_far[1e-3]).mean()), 4),
    "mean_threshold": round(statistics.mean(thr), 3),
    "no_face_detected_images": int((~detected).sum()), "images": int(detected.size),
}
print(json.dumps(res), flush=True)
