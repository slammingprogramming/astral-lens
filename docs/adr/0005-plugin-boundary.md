# ADR-0005: Plugins exchange messages, not objects; isolation is profile-aware

**Status:** Accepted

## Context

Two questions had to be settled together.

**Where is the boundary?** The surveyed tools agree in practice: Maltego,
SpiderFoot, IntelOwl, OpenCTI and theHarvester all compose at a data-exchange
boundary — a typed message — not at a language import. A plugin that imports
core objects couples itself to core internals and breaks on every refactor.

**How are plugins isolated?** An earlier draft of this design made
containers-per-plugin the universal default, on the usual isolation argument.
Research into the reference hardware showed that choice is wrong at the small
tier: on a 32 GB box already running Postgres, the API, workers and resident
models, a container per plugin spends RAM that models need — and it requires a
Docker socket or a rootless runner, itself a privilege boundary, on a personal
machine.

## Decision

1. **The boundary is a typed message.** Plugins declare `consumes` and
   `produces` types in a manifest and exchange JSON over a wire protocol
   (`invoke` / `emit` / `log` / `progress` / `result`). No plugin imports core
   objects, and **no plugin imports a model library directly** — model access
   is through declared backends.
2. **One plugin type, three orthogonal metadata axes:** stage (collection,
   enrichment, analysis, export), direction (inbound/outbound), and footprint
   (`none` / `third_party` / `dns` / `target_contact`). Each surveyed tool
   picked one axis as *the* taxonomy and then needed escape hatches; the axes
   are independent and are modelled as such.
3. **Runtime is profile-aware, not fixed.** In-process is the default for
   first-party, CPU-light plugins at small tiers. Containers are the default at
   the server and cluster tiers, and are used at any tier for third-party,
   dependency-heavy or untrusted plugins. **A small-tier install must be fully
   useful with no container runtime present at all.**

## Consequences

- The same plugin runs in either runtime unmodified, because the protocol is
  identical.
- Running untrusted code in-process at the small tier is an **accepted risk**.
  It is stated plainly rather than hidden; plugin signing mitigates part of it;
  and the operator can require containers at any tier.
- Adding a source requires a manifest and a function — no core code, no UI
  code, no migration. That is the test this boundary has to pass.
