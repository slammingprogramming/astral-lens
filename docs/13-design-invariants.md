# 13 — Design invariants

The numbered invariants that source comments, tests and the other documents cite (for example
"invariant 10" in `ai.py`). They are architectural: breaking one is a design regression, not a
local bug. The numbers are stable; a new invariant takes the next number, and none is renumbered.

1. **Assertions are the system of record.** The entity graph is a disposable
   projection. Nothing writes to layer 1 except the assertion writer, and
   nothing writes without evidence and `derived_by`.
2. **Nothing is deleted, only retracted** — except the deliberate, logged,
   case-scoped hard-delete that exists for legal obligation.
3. **Unknown is never reported as negative.** A quarantined or failed source
   yields "unknown". A broken username checker reporting "no account found" is
   a false exculpatory finding and is the most dangerous output this system can
   produce.
4. **Degradation is visible.** Backend substitution is recorded in
   `derived_by` and shown in the UI. No silent fallback.
5. **No plugin imports a model library directly.** Model access is through
   declared backend interfaces, so CPU/GPU swapping stays a config change.
6. **No second datastore at the small tier.** One Postgres. This excluded
   Celery and shaped the orchestrator choice
   ([ADR-0003](adr/0003-orchestration.md)).
7. **The small-tier install must work with no container runtime present at
   all.**
8. **Source and model registries carry licence metadata**, and preflight warns
   before a non-commercial weight or dataset is used in a deployment that
   declared itself commercial ([ADR-0002](adr/0002-licensing.md)).
9. **Adding a data source requires a manifest and a function** — no core code,
   no UI code, no migration. If it doesn't, the extension point is wrong.
10. **The AI layer may never** write an uncited assertion, merge two
    identities, invent an identifier, or escalate a confidence grade.
11. **Anything that arrives from outside only proposes.** A lens, an AI plan, a
    resolver candidate and an MCP caller can propose; a person approves. The
    MCP server is read-only and marks what it returns as untrusted content.
12. **A signature proves who and that it is unaltered, never that it is
    correct.** An invalid signature is always refused; unsigned or untrusted is
    allowed but flagged. The platform never asserts that an exhibit is
    admissible.
13. **A site pack can only say "absent" through an explicit absent rule that
    matched, and only while its control account still resolves.** Everything
    else, including 403/429/5xx and truncated bodies, is unknown. Third-party
    packs stay in a separately licensed directory (CC BY-SA data is share-alike).

14. **Workbench data is the analyst's workspace, not evidence.** Annotations,
    saved views and machines never become assertions. Link analysis reports
    recorded links and never infers one. A machine is data that is proposed,
    then approved by a person (a definition cannot approve itself, and
    target-contact is decided at approval), then run under call and entity caps.
15. **Write-once copies outlive the case.** Erasing a case that has write-once
    copies is refused until the operator acknowledges that those copies cannot be
    deleted before their retention ends; the response lists them. Nothing may
    claim a bucket is immutable unless it was created with Object Lock.
16. **Dedicated search and vector services are rebuildable projections.**
    Postgres is the truth: a service must never show what Postgres no longer
    holds, a down service degrades visibly to Postgres, and biometric vectors
    leave the service when they leave Postgres. A deletion that fails is
    recorded, reported and retryable, never swallowed. Dense text vectors are
    the same kind of projection: derived from chunks, erased with the case, and
    a model that cannot load degrades to the lexical rankers with a note. The
    Apache AGE graph is one too: ids only, rebuilt when the case graph changes,
    queried read-only (checked, and enforced by a READ ONLY transaction), names
    re-read from Postgres, erased with the case.
17. **A stream or a remote file is collected, bounded and policy-checked like any fetch.**
    `parquet.query` reads a remote Parquet file only by range requests under hard
    byte, request, row-group and row budgets, refuses a server that ignores
    `Range`, and reports a budget stop as not fully read, never as empty.
    `ws.collect` obeys the case's egress policy, refuses proxy mode, stops at
    its time, message and byte caps, and never stores the subscription frame
    (it carries the API key). A connection that fails before any message is an
    error (unknown), never an empty answer; one that ends early keeps what
    arrived and records why it stopped.
