## What and why

## Checklist

- [ ] Tests added or updated; `python -m pytest` passes (in `backend/`) and `npm run build` passes (in `frontend/`).
- [ ] Respects the [design invariants](../docs/13-design-invariants.md). In particular: an unreachable source is *unknown*, never "no results"; a degraded backend is visible; nothing the AI layer produces is written as an assertion.
- [ ] A new dependency or model has its licence checked (and recorded in `backend/src/astral/data/models.yaml` for a model). Code under a copyleft or non-commercial licence is called out here.
- [ ] A new connector says what was run live and what was only tested against fixtures.
- [ ] No case data, names, keys or personal information in the diff or the tests.
- [ ] Docs updated where behaviour changed (the status blocks in `docs/11-roadmap.md` separate verified from unverified).

By contributing you agree your contribution is licensed under AGPL-3.0-or-later, like the rest of the project.
