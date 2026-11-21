"""Multi-object tracking by detection: link per-frame boxes into tracks (a ByteTrack-style tracker, written out here).

What a track is, and is not. A track says "boxes with the same detector label overlapped plausibly from one sampled
frame to the next". It is a grouping of detections, not an identity: two people who cross, or an object that leaves
and re-enters, can be split into two tracks or swapped between them, and nothing here re-identifies anyone across a
gap longer than `max_lost` sampled frames. The label is the detector's class guess (score-weighted vote across the
track), not a fact. Frames are sampled, so a track's start and end are the first and last SAMPLED frame in which the
object was seen, and the object may have been present between samples.

Method (Zhang et al., "ByteTrack", ECCV 2022): a constant-velocity Kalman filter on (centre, aspect ratio, height)
predicts each track; high-score detections are matched to tracks first (IoU, Hungarian assignment), then the
low-score detections are used only to keep tracks that were just matched poorly, which is what recovers an object
that is briefly blurred or half hidden; an unmatched high-score detection starts a track. A track is reported only
after `min_hits` matched frames, so a one-frame flicker is not a track. Matching is within the same class only.

No third-party tracker is used; numpy is the only dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

_WP, _WV = 1.0 / 20, 1.0 / 160            # the position / velocity noise weights ByteTrack and SORT use


@dataclass
class Det:
    x: float
    y: float
    w: float
    h: float
    score: float
    label: str
    class_id: int = -1


def iou_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """a: (n,4) and b: (m,4) as x, y, w, h -> (n,m) intersection over union."""
    if len(a) == 0 or len(b) == 0:
        return np.zeros((len(a), len(b)))
    ax2, ay2, bx2, by2 = a[:, 0] + a[:, 2], a[:, 1] + a[:, 3], b[:, 0] + b[:, 2], b[:, 1] + b[:, 3]
    iw = np.clip(np.minimum(ax2[:, None], bx2[None]) - np.maximum(a[:, 0][:, None], b[:, 0][None]), 0, None)
    ih = np.clip(np.minimum(ay2[:, None], by2[None]) - np.maximum(a[:, 1][:, None], b[:, 1][None]), 0, None)
    inter = iw * ih
    union = (a[:, 2] * a[:, 3])[:, None] + (b[:, 2] * b[:, 3])[None] - inter
    return np.where(union > 0, inter / np.where(union > 0, union, 1), 0.0)


def assign(cost: np.ndarray) -> list[tuple[int, int]]:
    """Minimum-cost assignment on a rectangular cost matrix (Hungarian algorithm, O(n^3)); returns (row, col) pairs."""
    n, m = cost.shape
    if n == 0 or m == 0:
        return []
    transposed = n > m
    c = cost.T if transposed else cost
    n, m = c.shape                                              # now n <= m
    inf = float("inf")
    u, v = [0.0] * (n + 1), [0.0] * (m + 1)
    p, way = [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0], j0 = i, 0
        minv, used = [inf] * (m + 1), [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], inf, 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = float(c[i0 - 1, j - 1]) - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    pairs = [(p[j] - 1, j - 1) for j in range(1, m + 1) if p[j]]
    return [(cj, ri) for ri, cj in pairs] if transposed else pairs


class _Kalman:
    """Constant-velocity filter over (cx, cy, aspect, height) and their rates, one step = one sampled frame."""

    F = np.eye(8)
    F[:4, 4:] = np.eye(4)
    H = np.eye(4, 8)

    def __init__(self, z: np.ndarray) -> None:
        h = z[3]
        self.m = np.concatenate([z, np.zeros(4)])
        self.P = np.diag(np.square([2 * _WP * h, 2 * _WP * h, 1e-2, 2 * _WP * h, 10 * _WV * h, 10 * _WV * h, 1e-5, 10 * _WV * h]))

    def predict(self) -> None:
        h = self.m[3]
        q = np.diag(np.square([_WP * h, _WP * h, 1e-2, _WP * h, _WV * h, _WV * h, 1e-5, _WV * h]))
        self.m = self.F @ self.m
        self.P = self.F @ self.P @ self.F.T + q

    def update(self, z: np.ndarray) -> None:
        h = self.m[3]
        r = np.diag(np.square([_WP * h, _WP * h, 1e-1, _WP * h]))
        s = self.H @ self.P @ self.H.T + r
        k = self.P @ self.H.T @ np.linalg.inv(s)
        self.m = self.m + k @ (z - self.H @ self.m)
        self.P = self.P - k @ s @ k.T

    def box(self) -> np.ndarray:
        cx, cy, a, h = self.m[:4]
        w = a * h
        return np.array([cx - w / 2, cy - h / 2, w, h])


def _z(d: Det | np.ndarray) -> np.ndarray:
    x, y, w, h = (d.x, d.y, d.w, d.h) if isinstance(d, Det) else d
    h = max(float(h), 1e-3)
    return np.array([x + w / 2, y + h / 2, w / h, h])


@dataclass
class _Track:
    id: int
    kf: _Kalman
    votes: dict[str, float]
    class_ids: dict[str, int]
    first_ms: int
    last_ms: int
    hits: int = 1
    lost: int = 0
    obs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def label(self) -> str:
        return max(self.votes.items(), key=lambda kv: kv[1])[0]


class Tracker:
    def __init__(self, *, high: float = 0.5, low: float = 0.1, new: float = 0.6, match_iou: float = 0.3,
                 low_iou: float = 0.5, max_lost: int = 5, min_hits: int = 2) -> None:
        self.high, self.low, self.new, self.match_iou, self.low_iou = high, low, new, match_iou, low_iou
        self.max_lost, self.min_hits = max_lost, min_hits
        self._live: list[_Track] = []
        self._done: list[_Track] = []
        self._next = 1

    # -- one sampled frame ---------------------------------------------------------------------------------
    def update(self, t_ms: int, dets: list[Det]) -> None:
        for t in self._live:
            t.kf.predict()
        hi = [d for d in dets if d.score >= self.high]
        lo = [d for d in dets if self.low <= d.score < self.high]
        matched: set[int] = set()
        used_hi = self._associate(self._live, hi, self.match_iou, t_ms, matched)
        rest = [t for t in self._live if t.id not in matched and t.lost == 0]              # tracks seen on the previous frame
        self._associate(rest, lo, self.low_iou, t_ms, matched)
        for t in self._live:
            if t.id not in matched:
                t.lost += 1
        for i, d in enumerate(hi):
            if i not in used_hi and d.score >= self.new:
                self._live.append(self._start(t_ms, d))
        keep = []
        for t in self._live:
            (self._done if t.lost > self.max_lost else keep).append(t)
        self._live = keep

    def _associate(self, tracks: list[_Track], dets: list[Det], min_iou: float, t_ms: int, matched: set[int]) -> set[int]:
        if not tracks or not dets:
            return set()
        iou = iou_matrix(np.array([t.kf.box() for t in tracks]), np.array([[d.x, d.y, d.w, d.h] for d in dets]))
        cost = 1.0 - iou
        for i, t in enumerate(tracks):
            for j, d in enumerate(dets):
                if d.label not in t.votes or t.label != d.label:
                    cost[i, j] = 1.0                                                          # never match across classes
        used = set()
        for i, j in assign(cost):
            if cost[i, j] <= 1.0 - min_iou:
                self._observe(tracks[i], t_ms, dets[j])
                matched.add(tracks[i].id)
                used.add(j)
        return used

    def _observe(self, t: _Track, t_ms: int, d: Det) -> None:
        t.kf.update(_z(d))
        t.hits += 1
        t.lost = 0
        t.last_ms = t_ms
        t.votes[d.label] = t.votes.get(d.label, 0.0) + d.score
        t.class_ids[d.label] = d.class_id
        t.obs.append({"t_ms": t_ms, "x": round(d.x, 1), "y": round(d.y, 1), "w": round(d.w, 1), "h": round(d.h, 1), "score": round(d.score, 3)})

    def _start(self, t_ms: int, d: Det) -> _Track:
        t = _Track(self._next, _Kalman(_z(d)), {d.label: d.score}, {d.label: d.class_id}, t_ms, t_ms)
        t.obs.append({"t_ms": t_ms, "x": round(d.x, 1), "y": round(d.y, 1), "w": round(d.w, 1), "h": round(d.h, 1), "score": round(d.score, 3)})
        self._next += 1
        return t

    # -- result --------------------------------------------------------------------------------------------
    def tracks(self) -> list[dict[str, Any]]:
        out = []
        for t in sorted(self._done + self._live, key=lambda t: (t.first_ms, t.id)):
            if t.hits < self.min_hits:
                continue
            scores = [o["score"] for o in t.obs]
            out.append({"track": t.id, "label": t.label, "class_id": t.class_ids[t.label], "start_ms": t.first_ms,
                        "end_ms": t.last_ms, "frames": t.hits, "mean_score": round(sum(scores) / len(scores), 3),
                        "max_score": max(scores), "boxes": t.obs,
                        "labels_seen": {k: round(v, 3) for k, v in sorted(t.votes.items(), key=lambda kv: -kv[1])}})
        return out
