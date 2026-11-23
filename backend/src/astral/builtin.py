"""Built-in lenses and machines: the acceptance scenarios (doc 01) shipped as definitions.

They are files in the package (data/lenses, data/machines), validated like any other definition,
and installed into a workspace only when an operator asks. Installing saves them with origin
"builtin"; running them is the same as running any lens or machine, so a built-in lens still only
PROPOSES a plan and a built-in machine still waits for approval, and connectors that contact the
subject still need it allowed at approval.

What ships:
  photo-candidates   (scenario 1) face search over the case and watchlist, plus what is recorded about the photo
  media-dossier      (scenario 2) what the pipelines recorded about one video or image
  name-to-network    (scenario 3) sanctions, LEI and registry lookups, then the companies and people they found
  shared-identifier  (scenario 4) directors of companies that share a phone number, email, IBAN or address
Scenario 5 (case to exhibit) is the report export, not a pipeline.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

import psycopg

from astral import lens, machines
from astral.db import Actor

KINDS = {"lens": ("lenses", lens.validate, "lens"), "machine": ("machines", machines.validate, "machine")}
TRUST = {"status": "builtin", "detail": "shipped with Astral Lens; the sha256 shows it is the definition this release ships"}


class BuiltinError(LookupError):
    pass


def definitions(kind: str) -> dict[str, dict[str, Any]]:
    """name -> validated definition for one kind."""
    if kind not in KINDS:
        raise BuiltinError(f"kind is one of {sorted(KINDS)}")
    folder, validate, key = KINDS[kind]
    out: dict[str, dict[str, Any]] = {}
    for f in sorted(resources.files("astral").joinpath("data", folder).iterdir(), key=lambda p: p.name):
        if f.name.endswith(".json"):
            d = validate(json.loads(f.read_text(encoding="utf-8")))
            out[d[key]] = d
    return out


def catalogue(conn: psycopg.Connection, actor: Actor) -> list[dict[str, Any]]:
    rows = []
    for kind, (_, _, key) in KINDS.items():
        table = "lens" if kind == "lens" else "machine"
        for name, d in definitions(kind).items():
            h = lens.digest(d)
            have = conn.execute(f"SELECT sha256 FROM {table} WHERE workspace_id=%s AND name=%s AND version=%s",   # noqa: S608
                                (actor.workspace_id, name, d["version"])).fetchone()
            rows.append({"kind": kind, "name": name, "version": d["version"], "description": d.get("description", ""),
                         "sha256": h, "installed": bool(have and have["sha256"] == h),
                         "conflict": bool(have and have["sha256"] != h)})
    return rows


def install(conn: psycopg.Connection, actor: Actor, kind: str, name: str) -> dict[str, Any]:
    defs = definitions(kind)
    if name not in defs:
        raise BuiltinError(f"no built-in {kind} {name!r}; built-in {kind}s: {sorted(defs)}")
    d = defs[name]
    if kind == "lens":
        lens.save(conn, actor, d, origin="builtin", trust=TRUST)
    else:
        machines.save(conn, actor, d, origin="builtin", trust=TRUST)
    return {"kind": kind, "name": name, "version": d["version"], "sha256": lens.digest(d)}
