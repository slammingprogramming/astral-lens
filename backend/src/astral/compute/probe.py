"""Host probing for the preflight report (05 §6). Deliberately free of model
libraries: it shells out to vendor tools and inspects module availability
without importing it."""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass, field


@dataclass
class Gpu:
    index: int
    name: str
    vram_mb: int
    free_vram_mb: int
    driver: str


@dataclass
class HostReport:
    os: str
    cpu_name: str
    cpu_threads: int
    ram_mb: int
    disk_free_mb: int
    gpus: list[Gpu] = field(default_factory=list)
    container_runtime: bool = False
    python_modules: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _nvidia_smi() -> list[Gpu]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run(
            [exe, "--query-gpu=index,name,memory.total,memory.free,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 5:
            gpus.append(Gpu(int(parts[0]), parts[1], int(parts[2]), int(parts[3]), parts[4]))
    return gpus


def _ram_mb() -> int:
    try:
        import psutil  # optional

        return int(psutil.virtual_memory().total / 2**20)
    except ImportError:
        pass
    if hasattr(os, "sysconf") and "SC_PHYS_PAGES" in os.sysconf_names:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**20)
    return 0


def _cpu_name() -> str:
    return platform.processor() or platform.machine() or "unknown"


def free_vram_mb(index: int = 0) -> int | None:
    g = [g for g in _nvidia_smi() if g.index == index]
    return g[0].free_vram_mb if g else None


def probe(model_modules: tuple[str, ...] = ("onnxruntime", "llama_cpp", "faster_whisper")) -> HostReport:
    gpus = _nvidia_smi()
    notes = []
    if not gpus:
        notes.append("no NVIDIA accelerator detected; GPU stages will use CPU backends")
    disk = shutil.disk_usage(os.getcwd()).free // 2**20
    return HostReport(
        os=f"{platform.system()} {platform.release()}",
        cpu_name=_cpu_name(),
        cpu_threads=os.cpu_count() or 1,
        ram_mb=_ram_mb(),
        disk_free_mb=int(disk),
        gpus=gpus,
        # presence of a binary only: the small tier must not need one (invariant 7)
        container_runtime=bool(shutil.which("docker") or shutil.which("podman")),
        python_modules={m: importlib.util.find_spec(m) is not None for m in model_modules},
        notes=notes,
    )
