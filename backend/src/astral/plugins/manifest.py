"""The `astral.plugin/v1` manifest (04 §2). One plugin type; stage, direction
and footprint are three orthogonal declared axes."""

from __future__ import annotations

from typing import Any, Literal

import jsonschema
from pydantic import BaseModel, Field, field_validator, model_validator

API_VERSION = "astral.plugin/v1"
SUPPORTED_API_VERSIONS = {API_VERSION}      # current major and the one before it


class Consumes(BaseModel):
    type: str
    required: list[str] = Field(default_factory=list)
    scheme: str | None = None


class RateLimit(BaseModel):
    requests: int = Field(gt=0)
    per: str = Field(pattern=r"^\d+[smh]$")
    scope: Literal["plugin", "credential", "host"] = "plugin"

    def window_seconds(self) -> float:
        n, unit = int(self.per[:-1]), self.per[-1]
        return n * {"s": 1, "m": 60, "h": 3600}[unit]


class Resources(BaseModel):
    accelerator: Literal["required", "preferred", "cpu_only", "remote"] = "cpu_only"
    vram_mb: int = 0
    ram_mb: int = 256


class Health(BaseModel):
    check: str | None = None                  # "file.py:selfcheck" for in-process
    interval: str = "24h"


class LicenceInfo(BaseModel):
    """Invariant 8: licence metadata for the plugin and for any dataset it
    reaches, so preflight can warn a commercial deployment."""

    code: str | None = None                   # SPDX id of the plugin code
    data: str | None = None                   # licence of the data/service it reads
    commercial_use: bool | None = None        # None = unknown (never assume True)
    attribution: str | None = None


class ConfigInvalid(ValueError):
    """The configuration given for a plugin does not satisfy its schema (e.g. a required value is missing)."""


class PluginManifest(BaseModel):
    api_version: str
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$")
    title: str

    stage: Literal["ingest", "extract", "enrich", "resolve", "analyze", "render", "export"]
    direction: Literal["pull", "push", "internal"]
    footprint: Literal["none", "third_party", "dns", "target_contact"]

    consumes: list[Consumes] = Field(default_factory=list)
    produces: list[str] = Field(default_factory=list)

    config: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    secrets: list[str] = Field(default_factory=list)
    rate_limit: RateLimit | None = None
    resources: Resources = Field(default_factory=Resources)
    health: Health = Field(default_factory=Health)

    runtimes: list[Literal["in_process", "container"]] = Field(default=["in_process"])
    entrypoint: str | None = None             # "file.py:function" relative to the plugin dir
    # Core capabilities this plugin calls (asr.transcribe, face.embed, ...).
    # A plugin never loads a model itself (invariant 5).
    capabilities: list[str] = Field(default_factory=list)
    licence: LicenceInfo = Field(default_factory=LicenceInfo)
    # Upstream origin of the data. Sources sharing an origin count ONCE when
    # corroboration is tallied (03 "Confidence propagation", rule 3). A plugin
    # that re-serves another origin's data (an aggregator) should name the
    # origin it truly comes from, or none if it mixes several.
    origin: str | None = None

    @field_validator("api_version")
    @classmethod
    def _api(cls, v: str) -> str:
        if v not in SUPPORTED_API_VERSIONS:
            raise ValueError(f"unsupported api_version {v!r}; supported: {sorted(SUPPORTED_API_VERSIONS)}")
        return v

    @field_validator("config")
    @classmethod
    def _config_is_schema(cls, v: dict[str, Any]) -> dict[str, Any]:
        try:
            jsonschema.Draft202012Validator.check_schema(v)
        except jsonschema.SchemaError as e:
            raise ValueError(f"config is not a valid JSON Schema: {e.message}") from e
        return v

    @model_validator(mode="after")
    def _entrypoint_for_in_process(self) -> "PluginManifest":
        if "in_process" in self.runtimes and not self.entrypoint:
            raise ValueError("in_process runtime requires an entrypoint")
        if self.resources.accelerator == "remote" and self.footprint == "none":
            raise ValueError("a remote plugin cannot declare footprint 'none'")
        return self

    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Apply defaults from the config schema and validate."""
        merged = dict(config)
        for name, spec in (self.config.get("properties") or {}).items():
            if name not in merged and isinstance(spec, dict) and "default" in spec:
                merged[name] = spec["default"]
        try:
            jsonschema.validate(merged, self.config)
        except jsonschema.ValidationError as e:
            where = ".".join(str(p) for p in e.absolute_path)
            raise ConfigInvalid(f"{self.id} is not configured correctly" + (f" ({where})" if where else "") + f": {e.message}") from e
        return merged
