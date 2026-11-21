"""Model residency manager (05 §3).

Owns each accelerator and brokers access: VRAM accounting using *measured*
footprints once known, LRU eviction with pinning, admission control decided
before work starts, and device serialization so two big models can never both
conclude they fit.

This module deliberately knows nothing about any model library. Callers
supply loader/unloader callables; plugins never load models themselves
(invariant 5).
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Literal

import psycopg

from astral.compute.profiles import Profile

Accel = Literal["required", "preferred", "cpu_only", "remote"]


@dataclass(frozen=True)
class BackendSpec:
    name: str                    # e.g. "sface-onnx-cuda"
    device: Literal["cuda", "cpu", "remote"]
    vram_mb: int = 0
    ram_mb: int = 0


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    capability: str
    backends: tuple[BackendSpec, ...]          # in preference order
    accelerator: Accel = "preferred"


@dataclass
class Placement:
    model_id: str
    backend: str
    device: str
    handle: Any
    # Degradation is visible, never silent (invariant 4)
    degraded: bool = False
    degraded_from: str | None = None
    reason: str | None = None

    def derived_by_fields(self) -> dict[str, Any]:
        """Fields to merge into an assertion's derived_by."""
        out: dict[str, Any] = {"backend": self.backend, "model": self.model_id,
                               "degraded": self.degraded}
        if self.degraded:
            out["degraded_from"] = self.degraded_from
        return out


class AdmissionRejected(Exception):
    """The job cannot be served on this host, now or ever. Decided up front."""


class AdmissionDeferred(Exception):
    """The device is busy; retry later. Distinct from rejection."""


@dataclass
class _Loaded:
    spec: ModelSpec
    backend: BackendSpec
    handle: Any
    unloader: Callable[[Any], None]
    vram_mb: int
    large: bool
    users: int = 0
    pinned: bool = False


@dataclass
class FootprintCache:
    """Measured footprints, preferred over declared ones: declared figures are
    always optimistic (05 §3)."""

    data: dict[tuple[str, str, str], tuple[int, int]] = field(default_factory=dict)

    def get(self, model_id: str, backend: str, device: str) -> tuple[int, int] | None:
        return self.data.get((model_id, backend, device))

    def put(self, model_id: str, backend: str, device: str, vram: int, ram: int) -> None:
        self.data[(model_id, backend, device)] = (vram, ram)

    def load_from_db(self, conn: psycopg.Connection) -> None:
        for r in conn.execute(
            "SELECT DISTINCT ON (model_id, backend, device) model_id, backend, device, vram_mb, ram_mb"
            " FROM model_footprint ORDER BY model_id, backend, device, id DESC"
        ):
            self.put(r["model_id"], r["backend"], r["device"], r["vram_mb"], r["ram_mb"])

    @staticmethod
    def persist(conn: psycopg.Connection, model_id: str, backend: str, device: str,
                vram: int, ram: int, host: str, notes: str | None = None) -> None:
        from datetime import datetime, timezone

        conn.execute(
            "INSERT INTO model_footprint (model_id, backend, device, vram_mb, ram_mb,"
            " measured_at, host, notes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (model_id, backend, device, vram, ram, datetime.now(timezone.utc), host, notes),
        )


