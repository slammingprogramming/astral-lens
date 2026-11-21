"""Startup preflight: probe the host, select a profile, report what is
available, degraded and unavailable, and why (05 §6)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from astral.compute.models import LicenceFinding, licence_preflight, load_registry
from astral.compute.probe import HostReport, probe
from astral.compute.profiles import Profile, select_profile


@dataclass
class PreflightReport:
    host: HostReport
    profile: Profile
    profile_reasons: list[str]
    licence_findings: list[LicenceFinding] = field(default_factory=list)
    available: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host.to_dict(),
            "profile": self.profile.name,
            "profile_reasons": self.profile_reasons,
            "queues": self.profile.queues,
            "licence_findings": [f.__dict__ for f in self.licence_findings],
            "available": self.available, "degraded": self.degraded,
            "unavailable": self.unavailable,
        }

    def render(self) -> str:
        h = self.host
        lines = [
            f"Host     {h.os}; {h.cpu_threads} threads; {h.ram_mb} MB RAM; "
            f"{h.disk_free_mb} MB disk free",
            "GPU      " + ("; ".join(f"{g.name} {g.vram_mb} MB (driver {g.driver})" for g in h.gpus)
                           or "none"),
            f"Profile  {self.profile.name}  ({'; '.join(self.profile_reasons)})",
            "Queues   " + ", ".join(f"{q}x{n}" for q, n in self.profile.queues.items()),
            f"Container runtime present: {h.container_runtime} (not required at this tier)",
        ]
        for title, items in (("Available", self.available), ("Degraded", self.degraded),
                             ("Unavailable", self.unavailable)):
            lines.append(f"{title}:")
            lines += [f"  - {i}" for i in items] or ["  (none)"]
        if self.licence_findings:
            lines.append("Licence posture:")
            lines += [f"  - [{f.level}] {f.model_id}: {f.message}" for f in self.licence_findings]
        return "\n".join(lines)


def plugin_licence_findings(plugins: list[dict[str, Any]], *, deployment_commercial: bool) -> list[LicenceFinding]:
    """Invariant 8 for connectors: a plugin that reads data under a non-commercial
    licence blocks a commercial deployment; an unconfirmed one is a warning (None is
    never read as permission). Only enabled plugins that reach third-party data count."""
    if not deployment_commercial:
        return []
    out: list[LicenceFinding] = []
    for p in plugins:
        if p.get("state") != "enabled" or p.get("footprint") == "none":
            continue
        lic = p.get("licence") or {}
        if lic.get("commercial_use") is False:
            out.append(LicenceFinding(p["id"], "block-commercial",
                                      f"data licence: {lic.get('data') or 'non-commercial'}"))
        elif lic.get("commercial_use") is None:
            out.append(LicenceFinding(p["id"], "warn", "commercial use of the data it reads is unconfirmed; "
                                                       f"check: {lic.get('data') or 'no licence recorded'}"))
    return out


def run_preflight(
    *, deployment_commercial: bool = False, host: HostReport | None = None,
    models_in_use: list[str] | None = None, profile: Profile | None = None,
    plugins: list[dict[str, Any]] | None = None,
) -> PreflightReport:
    host = host or probe()
    chosen, reasons = (profile, ["operator override"]) if profile else select_profile(host)
    reg = load_registry()
    use = models_in_use if models_in_use is not None else [
        m.id for m in reg.values() if m.default
    ]
    report = PreflightReport(host, chosen, reasons,
                             licence_findings=licence_preflight(
                                 use, deployment_commercial=deployment_commercial, registry=reg)
                             + plugin_licence_findings(plugins or [], deployment_commercial=deployment_commercial))

    report.available += ["document, text, registry and network OSINT (CPU)",
                         "hashing, metadata extraction, projections, full-text search"]
    if chosen.accelerator:
        report.available.append(f"GPU stages via {chosen.name} (1 large model resident at a time)"
                                if chosen.max_resident_large == 1 else f"GPU stages via {chosen.name}")
    else:
        report.degraded += ["PREFERRED-accelerator stages run on CPU backends and are annotated "
                            "in derived_by",
                            "audio transcription is slow but workable overnight",
                            "face search across a large corpus is a batch job"]
        report.unavailable += ["REQUIRED-accelerator stages (rejected with an explanation)",
                               "video analysis beyond keyframe sampling"]
    if host.ram_mb and host.ram_mb < 24_000:
        report.degraded.append(f"{host.ram_mb} MB RAM is below the 32 GB reference target")
    for mod, present in host.python_modules.items():
        (report.available if present else report.unavailable).append(
            f"backend runtime {mod}: {'installed' if present else 'not installed'}")
    return report
