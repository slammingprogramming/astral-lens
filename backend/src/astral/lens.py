"""Lenses: reusable, shareable methodology (02 §4).

A lens is a bounded pipeline (the same read-only step types the planner may use) with named
parameters and a question template. Running one does NOT run anything: it fills the parameters
and proposes a plan, which must be approved like any other, so a lens received from someone else
can never act without the operator seeing and approving what it will do.

Scope today. A lens uses the AI layer's read-only step types: search_text, entity_facts,
artifact_facts (what is recorded about one file, including regions a geolocation run recorded)
and face_search (case faces and watchlist, results kept apart as candidates). Reverse-image search
is not a step: it needs a paid external service, so it would not be read-only. The acceptance
scenarios that a read-only plan can express ship as built-in lenses (builtin.py); "name to
network" contacts registries and ships as a built-in machine instead.

Sharing. A lens exports as a package with the hash of its definition and, if the exporter has a
signing key, an Ed25519 signature. Import verifies it against the operator's trust store. A
signature shows who published it and that it is unaltered; it does not make a lens correct. An
invalid signature is refused; unsigned and untrusted ones import, flagged. A lens built from an
existing plan must replace the case's own names with parameters, and the replacement is checked,
but a person should still read a lens before sharing it: nothing can prove it holds no case detail.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

import psycopg

from astral import ai, audit, signing
from astral.db import Actor
from astral.plugins.trust import TrustStore

SCHEMA = "astral.lens/v1"
PACKAGE = "astral.lens-package/v1"
_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MAX_INPUTS = 5


class LensError(ValueError):
    pass


def canonical(defn: dict[str, Any]) -> bytes:
    return json.dumps(defn, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(defn: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(defn)).hexdigest()


def _fill(text: str, values: dict[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], text)


def _used(defn: dict[str, Any]) -> set[str]:
    found: set[str] = set(_PLACEHOLDER.findall(defn.get("question", "")))
    for s in defn.get("steps", []):
        for v in (s.get("args") or {}).values():
            if isinstance(v, str):
                found |= set(_PLACEHOLDER.findall(v))
    return found


def validate(defn: Any) -> dict[str, Any]:
    if not isinstance(defn, dict) or defn.get("schema") != SCHEMA:
        raise LensError(f"not a {SCHEMA} definition")
    if not _NAME.match(str(defn.get("lens", ""))):
        raise LensError("lens name must be lowercase letters, digits, '.', '_' or '-' (max 64)")
    if not isinstance(defn.get("version"), int) or defn["version"] < 1:
        raise LensError("version must be an integer >= 1")
    inputs = defn.get("inputs")
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= MAX_INPUTS or any(not re.fullmatch(r"[a-z_][a-z0-9_]*", str(i)) for i in inputs) \
            or len(set(inputs)) != len(inputs):
        raise LensError(f"inputs must be 1-{MAX_INPUTS} distinct lowercase names")
    if not isinstance(defn.get("question"), str) or not 3 <= len(defn["question"]) <= 300:
        raise LensError("a question template of 3-300 characters is required")
    if len(str(defn.get("description", ""))) > 500:
        raise LensError("description is limited to 500 characters")
    used = _used(defn)
    if used - set(inputs):
        raise LensError(f"placeholders without an input: {sorted(used - set(inputs))}")
    if set(inputs) - used:
        raise LensError(f"inputs never used: {sorted(set(inputs) - used)}")
    try:
        # a 64-hex stand-in satisfies every argument type, including a file's sha256
        ai.validate_plan({"steps": json.loads(json.dumps(_fill_steps(defn, {i: "0" * 64 for i in inputs})))})
    except ai.PlanError as e:
        raise LensError(f"steps are not a valid plan: {e}") from e
    return defn


def _fill_steps(defn: dict[str, Any], values: dict[str, str]) -> list[dict[str, Any]]:
    return [{**s, "args": {k: (_fill(v, values) if isinstance(v, str) else v) for k, v in (s.get("args") or {}).items()}}
            for s in defn["steps"]]


def instantiate(defn: dict[str, Any], inputs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """(question, plan) for these inputs. Nothing runs."""
    missing = [i for i in defn["inputs"] if i not in inputs]
    extra = [k for k in inputs if k not in defn["inputs"]]
    if missing or extra:
        raise LensError(f"inputs must be exactly {defn['inputs']} (missing {missing}, unexpected {extra})")
    vals = {k: str(v).strip() for k, v in inputs.items()}
    if any(not v or len(v) > 200 or "\n" in v for v in vals.values()):
        raise LensError("each input must be 1-200 characters on one line")
    try:
        plan = ai.validate_plan({"steps": json.loads(json.dumps(_fill_steps(defn, vals)))})
    except ai.PlanError as e:
        raise LensError(str(e)) from e
    return _fill(defn["question"], vals), plan


def from_plan(question: str, plan: dict[str, Any], *, name: str, version: int, description: str, parameters: dict[str, str]) -> dict[str, Any]:
    """Turn a finished plan into a lens by replacing the case's own words with parameters."""
    if not parameters:
        raise LensError("name at least one parameter: a lens with none would carry this case's own wording")
    steps = json.loads(json.dumps(plan["steps"]))
    q = question
    for param, literal in parameters.items():
        if not literal.strip():
            raise LensError(f"parameter {param!r} has no value to replace")
        marker = "{" + param + "}"
        found = literal in q
        q = q.replace(literal, marker)
        for s in steps:
            for k, v in list(s["args"].items()):
                if isinstance(v, str) and literal in v:
                    s["args"][k] = v.replace(literal, marker)
                    found = True
        if not found:
            raise LensError(f"{literal!r} does not appear in the plan, so {param!r} would parameterise nothing")
    defn = {"schema": SCHEMA, "lens": name, "version": version, "description": description, "inputs": list(parameters),
            "question": q, "steps": steps}
    validate(defn)
    for literal in parameters.values():                                # the case's words must not survive anywhere
        if literal in json.dumps(defn, ensure_ascii=False):
            raise LensError(f"{literal!r} is still present in the lens")
    return defn


