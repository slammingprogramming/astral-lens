"""FastAPI skeleton: REST over the policy layer (02 §1).

Thin on purpose. Every route authorizes through the policy layer and writes
through the assertion writer; nothing here talks to layer 1 directly.
"""

from __future__ import annotations

import contextvars
import os
import uuid
import psycopg.types.json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

import asyncio

from fastapi.responses import Response

from astral import ai, analysis, audit, auth, cases, egress, hub, services as services_mod, worm, investigate, lens, machines, workbench, monitoring, projector, resolution, resolver, views
from astral.assertions import AssertionRejected, AssertionWriter
from astral.compute.preflight import run_preflight
from astral.config import Settings
from astral.db import SOLO_ACTOR, Actor, Database
from astral.erasure import erase_case
from astral.evidence import EvidenceStore
from astral.explain import explain_assertion, originating_artifacts
from astral.plugins import registry
from astral.plugins.host import PluginHost, RateLimitExceeded
from astral.oidc import OidcError
from astral.policy import ApprovalRequired, PermissionDenied, SoloPolicy, TeamPolicy
from astral.plugins.host import EnvSecrets
from astral.secrets import ChainSecrets, SecretStore, SecretsError, passphrase_from_environment

# Required on every state-changing request. A browser cannot add a custom
# header cross-origin without a CORS preflight (which we never grant), so a web
# page the operator happens to visit cannot drive the no-login solo API.
CLIENT_HEADER = "x-astral-client"


class NewCase(BaseModel):
    name: str
    retention_policy: dict[str, Any] = {}
    collection_identity: dict[str, Any] = {}


class InvokeBody(BaseModel):
    input: dict[str, Any] = {}
    config: dict[str, Any] = {}
    approved: bool = False
    runtime: str | None = None


class JudgmentBody(BaseModel):
    entity_a: uuid.UUID
    entity_b: uuid.UUID
    verdict: str
    comparison: dict[str, Any] = {}
    reasoning: str | None = None


class RetractBody(BaseModel):
    reason: str
    cascade: bool = True


class EraseBody(BaseModel):
    legal_basis: str
    acknowledge_worm: bool = False       # required when the case has write-once copies that erasure cannot remove


class InstallBody(BaseModel):
    path: str


class NameQuery(BaseModel):
    name: str
    country: str | None = None
    schema_name: str | None = Field(None, alias="schema")
    plugin_inputs: dict[str, dict[str, Any]] = {}
    plugin_configs: dict[str, dict[str, Any]] = {}
    only: list[str] | None = None
    propose: bool = True

    model_config = {"populate_by_name": True, "protected_namespaces": ()}


class FaceSearchBody(BaseModel):
    artifact: str | None = None          # a probe image already in the case
    face_id: str | None = None           # or an indexed face
    face_index: int = 0                  # which face of the probe image (0 = highest detector score)
    scope: str = Field("all", pattern="^(all|case|watchlist)$")
    k: int = Field(10, ge=1, le=100)
    min_score: float | None = None
    prefer: str | None = None
    allow_noncommercial: bool = False


TARGET_KINDS = ("case", "entity", "artifact", "assertion", "plan")


class RoleBody(BaseModel):
    role: str


class CommentBody(BaseModel):
    target_kind: str = "case"
    target_id: str | None = Field(None, max_length=200)
    body: str = Field(min_length=1, max_length=10000)


class AssignBody(BaseModel):
    assignee: uuid.UUID
    target_kind: str = "case"
    target_id: str | None = Field(None, max_length=200)
    note: str = Field("", max_length=2000)


class MonitorBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    query: dict[str, Any]
    interval_minutes: int | None = Field(None, ge=5)


class MonitorActive(BaseModel):
    active: bool


class TermBody(BaseModel):
    kind: str
    value: str = Field(min_length=2, max_length=300)
    note: str = Field("", max_length=1000)


class HubInstall(BaseModel):
    repository: str
    kind: str
    id: str
    version: str | None = None


class TransformRun(BaseModel):
    config: dict[str, Any] | None = None
    approved: bool = False
    overrides: dict[str, Any] | None = None


class CypherBody(BaseModel):
    query: str


class ContextBody(BaseModel):
    limit: int = Field(50, ge=1, le=500)


class AnnotationBody(BaseModel):
    kind: str
    body: str
    cluster_id: uuid.UUID | None = None


class ViewBody(BaseModel):
    name: str
    layout: str = "organic"
    state: dict[str, Any] = Field(default_factory=dict)


class TableImport(BaseModel):
    csv_text: str
    mapping: dict[str, Any]
    filename: str = "table.csv"
    source_grade: str = "F"
    info_grade: int = Field(6, ge=1, le=6)


class MachineProposal(BaseModel):
    seeds: list[uuid.UUID]
    version: int | None = None


class MachineApproval(BaseModel):
    allow_target_contact: bool = False


class MachineSchedule(BaseModel):
    machine: str
    seeds: list[uuid.UUID]
    interval_minutes: int = Field(60, ge=5)


class LensRun(BaseModel):
    version: int | None = None
    inputs: dict[str, str]


class LensFromPlan(BaseModel):
    name: str
    version: int = Field(1, ge=1)
    description: str = Field("", max_length=500)
    parameters: dict[str, str]                 # parameter name -> the literal text in this plan to replace


class AskBody(BaseModel):
    question: str = Field(min_length=3, max_length=2000)


class ExhibitBody(BaseModel):
    statement: str = Field(min_length=1, max_length=20000)   # the operator's own account
    plan_ids: list[uuid.UUID] = []
    include_all_artifacts: bool = False
    sign: bool = False
    allow_integrity_failures: bool = False
    worm: bool = False                  # also preserve the package in the write-once bucket


class EnrolBody(BaseModel):
    artifact: str
    label: str = Field(min_length=1, max_length=200)
    basis: str = Field("", max_length=1000)     # recorded, not judged
    watchlist: str = Field("default", max_length=80)


class GradeBody(BaseModel):
    grade: str


class SecretBody(BaseModel):
    value: str


class ProposeBody(BaseModel):
    min_score: float = resolution.MIN_SCORE


def _needed_secrets(app: FastAPI) -> list[dict[str, Any]]:
    """Which secrets installed plugins declare, and whether each is set."""
    out: dict[str, set[str]] = {}
    with app.state.db.connection() as conn:
        for r in conn.execute("SELECT id, manifest FROM plugin"):
            for s in r["manifest"].get("secrets") or []:
                out.setdefault(s, set()).add(r["id"])
    return [{"name": n, "plugins": sorted(p), "set": app.state.host.secrets.get(n) is not None}
            for n, p in sorted(out.items())]


async def _monitor_loop(app: FastAPI, every: int) -> None:
    """Scheduled monitors (opt-in). Each runs as the person who created it, so the policy layer applies."""
    while True:
        await asyncio.sleep(every)
        try:
            with app.state.db.connection() as conn:
                due = monitoring.due(conn)
            with app.state.db.connection() as conn:
                try:
                    machines.run_due(app.state.host, conn)
                except Exception:               # noqa: BLE001 - scheduled machines must not stop monitors
                    pass
            for m in due:
                with app.state.db.connection() as conn:
                    try:
                        monitoring.run(app.state.host, conn, Actor(m["workspace_id"], m["created_by"]), m["case_id"], m["id"],
                                       app.state.store)
                    except Exception:           # noqa: BLE001 - one failing monitor must not stop the others
                        pass
        except Exception:                       # noqa: BLE001
            pass


async def _sweep_loop(app: FastAPI, every: int) -> None:
    """Background plugin self-checks (04 §5). A failure here is logged into the
    plugin_health table by the sweep itself; the loop must never die."""
    while True:
        await asyncio.sleep(every)
        try:
            with app.state.db.connection() as conn:
                app.state.host.health_sweep(conn, app.state.system_actor)
        except Exception:               # noqa: BLE001
            pass


