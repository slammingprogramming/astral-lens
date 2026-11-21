# Security policy

## Reporting a vulnerability

Please report it privately, not in a public issue or pull request:

**SimpleX:** <https://smp14.simplex.im/a#3gZ-zeHs4QrFZKLAN0o3SC_XQJXhj1eYBVTO_c0FAtg>
(open the link with the SimpleX app).

Say what you found, how to reproduce it, and which commit or version. Use synthetic data; do not
send case data, evidence, names or keys. This is a one-person, unfunded project: expect an
acknowledgement and an honest assessment, not a service-level agreement or a bounty.

## Supported versions

Astral Lens is pre-1.0 (`0.0.x`). Only the latest commit on `main` is supported; there are no
backported fixes.

## What counts

The properties this project claims, and so what a report can break:

- **Egress.** A request that leaves the machine without passing the case's egress policy, or a
  local-only language-model setting that reaches a non-local endpoint.
- **Evidence and audit integrity.** Altering or deleting an assertion, artifact, audit entry or
  exhibit without the break being detectable; a verifier that accepts a tampered bundle.
- **Plugin trust.** A plugin that runs without passing the signature, trust and lint checks it is
  documented to pass, imports a model library directly, or escapes its declared capabilities.
- **Access control.** Reading or writing another case or tenant's data; a role that can do more
  than it is documented to.
- **Injection and resource limits.** SQL, Cypher or command injection; bounded readers
  (`http.fetch`, `ws.collect`, `parquet.query`) exceeding their stated limits.
- **Secrets.** A key or token written to the database, a log, an audit entry or an exhibit in
  clear text.

Not in scope: the content of third-party data sources, the accuracy of a model's output (the
platform labels such output as unproven), and findings that need an attacker who already has the
operator's credentials or shell on the host.
