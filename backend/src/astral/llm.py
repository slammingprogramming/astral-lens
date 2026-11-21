"""A thin client for an OpenAI-compatible chat endpoint (a local llama.cpp server).

Egress is enforced HERE, in code, not in a prompt (docs/08 §0, docs/10 §6):
- `local-only` (the default) refuses any endpoint that is not loopback/private. A
  case whose own collection egress is `local-only` forces this regardless of
  the deployment setting.
- `hosted-direct` sends case text to a third party. It is allowed only when the
  operator set it explicitly, and never for a `local-only` case.
- `hosted-redacted` (redact-then-send) is NOT built, so it is refused rather than
  quietly treated as one of the others.

Nothing here has talked to a real model: the development machine has no llama.cpp
server and no weights. The tests use a scripted stand-in.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from astral import egress


class LlmError(Exception):
    pass


class LlmPolicyError(LlmError):
    pass


def check_endpoint(url: str, *, deployment_mode: str, case_identity: dict[str, Any] | None) -> str:
    """-> the effective mode, or raises LlmPolicyError. Called before every request."""
    if deployment_mode == "hosted-redacted":
        raise LlmPolicyError("egress mode 'hosted-redacted' is not implemented; use 'local-only' or 'hosted-direct'")
    if deployment_mode not in ("local-only", "hosted-direct"):
        raise LlmPolicyError(f"unknown LLM egress mode {deployment_mode!r}")
    case_local = (case_identity or {}).get("egress") == "local-only"
    host = urlsplit(url).hostname or ""
    local = egress.is_local_host(host)
    if (deployment_mode == "local-only" or case_local) and not local:
        why = "this case is local-only" if case_local else "the deployment is local-only"
        raise LlmPolicyError(f"{why}; the model endpoint {host!r} is not on this machine, so it was not contacted")
    return "local-only" if local else "hosted-direct"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class LlmClient:
    name = "openai-compatible"

    def __init__(self, base_url: str, *, model: str = "local", timeout: float = 180.0, api_key: str | None = None,
                 deployment_mode: str = "local-only", case_identity: dict[str, Any] | None = None):
        self.base_url = base_url.rstrip("/")
        self.model, self.timeout, self.api_key = model, timeout, api_key
        self.mode = check_endpoint(self.base_url, deployment_mode=deployment_mode, case_identity=case_identity)

    def chat_json(self, system: str, user: str, schema: dict[str, Any], *, max_tokens: int = 1200) -> dict[str, Any]:
        """One call with constrained JSON decoding (llama.cpp honours `response_format` json_schema)."""
        body = {"model": self.model, "temperature": 0, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": {"type": "json_schema", "json_schema": {"name": "out", "schema": schema, "strict": True}}}
        req = urllib.request.Request(self.base_url + "/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})})
        try:
            with urllib.request.build_opener(_NoRedirect).open(req, timeout=self.timeout) as r:
                data = json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise LlmError(f"model endpoint failed: {type(e).__name__}: {e}") from e
        try:
            return json.loads(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, ValueError, TypeError) as e:
            raise LlmError("model returned something that is not the requested JSON") from e
