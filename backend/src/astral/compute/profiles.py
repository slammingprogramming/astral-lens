"""Compute profiles (05 §5). A profile is a shipped default, never a cage:
every field is individually overridable."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from astral.compute.probe import HostReport


@dataclass(frozen=True)
class Profile:
    name: str
    accelerator: bool
    # worker pools this host runs, and their concurrency
    queues: dict[str, int] = field(default_factory=dict)
    # a model above this is "large": only one resident at a time per device
    large_model_mb: int = 2048
    max_resident_large: int = 1
    # headroom kept free for the desktop / driver / activations
    vram_reserve_mb: int = 512
    vector_index: str = "embedded"
    local_llm: str = "no"
    notes: tuple[str, ...] = ()

    def with_overrides(self, **kw: Any) -> "Profile":
        return replace(self, **kw)


PROFILES: dict[str, Profile] = {
    "cpu-only": Profile(
        "cpu-only", accelerator=False, queues={"cpu": 4, "remote": 2},
        local_llm="no / tiny",
        notes=("REQUIRED-accelerator stages are rejected with an explanation.",),
    ),
    "small-gpu": Profile(
        "small-gpu", accelerator=True,
        queues={"cpu": 4, "gpu-small": 2, "gpu-large": 1, "remote": 2},
        local_llm="quantized, swapped",
    ),
    "workstation": Profile(
        "workstation", accelerator=True,
        queues={"cpu": 8, "gpu-small": 4, "gpu-large": 2, "remote": 4},
        max_resident_large=3, vram_reserve_mb=1024, local_llm="resident",
    ),
    "server": Profile(
        "server", accelerator=True,
        queues={"cpu": 16, "gpu-small": 8, "gpu-large": 4, "remote": 8},
        max_resident_large=4, vram_reserve_mb=2048, local_llm="strong, resident",
    ),
    "cluster": Profile(
        "cluster", accelerator=True,
        queues={"cpu": 32, "gpu-small": 16, "gpu-large": 8, "remote": 16},
        max_resident_large=4, vram_reserve_mb=2048, vector_index="service",
        local_llm="served",
    ),
}


def select_profile(report: HostReport) -> tuple[Profile, list[str]]:
    """Pick a profile from the probe. Returns (profile, reasons) so the choice
    is explainable rather than magic."""
    if not report.gpus:
        return PROFILES["cpu-only"], ["no accelerator detected"]
    biggest = max(g.vram_mb for g in report.gpus)
    n = len(report.gpus)
    if n >= 2 and biggest >= 40_000:
        return PROFILES["server"], [f"{n} GPUs, largest {biggest} MB"]
    if biggest >= 20_000:
        return PROFILES["workstation"], [f"one large GPU ({biggest} MB)"]
    reasons = [f"GPU with {biggest} MB VRAM"]
    if report.ram_mb and report.ram_mb < 24_000:
        reasons.append(f"only {report.ram_mb} MB system RAM: below the 32 GB reference")
    return PROFILES["small-gpu"], reasons