# ------------------------------------------------------------------ storage
def save(conn: psycopg.Connection, actor: Actor, defn: dict[str, Any], *, origin: str = "local",
         trust: dict[str, Any] | None = None) -> uuid.UUID:
    validate(defn)
    h = digest(defn)
    row = conn.execute("SELECT id, sha256 FROM lens WHERE workspace_id=%s AND name=%s AND version=%s",
                       (actor.workspace_id, defn["lens"], defn["version"])).fetchone()
    if row:
        if row["sha256"] == h:
            return row["id"]
        raise LensError(f"{defn['lens']} v{defn['version']} already exists with different content; publish a new version")
    lid = uuid.uuid4()
    conn.execute("INSERT INTO lens (id, workspace_id, name, version, definition, sha256, origin, trust, created_by)"
                 " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                 (lid, actor.workspace_id, defn["lens"], defn["version"], json.dumps(defn), h, origin,
                  json.dumps(trust or {}), actor.principal_id))
    audit.record(conn, actor, "lens.save", target_kind="lens", target_id=f"{defn['lens']}@{defn['version']}",
                 detail={"origin": origin, "sha256": h, "trust": (trust or {}).get("status")})
    return lid


def get(conn: psycopg.Connection, actor: Actor, name: str, version: int | None = None) -> dict[str, Any]:
    rows = conn.execute("SELECT * FROM lens WHERE workspace_id=%s AND name=%s AND (%s::int IS NULL OR version=%s)"
                        " ORDER BY version DESC LIMIT 1", (actor.workspace_id, name, version, version)).fetchall()
    if not rows:
        raise LookupError(f"no lens {name}" + (f" v{version}" if version else ""))
    return rows[0]


def export(row: dict[str, Any], signing_key: tuple[bytes, str | None] | None = None, *, package: str = PACKAGE) -> dict[str, Any]:
    defn = row["definition"]
    pkg: dict[str, Any] = {"schema": package, "definition": defn, "sha256": digest(defn), "signature": None}
    if signing_key:
        pub = signing.public_from_private(*signing_key)
        pkg["signature"] = {"algorithm": "ed25519", "signature": signing.sign(signing_key[0], canonical(defn), signing_key[1]),
                            "public_key": pub.decode(), "key_fingerprint": signing.fingerprint(pub)}
    return pkg


def verify_package(pkg: Any, trust: TrustStore | None, *, package: str = PACKAGE,
                   validator: Any = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """(definition, trust verdict). Raises LensError for a package that must not be imported.
    `package` and `validator` let other signed-definition kinds (machines) share this check."""
    if not isinstance(pkg, dict) or pkg.get("schema") != package or not isinstance(pkg.get("definition"), dict):
        raise LensError(f"not a {package}")
    defn = (validator or validate)(pkg["definition"])
    if digest(defn) != pkg.get("sha256"):
        raise LensError("the definition does not match its recorded hash: the package was altered")
    sig = pkg.get("signature")
    if not sig:
        return defn, {"status": "unsigned", "detail": "no signature: the publisher is unknown"}
    try:
        pub = sig["public_key"].encode()
        fp = signing.fingerprint(pub)
        ok = signing.verify(pub, canonical(defn), sig["signature"])
    except (KeyError, signing.SigningError, AttributeError) as e:
        raise LensError(f"unreadable signature: {e}") from e
    if not ok:
        raise LensError("the signature does not verify")
    if trust and fp in trust.keys:
        return defn, {"status": "trusted", "fingerprint": fp, "detail": f"signed by trusted key '{trust.keys[fp]}'"}
    return defn, {"status": "signed-untrusted", "fingerprint": fp, "detail": "valid signature by a key not in the trust store"}
