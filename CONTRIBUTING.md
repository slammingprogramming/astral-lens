# Contributing

Thanks for helping. Astral Lens is AGPL-3.0-or-later; by contributing you agree your work is
licensed the same way.

## Before you start

- Read the [design invariants](docs/13-design-invariants.md). They recur across the source and the
  documents; breaking one is a design regression, not a local bug. The short version: assertions
  are the record, unknown is never reported as negative, degradation is visible, and the AI layer
  never merges identities, invents identifiers or writes uncited assertions.
- Read the relevant status block in [docs/11-roadmap.md](docs/11-roadmap.md). Each one separates what
  was verified from what was not; do not claim more than it does.
- For a large change, open an issue first. For a security problem, see [SECURITY.md](SECURITY.md).

## Setting up

```bash
docker run -d --name astral-pg-dev -e POSTGRES_PASSWORD=astral -p 55432:5432 postgres:16-alpine
cd backend
python -m venv .venv && .venv/bin/pip install -e ".[dev]"     # .venv\Scripts\ on Windows
.venv/bin/python -m pytest

cd ../frontend && npm ci && npm run build
```

Optional backends are not dependencies; their tests skip when the backend is absent. The extras are
listed in `backend/pyproject.toml`. Tests that need a Qdrant or Apache AGE server read
`ASTRAL_TEST_QDRANT` / `ASTRAL_TEST_AGE`; face tests read `ASTRAL_FACE_TEST_DIR` (never commit
photographs).

## What a good change looks like

- **Tests.** A behaviour change comes with a test; a bug fix with a regression test. Tests use
  synthetic data and local fixture servers, never live services and never real people.
- **Connectors and plugins.** A connector is a manifest plus a function ([docs/04](docs/04-plugin-system.md)).
  Write it from the service's documentation or a live check you ran, never from memory. A source
  that cannot be asked reports *unknown*, not an empty result. Record what was run live and what
  was only tested against fixtures.
- **Dependencies and models.** Check the licence of the code *and* of any weights or dataset
  before adding it. Weights often differ from the repository's licence. A model goes in
  `backend/src/astral/data/models.yaml` with its licence class and commercial-use flag.
- **No personal data.** No case data, real names, keys, tokens or machine-identifying details in
  code, tests, fixtures or docs.
- **Docs.** State what was verified and what was not, in plain words.

## Pull requests

Keep each pull request to one change, fill in the template, and make sure CI passes (backend tests
on Python 3.11 and 3.12, and the frontend type-check and build).
