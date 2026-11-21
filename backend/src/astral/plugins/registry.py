"""Plugins are registry rows, not filesystem scans (04 §6). Installing validates
the manifest and lints the source; the plugin stays disabled until enabled."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
import yaml

from astral import audit
from astral.db import Actor
from astral.plugins.lint import lint_plugin_dir
from astral.plugins import trust as trustmod
from astral.plugins.manifest import PluginManifest


class PluginError(Exception):
    pass


def load_manifest(plugin_dir: Path) -> PluginManifest:
    path = plugin_dir / "plugin.yaml"
    if not path.is_file():
        raise PluginError(f"{plugin_dir} has no plugin.yaml")
    return PluginManifest.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def install_from_dir(
    conn: psycopg.Connection, actor: Actor, plugin_dir: Path, *,
    trust: trustmod.TrustStore | None = None, require_trusted: bool = False,
) -> PluginManifest:
    """Validate, lint, check the signature and register a plugin. Leaves it disabled.

    A signature that does not verify (or files changed since signing) is always refused.
    Unsigned and signed-by-an-untrusted-key plugins install, flagged as such, unless
    `require_trusted` (ASTRAL_PLUGIN_TRUST=require)."""
    plugin_dir = plugin_dir.resolve()
    manifest = load_manifest(plugin_dir)
    problems = lint_plugin_dir(plugin_dir)
    if problems:
        raise PluginError("plugin rejected in review:\n  " + "\n  ".join(problems))
    verdict = trustmod.verify_package(plugin_dir, manifest.id, manifest.version, trust)
    if verdict.status == "invalid":
        raise PluginError(f"plugin signature rejected: {verdict.detail}")
    if require_trusted and verdict.status != "trusted":
        raise PluginError(f"this install requires plugins signed by a trusted key; this one is {verdict.status} "
                          f"({verdict.detail})")
    signature = json.dumps(verdict.as_record())
    stored: dict[str, Any] = manifest.model_dump(mode="json")
    stored["_install"] = {"dir": str(plugin_dir)}
    conn.execute(
        "INSERT INTO plugin (id, version, api_version, manifest, runtime, signature)"
        " VALUES (%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (id, version) DO UPDATE SET manifest = EXCLUDED.manifest,"
        "   api_version = EXCLUDED.api_version, runtime = EXCLUDED.runtime,"
        "   signature = EXCLUDED.signature",
        (manifest.id, manifest.version, manifest.api_version, json.dumps(stored),
         manifest.runtimes[0], signature),
    )
    audit.record(conn, actor, "plugin.install", target_kind="plugin",
                 target_id=f"{manifest.id}@{manifest.version}",
                 detail={"trust": verdict.status, "key": verdict.fingerprint, "footprint": manifest.footprint})
    return manifest


def trust_record(row: dict[str, Any] | None) -> dict[str, Any]:
    """What install recorded about a plugin's signature (empty for rows from before signing)."""
    try:
        return json.loads((row or {}).get("signature") or "{}")
    except ValueError:
        return {}


def set_state(
    conn: psycopg.Connection, actor: Actor, plugin_id: str, version: str, state: str,
    *, reason: str | None = None,
) -> None:
    if state not in ("disabled", "enabled", "quarantined"):
        raise PluginError(f"bad state {state!r}")
    cur = conn.execute(
        "UPDATE plugin SET state = %s, quarantine_reason = %s WHERE id = %s AND version = %s",
        (state, reason if state == "quarantined" else None, plugin_id, version),
    )
    if cur.rowcount == 0:
        raise PluginError(f"unknown plugin {plugin_id}@{version}")
    audit.record(conn, actor, f"plugin.{state}", target_kind="plugin",
                 target_id=f"{plugin_id}@{version}", detail={"reason": reason})


def get(conn: psycopg.Connection, plugin_id: str, version: str | None = None) -> dict[str, Any]:
    """The pinned version, or the newest installed one."""
    rows = conn.execute(
        "SELECT * FROM plugin WHERE id = %s AND (%s::text IS NULL OR version = %s)",
        (plugin_id, version, version),
    ).fetchall()
    if not rows:
        raise PluginError(f"plugin {plugin_id} is not installed")
    rows.sort(key=lambda r: tuple(int(p) for p in r["version"].split("-")[0].split("+")[0].split(".")))
    return rows[-1]


def record_health(
    conn: psycopg.Connection, plugin_id: str, version: str, *, ok: bool,
    error_class: str | None = None, latency_ms: int | None = None, detail: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO plugin_health (plugin_id, plugin_version, checked_at, ok, error_class,"
        " latency_ms, detail) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (plugin_id, version, datetime.now(timezone.utc), ok, error_class, latency_ms, detail),
    )
