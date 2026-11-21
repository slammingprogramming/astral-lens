# 12 — The hub: plugin registry and community repositories

**Status (2026-11-07): format, tooling, verification and install are built and tested against local
directories. No public community repository exists, and this project does not operate one.** Whoever
runs a repository decides what is in it; nothing here vets a publisher's intent.

## What a repository is

A directory (or a URL serving the same files):

```
index.json                      astral.repo/v1: name, generated_at, packages[]
packages/<kind>/<id>-<version>.zip
```

Four kinds: `plugin` (connectors and others), `site-pack`, `lens`, `machine`. Each index entry carries
id, version, title, description, licence, size, `sha256`, and an optional Ed25519 `signature` with the
signer's key fingerprint.

## Publishing

```bash
astral keygen --out publisher.pem
astral repo build ./my-repo --plugin plugins/connectors/etherscan --pack packs/example.json --key publisher.pem --name my-repo
astral repo verify ./my-repo       # re-reads every package: size, sha256, signature, trust verdict
astral repo search ./my-repo etherscan
```

The signature covers `(kind, id, version, sha256)`, so a package cannot be renamed, re-versioned or
swapped under a valid signature.

## Installing (API `/hub/*`, and the Hub tab)

Checks run in this order and the first failure stops the install:

1. **Size and sha256** match the index. This detects corruption or a swapped file. It cannot detect a
   malicious index: whoever writes the index writes the hashes.
2. **Signature against the operator's trust store**: `trusted`, `signed-untrusted` (valid, key unknown),
   `unsigned`, or `invalid` (always refused). A signature says who published it and that it is unaltered,
   never that the code is safe ([invariant 12](13-design-invariants.md)).
3. **Safe extraction**: absolute paths, `..`, links, device files and oversize archives are refused.
4. **Normal plugin review** (manifest, lint, in-package signature). A plugin installs **disabled**; a
   person enables it. A site pack is stored in a directory per licence so share-alike data never mixes
   into the original-work directory; the site-pack plugin validates it when it loads. Lenses and machines
   go through their own package verifiers and are saved marked as imported with their trust result; running
   a lens only proposes a plan, and a machine must be approved before it runs.
5. `ASTRAL_PLUGIN_TRUST=require` refuses anything not signed by a trusted key.

## Network

Repositories are listed in `ASTRAL_REPOSITORIES` (comma-separated directories or URLs). Remote ones are
read through the egress policy. The default is `local-only`: a non-local URL is refused, so installing the
hub does not by itself make the platform talk to the internet. Set `ASTRAL_HUB_EGRESS=direct` to allow it.
Updates are listed (`/hub/updates`) and never applied automatically.

## What was not verified

- A repository was served over HTTP only from a loopback test server; no repository on the real internet
  was read, and TLS handling is whatever the egress layer does.
- Key distribution is manual: confirm a publisher's fingerprint by a channel other than the repository.
- Revocation is "remove the key from the trust store". There is no revocation list.