def create_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    team = settings.profile == "team"
    if settings.profile not in ("solo", "team"):
        raise RuntimeError(f"unknown profile {settings.profile!r}")
    validator = None
    if team:
        # No fake authentication: the team profile does not start without a real identity provider.
        if not (settings.oidc_issuer and settings.oidc_audience):
            raise RuntimeError("profile 'team' requires ASTRAL_OIDC_ISSUER and ASTRAL_OIDC_AUDIENCE; there is no "
                               "header-based or anonymous stand-in")
        from astral.oidc import OidcValidator

        validator = OidcValidator(settings.oidc_issuer, settings.oidc_audience)
        if settings.oidc_browser_token not in ("id_token", "access_token"):
            raise RuntimeError("ASTRAL_OIDC_BROWSER_TOKEN must be id_token or access_token")
    policy = TeamPolicy() if team else SoloPolicy()
    current_actor: contextvars.ContextVar = contextvars.ContextVar("astral_actor", default=None)

    class _ActorProxy:
        """In the team profile `actor` is whoever the request authenticated as."""

        def _a(self):
            a = current_actor.get()
            if a is None:
                raise PermissionDenied("not authenticated")
            return a

        workspace_id = property(lambda self: self._a().workspace_id)
        principal_id = property(lambda self: self._a().principal_id)
        kind = property(lambda self: self._a().kind)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = db or Database(settings.dsn).open()
        app.state.owns_db = db is None
        app.state.db.migrate()
        if team:
            with app.state.db.connection() as _c:
                auth.bootstrap_team(_c)
        else:
            app.state.db.bootstrap_solo()
        app.state.system_actor = auth.SYSTEM_ACTOR if team else SOLO_ACTOR
        app.state.store = EvidenceStore(settings.data_dir / "evidence")
        app.state.services = services_mod.Services.from_settings(settings)
        app.state.writer = AssertionWriter(policy, app.state.store)
        from astral.defaults import default_backends

        app.state.secrets_file = settings.data_dir / "secrets.json"
        pw = passphrase_from_environment()
        app.state.secret_store = (SecretStore(app.state.secrets_file, pw)
                                  if pw and (app.state.secrets_file.exists() or settings.secrets_enabled)
                                  else None)
        app.state.host = PluginHost(app.state.writer, policy,
                                    secrets=ChainSecrets(app.state.secret_store, EnvSecrets()),
                                    default_runtime=settings.plugin_runtime,
                                    backends=default_backends(settings.tika_url))
        sweeper = (asyncio.create_task(_sweep_loop(app, settings.health_sweep_seconds))
                   if settings.health_sweep_seconds > 0 else None)
        monitor_task = (asyncio.create_task(_monitor_loop(app, settings.monitor_sweep_seconds))
                        if settings.monitor_sweep_seconds > 0 else None)
        yield
        if sweeper:
            sweeper.cancel()
        if monitor_task:
            monitor_task.cancel()
        if app.state.owns_db:
            app.state.db.close()

    app = FastAPI(title="Astral Lens", version="0.0.1", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware,
                       allowed_hosts=list(settings.allowed_hosts))

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS") and CLIENT_HEADER not in request.headers:
            return JSONResponse({"detail": f"missing {CLIENT_HEADER} header"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(PermissionDenied)
    async def _denied(_: Request, e: PermissionDenied):
        return JSONResponse({"detail": str(e)}, status_code=404)       # do not reveal existence

    @app.exception_handler(AssertionRejected)
    async def _rejected(_: Request, e: AssertionRejected):
        return JSONResponse({"detail": str(e)}, status_code=422)

    @app.exception_handler(ApprovalRequired)
    async def _approval(_: Request, e: ApprovalRequired):
        return JSONResponse({"detail": str(e), "needs_approval": True}, status_code=409)

    @app.exception_handler(RateLimitExceeded)
    async def _rate(_: Request, e: RateLimitExceeded):
        return JSONResponse({"detail": str(e)}, status_code=429)

    @app.exception_handler(workbench.WorkbenchError)
    async def _workbench(_: Request, e: workbench.WorkbenchError):
        return JSONResponse({"detail": str(e)}, status_code=422)

    @app.exception_handler(machines.MachineError)
    async def _machine(_: Request, e: machines.MachineError):
        return JSONResponse({"detail": str(e)}, status_code=422)

    @app.exception_handler(workbench.NotFound)
    async def _not_found(_: Request, e: workbench.NotFound):
        return JSONResponse({"detail": str(e)}, status_code=404)

    @app.exception_handler(worm.WormError)
    async def _worm(_: Request, e: worm.WormError):
        return JSONResponse({"detail": str(e)}, status_code=502)

    @app.exception_handler(hub.HubError)
    async def _hub(_: Request, e: hub.HubError):
        return JSONResponse({"detail": str(e)}, status_code=422)

    @app.exception_handler(registry.PluginError)
    async def _plugin(_: Request, e: registry.PluginError):
        return JSONResponse({"detail": str(e)}, status_code=400)

    actor = _ActorProxy() if team else SOLO_ACTOR

    ADMIN_PREFIXES = ("/plugins", "/secrets", "/sources", "/biometrics")

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        path = request.url.path
        if not team or path in ("/health", "/", "/auth/config") or path.startswith("/ui"):
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return JSONResponse({"detail": "bearer token required"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})

        def resolve():
            claims = validator.validate(header[7:].strip())
            with app.state.db.connection() as conn:
                a = auth.actor_for(conn, validator.issuer, claims, set(settings.team_admins))
                if request.method not in ("GET", "HEAD", "OPTIONS") and path.startswith(ADMIN_PREFIXES):
                    policy.authorize(conn, a, "plugin.manage", None)
            return a

        try:
            a = await run_in_threadpool(resolve)
        except OidcError:
            return JSONResponse({"detail": "invalid token"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
        except PermissionDenied:
            return JSONResponse({"detail": "not permitted"}, status_code=403)
        except Exception:                                          # noqa: BLE001 - provider unreachable etc.
            return JSONResponse({"detail": "identity provider unavailable"}, status_code=503)
        tok = current_actor.set(a)
        try:
            return await call_next(request)
        finally:
            current_actor.reset(tok)

    @app.get("/auth/config")
    def auth_config() -> dict[str, Any]:
        """What the browser needs to sign in. Public by necessity (it is read before there is a token) and holds nothing
        secret: the issuer, a public client id and the provider's own endpoints. The UI does the authorization-code + PKCE
        exchange itself; this API never sees a code, a verifier or a password."""
        if not team:
            return {"enabled": False}
        out: dict[str, Any] = {"enabled": True, "issuer": validator.issuer, "browser_login": bool(settings.oidc_client_id)}
        if not settings.oidc_client_id:
            return out
        try:
            doc = validator.discovery()
        except Exception as e:                                     # noqa: BLE001 - provider unreachable or wrong
            raise HTTPException(503, "identity provider unavailable or its discovery document is not acceptable") from e
        return {**out, "client_id": settings.oidc_client_id, "authorization_endpoint": doc["authorization_endpoint"],
                "token_endpoint": doc["token_endpoint"], "scope": settings.oidc_scope, "token_use": settings.oidc_browser_token}

    # The built UI (frontend/dist), when present. The API works without it.
    ui_dir = Path(os.environ.get("ASTRAL_UI_DIR") or Path(__file__).resolve().parents[3] / "frontend" / "dist")
    if (ui_dir / "index.html").exists():
        from fastapi.responses import RedirectResponse
        from fastapi.staticfiles import StaticFiles

        app.mount("/ui", StaticFiles(directory=ui_dir, html=True), name="ui")

        @app.get("/", include_in_schema=False)
        def root() -> RedirectResponse:
            return RedirectResponse("/ui/")

    # ------------------------------------------------------------------ meta
    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/preflight")
    def preflight() -> dict[str, Any]:
        with app.state.db.connection() as conn:
            plugins = [{"id": r["id"], "state": r["state"], "footprint": r["manifest"]["footprint"],
                        "licence": r["manifest"]["licence"]}
                       for r in conn.execute("SELECT id, state, manifest FROM plugin").fetchall()]
        return run_preflight(deployment_commercial=settings.commercial, plugins=plugins).to_dict()

    @app.get("/audit/verify")
    def audit_verify() -> dict[str, Any]:
        with app.state.db.connection() as conn:
            ok, bad = audit.verify_chain(conn, actor.workspace_id)
        return {"ok": ok, "first_bad_entry": bad}

    # ----------------------------------------------------------------- cases
    @app.post("/cases", status_code=201)
    def new_case(body: NewCase) -> dict[str, str]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.create", None)
            cid = cases.create_case(conn, actor, body.name, retention_policy=body.retention_policy,
                                    collection_identity=body.collection_identity)
            if team:
                auth.grant(conn, actor, cid, actor.principal_id, "owner")
        return {"id": str(cid)}

    @app.get("/cases")
    def list_cases() -> list[dict[str, Any]]:
        with app.state.db.connection() as conn:
            visible = auth.visible_case_ids(conn, actor) if team else None
            rows = conn.execute("SELECT id, name, state, created_at FROM case_ WHERE workspace_id=%s"
                                " AND (%s::uuid[] IS NULL OR id = ANY(%s::uuid[])) ORDER BY created_at DESC",
                                (actor.workspace_id, visible, visible)).fetchall()
        return [{**r, "id": str(r["id"])} for r in rows]

    @app.get("/cases/{case_id}")
    def get_case(case_id: uuid.UUID) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.read", case_id)
            row = conn.execute("SELECT id, name, state, created_at FROM case_ WHERE id=%s",
                               (case_id,)).fetchone()
        return {**row, "id": str(row["id"])}

    # ------------------------------------------------- people, notes and review
    @app.get("/me")
    def me() -> dict[str, Any]:
        with app.state.db.connection() as conn:
            p = conn.execute("SELECT name FROM principal WHERE id=%s", (actor.principal_id,)).fetchone()
            grants = conn.execute("SELECT case_id, role FROM role_grant WHERE principal_id=%s AND workspace_id=%s",
                                  (actor.principal_id, actor.workspace_id)).fetchall()
        return {"principal_id": str(actor.principal_id), "name": p["name"] if p else None, "profile": settings.profile,
                "grants": [{"case_id": str(g["case_id"]) if g["case_id"] else None, "role": g["role"]} for g in grants]}

    @app.get("/principals")
    def principals() -> list[dict[str, Any]]:
        with app.state.db.connection() as conn:
            rows = conn.execute("SELECT id, name, kind FROM principal WHERE workspace_id=%s ORDER BY name",
                                (actor.workspace_id,)).fetchall()
        return [{**r, "id": str(r["id"])} for r in rows]

    @app.get("/cases/{case_id}/members")
    def members(case_id: uuid.UUID) -> list[dict[str, Any]]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.read", case_id)
            rows = conn.execute(
                "SELECT g.principal_id, p.name, g.role, g.case_id FROM role_grant g JOIN principal p ON p.id = g.principal_id"
                " WHERE g.workspace_id=%s AND (g.case_id=%s OR g.case_id IS NULL) ORDER BY p.name, g.role",
                (actor.workspace_id, case_id)).fetchall()
        return [{"principal_id": str(r["principal_id"]), "name": r["name"], "role": r["role"],
                 "scope": "case" if r["case_id"] else "workspace"} for r in rows]

    @app.put("/cases/{case_id}/members/{principal_id}")
    def set_member(case_id: uuid.UUID, principal_id: uuid.UUID, body: RoleBody) -> dict[str, str]:
        if not team:
            raise HTTPException(400, "roles exist only in the team profile")
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.grant", case_id)
            try:
                auth.revoke(conn, actor, case_id, principal_id)              # one case role per person
                auth.grant(conn, actor, case_id, principal_id, body.role)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            except LookupError as e:
                raise HTTPException(404, str(e)) from e
        return {"role": body.role}

    @app.delete("/cases/{case_id}/members/{principal_id}")
    def remove_member(case_id: uuid.UUID, principal_id: uuid.UUID) -> dict[str, int]:
        if not team:
            raise HTTPException(400, "roles exist only in the team profile")
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.grant", case_id)
            return {"removed": auth.revoke(conn, actor, case_id, principal_id)}

    @app.post("/cases/{case_id}/comments", status_code=201)
    def add_comment(case_id: uuid.UUID, body: CommentBody) -> dict[str, int]:
        if body.target_kind not in TARGET_KINDS:
            raise HTTPException(422, f"target_kind must be one of {TARGET_KINDS}")
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "comment.write", case_id)
            cid = conn.execute(
                "INSERT INTO comment (workspace_id, case_id, target_kind, target_id, body, author) VALUES (%s,%s,%s,%s,%s,%s)"
                " RETURNING id", (actor.workspace_id, case_id, body.target_kind, body.target_id, body.body,
                                  actor.principal_id)).fetchone()["id"]
            audit.record(conn, actor, "comment.write", case_id=case_id, target_kind=body.target_kind, target_id=body.target_id,
                         detail={"comment_id": cid, "length": len(body.body)})          # the text is not copied into the audit log
        return {"id": cid}

    @app.get("/cases/{case_id}/comments")
    def comments(case_id: uuid.UUID, target_kind: str | None = None, target_id: str | None = None) -> list[dict[str, Any]]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.read", case_id)
            rows = conn.execute(
                "SELECT c.id, c.target_kind, c.target_id, c.body, c.created_at, c.author, p.name AS author_name FROM comment c"
                " JOIN principal p ON p.id = c.author WHERE c.case_id=%s AND (%s::text IS NULL OR c.target_kind=%s)"
                " AND (%s::text IS NULL OR c.target_id=%s) ORDER BY c.id",
                (case_id, target_kind, target_kind, target_id, target_id)).fetchall()
        return [{**r, "author": str(r["author"])} for r in rows]

    @app.post("/cases/{case_id}/assignments", status_code=201)
    def assign(case_id: uuid.UUID, body: AssignBody) -> dict[str, int]:
        if body.target_kind not in TARGET_KINDS:
            raise HTTPException(422, f"target_kind must be one of {TARGET_KINDS}")
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "assignment.write", case_id)
            if not conn.execute("SELECT 1 FROM principal WHERE id=%s AND workspace_id=%s", (body.assignee, actor.workspace_id)).fetchone():
                raise HTTPException(404, "no such principal")
            try:
                policy.authorize(conn, Actor(actor.workspace_id, body.assignee), "case.read", case_id)
            except PermissionDenied as e:
                raise HTTPException(400, "the assignee has no access to this case; grant a role first") from e
            aid = conn.execute(
                "INSERT INTO assignment (workspace_id, case_id, target_kind, target_id, assignee, assigned_by, note)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (actor.workspace_id, case_id, body.target_kind, body.target_id, body.assignee, actor.principal_id,
                 body.note)).fetchone()["id"]
            audit.record(conn, actor, "assignment.create", case_id=case_id, target_kind=body.target_kind,
                         target_id=body.target_id, detail={"assignment_id": aid, "assignee": str(body.assignee)})
        return {"id": aid}

    @app.get("/cases/{case_id}/assignments")
    def assignments(case_id: uuid.UUID, status: str | None = None) -> list[dict[str, Any]]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.read", case_id)
            rows = conn.execute(
                "SELECT a.id, a.target_kind, a.target_id, a.note, a.status, a.created_at, a.closed_at, a.assignee,"
                " p.name AS assignee_name FROM assignment a JOIN principal p ON p.id = a.assignee"
                " WHERE a.case_id=%s AND (%s::text IS NULL OR a.status=%s) ORDER BY a.id", (case_id, status, status)).fetchall()
        return [{**r, "assignee": str(r["assignee"])} for r in rows]

    @app.post("/cases/{case_id}/assignments/{assignment_id}/done")
    def finish_assignment(case_id: uuid.UUID, assignment_id: int) -> dict[str, str]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "comment.write", case_id)
            row = conn.execute("SELECT assignee, status FROM assignment WHERE id=%s AND case_id=%s", (assignment_id, case_id)).fetchone()
            if not row:
                raise HTTPException(404, "no such assignment")
            if row["assignee"] != actor.principal_id:
                policy.authorize(conn, actor, "assignment.write", case_id)    # others need the right to assign
            conn.execute("UPDATE assignment SET status='done', closed_at=now() WHERE id=%s", (assignment_id,))
            audit.record(conn, actor, "assignment.done", case_id=case_id, target_kind="assignment", target_id=str(assignment_id))
        return {"status": "done"}

    @app.get("/cases/{case_id}/audit")
    def case_audit(case_id: uuid.UUID, after: int = 0, limit: int = 500) -> dict[str, Any]:
        """Per-case audit review: who did what in this case, with the chain check for the workspace."""
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.audit", case_id)
            rows = conn.execute(
                "SELECT l.id, l.at, l.action, l.target_kind, l.target_id, l.detail, l.principal_id, p.name AS principal"
                " FROM audit_log l LEFT JOIN principal p ON p.id = l.principal_id"
                " WHERE l.workspace_id=%s AND l.case_id=%s AND l.id > %s ORDER BY l.id LIMIT %s",
                (actor.workspace_id, case_id, after, max(1, min(limit, 2000)))).fetchall()
            ok, bad = audit.verify_chain(conn, actor.workspace_id)
            bad_here = bool(bad) and bool(conn.execute("SELECT 1 FROM audit_log WHERE id=%s AND case_id=%s", (bad, case_id)).fetchone())
        return {"chain_ok": ok, "first_bad_entry_is_in_this_case": bad_here,
                "entries": [{**r, "principal_id": str(r["principal_id"])} for r in rows]}

    # ------------------------------------------------------ monitoring and watch terms
    @app.post("/cases/{case_id}/monitors", status_code=201)
    def new_monitor(case_id: uuid.UUID, body: MonitorBody) -> dict[str, str]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "plugin.invoke", case_id)
            try:
                mid = monitoring.create(conn, actor, case_id, body.name, body.query, body.interval_minutes)
            except monitoring.MonitorError as e:
                raise HTTPException(422, str(e)) from e
        return {"id": str(mid)}

    @app.get("/cases/{case_id}/monitors")
    def list_monitors(case_id: uuid.UUID) -> list[dict[str, Any]]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.read", case_id)
            rows = conn.execute("SELECT id, name, query, interval_minutes, active, created_at FROM monitor WHERE case_id=%s"
                                " ORDER BY created_at", (case_id,)).fetchall()
            out = []
            for r in rows:
                last = conn.execute("SELECT ran_at, diff, coverage FROM monitor_run WHERE monitor_id=%s ORDER BY id DESC LIMIT 1",
                                    (r["id"],)).fetchone()
                out.append({**r, "id": str(r["id"]), "last_run": last})
        return out

    @app.put("/cases/{case_id}/monitors/{monitor_id}")
    def monitor_active(case_id: uuid.UUID, monitor_id: uuid.UUID, body: MonitorActive) -> dict[str, bool]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "plugin.invoke", case_id)
            n = conn.execute("UPDATE monitor SET active=%s WHERE id=%s AND case_id=%s", (body.active, monitor_id, case_id)).rowcount
            if not n:
                raise HTTPException(404, "no such monitor")
            audit.record(conn, actor, "monitor.active", case_id=case_id, target_kind="monitor", target_id=str(monitor_id),
                         detail={"active": body.active})
        return {"active": body.active}

    @app.post("/cases/{case_id}/monitors/{monitor_id}/run")
    def run_monitor(case_id: uuid.UUID, monitor_id: uuid.UUID) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "plugin.invoke", case_id)
            try:
                return monitoring.run(app.state.host, conn, actor, case_id, monitor_id, app.state.store)
            except monitoring.MonitorError as e:
                raise HTTPException(404, str(e)) from e

    @app.get("/cases/{case_id}/monitors/{monitor_id}/runs")
    def monitor_runs(case_id: uuid.UUID, monitor_id: uuid.UUID) -> list[dict[str, Any]]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.read", case_id)
            rows = conn.execute("SELECT id, run_id, ran_at, coverage, diff FROM monitor_run WHERE monitor_id=%s AND case_id=%s"
                                " ORDER BY id DESC LIMIT 50", (monitor_id, case_id)).fetchall()
        return [{**r, "run_id": str(r["run_id"])} for r in rows]

    @app.post("/cases/{case_id}/watch-terms", status_code=201)
    def add_watch_term(case_id: uuid.UUID, body: TermBody) -> dict[str, int]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "plugin.invoke", case_id)
            try:
                return {"id": monitoring.add_term(conn, actor, case_id, body.kind, body.value, body.note)}
            except monitoring.MonitorError as e:
                raise HTTPException(422, str(e)) from e

    @app.get("/cases/{case_id}/watch-terms")
    def watch_terms(case_id: uuid.UUID) -> list[dict[str, Any]]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.read", case_id)
            return conn.execute("SELECT id, kind, value, note, created_at FROM watch_term WHERE case_id=%s ORDER BY id",
                                (case_id,)).fetchall()

    @app.delete("/cases/{case_id}/watch-terms/{term_id}")
    def delete_watch_term(case_id: uuid.UUID, term_id: int) -> dict[str, int]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "plugin.invoke", case_id)
            n = conn.execute("DELETE FROM watch_term WHERE id=%s AND case_id=%s", (term_id, case_id)).rowcount
            if n:
                audit.record(conn, actor, "watch.remove", case_id=case_id, target_kind="watch_term", target_id=str(term_id))
        return {"removed": n}

    @app.get("/cases/{case_id}/watch/matches")
    def watch_matches(case_id: uuid.UUID) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.read", case_id)
            m = monitoring.cross_reference(conn, actor, case_id, app.state.store)
        return {"matches": m, "note": "name matches are candidates, not identifications"}

    # ------------------------------------------------------------------ workbench
    @app.get("/cases/{case_id}/entities/{cluster_id}/transforms")
    def entity_transforms(case_id: uuid.UUID, cluster_id: uuid.UUID) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            _read(conn, case_id, "entity.read")
            return workbench.transforms_for(conn, case_id, cluster_id)

    @app.post("/cases/{case_id}/entities/{cluster_id}/transforms/{plugin_id}/run")
    def run_entity_transform(case_id: uuid.UUID, cluster_id: uuid.UUID, plugin_id: str, body: TransformRun) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "plugin.invoke", case_id)
            return workbench.run_transform(app.state.host, conn, actor, case_id, cluster_id, plugin_id, config=body.config,
                                           approved=body.approved, overrides=body.overrides)

    def _adj(conn, case_id: uuid.UUID, documents: bool = False):
        g = views.build_graph(conn, case_id, include_documents=documents)
        return g, analysis.adjacency(g)

    @app.get("/cases/{case_id}/analysis")
    def graph_analysis(case_id: uuid.UUID, documents: bool = False) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            _read(conn, case_id, "entity.read")
            return analysis.analyse(views.build_graph(conn, case_id, include_documents=documents))

    @app.get("/cases/{case_id}/analysis/path")
    def graph_path(case_id: uuid.UUID, a: uuid.UUID, b: uuid.UUID, limit: int = 5) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            _read(conn, case_id, "entity.read")
            g, adj = _adj(conn, case_id)
        try:
            res = analysis.shortest_paths(adj, str(a), str(b), limit)
        except KeyError as e:
            raise HTTPException(404, "unknown entity") from e
        names = {n["id"]: n.get("caption") for n in g["nodes"]}
        res["named"] = [[names.get(x) for x in p] for p in res["paths"]]
        return res

    @app.get("/cases/{case_id}/analysis/neighbourhood")
    def graph_neighbourhood(case_id: uuid.UUID, id: uuid.UUID, hops: int = 1) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            _read(conn, case_id, "entity.read")
            g, adj = _adj(conn, case_id)
        try:
            d = analysis.neighbourhood(adj, str(id), hops)
        except KeyError as e:
            raise HTTPException(404, "unknown entity") from e
        return {"distance": d, "count": len(d)}

    @app.get("/cases/{case_id}/analysis/common")
    def graph_common(case_id: uuid.UUID, ids: str) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            _read(conn, case_id, "entity.read")
            g, adj = _adj(conn, case_id)
        shared = analysis.common_neighbours(adj, [x for x in ids.split(",") if x])
        names = {n["id"]: n.get("caption") for n in g["nodes"]}
        return {"common": [{"id": s, "caption": names.get(s)} for s in shared]}

    @app.post("/cases/{case_id}/annotations", status_code=201)
    def add_annotation(case_id: uuid.UUID, body: AnnotationBody) -> dict[str, str]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "workbench.write", case_id)
            return {"id": str(workbench.annotate(conn, actor, case_id, body.kind, body.body, body.cluster_id))}

    @app.get("/cases/{case_id}/annotations")
    def list_annotations(case_id: uuid.UUID) -> list[dict[str, Any]]:
        with app.state.db.connection() as conn:
            _read(conn, case_id, "entity.read")
            return workbench.annotations(conn, case_id)

    @app.delete("/cases/{case_id}/annotations/{annotation_id}")
    def delete_annotation(case_id: uuid.UUID, annotation_id: uuid.UUID) -> dict[str, bool]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "workbench.write", case_id)
            workbench.remove_annotation(conn, actor, case_id, annotation_id)
        return {"removed": True}

    @app.put("/cases/{case_id}/views")
    def save_graph_view(case_id: uuid.UUID, body: ViewBody) -> dict[str, str]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "workbench.write", case_id)
            return {"id": str(workbench.save_view(conn, actor, case_id, body.name, body.layout, body.state))}

    @app.get("/cases/{case_id}/views")
    def graph_views(case_id: uuid.UUID) -> list[dict[str, Any]]:
        with app.state.db.connection() as conn:
            _read(conn, case_id, "entity.read")
            return workbench.list_views(conn, case_id)

    @app.post("/cases/{case_id}/import/table", status_code=201)
    def import_table(case_id: uuid.UUID, body: TableImport) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "assertion.write", case_id)
            policy.authorize(conn, actor, "artifact.write", case_id)
            res = workbench.import_table(conn, app.state.store, app.state.writer, actor, case_id, body.csv_text.encode("utf-8"), body.mapping,
                                         filename=body.filename, source_grade=body.source_grade, info_grade=body.info_grade)
            projector.rebuild_case(conn, case_id)
        return res

    @app.get("/cases/{case_id}/export/graph")
    def export_graph(case_id: uuid.UUID, format: str = "graphml") -> Response:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.export", case_id)
            g = views.build_graph(conn, case_id, include_documents=False)
            audit.record(conn, actor, "workbench.export", case_id=case_id, target_kind="graph", detail={"format": format})
        if format == "graphml":
            return Response(workbench.to_graphml(g), media_type="application/xml")
        if format == "json":
            return JSONResponse(g)
        if format in ("nodes.csv", "edges.csv"):
            return Response(workbench.to_csv(g)[format], media_type="text/csv")
        raise HTTPException(422, "format is graphml, json, nodes.csv or edges.csv")

    @app.get("/cases/{case_id}/report.html")
    def case_report(case_id: uuid.UUID) -> Response:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.export", case_id)
            name = conn.execute("SELECT name FROM case_ WHERE id=%s", (case_id,)).fetchone()["name"]
            body = workbench.report_html(conn, case_id, name)
            audit.record(conn, actor, "workbench.report", case_id=case_id, target_kind="graph")
        return Response(body, media_type="text/html", headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})

    @app.get("/cases/{case_id}/report.pdf")
    def case_report_pdf(case_id: uuid.UUID) -> Response:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.export", case_id)
            name = conn.execute("SELECT name FROM case_ WHERE id=%s", (case_id,)).fetchone()["name"]
            body = workbench.report_pdf(conn, case_id, name)
            audit.record(conn, actor, "workbench.report", case_id=case_id, target_kind="graph", detail={"format": "pdf"})
        return Response(body, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="case-{str(case_id)[:8]}.pdf"'})

    # ---- dedicated search and vector services
    @app.get("/services")
    def services_status() -> dict[str, Any]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.read", None)
            return app.state.services.status(conn)

    @app.post("/cases/{case_id}/services/sync")
    def services_sync(case_id: uuid.UUID) -> dict[str, Any]:
        """Bring the services in step with Postgres for one case (they are rebuildable projections)."""
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.export", case_id)
            if not app.state.services.configured:
                raise HTTPException(400, "no dedicated services are configured (ASTRAL_OPENSEARCH_URL / ASTRAL_QDRANT_URL)")
            from astral import retrieval

            retrieval.ensure_indexed(conn, app.state.store, actor, case_id)
            return app.state.services.sync_case(conn, case_id)

    @app.get("/cases/{case_id}/graph/schema")
    def graph_schema(case_id: uuid.UUID) -> dict[str, Any]:
        """The vertex labels and edge labels a Cypher query over this case can use (read from Postgres; no graph server needed)."""
        from astral import graphstore

        with app.state.db.connection() as conn:
            _read(conn, case_id, "entity.read")
            return {**graphstore.schema(conn, case_id), "projection_configured": app.state.services.graph is not None}

    @app.post("/cases/{case_id}/graph/cypher")
    def graph_cypher(case_id: uuid.UUID, body: CypherBody) -> dict[str, Any]:
        """A read-only Cypher query over the case's Apache AGE projection (rebuilt first if the case graph changed)."""
        from astral import graphstore

        with app.state.db.connection() as conn:
            _read(conn, case_id, "entity.read")
            g = app.state.services.graph
            if g is None:
                raise HTTPException(400, "no graph projection is configured (ASTRAL_AGE_DSN); the built-in path, neighbourhood and analysis endpoints work without it")
            try:
                return graphstore.run_query(conn, g, case_id, body.query)
            except graphstore.GraphQueryError as e:
                raise HTTPException(400, str(e)) from e
            except services_mod.ServiceError as e:
                raise HTTPException(503, f"graph projection unavailable: {str(e)[:200]}") from e

    @app.post("/services/retry-deletes")
    def services_retry() -> dict[str, Any]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.erase", None)
            return app.state.services.retry_pending(conn)

    # ---- write-once preservation
    def _worm() -> worm.WormStore:
        injected = getattr(app.state, "worm", None)
        if injected is not None:
            return injected
        if not settings.worm_endpoint:
            raise HTTPException(400, "write-once preservation is not configured (set ASTRAL_WORM_ENDPOINT and the bucket)")
        ak, sk = app.state.host.secrets.get("ASTRAL_WORM_ACCESS_KEY"), app.state.host.secrets.get("ASTRAL_WORM_SECRET_KEY")
        if not (ak and sk):
            raise HTTPException(400, "ASTRAL_WORM_ACCESS_KEY and ASTRAL_WORM_SECRET_KEY are not set")
        return worm.WormStore(worm.WormConfig(settings.worm_endpoint, settings.worm_bucket, ak, sk, settings.worm_region, settings.worm_mode,
                                              settings.worm_retain_days))

    @app.get("/worm")
    def worm_status() -> dict[str, Any]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.read", None)
        if not settings.worm_endpoint and getattr(app.state, "worm", None) is None:
            return {"configured": False}
        return {"configured": True, **_worm().status()}

    @app.post("/cases/{case_id}/worm/mirror")
    def worm_mirror(case_id: uuid.UUID) -> dict[str, Any]:
        store = _worm()
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.export", case_id)
            return worm.mirror_case(conn, actor, app.state.store, store, case_id)

    @app.get("/cases/{case_id}/worm")
    def worm_copies(case_id: uuid.UUID) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            _read(conn, case_id, "artifact.read")
            return {"copies": worm.copies_for_case(conn, case_id)}

    @app.post("/cases/{case_id}/worm/verify")
    def worm_verify(case_id: uuid.UUID) -> dict[str, Any]:
        store = _worm()
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.export", case_id)
            return worm.verify_case(conn, actor, store, case_id)

    @app.post("/cases/{case_id}/worm/restore")
    def worm_restore(case_id: uuid.UUID) -> dict[str, Any]:
        store = _worm()
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "artifact.write", case_id)
            return worm.restore_missing(conn, actor, app.state.store, store, case_id)

    # ---- the hub: plugin registry and community repositories
    def _repos() -> list:
        pol = egress.EgressPolicy("local-only" if settings.hub_egress != "direct" else "direct")
        return [hub.Repository(loc, remote_policy=pol) for loc in settings.repositories]

    @app.get("/hub")
    def hub_status() -> dict[str, Any]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.read", None)
        out = []
        for r in _repos():
            try:
                out.append({"repository": r.location, "name": r.index.get("name"), "packages": len(r.index["packages"]), "remote": r.remote})
            except hub.HubError as e:
                out.append({"repository": r.location, "error": str(e), "remote": r.remote})
        return {"repositories": out, "egress": settings.hub_egress, "trust": settings.plugin_trust}

    @app.get("/hub/search")
    def hub_search(q: str = "", kind: str | None = None) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.read", None)
        found, errors = [], []
        for r in _repos():
            try:
                found += r.search(q, kind)
            except hub.HubError as e:
                errors.append({"repository": r.location, "error": str(e)})
        return {"results": found, "errors": errors}

    @app.post("/hub/install", status_code=201)
    def hub_install(body: HubInstall) -> dict[str, Any]:
        from astral.plugins.trust import TrustStore

        repo = next((r for r in _repos() if r.location == body.repository), None)
        if repo is None:
            raise HTTPException(404, "that repository is not configured (ASTRAL_REPOSITORIES)")
        need = {"plugin": "plugin.manage", "site-pack": "plugin.manage", "lens": "lens.write", "machine": "machine.write"}.get(body.kind)
        if need is None:
            raise HTTPException(422, "kind is plugin, site-pack, lens or machine")
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, need, None)
            return hub.install(conn, actor, repo, body.kind, body.id, body.version, data_dir=settings.data_dir,
                               trust=TrustStore(settings.data_dir / "trusted-keys"), require_trusted=settings.plugin_trust == "require")

    @app.get("/hub/updates")
    def hub_updates() -> list[dict[str, Any]]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.read", None)
            return hub.updates(conn, _repos())

    # ---- machines
    @app.get("/machines")
    def list_machines() -> list[dict[str, Any]]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.read", None)
            rows = conn.execute("SELECT name, version, definition, sha256, origin, trust FROM machine WHERE workspace_id=%s"
                                " ORDER BY name, version DESC", (actor.workspace_id,)).fetchall()
        return [{"name": r["name"], "version": r["version"], "description": r["definition"].get("description", ""),
                 "stages": r["definition"]["stages"], "seed": r["definition"]["seed"], "sha256": r["sha256"], "origin": r["origin"],
                 "trust": r["trust"].get("status", "local" if r["origin"] == "local" else "unsigned")} for r in rows]

    @app.post("/machines", status_code=201)
    def save_machine(definition: dict[str, Any]) -> dict[str, str]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "machine.write", None)
            return {"id": str(machines.save(conn, actor, definition))}

    @app.get("/machines/{name}/{version}/export")
    def export_machine(name: str, version: int, sign: bool = False) -> dict[str, Any]:
        key = None
        if sign:
            if not settings.signing_key:
                raise HTTPException(400, "no signing key is configured (set ASTRAL_SIGNING_KEY)")
            key = (Path(settings.signing_key).read_bytes(), os.environ.get("ASTRAL_SIGNING_PASSPHRASE") or None)
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.read", None)
            row = machines.get(conn, actor, name, version)
            audit.record(conn, actor, "machine.export", target_kind="machine", target_id=f"{name}@{version}", detail={"signed": bool(key)})
        return machines.export(row, key)

    @app.post("/machines/import", status_code=201)
    def import_machine(package: dict[str, Any]) -> dict[str, Any]:
        from astral.plugins.trust import TrustStore

        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "machine.write", None)
            defn, verdict = machines.verify_package(package, TrustStore(settings.data_dir / "trusted-keys"))
            machines.save(conn, actor, defn, origin="imported", trust=verdict)
        return {"machine": defn["machine"], "version": defn["version"], "trust": verdict["status"], "detail": verdict["detail"]}

    @app.post("/cases/{case_id}/machines/{name}/propose", status_code=201)
    def propose_machine(case_id: uuid.UUID, name: str, body: MachineProposal) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "plugin.invoke", case_id)
            return machines.propose(conn, actor, case_id, name, body.seeds, body.version)

    @app.post("/cases/{case_id}/machine-runs/{run_id}/approve")
    def approve_machine_run(case_id: uuid.UUID, run_id: uuid.UUID, body: MachineApproval) -> dict[str, str]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "plugin.invoke", case_id)
            machines.approve(conn, actor, case_id, run_id, allow_target_contact=body.allow_target_contact)
        return {"status": "approved"}

    @app.post("/cases/{case_id}/machine-runs/{run_id}/run")
    def run_machine(case_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "plugin.invoke", case_id)
            return machines.run(app.state.host, conn, actor, case_id, run_id)

    @app.get("/cases/{case_id}/machine-runs/{run_id}")
    def machine_run_detail(case_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            _read(conn, case_id, "entity.read")
            return machines.get_run(conn, case_id, run_id)

    @app.post("/cases/{case_id}/machine-schedules", status_code=201)
    def schedule_machine(case_id: uuid.UUID, body: MachineSchedule) -> dict[str, str]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "machine.write", case_id)
            policy.authorize(conn, actor, "plugin.invoke", case_id)
            return {"id": str(machines.schedule(conn, actor, case_id, body.machine, body.seeds, body.interval_minutes))}

    @app.get("/cases/{case_id}/machine-schedules")
    def machine_schedules(case_id: uuid.UUID) -> list[dict[str, Any]]:
        with app.state.db.connection() as conn:
            _read(conn, case_id, "entity.read")
            return machines.list_schedules(conn, case_id)

    @app.post("/cases/{case_id}/machine-schedules/{schedule_id}/{state}")
    def machine_schedule_state(case_id: uuid.UUID, schedule_id: uuid.UUID, state: str) -> dict[str, bool]:
        if state not in ("enable", "disable"):
            raise HTTPException(404, "state is enable or disable")
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "machine.write", case_id)
            machines.set_schedule_enabled(conn, actor, case_id, schedule_id, state == "enable")
        return {"enabled": state == "enable"}

    # ------------------------------------------------------------------ lenses
    def _lens_policy(conn, action: str) -> None:
        policy.authorize(conn, actor, action, None)

    @app.get("/lenses")
    def lenses() -> list[dict[str, Any]]:
        with app.state.db.connection() as conn:
            _lens_policy(conn, "case.read")
            rows = conn.execute("SELECT name, version, definition, sha256, origin, trust, created_at FROM lens WHERE workspace_id=%s"
                                " ORDER BY name, version DESC", (actor.workspace_id,)).fetchall()
        return [{"name": r["name"], "version": r["version"], "description": r["definition"].get("description", ""),
                 "inputs": r["definition"]["inputs"], "question": r["definition"]["question"],
                 "steps": r["definition"]["steps"], "sha256": r["sha256"], "origin": r["origin"],
                 "trust": r["trust"].get("status", "local" if r["origin"] == "local" else "unsigned")} for r in rows]

    @app.post("/lenses", status_code=201)
    def save_lens(definition: dict[str, Any]) -> dict[str, str]:
        with app.state.db.connection() as conn:
            _lens_policy(conn, "lens.write")
            try:
                return {"id": str(lens.save(conn, actor, definition))}
            except lens.LensError as e:
                raise HTTPException(422, str(e)) from e

    @app.post("/cases/{case_id}/plans/{plan_id}/save-as-lens", status_code=201)
    def plan_to_lens(case_id: uuid.UUID, plan_id: uuid.UUID, body: LensFromPlan) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.read", case_id)
            _lens_policy(conn, "lens.write")
            row = conn.execute("SELECT question, plan FROM ai_plan WHERE id=%s AND case_id=%s", (plan_id, case_id)).fetchone()
            if not row:
                raise HTTPException(404, "no such plan")
            try:
                defn = lens.from_plan(row["question"], row["plan"], name=body.name, version=body.version,
                                      description=body.description, parameters=body.parameters)
                lens.save(conn, actor, defn)
            except lens.LensError as e:
                raise HTTPException(422, str(e)) from e
        return {"lens": defn, "warning": "read this before sharing it: the replacement was checked, but nothing can prove a lens "
                                         "holds no other detail from the case"}

    @app.get("/lenses/{name}/{version}/export")
    def export_lens(name: str, version: int, sign: bool = False) -> dict[str, Any]:
        key = None
        if sign:
            if not settings.signing_key:
                raise HTTPException(400, "no signing key is configured (set ASTRAL_SIGNING_KEY)")
            key = (Path(settings.signing_key).read_bytes(), os.environ.get("ASTRAL_SIGNING_PASSPHRASE") or None)
        with app.state.db.connection() as conn:
            _lens_policy(conn, "case.read")
            try:
                row = lens.get(conn, actor, name, version)
            except LookupError as e:
                raise HTTPException(404, str(e)) from e
            audit.record(conn, actor, "lens.export", target_kind="lens", target_id=f"{name}@{version}", detail={"signed": bool(key)})
        return lens.export(row, key)

    @app.post("/lenses/import", status_code=201)
    def import_lens(package: dict[str, Any]) -> dict[str, Any]:
        from astral.plugins.trust import TrustStore

        with app.state.db.connection() as conn:
            _lens_policy(conn, "lens.write")
            try:
                defn, verdict = lens.verify_package(package, TrustStore(settings.data_dir / "trusted-keys"))
                lens.save(conn, actor, defn, origin="imported", trust=verdict)
            except lens.LensError as e:
                raise HTTPException(422, str(e)) from e
        return {"lens": defn["lens"], "version": defn["version"], "trust": verdict["status"], "detail": verdict["detail"]}

    @app.post("/cases/{case_id}/lenses/{name}/run", status_code=201)
    def run_lens(case_id: uuid.UUID, name: str, body: LensRun) -> dict[str, Any]:
        """Fill a lens and PROPOSE the plan. It runs only after the operator approves it, like any plan."""
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "plugin.invoke", case_id)
            try:
                row = lens.get(conn, actor, name, body.version)
                question, plan = lens.instantiate(row["definition"], body.inputs)
            except LookupError as e:
                raise HTTPException(404, str(e)) from e
            except lens.LensError as e:
                raise HTTPException(422, str(e)) from e
            client = _llm(conn, case_id)
            planner = f"lens:{name}@{row['version']}"
            pid = ai.store_plan(conn, actor, case_id, question, plan, planner, client)
        return {"id": str(pid), "status": "proposed", "planner": planner, "plan": plan, "question": question}

    @app.post("/cases/{case_id}/erase")
    def erase(case_id: uuid.UUID, body: EraseBody) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.erase", case_id)
            kept = worm.copies_for_case(conn, case_id)
            if kept and not body.acknowledge_worm:
                raise HTTPException(409, {"message": f"{len(kept)} write-once cop(ies) of this case's evidence exist and cannot be deleted before their retention "
                                                      "ends. Erasure would remove the local data only. Repeat with acknowledge_worm=true to proceed.",
                                          "worm_copies": kept})
            plan = erase_case(conn, actor, case_id, app.state.store, legal_basis=body.legal_basis, policy=policy)
        plan.finish(app.state.store)                 # unlink files only after commit
        with app.state.db.connection() as conn:
            forgotten = app.state.services.forget_case(conn, case_id)
        return {"tombstone": str(plan.tombstone_id), "counts": plan.counts, "services": forgotten,
                "worm_retained": [{"key": c["key"], "retain_until": c["retain_until"], "mode": c["mode"]} for c in kept]}

    # -------------------------------------------------------------- artifacts
    @app.post("/cases/{case_id}/artifacts", status_code=201)
    async def upload(case_id: uuid.UUID, request: Request) -> dict[str, Any]:
        data = await request.body()
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "artifact.write", case_id)
            ing = app.state.store.ingest(
                conn, actor, case_id, data, acquired_by="upload",
                media_type=request.headers.get("content-type") or None,
                acquired_from=request.headers.get("x-astral-filename"))
        return {"sha256": ing.hex, "new_blob": ing.new_blob}

    # --------------------------------------------------------------- plugins
    @app.post("/plugins", status_code=201)
    def install(body: InstallBody) -> dict[str, str]:
        from astral.plugins.trust import TrustStore

        with app.state.db.connection() as conn:
            try:
                m = registry.install_from_dir(conn, actor, Path(body.path), trust=TrustStore(settings.data_dir / "trusted-keys"),
                                              require_trusted=settings.plugin_trust == "require")
            except registry.PluginError as e:
                raise HTTPException(400, str(e)) from e
            rec = registry.trust_record(conn.execute("SELECT signature FROM plugin WHERE id=%s AND version=%s",
                                                     (m.id, m.version)).fetchone())
        return {"id": m.id, "version": m.version, "state": "disabled", "trust": rec.get("status", "unsigned"),
                "trust_detail": rec.get("detail", "")}

    @app.post("/plugins/{plugin_id}/{version}/{state}")
    def plugin_state(plugin_id: str, version: str, state: str) -> dict[str, str]:
        with app.state.db.connection() as conn:
            registry.set_state(conn, actor, plugin_id, version, state)
        return {"state": state}

    @app.post("/cases/{case_id}/plugins/{plugin_id}/invoke")
    def invoke(case_id: uuid.UUID, plugin_id: str, body: InvokeBody) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            res = app.state.host.invoke(conn, actor, case_id, plugin_id, body.input,
                                        config=body.config, approved=body.approved,
                                        runtime=body.runtime)
        return {"status": res.status, "assertion_ids": res.assertion_ids, "gaps": res.gaps,
                "stats": res.stats, "runtime": res.runtime}

    # ----------------------------------------------------------- assertions
    @app.get("/cases/{case_id}/assertions/{aid}/explain")
    def explain(case_id: uuid.UUID, aid: int) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "assertion.read", case_id)
            tree = explain_assertion(conn, case_id, aid)
        return {"tree": tree, "originating_artifacts": originating_artifacts(tree)}

    @app.post("/cases/{case_id}/assertions/{aid}/retract")
    def retract(case_id: uuid.UUID, aid: int, body: RetractBody) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            done = app.state.writer.retract(conn, actor, case_id, aid, body.reason,
                                            cascade=body.cascade)
        return {"retracted": done}

    @app.post("/cases/{case_id}/judgments", status_code=201)
    def judge(case_id: uuid.UUID, body: JudgmentBody) -> dict[str, int]:
        with app.state.db.connection() as conn:
            jid = resolver.record_judgment(conn, actor, case_id, body.entity_a, body.entity_b,
                                           body.verdict, comparison=body.comparison,
                                           reasoning=body.reasoning)
        return {"id": jid}

    # ------------------------------------------------------------ projections
    @app.post("/cases/{case_id}/rebuild")
    def rebuild(case_id: uuid.UUID) -> dict[str, int]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.read", case_id)
            return projector.rebuild_case(conn, case_id)

    @app.get("/cases/{case_id}/entities")
    def entities(case_id: uuid.UUID) -> list[dict[str, Any]]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "entity.read", case_id)
            rows = conn.execute(
                "SELECT cluster_id, schema_name, caption, member_count FROM proj_entity"
                " WHERE case_id=%s ORDER BY caption NULLS LAST, cluster_id", (case_id,)).fetchall()
        return [{**r, "cluster_id": str(r["cluster_id"])} for r in rows]

    @app.get("/cases/{case_id}/entities/{cluster_id}")
    def entity(case_id: uuid.UUID, cluster_id: uuid.UUID) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "entity.read", case_id)
            cur = conn.execute("SELECT current_id FROM proj_redirect WHERE case_id=%s AND old_id=%s",
                               (case_id, cluster_id)).fetchone()
            cluster_id = cur["current_id"] if cur else cluster_id
            e = conn.execute("SELECT * FROM proj_entity WHERE case_id=%s AND cluster_id=%s",
                             (case_id, cluster_id)).fetchone()
            if e is None:
                raise HTTPException(404, "entity not found")
            props = conn.execute(
                "SELECT predicate, value, assertion_id, source_grade, info_grade,"
                " independent_sources FROM proj_property WHERE case_id=%s AND cluster_id=%s"
                " ORDER BY predicate, assertion_id", (case_id, cluster_id)).fetchall()
            edges = conn.execute(
                "SELECT predicate, object_cluster, assertion_id, source_grade, info_grade,"
                " independent_sources FROM proj_edge WHERE case_id=%s AND subject_cluster=%s"
                " ORDER BY assertion_id", (case_id, cluster_id)).fetchall()
        return {"cluster_id": str(cluster_id), "schema": e["schema_name"], "caption": e["caption"],
                "properties": props,
                "edges": [{**x, "object_cluster": str(x["object_cluster"])} for x in edges]}

    # ============================================================ M1: workspace
    def _read(conn, case_id: uuid.UUID, action: str = "case.read") -> None:
        policy.authorize(conn, actor, action, case_id)

    @app.get("/plugins")
    def list_plugins() -> list[dict[str, Any]]:
        with app.state.db.connection() as conn:
            rows = conn.execute("SELECT id, version, state, quarantine_reason, manifest FROM plugin"
                                " ORDER BY id, version").fetchall()
            health = {(h["plugin_id"], h["plugin_version"]): h for h in conn.execute(
                "SELECT DISTINCT ON (plugin_id, plugin_version) plugin_id, plugin_version, ok,"
                " error_class, checked_at FROM plugin_health ORDER BY plugin_id, plugin_version, id DESC")}
        out = []
        for r in rows:
            m = r["manifest"]
            h = health.get((r["id"], r["version"]))
            out.append({"id": r["id"], "version": r["version"], "state": r["state"],
                        "quarantine_reason": r["quarantine_reason"], "title": m["title"],
                        "stage": m["stage"], "footprint": m["footprint"], "licence": m["licence"],
                        "consumes": m.get("consumes", []), "origin": m.get("origin"),
                        "capabilities": m["capabilities"], "secrets": m["secrets"],
                        "last_check": ({"ok": h["ok"], "error_class": h["error_class"],
                                        "at": h["checked_at"].isoformat()} if h else None)})
        return out

    @app.post("/plugins/health-sweep")
    def health_sweep(force: bool = True) -> list[dict[str, Any]]:
        with app.state.db.connection() as conn:
            return app.state.host.health_sweep(conn, actor, force=force)

    @app.get("/secrets")
    def secret_names() -> dict[str, Any]:
        st = app.state.secret_store
        return {"enabled": st is not None, "names": st.names() if st else [],
                "needed": _needed_secrets(app), "note": None if st else (
                    "set ASTRAL_SECRETS_PASSPHRASE (and ASTRAL_SECRETS_ENABLED=1) to keep keys in the "
                    "encrypted store; environment variables still work")}

    @app.put("/secrets/{name}")
    def put_secret(name: str, body: SecretBody) -> dict[str, str]:
        st = app.state.secret_store
        if st is None:
            raise HTTPException(409, "no encrypted secret store is configured (see GET /secrets)")
        try:
            st.set(name, body.value)
        except SecretsError as e:
            raise HTTPException(422, str(e))
        with app.state.db.connection() as conn:
            audit.record(conn, actor, "secret.set", target_kind="secret", target_id=name, detail={})
        return {"status": "stored"}

    @app.delete("/secrets/{name}")
    def del_secret(name: str) -> dict[str, bool]:
        st = app.state.secret_store
        if st is None:
            raise HTTPException(409, "no encrypted secret store is configured")
        with app.state.db.connection() as conn:
            audit.record(conn, actor, "secret.delete", target_kind="secret", target_id=name, detail={})
        return {"deleted": st.delete(name)}

    @app.get("/sources")
    def sources() -> list[dict[str, Any]]:
        with app.state.db.connection() as conn:
            rows = conn.execute("SELECT id, kind, name, origin_key, default_source_grade FROM source"
                                " WHERE workspace_id=%s ORDER BY kind, name", (actor.workspace_id,)).fetchall()
        return [{**r, "id": str(r["id"])} for r in rows]

    @app.put("/sources/{source_id}/grade")
    def grade_source(source_id: uuid.UUID, body: GradeBody) -> dict[str, str]:
        with app.state.db.connection() as conn:
            try:
                cases.set_source_grade(conn, actor, source_id, body.grade.upper())
            except LookupError:
                raise HTTPException(404, "source not found")
            except ValueError as e:
                raise HTTPException(422, str(e))
        return {"grade": body.grade.upper()}

    @app.post("/cases/{case_id}/investigate/name")
    def investigate_name(case_id: uuid.UUID, body: NameQuery) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "plugin.invoke", case_id)
            return investigate.name_to_network(
                app.state.host, conn, actor, case_id, name=body.name, country=body.country,
                schema=body.schema_name, plugin_inputs=body.plugin_inputs,
                plugin_configs=body.plugin_configs, only=body.only, propose=body.propose)

    @app.post("/cases/{case_id}/artifacts/{sha}/ingest")
    def ingest_doc(case_id: uuid.UUID, sha: str) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "plugin.invoke", case_id)
            row = conn.execute("SELECT media_type FROM artifact WHERE sha256=%s", (bytes.fromhex(sha),)).fetchone()
            mt = (row["media_type"] if row else "") or ""
            if mt.startswith(("audio/", "video/", "image/")):
                out = investigate.ingest_media(app.state.host, conn, actor, case_id, sha)
            else:
                out = investigate.ingest_document(app.state.host, conn, actor, case_id, sha)
            projector.rebuild_case(conn, case_id)
        return out

    # --------------------------------------------------------------- AI layer
    def _llm(conn, case_id: uuid.UUID):
        """A client for this case, or None when no model is configured. Raises on a policy
        violation (e.g. a hosted endpoint for a local-only case) instead of quietly proceeding."""
        from astral import llm

        if not settings.llm_url:
            return None
        ident = conn.execute("SELECT collection_identity FROM case_ WHERE id=%s", (case_id,)).fetchone()["collection_identity"]
        try:
            return llm.LlmClient(settings.llm_url, model=settings.llm_model, deployment_mode=settings.llm_egress,
                                 case_identity=ident)
        except llm.LlmPolicyError as e:
            raise HTTPException(403, str(e)) from e

    @app.post("/cases/{case_id}/exhibit")
    def exhibit_export(case_id: uuid.UUID, body: ExhibitBody):
        """Assemble the case into a self-verifying package and return it as a download."""
        from datetime import datetime, timezone

        from fastapi.responses import FileResponse

        from astral import exhibit

        key = None
        if body.sign:
            if not settings.signing_key:
                raise HTTPException(400, "no signing key is configured (set ASTRAL_SIGNING_KEY to an Ed25519 private key PEM)")
            key = (Path(settings.signing_key).read_bytes(), os.environ.get("ASTRAL_SIGNING_PASSPHRASE") or None)
        out = settings.data_dir / "exports"
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"exhibit-{str(case_id)[:8]}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.zip"
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "case.export", case_id)
            try:
                summary = exhibit.build(conn, app.state.store, actor, case_id, path, operator_statement=body.statement,
                                        plan_ids=body.plan_ids, include_all_artifacts=body.include_all_artifacts,
                                        signing_key=key, allow_integrity_failures=body.allow_integrity_failures)
            except exhibit.ExhibitError as e:
                raise HTTPException(409, {"message": str(e), "problems": e.problems}) from e
        hdrs = {"X-Exhibit-Sha256": summary["bundle_sha256"] or "", "X-Exhibit-Integrity-Ok": str(summary["integrity_ok"]).lower()}
        if body.worm:
            with app.state.db.connection() as conn:
                kept = worm.mirror_exhibit(conn, actor, _worm(), case_id, path.read_bytes())
            hdrs |= {"X-Worm-Version": kept["version_id"], "X-Worm-Retain-Until": kept["retain_until"]}
        return FileResponse(path, media_type="application/zip", filename=path.name, headers=hdrs)

    @app.get("/ai/status")
    def ai_status() -> dict[str, Any]:
        return {"model_configured": bool(settings.llm_url), "egress_mode": settings.llm_egress,
                "planner": "model" if settings.llm_url else "rules", "composer": "model" if settings.llm_url else "extractive",
                "retrieval": ["fts", "fuzzy"] + (["opensearch"] if app.state.services.text else [])
                             + ([f"dense:{app.state.services.dense.model_id}"] if app.state.services.dense else [])
                             + ([f"rerank:{app.state.services.dense.backend.reranker_model}"] if app.state.services.dense and app.state.services.dense.can_rerank else []),
                "not_configured": [n for n, on in (("dense embedder", app.state.services.dense),
                                                   ("reranker", app.state.services.dense and app.state.services.dense.can_rerank)) if not on],
                "note": "Without a model, answers are verbatim quotations from the case; they are verified the same way."}

    @app.post("/cases/{case_id}/retrieval/context")
    def retrieval_context(case_id: uuid.UUID, body: ContextBody) -> dict[str, Any]:
        """Have the configured model write a one-sentence situating header for passages that lack one (a search aid, not
        evidence). Explicit and bounded; passages are the case's own text, sent only where the case's egress rule allows."""
        from astral import audit, contextual, retrieval

        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "plugin.invoke", case_id)
            client = _llm(conn, case_id)
            if client is None:
                raise HTTPException(400, "no language model is configured (ASTRAL_LLM_URL); context headers need one")
            retrieval.ensure_indexed(conn, app.state.store, actor, case_id)
            out = contextual.generate(conn, app.state.store, actor, case_id, client, limit=body.limit)
            audit.record(conn, actor, "ai.contextualize", case_id=case_id, target_kind="text_chunk",
                         detail={k: out[k] for k in ("written", "ungrounded", "unusable", "model")})
        return out

    @app.post("/cases/{case_id}/ask", status_code=201)
    def ask(case_id: uuid.UUID, body: AskBody) -> dict[str, Any]:
        """Propose a plan. NOTHING runs until the operator approves it."""
        from astral import ai, audit

        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "plugin.invoke", case_id)
            client = _llm(conn, case_id)
            planner = "rules"
            try:
                plan = ai.llm_plan(client, body.question) if client else ai.rules_plan(body.question)
                planner = client.model if client else "rules"
            except Exception:                                           # noqa: BLE001 - a bad model plan degrades to rules
                plan = ai.rules_plan(body.question)
            pid = ai.store_plan(conn, actor, case_id, body.question, plan, planner, client)
        return {"id": str(pid), "status": "proposed", "planner": planner, "plan": plan}

    @app.get("/cases/{case_id}/plans")
    def plans(case_id: uuid.UUID) -> list[dict[str, Any]]:
        with app.state.db.connection() as conn:
            _read(conn, case_id, "entity.read")
            rows = conn.execute("SELECT id, question, planner, plan, status, result, created_at FROM ai_plan"
                                " WHERE case_id=%s ORDER BY created_at DESC LIMIT 50", (case_id,)).fetchall()
        return [{**r, "id": str(r["id"])} for r in rows]

    @app.post("/cases/{case_id}/plans/{plan_id}/approve")
    def plan_approve(case_id: uuid.UUID, plan_id: uuid.UUID) -> dict[str, Any]:
        from astral import audit

        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "plugin.invoke", case_id)
            n = conn.execute("UPDATE ai_plan SET status='approved', approved_at=now() WHERE id=%s AND case_id=%s"
                             " AND status='proposed'", (plan_id, case_id)).rowcount
            if not n:
                raise HTTPException(409, "plan not found or not awaiting approval")
            audit.record(conn, actor, "ai.approve", case_id=case_id, target_kind="ai_plan", target_id=str(plan_id))
        return {"status": "approved"}

    @app.post("/cases/{case_id}/plans/{plan_id}/run")
    def plan_run(case_id: uuid.UUID, plan_id: uuid.UUID) -> dict[str, Any]:
        from astral import ai, audit, citations

        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "plugin.invoke", case_id)
            row = conn.execute("SELECT question, plan, status FROM ai_plan WHERE id=%s AND case_id=%s",
                               (plan_id, case_id)).fetchone()
            if not row:
                raise HTTPException(404, "no such plan")
            if row["status"] != "approved":
                raise HTTPException(409, f"plan is {row['status']}; only an approved plan runs")
            client = _llm(conn, case_id)
            verifiers: list[Any] = [citations.LexicalVerifier()]
            if client:
                verifiers += [citations.LlmVerifier(client, "strict"), citations.LlmVerifier(client, "contradiction")]
            conn.execute("UPDATE ai_plan SET status='running' WHERE id=%s", (plan_id,))
            try:
                res = ai.run_plan(conn, app.state.store, actor, case_id, row["question"], row["plan"], client, verifiers, app.state.services)
            except Exception as e:                                      # noqa: BLE001
                conn.execute("UPDATE ai_plan SET status='failed', finished_at=now(), result=%s WHERE id=%s",
                             (psycopg.types.json.Jsonb({"error": f"{type(e).__name__}: {e}"}), plan_id))
                raise HTTPException(500, f"plan failed: {type(e).__name__}: {e}") from e
            conn.execute("UPDATE ai_plan SET status='done', finished_at=now(), result=%s WHERE id=%s",
                         (psycopg.types.json.Jsonb(res), plan_id))
            audit.record(conn, actor, "ai.run", case_id=case_id, target_kind="ai_plan", target_id=str(plan_id),
                         detail={"claims": len(res["claims"]), "rejected": len(res["rejected"]), "composer": res["composer"],
                                 "egress": client.mode if client else "none", "latency_ms": res["latency_ms"]})
        return res

    # ------------------------------------------------------------------ faces
    def _detect(conn, case_id: uuid.UUID, sha: str, body: Any) -> tuple[Any, dict[str, Any]]:
        try:
            res = app.state.host.backends.resolve("face.embed", prefer=getattr(body, "prefer", None),
                                                  allow_noncommercial=bool(getattr(body, "allow_noncommercial", False)))
        except Exception as e:                                          # NoBackend
            raise HTTPException(503, str(e)) from e
        data = app.state.store.read(conn, actor, case_id, bytes.fromhex(sha))
        try:
            return res, res.backend.run(data=data)
        except Exception as e:                                          # noqa: BLE001
            raise HTTPException(422, f"{res.backend.name} failed: {type(e).__name__}: {e}") from e

    @app.get("/cases/{case_id}/faces")
    def faces_list(case_id: uuid.UUID) -> list[dict[str, Any]]:
        with app.state.db.connection() as conn:
            _read(conn, case_id, "entity.read")
            rows = conn.execute(
                "SELECT id, artifact_sha, frame_ms, box, det_score, model_id, backend, watchlist, label, expires_at"
                " FROM face WHERE case_id=%s ORDER BY created_at, id", (case_id,)).fetchall()
        return [{**r, "id": str(r["id"]), "artifact": bytes(r["artifact_sha"]).hex(), "artifact_sha": None} for r in rows]

    @app.get("/cases/{case_id}/faces/{face_id}/crop")
    def face_crop(case_id: uuid.UUID, face_id: uuid.UUID) -> Response:
        from astral import faces

        with app.state.db.connection() as conn:
            _read(conn, case_id, "artifact.read")
            r = conn.execute("SELECT artifact_sha, box FROM face WHERE id=%s AND case_id=%s", (face_id, case_id)).fetchone()
            if not r:
                raise HTTPException(404, "no such face")
            data = app.state.store.read(conn, actor, case_id, bytes(r["artifact_sha"]))
        return Response(faces.crop_jpeg(data, r["box"]), media_type="image/jpeg",
                        headers={"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff"})

    @app.post("/cases/{case_id}/faces/search")
    def faces_search(case_id: uuid.UUID, body: FaceSearchBody) -> dict[str, Any]:
        """Ranked CANDIDATES against the case's own faces and the operator's watchlist.
        Every call is recorded (ids, hashes, counts) and nothing is ever a verdict."""
        from astral import faces
        from astral.compute.models import load_registry

        if not body.artifact and not body.face_id:
            raise HTTPException(422, "give an artifact (probe image) or a face_id")
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "plugin.invoke", case_id)
            exclude = None
            exclude_face = uuid.UUID(body.face_id) if body.face_id else None
            if body.face_id:
                r = conn.execute("SELECT embedding, model_id, backend, artifact_sha FROM face WHERE id=%s AND case_id=%s",
                                 (uuid.UUID(body.face_id), case_id)).fetchone()
                if not r:
                    raise HTTPException(404, "no such face")
                emb, model_id, backend, qsha = r["embedding"], r["model_id"], r["backend"], bytes(r["artifact_sha"]).hex()
                n_faces = 1
            else:
                res, out = _detect(conn, case_id, body.artifact, body)
                n_faces = len(out["faces"])
                if not n_faces:
                    raise HTTPException(422, "no face was detected in the probe image (this is not proof there is none)")
                if body.face_index >= n_faces:
                    raise HTTPException(422, f"face_index {body.face_index} out of range ({n_faces} faces found)")
                emb, model_id, backend, qsha = (out["faces"][body.face_index]["embedding"], res.backend.model_id,
                                                res.backend.name, body.artifact)
                exclude = bytes.fromhex(body.artifact)
            hits, degraded = app.state.services.face_hits(conn, case_id, emb, model_id=model_id, scope=body.scope, k=body.k,
                                                          min_score=body.min_score, exclude_artifact=exclude, exclude_face=exclude_face)
            engine = "qdrant (exact)" if hits is not None else "postgres"
            if hits is None:
                hits = faces.search(conn, case_id, emb, model_id=model_id, scope=body.scope, k=body.k,
                                    min_score=body.min_score, exclude_artifact=exclude, exclude_face=exclude_face)
            faces.record_search(conn, actor, case_id, query_sha=qsha, model_id=model_id, backend=backend,
                                scope=body.scope, k=body.k, min_score=body.min_score, results=len(hits))
            lic = {m: {"licence": v.licence, "licence_class": v.licence_class, "commercial_use": v.commercial_use}
                   for m, v in load_registry().items() if m in (model_id, "yunet")}
        return {"candidates": hits, "model_id": model_id, "backend": backend, "licences": lic,
                "faces_in_probe": n_faces, "engine": engine, "degraded": degraded,
                "notice": "Ranked candidates by cosine similarity. A score is not an identification; the reference "
                          "threshold comes from an easy frontal benchmark and does not transfer to poor images."}

    @app.post("/cases/{case_id}/watchlist", status_code=201)
    def watchlist_enrol(case_id: uuid.UUID, body: EnrolBody) -> dict[str, Any]:
        from astral import audit, faces

        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "plugin.invoke", case_id)
            res, out = _detect(conn, case_id, body.artifact, body)
            if len(out["faces"]) != 1:
                raise HTTPException(422, f"a watchlist photo must contain exactly one detectable face "
                                         f"({len(out['faces'])} found)")
            f = out["faces"][0]
            b = f["box"]
            fid = uuid.uuid5(uuid.NAMESPACE_URL, f"astral-watchlist:{case_id}:{body.artifact}:{b['x']},{b['y']},{b['w']},{b['h']}")
            faces.index_faces(conn, actor, case_id, artifact_sha=bytes.fromhex(body.artifact),
                              faces=[{**f, "entity": str(fid)}], model_id=res.backend.model_id, backend=res.backend.name,
                              watchlist=body.watchlist, label=body.label, basis=body.basis)
            audit.record(conn, actor, "watchlist.enrol", case_id=case_id, target_kind="face", target_id=str(fid),
                         detail={"watchlist": "named" if body.watchlist != "default" else "default",
                                 "model": res.backend.model_id, "has_basis": bool(body.basis)})
        return {"id": str(fid), "model_id": res.backend.model_id, "backend": res.backend.name}

    @app.delete("/cases/{case_id}/watchlist/{face_id}")
    def watchlist_remove(case_id: uuid.UUID, face_id: uuid.UUID) -> dict[str, Any]:
        from astral import audit

        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "plugin.invoke", case_id)
            n = conn.execute("DELETE FROM face WHERE id=%s AND case_id=%s AND watchlist IS NOT NULL",
                             (face_id, case_id)).rowcount
            if n:
                audit.record(conn, actor, "watchlist.remove", case_id=case_id, target_kind="face", target_id=str(face_id))
            gone = app.state.services.reconcile_faces(conn, [case_id]) if n else {}
        return {"removed": n, **({"services": gone} if gone else {})}

    @app.post("/biometrics/purge")
    def biometrics_purge() -> dict[str, Any]:
        from astral import faces

        with app.state.db.connection() as conn:
            n = faces.purge_expired(conn, actor)
            gone = app.state.services.reconcile_faces(conn)           # biometric vectors must leave Qdrant as they leave Postgres
            return {"deleted": n, **({"services": gone} if gone else {})}

    @app.get("/cases/{case_id}/images/near-duplicates")
    def near_duplicates(case_id: uuid.UUID, max_distance: int = 32) -> list[dict[str, Any]]:
        from astral import pdq

        with app.state.db.connection() as conn:
            _read(conn, case_id, "artifact.read")
            rows = conn.execute("SELECT artifact_sha, hash, quality FROM image_hash WHERE case_id=%s AND algo=%s",
                                (case_id, pdq.ALGO)).fetchall()
        out = []
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                d = pdq.distance(a["hash"], b["hash"])
                if d <= max_distance:
                    out.append({"a": bytes(a["artifact_sha"]).hex(), "b": bytes(b["artifact_sha"]).hex(), "distance": d,
                                "of_bits": 256, "low_quality": min(a["quality"], b["quality"]) < 0.1,
                                "algo": pdq.ALGO})
        return sorted(out, key=lambda x: x["distance"])

    @app.get("/cases/{case_id}/media")
    def media_list(case_id: uuid.UUID) -> list[dict[str, Any]]:
        with app.state.db.connection() as conn:
            _read(conn, case_id, "entity.read")
            return views.list_media(conn, case_id)

    @app.get("/cases/{case_id}/media/{sha}")
    def media_detail(case_id: uuid.UUID, sha: str) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            _read(conn, case_id, "artifact.read")
            return views.media_view(conn, app.state.store, actor, case_id, sha)

    @app.get("/cases/{case_id}/media/{sha}/stream")
    def media_stream(case_id: uuid.UUID, sha: str):
        """The original file for the in-page player, with HTTP range support. Only
        audio, video and image types are served inline; the CSP sandbox still applies."""
        from fastapi.responses import FileResponse

        with app.state.db.connection() as conn:
            _read(conn, case_id, "artifact.read")
            path = app.state.store.path_for(conn, actor, case_id, bytes.fromhex(sha))
            row = conn.execute("SELECT media_type FROM artifact WHERE sha256=%s", (bytes.fromhex(sha),)).fetchone()
        mt = row["media_type"]
        if not mt.startswith(("audio/", "video/", "image/")):
            raise HTTPException(415, "not a media file")
        return FileResponse(path, media_type=mt, headers={"Content-Security-Policy": "sandbox",
                                                          "X-Content-Type-Options": "nosniff"})

    @app.get("/cases/{case_id}/artifacts")
    def artifacts(case_id: uuid.UUID) -> list[dict[str, Any]]:
        with app.state.db.connection() as conn:
            _read(conn, case_id, "artifact.read")
            rows = conn.execute(
                "SELECT a.sha256, a.media_type, a.byte_size, a.parent_sha256, a.derivation,"
                " min(q.acquired_at) AS first_acquired, count(*) AS acquisitions,"
                " (array_agg(q.acquired_by ORDER BY q.id))[1] AS acquired_by,"
                " (array_agg(q.acquired_from ORDER BY q.id))[1] AS acquired_from"
                " FROM artifact a JOIN artifact_acquisition q ON q.sha256 = a.sha256"
                " WHERE q.case_id=%s GROUP BY a.sha256 ORDER BY min(q.acquired_at) DESC", (case_id,)).fetchall()
        return [{**r, "sha256": bytes(r["sha256"]).hex(),
                 "parent_sha256": bytes(r["parent_sha256"]).hex() if r["parent_sha256"] else None}
                for r in rows]

    @app.get("/cases/{case_id}/artifacts/{sha}")
    def artifact_bytes(case_id: uuid.UUID, sha: str) -> Response:
        with app.state.db.connection() as conn:
            data = app.state.store.read(conn, actor, case_id, bytes.fromhex(sha))
            row = conn.execute("SELECT media_type FROM artifact WHERE sha256=%s",
                               (bytes.fromhex(sha),)).fetchone()
        mt = row["media_type"]
        # never let captured content execute in the app's origin
        safe = mt if mt.startswith(("text/plain", "image/", "application/json", "application/pdf")) \
            else "application/octet-stream"
        return Response(data, media_type=safe, headers={
            "Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'inline; filename="{sha[:16]}"'})

    @app.get("/cases/{case_id}/coverage")
    def coverage(case_id: uuid.UUID, run_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
        with app.state.db.connection() as conn:
            _read(conn, case_id)
            return views.coverage_ledger(conn, case_id, run_id)

    @app.get("/cases/{case_id}/graph")
    def graph(case_id: uuid.UUID, documents: bool = True) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            _read(conn, case_id, "entity.read")
            return views.build_graph(conn, case_id, include_documents=documents)

    @app.get("/cases/{case_id}/geo")
    def geo(case_id: uuid.UUID) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            _read(conn, case_id, "entity.read")
            return views.build_geo(conn, case_id)

    @app.get("/cases/{case_id}/timeline")
    def timeline(case_id: uuid.UUID) -> list[dict[str, Any]]:
        with app.state.db.connection() as conn:
            _read(conn, case_id, "entity.read")
            return views.build_timeline(conn, case_id)

    @app.get("/cases/{case_id}/documents")
    def documents(case_id: uuid.UUID) -> list[dict[str, Any]]:
        with app.state.db.connection() as conn:
            _read(conn, case_id, "entity.read")
            return views.list_documents(conn, case_id)

    @app.get("/cases/{case_id}/documents/{text_sha}")
    def document(case_id: uuid.UUID, text_sha: str) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            _read(conn, case_id, "artifact.read")
            return views.document_view(conn, app.state.store, actor, case_id, text_sha)

    @app.get("/cases/{case_id}/candidates")
    def candidates(case_id: uuid.UUID, min_score: float = resolution.MIN_SCORE) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            _read(conn, case_id, "entity.read")
            rep = resolution.find_candidates(conn, case_id, min_score=min_score)
        return {"candidates": [c.as_dict() for c in rep.candidates], "compared": rep.compared,
                "skipped_blocks": rep.skipped_blocks,
                "note": "scores rank pairs for human review; they are not probabilities and nothing is merged"}

    @app.post("/cases/{case_id}/candidates/propose")
    def propose_candidates(case_id: uuid.UUID, body: ProposeBody) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            policy.authorize(conn, actor, "judgment.write", case_id)
            rep = resolution.find_candidates(conn, case_id, min_score=body.min_score)
            ids = resolution.propose(conn, actor, case_id, rep.candidates)
        return {"proposed": len(ids), "judgment_ids": ids}

    @app.get("/cases/{case_id}/judgments")
    def judgments(case_id: uuid.UUID, verdict: str | None = None) -> list[dict[str, Any]]:
        with app.state.db.connection() as conn:
            _read(conn, case_id, "entity.read")
            rows = conn.execute(
                "SELECT j.id, j.entity_a, j.entity_b, j.verdict, j.comparison, j.reasoning, j.decided_at,"
                " r.id IS NOT NULL AS retracted FROM judgment j LEFT JOIN judgment_retraction r"
                " ON r.judgment_id = j.id WHERE j.case_id=%s AND (%s::text IS NULL OR j.verdict=%s)"
                " ORDER BY j.id DESC", (case_id, verdict, verdict)).fetchall()
        return [{**r, "entity_a": str(r["entity_a"]), "entity_b": str(r["entity_b"]),
                 "decided_at": r["decided_at"].isoformat()} for r in rows]

    @app.get("/cases/{case_id}/entities/{cluster_id}/members")
    def entity_members(case_id: uuid.UUID, cluster_id: uuid.UUID) -> dict[str, Any]:
        """The references merged into a cluster and the live `same` judgments that
        joined them: what the UI needs to show 'why' and to offer a split."""
        with app.state.db.connection() as conn:
            _read(conn, case_id, "entity.read")
            ms = [r["entity_id"] for r in conn.execute(
                "SELECT entity_id FROM proj_entity_member WHERE case_id=%s AND cluster_id=%s",
                (case_id, cluster_id))]
            js = conn.execute(
                "SELECT j.id, j.entity_a, j.entity_b, j.comparison, j.reasoning, j.decided_at FROM judgment j"
                " LEFT JOIN judgment_retraction r ON r.judgment_id=j.id WHERE j.case_id=%s AND r.id IS NULL"
                " AND j.verdict='same' AND j.entity_a = ANY(%s) AND j.entity_b = ANY(%s) ORDER BY j.id",
                (case_id, ms, ms)).fetchall()
        return {"members": [str(m) for m in ms],
                "judgments": [{**j, "entity_a": str(j["entity_a"]), "entity_b": str(j["entity_b"]),
                               "decided_at": j["decided_at"].isoformat()} for j in js]}

    @app.get("/cases/{case_id}/proposals")
    def proposals(case_id: uuid.UUID) -> list[dict[str, Any]]:
        """Pending merge proposals: undecided pairs a human has not yet ruled on."""
        with app.state.db.connection() as conn:
            _read(conn, case_id, "entity.read")
            cl = {r["entity_id"]: r["cluster_id"] for r in conn.execute(
                "SELECT entity_id, cluster_id FROM proj_entity_member WHERE case_id=%s", (case_id,))}
            rows = conn.execute(
                "SELECT j.id, j.entity_a, j.entity_b, j.verdict, j.comparison FROM judgment j"
                " LEFT JOIN judgment_retraction r ON r.judgment_id=j.id"
                " WHERE j.case_id=%s AND r.id IS NULL ORDER BY j.id", (case_id,)).fetchall()
        ruled = {frozenset((cl.get(r["entity_a"]), cl.get(r["entity_b"])))
                 for r in rows if r["verdict"] in ("same", "not_same")}
        out = []
        for r in rows:
            if r["verdict"] != "undecided":
                continue
            ca, cb = cl.get(r["entity_a"]), cl.get(r["entity_b"])
            if ca is None or cb is None or ca == cb or frozenset((ca, cb)) in ruled:
                continue
            out.append({"judgment_id": r["id"], "a_entity": str(r["entity_a"]), "b_entity": str(r["entity_b"]),
                        "a_cluster": str(ca), "b_cluster": str(cb), "comparison": r["comparison"]})
        out.sort(key=lambda p: -(p["comparison"].get("score") or 0))
        return out

    @app.post("/cases/{case_id}/judgments/{jid}/retract")
    def retract_judgment(case_id: uuid.UUID, jid: int, body: RetractBody) -> dict[str, str]:
        with app.state.db.connection() as conn:
            resolver.retract_judgment(conn, actor, case_id, jid, body.reason)
        return {"status": "retracted"}

    @app.get("/cases/{case_id}/merge-explanation")
    def merge_explanation(case_id: uuid.UUID, a: uuid.UUID, b: uuid.UUID) -> dict[str, Any]:
        with app.state.db.connection() as conn:
            _read(conn, case_id, "entity.read")
            try:
                return resolution.explain_pair(conn, case_id, a, b)
            except LookupError:
                raise HTTPException(404, "entity not found")

    return app
