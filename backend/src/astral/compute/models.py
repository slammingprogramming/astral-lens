"""Model registry and licence preflight (ADR-0002, invariant 8).

Preflight warns *before* a non-commercial or unknown-licence weight is used in
a deployment that declared itself commercial. It reports; it does not gate
(the operator chooses freely, the platform never hides what it costs them).
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from typing import Any

import yaml


@dataclass(frozen=True)
class ModelEntry:
    id: str
    capability: str
    licence: str
    licence_class: str
    commercial_use: bool | None
    default: bool
    verified: str
    source: str
    notes: str = ""
    checked: str = ""


def load_registry() -> dict[str, ModelEntry]:
    raw = yaml.safe_load(
        resources.files("astral").joinpath("data/models.yaml").read_text(encoding="utf-8")
    )
    out: dict[str, ModelEntry] = {}
    for m in raw["models"]:
        entry = ModelEntry(
            id=m["id"], capability=m["capability"], licence=m["licence"],
            licence_class=m["licence_class"], commercial_use=m.get("commercial_use"),
            default=bool(m.get("default", False)), verified=m.get("verified", "secondary"),
            source=m.get("source", ""), notes=(m.get("notes") or "").strip(),
            checked=str(m.get("checked", "")),
        )
        if entry.id in out:
            raise ValueError(f"duplicate model id {entry.id}")
        out[entry.id] = entry
    return out


@dataclass(frozen=True)
class LicenceFinding:
    model_id: str
    level: str          # 'block-commercial' | 'warn'
    message: str


def licence_preflight(
    model_ids: list[str], *, deployment_commercial: bool, registry: dict[str, ModelEntry] | None = None
) -> list[LicenceFinding]:
    """Findings for the models a deployment is about to use. Unknown is never
    treated as permitted."""
    reg = registry or load_registry()
    findings: list[LicenceFinding] = []
    for mid in model_ids:
        m = reg.get(mid)
        if m is None:
            findings.append(LicenceFinding(mid, "warn", "not in the model registry: licence unknown"))
            continue
        if deployment_commercial and m.commercial_use is False:
            findings.append(LicenceFinding(
                mid, "block-commercial",
                f"{m.licence} does not permit commercial use, but this deployment declared "
                "itself commercial. Obtain a commercial licence or use the licence-clean default."))
        elif deployment_commercial and m.commercial_use is None:
            findings.append(LicenceFinding(
                mid, "warn", f"commercial-use status of {m.licence} is unconfirmed"))
        if m.verified != "primary":
            findings.append(LicenceFinding(
                mid, "warn", "licence not verified against the primary model card"))
    return findings


def defaults_for(capability: str, registry: dict[str, ModelEntry] | None = None) -> list[ModelEntry]:
    reg = registry or load_registry()
    return [m for m in reg.values() if m.capability == capability and m.default]


def asdict_entry(m: ModelEntry) -> dict[str, Any]:
    return {k: getattr(m, k) for k in m.__dataclass_fields__}
