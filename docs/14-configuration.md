# 14 — Configuration

Astral Lens reads its settings from environment variables when it starts (`backend/src/astral/config.py`).
Nothing is read from a file the web UI can edit, and nothing that holds a secret is ever stored in the database.
Unset means off: every optional service falls back to the Postgres-only behaviour and says so in the result.

## Core

| Variable | Default | Meaning |
|---|---|---|
| `ASTRAL_DSN` | `postgresql://postgres:astral@localhost:55432/astral` | The Postgres database. The default is the development container; change the password for anything real. |
| `ASTRAL_DATA_DIR` | `data` | Where evidence bytes live (content-addressed files). Back this up with the database. |
| `ASTRAL_PROFILE` | `solo` | `solo` (one person, no sign-in) or `team` (every request carries a verified token). |
| `ASTRAL_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1],testserver` | `Host` headers the API accepts (DNS-rebinding guard). Add your public host name when serving a team. |
| `ASTRAL_UI_DIR` | `frontend/dist` | The built UI, served at `/ui/` when present. |
| `ASTRAL_COMMERCIAL` | `0` | Set to `1` if the deployment is commercial: the licence preflight then refuses non-commercial models. |
| `ASTRAL_PLUGIN_RUNTIME` | `in_process` | `in_process` or `container` (see [04](04-plugin-system.md)). `ASTRAL_PLUGIN_IMAGE` and `ASTRAL_PLUGIN_TIMEOUT` tune the container runtime. |
| `ASTRAL_PLUGIN_TRUST` | `warn` | `warn` installs unsigned plugins and flags them; `require` accepts only keys in the trust store. |
| `ASTRAL_HEALTH_SWEEP_SECONDS` | `3600` | Connector health sweep interval; `0` turns it off. |
| `ASTRAL_MONITOR_SWEEP_SECONDS` | `0` | Scheduled monitors contact third-party sources, so they are opt-in. |

## Models and search services

| Variable | Meaning |
|---|---|
| `ASTRAL_LLM_URL`, `ASTRAL_LLM_MODEL` | An OpenAI-compatible endpoint (for example a local llama.cpp server). Unset: every AI feature says it is unavailable. |
| `ASTRAL_LLM_EGRESS` | `local-only` (default; a non-local endpoint is refused) or `hosted-direct`. `hosted-redacted` is not built. |
| `ASTRAL_EMBED_MODEL`, `ASTRAL_RERANK_MODEL`, `ASTRAL_EMBED_CACHE` | Dense retrieval and reranking through `fastembed` (`pip install .[dense]`). Unset: lexical retrieval only. |
| `ASTRAL_OPENSEARCH_URL`, `ASTRAL_OPENSEARCH_INDEX`, `ASTRAL_OPENSEARCH_AUTH` | A dedicated text ranker added to the fusion. `ASTRAL_OPENSEARCH_AUTH` is `user:password` for Basic auth. |
| `ASTRAL_QDRANT_URL`, `ASTRAL_QDRANT_API_KEY` | Face-vector (and optionally dense-vector) search in Qdrant. |
| `ASTRAL_SERVICES_CA_FILE` | A private CA certificate (PEM) to trust for `https://` OpenSearch and Qdrant. Certificate verification is never switched off. |
| `ASTRAL_AGE_DSN` | An Apache AGE database for the Cypher graph projection. It holds a password, so it comes from the environment only. |
| `ASTRAL_TIKA_URL` | Apache Tika for document types the built-in parsers do not cover. |
| `ASTRAL_FACE_MODELS_DIR`, `ASTRAL_INSIGHTFACE_DIR` | Face detector/recogniser files (see [06](06-multimodal-pipelines.md) for licences). |
| `ASTRAL_DETECT_MODELS_DIR`, `ASTRAL_DETECT_THREADS` | Where the object detector's files are (`rtdetr_r18vd/model.onnx` and `config.json`; default `models/detect`), and how many CPU threads it may use (default 4). Never downloaded automatically. `pip install .[detect]`; video also needs `ffmpeg` on the PATH. |
| `ASTRAL_GEOCLIP_CLIP_DIR`, `ASTRAL_GEOCLIP_THREADS` | The region-guess model (optional, off unless the caller opts in): a local copy of `openai/clip-vit-large-patch14` (it is never downloaded at run time) and the CPU threads it may use (default 4). Needs `torch`, `transformers` and `geoclip`, which are not project dependencies. |
| `ASTRAL_WHISPER_DIR`, `ASTRAL_PARAKEET_DIR`, `ASTRAL_SILERO_VAD_PATH`, `ASTRAL_PYANNOTE_DIR`, `ASTRAL_MADLAD_DIR` | Speech, voice-activity, diarisation and translation models. |

