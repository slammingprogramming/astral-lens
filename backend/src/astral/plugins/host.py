"""The plugin host: policy, secrets, rate limits, invocation, assertion writing,
audit. Plugins only ever see serialized messages (ADR-0005)."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import psycopg
from pydantic import ValidationError

from astral import audit, cases, egress
from astral.assertions import AssertionDraft, AssertionRejected, AssertionWriter, DerivedBy
from astral.backends import BackendRegistry
from astral.capabilities import CallContext, CapabilityError, Capabilities, default_capabilities
from astral.db import Actor
from astral.plugins import registry
from astral.plugins.manifest import PluginManifest
from astral.plugins.runtime import runtime_for
from astral.policy import Policy, SoloPolicy, require_footprint_approval
from astral.secrets import Redactor


class RateLimitExceeded(Exception):
    pass


class SecretProvider(Protocol):
    def get(self, name: str) -> str | None: ...


class EnvSecrets:
    """Development secret source. Real deployments use an encrypted store keyed
    by passphrase/OS keystore (10 §1); this satisfies the same interface."""

    def get(self, name: str) -> str | None:
        return os.environ.get(name)


@dataclass
class InvocationResult:
    # 'unknown' is a first-class outcome: a quarantined or failed source yields
    # unknown, never "not found" (invariant 3).
    status: Literal["ok", "partial", "unknown", "error"]
    assertion_ids: list[int] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    runtime: str | None = None
    # One entry per source consulted: {"source", "outcome", "note"}. Outcome is
    # "hits", "no_hits" or "unknown". "no_hits" is NOT evidence of absence and
    # is never turned into an assertion (invariant 3).
    coverage: list[dict[str, Any]] = field(default_factory=list)


class _Bucket:
    def __init__(self, capacity: int, window: float) -> None:
        self.capacity, self.window = capacity, window
        self.stamps: list[float] = []

    def take(self) -> bool:
        now = time.monotonic()
        self.stamps = [t for t in self.stamps if now - t < self.window]
        if len(self.stamps) >= self.capacity:
            return False
        self.stamps.append(now)
        return True


_NS = uuid.UUID("0b0f2d1e-77a1-4c3a-9d2e-5e1f6a7b8c90")


def entity_namespace(case_id: uuid.UUID) -> uuid.UUID:
    """Case-scoped namespace for deterministic entity ids. Scoping to the case
    means two cases never share an entity id, so they can never be linked."""
    return uuid.uuid5(_NS, str(case_id))


class PluginHost:
    def __init__(
        self,
        writer: AssertionWriter | None = None,
        policy: Policy | None = None,
        secrets: SecretProvider | None = None,
        default_runtime: str = "in_process",
        quarantine_after: int = 2,
        fetcher: egress.Fetcher | None = None,
        capabilities: Capabilities | None = None,
        backends: BackendRegistry | None = None,
    ) -> None:
        self.policy = policy or SoloPolicy()
        self.writer = writer or AssertionWriter(self.policy)
        self.secrets = secrets or EnvSecrets()
        self.default_runtime = default_runtime
        self.quarantine_after = quarantine_after
        self.fetcher = fetcher or egress.Fetcher()
        self.capabilities = capabilities or default_capabilities()
        if backends is None:
            from astral.defaults import default_backends

            backends = default_backends()
        self.backends = backends
        self._buckets: dict[tuple[str, str], _Bucket] = {}

    # ----------------------------------------------------------------- invoke
    def invoke(
        self,
        conn: psycopg.Connection,
        actor: Actor,
        case_id: uuid.UUID,
        plugin_id: str,
        input: dict[str, Any],
        *,
        config: dict[str, Any] | None = None,
        version: str | None = None,
        approved: bool = False,
        runtime: str | None = None,
        budget: dict[str, Any] | None = None,
    ) -> InvocationResult:
        self.policy.authorize(conn, actor, "plugin.invoke", case_id)
        row = registry.get(conn, plugin_id, version)
        manifest = PluginManifest.model_validate(
            {k: v for k, v in row["manifest"].items() if not k.startswith("_")}
        )
        plugin_dir = Path(row["manifest"]["_install"]["dir"])
        case = conn.execute("SELECT collection_identity FROM case_ WHERE id = %s",
                            (case_id,)).fetchone()

        if row["state"] == "quarantined":
            audit.record(conn, actor, "plugin.invoke.skipped", case_id=case_id,
                         target_kind="plugin", target_id=f"{plugin_id}@{row['version']}",
                         detail={"reason": "quarantined", "why": row["quarantine_reason"]})
            return InvocationResult(
                status="unknown",
                gaps=[f"{plugin_id} is quarantined ({row['quarantine_reason']}); "
                      "its result is UNKNOWN, not negative"],
            )
        if row["state"] != "enabled":
            raise registry.PluginError(f"plugin {plugin_id}@{row['version']} is not enabled")

        rec = registry.trust_record(row)
        if rec.get("status") in ("trusted", "signed-untrusted"):       # a signed install must still be the signed files
            from astral.plugins import trust as trustmod

            ok, why = trustmod.still_matches(plugin_dir, plugin_id, row["version"], rec)
            if not ok:
                registry.set_state(conn, actor, plugin_id, row["version"], "quarantined",
                                   reason=f"package changed after signing: {why}")
                return InvocationResult(
                    status="unknown",
                    gaps=[f"{plugin_id} was quarantined: its files no longer match what was signed ({why}); "
                          "its result is UNKNOWN, not negative"])

        require_footprint_approval(manifest.footprint, approved)
        merged_config = manifest.validate_config(config or {})
        unknown = set(manifest.capabilities) - self.capabilities.known()
        if unknown:
            raise registry.PluginError(
                f"{plugin_id} needs capabilities this install lacks: {sorted(unknown)}")
        try:
            policy = egress.EgressPolicy.from_identity(case["collection_identity"] if case else {})
        except egress.EgressDenied as e:
            raise registry.PluginError(str(e)) from e
        if policy.mode == "local-only" and manifest.footprint in ("dns", "target_contact"):
            # These reach the network by means core cannot mediate (own DNS
            # lookups, direct contact), so local-only cannot be honoured.
            raise registry.PluginError(
                f"case egress is local-only; {plugin_id} declares footprint "
                f"{manifest.footprint!r} and is refused")
        self._rate_limit(manifest, actor)

        secrets = {}
        for name in manifest.secrets:               # only what the manifest declares
            val = self.secrets.get(name)
            if val is not None:
                secrets[name] = val

        source_id = cases.get_or_create_source(
            conn, actor, kind="plugin", name=manifest.id, locator=f"plugin:{manifest.id}",
            origin_key=manifest.origin)
        src = conn.execute("SELECT default_source_grade FROM source WHERE id = %s",
                           (source_id,)).fetchone()
        call_id = str(uuid.uuid4())
        request = {
            "op": "invoke", "call_id": call_id, "input": input, "config": merged_config,
            "secrets": secrets, "budget": budget or {},
            "context": {"entity_namespace": str(entity_namespace(case_id)),
                        "plugin": manifest.id, "version": manifest.version},
        }

        result = InvocationResult(status="ok", runtime=runtime or self.default_runtime)
        red = Redactor(secrets.values())
        ctx = CallContext(conn, actor, case_id, manifest, self, policy, redactor=red)
        started = time.monotonic()
        try:
            rt = runtime_for(runtime or self.default_runtime)
            it = rt.run(plugin_dir, manifest.entrypoint or "", request)
            try:
                msg = next(it)
                while True:
                    reply = self._handle(ctx, source_id, src["default_source_grade"], msg, result)
                    msg = it.send(reply)             # None for everything but a "call"
            except StopIteration:
                pass
        except Exception as e:                      # noqa: BLE001 - a plugin failure is a gap
            result.status = "partial" if result.assertion_ids else "error"
            result.gaps.append(f"plugin failed: {type(e).__name__}: {e}")
        if result.gaps and result.status == "ok":
            result.status = "partial"
        self._redact(result, red)

        audit.record(
            conn, actor, "plugin.invoke", case_id=case_id, target_kind="plugin",
            target_id=f"{manifest.id}@{manifest.version}",
            detail={
                "footprint": manifest.footprint, "runtime": result.runtime,
                "egress_identity": (case["collection_identity"] if case else {}),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "outcome": result.status, "assertions": len(result.assertion_ids),
                "gaps": len(result.gaps), "approved": approved,
                "egress_mode": policy.mode,
                # hosts and counts only, never URL paths (the log is immutable)
                "hosts_contacted": dict(ctx.hosts_contacted),
                "capability_calls": dict(ctx.calls),
            },
        )
        return result

    @staticmethod
    def _redact(result: InvocationResult, red: Redactor) -> None:
        """A secret must not survive in a log line, error message or coverage note."""
        result.logs = [red.text(x) for x in result.logs]
        result.gaps = [red.text(x) for x in result.gaps]
        for c in result.coverage:
            if c.get("note"):
                c["note"] = red.text(c["note"])
        result.stats = json.loads(red.text(json.dumps(result.stats, default=str)))

    # ---------------------------------------------------------------- message
    def _handle(
        self, ctx: CallContext, source_id: uuid.UUID, source_default: str,
        msg: dict[str, Any], result: InvocationResult,
    ) -> dict[str, Any] | None:
        conn, actor, case_id, manifest = ctx.conn, ctx.actor, ctx.case_id, ctx.manifest
        op = msg.get("op")
        if op == "emit":
            for raw in msg.get("assertions", []):
                self._write(conn, actor, case_id, manifest, source_id, source_default, raw, result)
        elif op == "log":
            result.logs.append(f"[{msg.get('level', 'info')}] {msg.get('msg', '')}")
        elif op == "progress":
            result.stats["progress"] = {"done": msg.get("done"), "total": msg.get("total")}
        elif op == "call":
            return self._call(ctx, msg)
        elif op == "result":
            status = msg.get("status", "ok")
            if status == "error":
                result.gaps.append(f"plugin reported error: {msg.get('error', 'unspecified')}")
                result.status = "partial" if result.assertion_ids else "error"
            result.stats.update(msg.get("stats") or {})
            for c in msg.get("coverage") or []:
                outcome = c.get("outcome") if c.get("outcome") in ("hits", "no_hits") else "unknown"
                result.coverage.append({"source": str(c.get("source", manifest.id)),
                                        "outcome": outcome, "note": c.get("note")})
                if outcome == "unknown":
                    result.gaps.append(
                        f"{c.get('source', manifest.id)}: result UNKNOWN, not negative"
                        + (f" ({c['note']})" if c.get("note") else ""))
        else:
            result.gaps.append(f"ignored unknown message op {op!r}")
        return None

    def _call(self, ctx: CallContext, msg: dict[str, Any]) -> dict[str, Any]:
        cid = msg.get("id")
        try:
            with ctx.conn.transaction():             # savepoint: a failed capability is a reply
                out = self.capabilities.call(ctx, str(msg.get("capability")), msg.get("args") or {})
            return {"op": "reply", "id": cid, "ok": True, "result": out}
        except CapabilityError as e:
            return {"op": "reply", "id": cid, "ok": False, "error": str(e)}
        except Exception as e:                      # noqa: BLE001 - a bug here must not kill the run
            return {"op": "reply", "id": cid, "ok": False,
                    "error": f"capability failed: {type(e).__name__}: {e}"}

    def _write(
        self, conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID,
        manifest: PluginManifest, source_id: uuid.UUID, source_default: str,
        raw: dict[str, Any], result: InvocationResult,
    ) -> None:
        # A plugin cannot forge who it is: plugin/version come from the registry.
        by = dict(raw.get("derived_by") or {})
        by.update(plugin=manifest.id, version=manifest.version)
        by.setdefault("backend", "native")
        claimed = raw.get("source_grade") or source_default
        # The operator, not the plugin, decides how reliable a source is: a
        # plugin may claim worse than its source grade, never better.
        grade = max(claimed, source_default)
        try:
            draft = AssertionDraft.model_validate({
                **{k: v for k, v in raw.items() if k not in ("derived_by", "source_grade")},
                "derived_by": DerivedBy.model_validate(by),
                "source_id": source_id,
                "source_grade": grade,
            })
        except ValidationError as e:
            result.gaps.append(f"rejected malformed assertion: {e.errors()[0]['msg']}")
            return
        try:
            with conn.transaction():                # savepoint: one bad row, not the batch
                result.assertion_ids.append(self.writer.write(conn, actor, case_id, draft))
        except AssertionRejected as e:
            result.gaps.append(f"writer rejected assertion: {e}")

    # ------------------------------------------------------------- rate limit
    def _rate_limit(self, manifest: PluginManifest, actor: Actor) -> None:
        rl = manifest.rate_limit
        if rl is None:
            return
        key = (manifest.id, str(actor.workspace_id) if rl.scope == "plugin" else rl.scope)
        bucket = self._buckets.setdefault(key, _Bucket(rl.requests, rl.window_seconds()))
        if not bucket.take():
            raise RateLimitExceeded(f"{manifest.id} exceeded {rl.requests}/{rl.per}")

    # ----------------------------------------------------------------- health
    def health_sweep(self, conn: psycopg.Connection, actor: Actor, *, force: bool = False) -> list[dict[str, Any]]:
        """Run the self-check of every enabled plugin whose interval has elapsed
        (04 §5). A plugin failing repeatedly is quarantined by run_health_check;
        its results then read UNKNOWN, never negative."""
        from datetime import datetime, timedelta, timezone

        out = []
        now = datetime.now(timezone.utc)
        for row in conn.execute("SELECT id, version, manifest FROM plugin WHERE state = 'enabled'"
                                " ORDER BY id, version").fetchall():
            m = PluginManifest.model_validate({k: v for k, v in row["manifest"].items() if not k.startswith("_")})
            if not m.health.check:
                continue
            n, unit = int(m.health.interval[:-1]), m.health.interval[-1]
            every = timedelta(seconds=n * {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit, 3600))
            last = conn.execute("SELECT max(checked_at) AS t FROM plugin_health WHERE plugin_id=%s"
                                " AND plugin_version=%s", (m.id, row["version"])).fetchone()["t"]
            if not force and last is not None and now - last < every:
                continue
            res = self.run_health_check(conn, actor, m.id, row["version"])
            state = registry.get(conn, m.id, row["version"])["state"]
            out.append({"plugin": m.id, "version": row["version"], "ok": bool(res.get("ok")),
                        "error_class": res.get("error_class"), "state": state})
        return out

    def run_health_check(
        self, conn: psycopg.Connection, actor: Actor, plugin_id: str, version: str | None = None
    ) -> dict[str, Any]:
        """Run the plugin's self-check, record history, auto-quarantine after
        repeated failure (04 §5). Quarantine means UNKNOWN, never negative."""
        row = registry.get(conn, plugin_id, version)
        manifest = PluginManifest.model_validate(
            {k: v for k, v in row["manifest"].items() if not k.startswith("_")}
        )
        if not manifest.health.check:
            return {"ok": True, "skipped": "no self-check declared"}
        from astral.plugins.runtime import load_callable

        started = time.monotonic()
        try:
            outcome = load_callable(Path(row["manifest"]["_install"]["dir"]),
                                    manifest.health.check)()
        except Exception as e:  # noqa: BLE001
            outcome = {"ok": False, "error_class": "other", "detail": f"{type(e).__name__}: {e}"}
        latency = int((time.monotonic() - started) * 1000)
        registry.record_health(conn, manifest.id, row["version"], ok=bool(outcome.get("ok")),
                               error_class=outcome.get("error_class"), latency_ms=latency,
                               detail=outcome.get("detail"))
        if not outcome.get("ok"):
            recent = conn.execute(
                "SELECT ok FROM plugin_health WHERE plugin_id=%s AND plugin_version=%s"
                " ORDER BY id DESC LIMIT %s", (manifest.id, row["version"], self.quarantine_after),
            ).fetchall()
            if len(recent) >= self.quarantine_after and not any(r["ok"] for r in recent):
                registry.set_state(conn, actor, manifest.id, row["version"], "quarantined",
                                   reason=outcome.get("error_class") or "self-check failing")
        return outcome