class ResidencyManager:
    def __init__(
        self,
        profile: Profile,
        device_vram_mb: int | None,
        *,
        footprints: FootprintCache | None = None,
        measure: Callable[[], int | None] | None = None,
        wait_timeout: float = 30.0,
    ) -> None:
        self.profile = profile
        self.total_mb = device_vram_mb if (profile.accelerator and device_vram_mb) else 0
        self.footprints = footprints or FootprintCache()
        self._measure = measure                  # returns current used VRAM in MB, or None
        self.wait_timeout = wait_timeout
        self._loaded: "OrderedDict[tuple[str, str], _Loaded]" = OrderedDict()
        self._cv = threading.Condition(threading.RLock())

    # --------------------------------------------------------------- accounting
    @property
    def budget_mb(self) -> int:
        return max(0, self.total_mb - self.profile.vram_reserve_mb)

    def used_mb(self) -> int:
        return sum(m.vram_mb for m in self._loaded.values())

    def free_mb(self) -> int:
        return self.budget_mb - self.used_mb()

    def resident(self) -> list[str]:
        return [f"{k[0]}/{k[1]}" for k in self._loaded]

    def _need(self, spec: ModelSpec, b: BackendSpec) -> int:
        m = self.footprints.get(spec.model_id, b.name, b.device)
        return m[0] if m else b.vram_mb

    # ----------------------------------------------------------------- acquire
    @contextmanager
    def use(
        self, spec: ModelSpec, loader: Callable[[BackendSpec, str], Any],
        unloader: Callable[[Any], None] = lambda h: None, *, pin: bool = False,
    ) -> Iterator[Placement]:
        placement, loaded = self._acquire(spec, loader, unloader, pin)
        try:
            yield placement
        finally:
            with self._cv:
                if loaded is not None:
                    loaded.users -= 1
                self._cv.notify_all()

    def unpin(self, model_id: str) -> None:
        with self._cv:
            for (mid, _), m in self._loaded.items():
                if mid == model_id:
                    m.pinned = False
            self._cv.notify_all()

    def _acquire(self, spec: ModelSpec, loader: Callable, unloader: Callable,
                 pin: bool) -> tuple[Placement, _Loaded | None]:
        first = spec.backends[0]
        cpu = [b for b in spec.backends if b.device in ("cpu", "remote")]
        gpu = [b for b in spec.backends if b.device == "cuda"]

        if spec.accelerator in ("cpu_only", "remote"):
            return self._cpu_placement(spec, cpu, loader, degraded=False, reason=None, first=first)

        # accelerator wanted
        if spec.accelerator == "required" and not self.total_mb:
            raise AdmissionRejected(
                f"{spec.model_id} requires an accelerator but profile "
                f"{self.profile.name!r} has none")

        fits_ever = [b for b in gpu if self.total_mb and self._need(spec, b) <= self.budget_mb]
        if not fits_ever:
            if spec.accelerator == "required":
                raise AdmissionRejected(
                    f"{spec.model_id} needs more VRAM than this device can offer even alone "
                    f"({self.budget_mb} MB usable)")
            reason = ("no accelerator in this profile" if not self.total_mb
                      else "does not fit on the device even alone")
            return self._cpu_placement(spec, cpu, loader, degraded=True, reason=reason, first=first)

        b = fits_ever[0]
        with self._cv:
            deadline = time.monotonic() + self.wait_timeout
            while True:
                existing = self._loaded.get((spec.model_id, b.name))
                if existing:
                    self._loaded.move_to_end((spec.model_id, b.name))
                    existing.users += 1
                    existing.pinned = existing.pinned or pin
                    return self._placement(existing, first), existing
                if self._make_room(spec, b):
                    break
                left = deadline - time.monotonic()
                if left <= 0:
                    raise AdmissionDeferred(
                        f"device busy: cannot place {spec.model_id} within {self.wait_timeout}s")
                self._cv.wait(timeout=min(left, 1.0))
            loaded = self._load(spec, b, loader, unloader, pin)
            return self._placement(loaded, first), loaded

    def _make_room(self, spec: ModelSpec, b: BackendSpec) -> bool:
        """Evict LRU unpinned, idle models until `b` fits. True when it does."""
        need = self._need(spec, b)
        large = need >= self.profile.large_model_mb
        while True:
            over_vram = self.used_mb() + need > self.budget_mb
            over_large = large and sum(1 for m in self._loaded.values() if m.large) >= \
                self.profile.max_resident_large
            if not over_vram and not over_large:
                return True
            # idle, unpinned, least recently used first; while the large-model
            # cap is what blocks us, only a large model can make room
            idle = [(k, m) for k, m in self._loaded.items() if m.users == 0 and not m.pinned]
            if over_large:
                idle = [(k, m) for k, m in idle if m.large]
            if not idle:
                return False
            self._evict(idle[0][0])

    def _evict(self, key: tuple[str, str]) -> None:
        m = self._loaded.pop(key)
        m.unloader(m.handle)

    def _load(self, spec: ModelSpec, b: BackendSpec, loader: Callable, unloader: Callable,
              pin: bool) -> _Loaded:
        before = self._measure() if self._measure else None
        handle = loader(b, "cuda")
        after = self._measure() if self._measure else None
        declared = self._need(spec, b)
        actual = max(0, after - before) if (before is not None and after is not None) else None
        if actual:
            # Measured beats declared from now on.
            self.footprints.put(spec.model_id, b.name, "cuda", actual, b.ram_mb)
        vram = actual or declared
        loaded = _Loaded(spec, b, handle, unloader, vram, vram >= self.profile.large_model_mb,
                         users=1, pinned=pin)
        self._loaded[(spec.model_id, b.name)] = loaded
        return loaded

    def _cpu_placement(self, spec: ModelSpec, cpu: list[BackendSpec], loader: Callable,
                       *, degraded: bool, reason: str | None,
                       first: BackendSpec) -> tuple[Placement, None]:
        if not cpu:
            raise AdmissionRejected(
                f"{spec.model_id}: no CPU or remote backend declared, and the accelerator "
                "cannot serve it")
        b = cpu[0]
        handle = loader(b, b.device)
        return Placement(spec.model_id, b.name, b.device, handle, degraded=degraded,
                         degraded_from=first.name if degraded else None, reason=reason), None

    @staticmethod
    def _placement(m: _Loaded, first: BackendSpec) -> Placement:
        degraded = m.backend.name != first.name
        return Placement(m.spec.model_id, m.backend.name, m.backend.device, m.handle,
                         degraded=degraded, degraded_from=first.name if degraded else None)
