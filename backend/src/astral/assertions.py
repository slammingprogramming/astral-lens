"""Layer 1: the assertion writer. The ONLY path that inserts assertions.

Enforces evidence, derived_by, grading and the AI-layer limits (invariants 1,
2, 10). The database independently refuses writes that did not come through
here (see astral_assertion_guard in 0001).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import psycopg
from pydantic import BaseModel, Field, field_validator, model_validator

from astral import audit
from astral.db import Actor
from astral.evidence import EvidenceStore
from astral.policy import Policy, SoloPolicy

SOURCE_GRADES = "ABCDEF"

# Predicates whose value is an identifier. An AI stage may never invent one:
# the value must appear verbatim in a cited, verified span (invariant 10).
IDENTIFIER_PREDICATES = frozenset({
    "registrationNumber", "idNumber", "taxNumber", "passportNumber", "vatCode",
    "leiCode", "swiftBic", "iban", "imoNumber", "mmsi", "email", "phone",
    "wikidataId", "opencorporatesUrl", "cryptoAddress",
})
# Identity assertions are the resolver's business, via judgments by a human.
IDENTITY_PREDICATES = frozenset({"sameAs", "same_as", "canonicalId"})


class AssertionRejected(ValueError):
    """The writer refused the assertion; nothing was written."""


class Citation(BaseModel):
    artifact: str                       # sha256 hex
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str


class DerivedBy(BaseModel):
    """Recorded on every assertion (05 §6, 03 §3). Reliability is inseparable
    from what produced a result."""

    plugin: str
    version: str
    backend: str
    kind: Literal["rule", "model", "llm", "analyst", "import"] = "rule"
    model: str | None = None
    model_version: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    # Degradation is visible, never silent (invariant 4)
    degraded: bool = False
    degraded_from: str | None = None
    egress_mode: Literal["local-only", "hosted-redacted", "hosted-direct"] | None = None
    licence_class: str | None = None

    @model_validator(mode="after")
    def _degradation_named(self) -> "DerivedBy":
        if self.degraded and not self.degraded_from:
            raise ValueError("degraded=True requires degraded_from (the backend not used)")
        return self


class AssertionDraft(BaseModel):
    subject: uuid.UUID
    predicate: str = Field(min_length=1)
    object_entity: uuid.UUID | None = None
    object_value: Any = None
    source_id: uuid.UUID
    evidence: list[str] = Field(min_length=1)          # sha256 hex digests
    derived_from: list[int] = Field(default_factory=list)
    derived_by: DerivedBy
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    # None => take the source's default grade. Never silently optimistic.
    source_grade: str | None = None
    info_grade: int = 6

    @field_validator("evidence")
    @classmethod
    def _hex(cls, v: list[str]) -> list[str]:
        for d in v:
            if len(d) != 64:
                raise ValueError("evidence must be sha256 hex digests")
            int(d, 16)
        return v

    @field_validator("source_grade")
    @classmethod
    def _grade(cls, v: str | None) -> str | None:
        if v is not None and (len(v) != 1 or v not in SOURCE_GRADES):
            raise ValueError("source_grade must be one of A..F")
        return v

    @field_validator("info_grade")
    @classmethod
    def _info(cls, v: int) -> int:
        if not 1 <= v <= 6:
            raise ValueError("info_grade must be 1..6")
        return v

    @model_validator(mode="after")
    def _object(self) -> "AssertionDraft":
        has_e, has_v = self.object_entity is not None, self.object_value is not None
        if has_e == has_v:
            raise ValueError("exactly one of object_entity / object_value is required")
        return self


class AssertionWriter:
    def __init__(self, policy: Policy | None = None, store: EvidenceStore | None = None) -> None:
        self.policy = policy or SoloPolicy()
        self.store = store

    # ------------------------------------------------------------------ write
    def write(
        self, conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID, draft: AssertionDraft
    ) -> int:
        self.policy.authorize(conn, actor, "assertion.write", case_id)
        source = conn.execute(
            "SELECT default_source_grade, workspace_id FROM source WHERE id = %s",
            (draft.source_id,),
        ).fetchone()
        if source is None or source["workspace_id"] != actor.workspace_id:
            raise AssertionRejected("unknown source")

        source_grade = draft.source_grade or source["default_source_grade"]
        self._check_weakest_link(conn, case_id, draft, source_grade)
        self._check_ai_limits(draft)

        # open the writer gate for exactly this insert
        conn.execute("SELECT set_config('astral.writer', '1', true)")
        try:
            try:
                # savepoint: a refused insert must not abort the caller's
                # transaction, and the gate below must still be closed after it
                with conn.transaction():
                    row = conn.execute(
                        "INSERT INTO assertion (workspace_id, case_id, subject, predicate,"
                        " object_kind, object_entity, object_value, source_id, evidence,"
                        " derived_from, derived_by, recorded_at, valid_from, valid_to,"
                        " source_grade, info_grade, created_by)"
                        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                        " RETURNING id",
                        (
                            actor.workspace_id, case_id, draft.subject, draft.predicate,
                            "entity" if draft.object_entity else "literal",
                            draft.object_entity,
                            None if draft.object_entity else json.dumps(draft.object_value),
                            draft.source_id,
                            [bytes.fromhex(d) for d in draft.evidence],
                            draft.derived_from,
                            draft.derived_by.model_dump_json(),
                            datetime.now(timezone.utc),
                            draft.valid_from, draft.valid_to, source_grade, draft.info_grade,
                            actor.principal_id,
                        ),
                    ).fetchone()
            except psycopg.errors.ForeignKeyViolation as e:
                raise AssertionRejected(str(e).splitlines()[0]) from e
        finally:
            conn.execute("SELECT set_config('astral.writer', '', true)")

        aid = int(row["id"])
        audit.record(
            conn, actor, "assertion.write", case_id=case_id, target_kind="assertion",
            target_id=str(aid),
            detail={"predicate": draft.predicate, "plugin": draft.derived_by.plugin,
                    "version": draft.derived_by.version, "backend": draft.derived_by.backend,
                    "degraded": draft.derived_by.degraded},
        )
        return aid

    def write_many(
        self, conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID,
        drafts: list[AssertionDraft],
    ) -> list[int]:
        return [self.write(conn, actor, case_id, d) for d in drafts]

    # ------------------------------------------------------------ validation
    def _check_weakest_link(
        self, conn: psycopg.Connection, case_id: uuid.UUID, draft: AssertionDraft,
        source_grade: str,
    ) -> None:
        """A derived grade is the weakest link, never a product and never an
        upgrade (03 'Confidence propagation', rule 1)."""
        if not draft.derived_from:
            return
        rows = conn.execute(
            "SELECT id, source_grade, info_grade FROM assertion"
            " WHERE case_id = %s AND id = ANY(%s)",
            (case_id, draft.derived_from),
        ).fetchall()
        if len(rows) != len(set(draft.derived_from)):
            raise AssertionRejected("derived_from names an assertion not in this case")
        worst_source = max(r["source_grade"] for r in rows)       # 'F' > 'A' lexically
        worst_info = max(r["info_grade"] for r in rows)
        if source_grade < worst_source or draft.info_grade < worst_info:
            raise AssertionRejected(
                f"grade escalation: a derived assertion cannot be better than its weakest "
                f"upstream (upstream {worst_source}{worst_info}, "
                f"attempted {source_grade}{draft.info_grade})"
            )

    def _check_ai_limits(self, draft: AssertionDraft) -> None:
        """Invariant 10: the AI layer never writes an uncited assertion, merges
        two identities, invents an identifier or escalates a grade."""
        if draft.derived_by.kind != "llm":
            return
        if draft.predicate in IDENTITY_PREDICATES:
            raise AssertionRejected("AI layer may not assert identity; identity is a judgment")
        raw = draft.derived_by.params.get("citations") or []
        if not raw:
            raise AssertionRejected("AI-produced assertion has no citations")
        cites = [Citation.model_validate(c) for c in raw]
        if self.store is None:
            raise AssertionRejected("cannot verify AI citations without an evidence store")
        for c in cites:
            if c.artifact not in draft.evidence:
                raise AssertionRejected("citation names an artifact not listed as evidence")
            text = self.store.verified_bytes(bytes.fromhex(c.artifact)).decode("utf-8", "replace")
            if text[c.start : c.end] != c.text:
                raise AssertionRejected("citation span does not match the artifact text")
        if draft.predicate in IDENTIFIER_PREDICATES:
            value = str(draft.object_value)
            if not any(value in c.text for c in cites):
                raise AssertionRejected(
                    "AI layer may not invent an identifier: value is not in any cited span"
                )
        # LLM output is triage, not fact: its information grade can never be
        # better than 4 ("doubtful") without a human upgrading it via a new
        # analyst assertion.
        if draft.info_grade < 4:
            raise AssertionRejected("AI-produced assertion cannot claim info_grade better than 4")

    # -------------------------------------------------------------- retract
    def retract(
        self, conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID, assertion_id: int,
        reason: str, *, cascade: bool = True, batch_id: uuid.UUID | None = None,
    ) -> list[int]:
        """Retract an assertion and, by default, every assertion derived from it.
        Returns the ids newly retracted. Nothing is deleted (invariant 2)."""
        self.policy.authorize(conn, actor, "assertion.retract", case_id)
        if not reason.strip():
            raise AssertionRejected("a retraction needs a reason")
        exists = conn.execute(
            "SELECT 1 FROM assertion WHERE id = %s AND case_id = %s", (assertion_id, case_id)
        ).fetchone()
        if not exists:
            raise AssertionRejected("assertion not found in case")
        batch_id = batch_id or uuid.uuid4()
        todo, seen, done = [assertion_id], set(), []
        while todo:
            aid = todo.pop()
            if aid in seen:
                continue
            seen.add(aid)
            if conn.execute(
                "SELECT 1 FROM assertion_retraction WHERE assertion_id = %s", (aid,)
            ).fetchone() is None:
                conn.execute(
                    "INSERT INTO assertion_retraction (workspace_id, case_id, assertion_id,"
                    " retracted_at, retracted_by, reason, batch_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (actor.workspace_id, case_id, aid, datetime.now(timezone.utc),
                     actor.principal_id, reason, batch_id),
                )
                done.append(aid)
            if cascade:
                todo += [r["id"] for r in conn.execute(
                    "SELECT id FROM assertion WHERE case_id = %s AND %s = ANY(derived_from)",
                    (case_id, aid),
                )]
        audit.record(
            conn, actor, "assertion.retract", case_id=case_id, target_kind="assertion",
            target_id=str(assertion_id),
            detail={"reason": reason, "count": len(done), "batch_id": str(batch_id),
                    "cascade": cascade},
        )
        return done

    def retract_by_plugin(
        self, conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID, plugin: str,
        reason: str, *, version: str | None = None,
    ) -> tuple[uuid.UUID, list[int]]:
        """'This plugin was broken for a month': retract everything it asserted
        (03 §9). The derivation chain makes this tractable."""
        self.policy.authorize(conn, actor, "assertion.retract", case_id)
        batch = uuid.uuid4()
        rows = conn.execute(
            "SELECT a.id FROM assertion a LEFT JOIN assertion_retraction r ON r.assertion_id = a.id"
            " WHERE a.case_id = %s AND a.derived_by ->> 'plugin' = %s"
            " AND (%s::text IS NULL OR a.derived_by ->> 'version' = %s) AND r.id IS NULL"
            " ORDER BY a.id",
            (case_id, plugin, version, version),
        ).fetchall()
        done: list[int] = []
        for r in rows:
            done += self.retract(conn, actor, case_id, r["id"], reason, batch_id=batch)
        return batch, sorted(set(done))
