"""Backend registry: capability -> ordered implementations (invariants 4, 5).

A capability such as `doc.parse` or `ner.extract` has several backends of
different quality and weight. Backends are listed best-first. `resolve` returns
the best AVAILABLE one that supports the input and says, in the result, whether
a better backend was skipped because it is not installed. That flag flows into
`derived_by.degraded` / `degraded_from`, so degradation is visible, never silent.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from typing import Any, Protocol


class Backend(Protocol):
    name: str
    capability: str
    model_id: str | None            # key in data/models.yaml, when a model is involved

    def available(self) -> tuple[bool, str]: ...
    def supports(self, **kw: Any) -> bool: ...
    def run(self, **kw: Any) -> Any: ...


@dataclass
class Resolved:
    backend: Backend
    degraded: bool = False
    degraded_from: str | None = None
    skipped: list[str] = field(default_factory=list)      # "name: reason"

    def derived_by(self) -> dict[str, Any]:
        d: dict[str, Any] = {"backend": self.backend.name, "degraded": self.degraded}
        if self.degraded:
            d["degraded_from"] = self.degraded_from
        if getattr(self.backend, "model_id", None):
            d["model"] = self.backend.model_id
        return d


class NoBackend(Exception):
    pass


class BackendRegistry:
    def __init__(self) -> None:
        self._by_cap: dict[str, list[Backend]] = {}

    def register(self, backend: Backend) -> None:
        self._by_cap.setdefault(backend.capability, []).append(backend)

    def backends(self, capability: str) -> list[Backend]:
        return list(self._by_cap.get(capability, []))

    def resolve(self, capability: str, *, prefer: str | None = None, **kw: Any) -> Resolved:
        order = self.backends(capability)
        if prefer:                                   # an explicit preference goes first
            order.sort(key=lambda b: b.name != prefer)
        skipped: list[str] = []
        first_choice: str | None = None
        for b in order:
            if not b.supports(**kw):
                continue
            ok, why = b.available()
            if first_choice is None:
                first_choice = b.name
            if ok:
                degraded = b.name != first_choice
                return Resolved(b, degraded, first_choice if degraded else None, skipped)
            skipped.append(f"{b.name}: {why}")
        raise NoBackend(f"no available backend for {capability}"
                        + (f" ({'; '.join(skipped)})" if skipped else ""))


def module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False