## Evidence preservation, signing, secrets

| Variable | Meaning |
|---|---|
| `ASTRAL_WORM_ENDPOINT`, `ASTRAL_WORM_BUCKET`, `ASTRAL_WORM_MODE`, `ASTRAL_WORM_RETAIN_DAYS`, `ASTRAL_WORM_REGION`, `ASTRAL_WORM_ACCESS_KEY`, `ASTRAL_WORM_SECRET_KEY` | An S3-compatible bucket with Object Lock for write-once preservation. `COMPLIANCE` mode means nobody can delete early. |
| `ASTRAL_SIGNING_KEY`, `ASTRAL_SIGNING_PASSPHRASE` | An Ed25519 private key (PEM) that signs exhibits, and its passphrase. |
| `ASTRAL_SECRETS_ENABLED`, `ASTRAL_SECRETS_PASSPHRASE` | The encrypted store for connector API keys. Created only when a passphrase is present. |
| `ASTRAL_REPOSITORIES`, `ASTRAL_HUB_EGRESS` | Plugin repositories (directories or URLs serving `index.json`). Remote ones are refused unless `ASTRAL_HUB_EGRESS=direct`. |

## Team sign-in (OpenID Connect)

The team profile validates a bearer token on every request (RS256 or ES256 only; `none` and HS algorithms are refused).
New people hold **no roles**: signing in says who you are, not what you may do.

| Variable | Meaning |
|---|---|
| `ASTRAL_OIDC_ISSUER` | The identity provider's issuer URL. Discovery is read from `<issuer>/.well-known/openid-configuration`. Plain `http://` is accepted only for a loopback address. |
| `ASTRAL_OIDC_AUDIENCE` | The audience a token must name. |
| `ASTRAL_OIDC_CLIENT_ID` | Set this to turn on browser sign-in. The UI signs in as a **public client** (authorization code with PKCE S256; no secret). |
| `ASTRAL_OIDC_SCOPE` | Default `openid profile email`. |
| `ASTRAL_OIDC_BROWSER_TOKEN` | `id_token` (default; its `aud` is the client id) or `access_token` (its `aud` must equal `ASTRAL_OIDC_AUDIENCE`). |
| `ASTRAL_TEAM_ADMINS` | Comma-separated **subjects** (the token's `sub` claim, not an e-mail address) that receive a workspace-wide owner grant the first time they appear. Everyone else is granted roles by an owner. |
| `ASTRAL_MCP_TOKEN` | For the MCP server in the team profile: an OIDC token, validated on every call. |

Setting up the provider (these are the steps that were run against Keycloak 26):

1. Create a **public** client, `Standard flow` on, `Direct access grants` and `Implicit flow` off, PKCE method `S256`.
2. Valid redirect URI: `https://<your host>/ui/`. Web origin: `https://<your host>` (the browser posts to the token endpoint, so the provider must allow your origin there).
3. Start Astral with `ASTRAL_PROFILE=team`, `ASTRAL_OIDC_ISSUER`, `ASTRAL_OIDC_AUDIENCE` and `ASTRAL_OIDC_CLIENT_ID` set to the client id.
4. Sign in once, then read your `sub` (in Keycloak: the user's ID on its admin page), put it in `ASTRAL_TEAM_ADMINS`, and **start from a fresh sign-in**:
   the admin grant is written on first sight only, so a person who signed in before being named stays without roles until an owner grants them one.

Limits: there is no token refresh (a session ends when the token expires, and the UI then returns to the sign-in prompt; with the
provider's own session still alive that is one click), and sign-out clears only the local token, not the provider's session.
Keep token lifetimes above a minute: the UI treats a token with under 30 seconds left as expired.
