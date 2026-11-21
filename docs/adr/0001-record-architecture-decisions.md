# ADR-0001: Record architecture decisions

**Status:** Accepted

## Context

Astral Lens makes several decisions that are expensive or impossible to
reverse: the assertion-log data model, the plugin boundary, the tenancy seam,
the commitment to run on 8 GB of VRAM. Future contributors will encounter these
as constraints and will reasonably want to change them. Without a record of why
they exist, each will be relitigated from scratch, and some will be undone by
someone who did not know what they were load-bearing for.

## Decision

Architecture decisions are recorded as numbered ADRs in `docs/adr/`, in
Michael Nygard's format: Context, Decision, Consequences. An ADR is immutable
once accepted; it is superseded by a later ADR rather than edited.

An ADR is written when a decision constrains future work, is costly to
reverse, or rejects an obvious alternative. Routine implementation choices do
not need one.

## Consequences

- Each irreversible decision has one citable location.
- Rejected alternatives are recorded with their reasons, which is usually the
  more useful half.
- Superseded ADRs stay in the repository, so the history of the design remains
  legible.
