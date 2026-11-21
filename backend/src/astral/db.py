"""Postgres access and migrations.

One Postgres, no second datastore (invariant 6). Migrations are plain ordered
SQL files applied once each inside a transaction.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


@dataclass(frozen=True)
class Actor:
    """Who is acting, within which workspace. The tenancy seam (ADR-0004)."""

    workspace_id: uuid.UUID
    principal_id: uuid.UUID
    kind: str = "user"


# The solo profile collapses the seam to one implicit workspace and principal.
# Fixed ids keep a solo install deterministic and make the team upgrade a
# configuration change plus user creation, never a migration.
_NS = uuid.UUID("6f1f3c52-3b0e-4c0e-9a57-5a1d2f9d6a10")
SOLO_WORKSPACE_ID = uuid.uuid5(_NS, "workspace:solo")
SOLO_PRINCIPAL_ID = uuid.uuid5(_NS, "principal:solo")
SOLO_ACTOR = Actor(SOLO_WORKSPACE_ID, SOLO_PRINCIPAL_ID, "user")


class Database:
    def __init__(self, dsn: str, *, max_size: int = 10) -> None:
        self.dsn = dsn
        self._pool = ConnectionPool(
            dsn,
            min_size=1,
            max_size=max_size,
            kwargs={"row_factory": dict_row},
            open=False,
        )

    def open(self) -> "Database":
        self._pool.open(wait=True, timeout=15)
        return self

    def close(self) -> None:
        self._pool.close()

    def __enter__(self) -> "Database":
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection]:
        """A transaction: commits on clean exit, rolls back on exception."""
        with self._pool.connection() as conn:
            yield conn

    # ---------------------------------------------------------------- migrate
    def migrate(self) -> list[str]:
        """Apply any unapplied migrations; returns the versions applied."""
        applied: list[str] = []
        files = sorted(
            (p for p in resources.files("astral").joinpath("migrations").iterdir()
             if p.name.endswith(".sql")),
            key=lambda p: p.name,
        )
        with self.connection() as conn:
            # Serialize concurrent migrators.
            conn.execute("SELECT pg_advisory_xact_lock(7311001)")
            have: set[str] = set()
            if conn.execute("SELECT to_regclass('schema_migration') AS t").fetchone()["t"]:
                have = {r["version"] for r in conn.execute("SELECT version FROM schema_migration")}
            for f in files:
                version = f.name[: -len(".sql")]
                if version in have:
                    continue
                conn.execute(f.read_text(encoding="utf-8"))
                applied.append(version)
        return applied

    # -------------------------------------------------------------- bootstrap
    def bootstrap_solo(self) -> Actor:
        """Create the implicit workspace and principal. Idempotent."""
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO workspace (id, name) VALUES (%s, 'solo') ON CONFLICT DO NOTHING",
                (SOLO_WORKSPACE_ID,),
            )
            conn.execute(
                "INSERT INTO principal (id, workspace_id, kind, name) "
                "VALUES (%s, %s, 'user', 'solo') ON CONFLICT DO NOTHING",
                (SOLO_PRINCIPAL_ID, SOLO_WORKSPACE_ID),
            )
        return SOLO_ACTOR
