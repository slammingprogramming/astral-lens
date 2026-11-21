"""Runtime configuration from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    dsn: str
    data_dir: Path
    profile: str = "solo"                  # deployment profile: solo | team (team needs M5 auth)
    commercial: bool = False               # declared commercial => licence preflight blocks
    plugin_runtime: str = "in_process"
    health_sweep_seconds: int = 0          # 0 = off; the API runs a sweep on this interval
    tika_url: str | None = None
    secrets_enabled: bool = False          # create the encrypted store when a passphrase is present
    llm_url: str | None = None             # OpenAI-compatible endpoint, e.g. a local llama.cpp server
    llm_model: str = "local"
    llm_egress: str = "local-only"         # local-only | hosted-direct (hosted-redacted is not built)
    repositories: tuple[str, ...] = ()      # plugin/pack repositories: directories or URLs serving index.json
    hub_egress: str = "local-only"          # local-only: remote repositories are refused | direct
    opensearch_url: str | None = None       # dedicated text search; unset = Postgres only
    opensearch_index: str = "astral-chunks"
    embed_model: str | None = None          # dense retrieval, e.g. intfloat/multilingual-e5-large (needs fastembed); unset = lexical only
    rerank_model: str | None = None         # cross-encoder reranker, e.g. BAAI/bge-reranker-v2-m3-int8
    embed_cache_dir: str | None = None
    qdrant_url: str | None = None           # dedicated face-vector search; unset = Postgres/pgvector only
    age_dsn: str | None = None              # Apache AGE database for the Cypher graph projection (holds a password: from the environment only)
    worm_endpoint: str | None = None        # S3-compatible endpoint with Object Lock; unset = no write-once preservation
    worm_bucket: str = "astral-evidence"
    worm_mode: str = "COMPLIANCE"            # COMPLIANCE (nobody can delete early) | GOVERNANCE (privileged accounts can)
    worm_retain_days: int = 365
    worm_region: str = "us-east-1"
    signing_key: str | None = None         # path to an Ed25519 private key PEM used to sign exhibits
    plugin_trust: str = "warn"             # warn: unsigned plugins install, flagged | require: trusted-key signatures only
    monitor_sweep_seconds: int = 0         # 0 = off: scheduled monitors contact third-party sources, so they are opt-in
    oidc_issuer: str | None = None         # team profile: tokens must come from this issuer
    oidc_audience: str | None = None       # ...and name this audience
    team_admins: tuple[str, ...] = ()      # OIDC subjects that receive a workspace-wide owner grant on first sight
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "[::1]", "testserver")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            dsn=os.environ.get("ASTRAL_DSN", "postgresql://postgres:astral@localhost:55432/astral"),
            data_dir=Path(os.environ.get("ASTRAL_DATA_DIR", "data")).resolve(),
            profile=os.environ.get("ASTRAL_PROFILE", "solo"),
            commercial=os.environ.get("ASTRAL_COMMERCIAL", "0") == "1",
            plugin_runtime=os.environ.get("ASTRAL_PLUGIN_RUNTIME", "in_process"),
            health_sweep_seconds=int(os.environ.get("ASTRAL_HEALTH_SWEEP_SECONDS", "3600")),
            tika_url=os.environ.get("ASTRAL_TIKA_URL") or None,
            secrets_enabled=os.environ.get("ASTRAL_SECRETS_ENABLED", "0") == "1",
            llm_url=os.environ.get("ASTRAL_LLM_URL") or None,
            llm_model=os.environ.get("ASTRAL_LLM_MODEL", "local"),
            llm_egress=os.environ.get("ASTRAL_LLM_EGRESS", "local-only"),
            repositories=tuple(x.strip() for x in os.environ.get("ASTRAL_REPOSITORIES", "").split(",") if x.strip()),
            hub_egress=os.environ.get("ASTRAL_HUB_EGRESS", "local-only"),
            opensearch_url=os.environ.get("ASTRAL_OPENSEARCH_URL") or None,
            opensearch_index=os.environ.get("ASTRAL_OPENSEARCH_INDEX", "astral-chunks"),
            embed_model=os.environ.get("ASTRAL_EMBED_MODEL") or None,
            rerank_model=os.environ.get("ASTRAL_RERANK_MODEL") or None,
            embed_cache_dir=os.environ.get("ASTRAL_EMBED_CACHE") or None,
            qdrant_url=os.environ.get("ASTRAL_QDRANT_URL") or None,
            age_dsn=os.environ.get("ASTRAL_AGE_DSN") or None,
            worm_endpoint=os.environ.get("ASTRAL_WORM_ENDPOINT") or None,
            worm_bucket=os.environ.get("ASTRAL_WORM_BUCKET", "astral-evidence"),
            worm_mode=os.environ.get("ASTRAL_WORM_MODE", "COMPLIANCE"),
            worm_retain_days=int(os.environ.get("ASTRAL_WORM_RETAIN_DAYS", "365")),
            worm_region=os.environ.get("ASTRAL_WORM_REGION", "us-east-1"),
            signing_key=os.environ.get("ASTRAL_SIGNING_KEY") or None,
            plugin_trust=os.environ.get("ASTRAL_PLUGIN_TRUST", "warn"),
            monitor_sweep_seconds=int(os.environ.get("ASTRAL_MONITOR_SWEEP_SECONDS", "0")),
            oidc_issuer=os.environ.get("ASTRAL_OIDC_ISSUER") or None,
            oidc_audience=os.environ.get("ASTRAL_OIDC_AUDIENCE") or None,
            team_admins=tuple(x.strip() for x in os.environ.get("ASTRAL_TEAM_ADMINS", "").split(",") if x.strip()),
            allowed_hosts=tuple(x.strip() for x in os.environ.get(
                "ASTRAL_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver").split(",") if x.strip()),
        )
