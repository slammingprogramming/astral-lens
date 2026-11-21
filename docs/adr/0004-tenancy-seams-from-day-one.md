# ADR-0004: Tenancy seams from day one, single-tenant defaults

**Status:** Accepted

## Context

Astral Lens targets two users whose needs differ structurally: a solo
investigator on one machine, and a team with compartmentalized cases, roles and
supervisory audit.

The usual approach is to build single-user first and add multi-tenancy when
demand appears. Retrofitting a tenancy boundary means touching every query in
the system, every index and every authorization path, with no test that proves
a query was not missed. The failure mode is silent cross-case data leakage,
which is the worst possible bug in this domain.

Conversely, forcing a solo investigator through workspace creation, role
assignment and a login ceremony to run one search on their own laptop is a
product failure of a different kind.

## Decision

The **schema and query layer carry the seam from the first migration**:
`workspace_id`, `case_id`, principals and role grants exist on every relevant
table, and every query is written against them.

The **solo profile collapses the seam at runtime**: one implicit workspace, one
implicit principal, no role checks, no login ceremony. The audit log still
runs, because its value to a lone investigator defending their own work is at
least as high as its value to a supervisor.

Switching a solo install to a team install is a configuration change plus a
user-creation step — never a migration.

## Consequences

- Marginally more verbose queries from the start.
- No migration pain, and no window during which cross-case leakage is possible.
- The case remains the single boundary for permissions, retention,
  compartmentalization and collection identity
  ([03 §7](../03-data-model.md)), which keeps those four concerns from drifting
  apart.
