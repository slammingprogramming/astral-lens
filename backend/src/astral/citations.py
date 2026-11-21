"""Citation enforcement for model-written (or extractive) answers.

A claim is {text, cites:[{evidence_id, quote}]}. The model never supplies spans or
assertion ids of its own: it names an evidence item we handed it and quotes from it,
and OUR code finds the quote, computes the span, and re-reads the stored bytes to
confirm they say that. Checks, in order:

 1. existence   the evidence id was one we supplied, the artifact is in this case,
                an assertion cited is real and not retracted
 2. span        the quote occurs verbatim (whitespace-insensitive) in the evidence
                text, and the located range matches the stored artifact
 3. identifiers every identifier-like token in the claim (IBAN, e-mail, long number,
                ISO date, URL) appears in a cited quote: a claim cannot introduce one
 4. entailment  pluggable verifiers decide whether the quotes SUPPORT the claim

Verdicts: `verified` (a model verifier agreed), `weakly_verified` (only the lexical
check ran: word overlap is NOT entailment), `rejected`. A deterministic share of
verified claims is flagged for human review. With no verifier model installed, the
best a claim can be is `weakly_verified`, and the output says so.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from typing import Any, Protocol

import psycopg

from astral.db import Actor
from astral.evidence import EvidenceStore

IDENT = re.compile(
    r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}"                                   # IBAN-shaped
    r"|[\w.+-]+@[\w-]+(?:\.[\w-]+)+"                                  # e-mail
    r"|https?://\S+"                                                  # URL
    r"|\b\d{4}-\d{2}(?:-\d{2})?\b"                                    # ISO date
    r"|\b\d[\d,.]{3,}\d\b"                                            # a number of 5+ chars
    r"|\b[A-Z0-9]{20}\b")                                             # LEI-shaped
_STOP = set("the a an and or of to in on for with by is are was were be been this that these those from at as it its "
            "has have had not but which who whom their there than then into about over under also".split())
HUMAN_REVIEW_SHARE = 0.2


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s)).strip().casefold()


class Verifier(Protocol):
    name: str
    weak: bool

    def check(self, claim: str, quotes: list[str]) -> tuple[bool | None, str]: ...


class LexicalVerifier:
    """Content-word overlap. Cheap and deterministic, and WEAK: a claim that reuses the
    quote's words while reversing its meaning passes. It catches invention, not distortion."""

    name, weak = "lexical-support", True

    def check(self, claim: str, quotes: list[str]) -> tuple[bool | None, str]:
        have = {w[:5] for q in quotes for w in re.findall(r"\w+", norm(q)) if len(w) >= 4}
        want = [w[:5] for w in re.findall(r"\w+", norm(claim)) if len(w) >= 4 and w not in _STOP]
        if not want:
            return True, "no content words to check"
        frac = sum(w in have for w in want) / len(want)
        return frac >= 0.6, f"{frac:.0%} of the claim's content words appear in the cited text"


class LlmVerifier:
    """Asks a model whether the quotes alone state the claim. Two instances with different
    wording give the 'two verifiers' the design calls for. Not run against a real model here."""

    weak = False
    SCHEMA = {"type": "object", "properties": {"supported": {"type": "boolean"}, "reason": {"type": "string"}},
              "required": ["supported", "reason"], "additionalProperties": False}
    PROMPTS = {"strict": "Answer supported=true ONLY if the quoted text explicitly states the claim. Inference, "
                         "background knowledge and plausibility do not count.",
               "contradiction": "Answer supported=false if the quoted text contradicts the claim, is about someone or "
                                "something else, or only partly covers it. Otherwise supported=true."}

    def __init__(self, client: Any, variant: str = "strict"):
        self.client, self.variant, self.name = client, variant, f"llm-verifier:{variant}"

    def check(self, claim: str, quotes: list[str]) -> tuple[bool | None, str]:
        try:
            r = self.client.chat_json(self.PROMPTS[self.variant], "CLAIM: " + claim + "\nQUOTES:\n- " + "\n- ".join(quotes),
                                      self.SCHEMA, max_tokens=200)
        except Exception as e:                                      # noqa: BLE001 - verifier down: unknown, not 'true'
            return None, f"verifier unavailable: {e}"
        return bool(r.get("supported")), str(r.get("reason", ""))[:200]


def _locate(quote: str, text: str) -> tuple[int, int] | None:
    words = quote.split()
    if not words:
        return None
    m = re.search(r"\s+".join(re.escape(w) for w in words), text)
    return (m.start(), m.end()) if m else None


def _needs_review(claim_text: str) -> bool:
    return int(hashlib.sha256(claim_text.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < HUMAN_REVIEW_SHARE


def verify_claim(conn: psycopg.Connection, store: EvidenceStore, actor: Actor, case_id: uuid.UUID,
                 claim: dict[str, Any], evidence: dict[str, dict[str, Any]], verifiers: list[Verifier]) -> dict[str, Any]:
    reasons: list[str] = []
    quotes: list[str] = []
    anchors: list[dict[str, Any]] = []
    cites = claim.get("cites") or []
    if not cites:
        return {"status": "rejected", "reasons": ["no citation"], "anchors": [], "checks": []}
    for c in cites:
        e = evidence.get(str(c.get("evidence_id")))
        q = str(c.get("quote") or "").strip()
        if e is None:
            reasons.append(f"unknown evidence id {c.get('evidence_id')!r}")
            continue
        if e["kind"] == "passage":
            hit = _locate(q, e["text"])
            if hit is None:
                reasons.append(f"quote not found in {e['id']}")
                continue
            a, b = e["start"] + hit[0], e["start"] + hit[1]
            try:
                stored = store.read(conn, actor, case_id, bytes.fromhex(e["text_sha"])).decode("utf-8", "replace")
            except Exception:                                       # noqa: BLE001
                reasons.append(f"artifact for {e['id']} is not in this case")
                continue
            if norm(stored[a:b]) != norm(q):
                reasons.append(f"stored text at {a}-{b} does not match the quote")
                continue
            quotes.append(stored[a:b])
            anchors.append({"kind": "span", "evidence_id": e["id"], "text_artifact": e["text_sha"], "start": a, "end": b})
        elif e["kind"] == "fact":
            row = conn.execute(
                "SELECT 1 FROM assertion a LEFT JOIN assertion_retraction r ON r.assertion_id=a.id"
                " WHERE a.id=%s AND a.case_id=%s AND r.id IS NULL", (e["assertion_id"], case_id)).fetchone()
            if not row:
                reasons.append(f"assertion #{e['assertion_id']} does not exist or was retracted")
                continue
            if q and norm(q) not in norm(e["text"]):
                reasons.append(f"quote does not match assertion #{e['assertion_id']}")
                continue
            quotes.append(e["text"])
            anchors.append({"kind": "assertion", "evidence_id": e["id"], "assertion_id": e["assertion_id"]})
        else:
            reasons.append(f"unsupported evidence kind {e['kind']!r}")
    if reasons or not quotes:
        return {"status": "rejected", "reasons": reasons or ["nothing citable"], "anchors": anchors, "checks": []}

    pool = norm(" ".join(quotes))
    invented = [t for t in IDENT.findall(claim["text"]) if norm(t).rstrip(".,") not in pool]
    if invented:
        return {"status": "rejected", "reasons": [f"identifier(s) not in any cited text: {', '.join(invented)}"],
                "anchors": anchors, "checks": []}

    checks, strong_ok, any_false = [], False, False
    for v in verifiers:
        ok, why = v.check(claim["text"], quotes)
        checks.append({"verifier": v.name, "weak": v.weak, "result": ok, "note": why})
        if ok is False:
            any_false = True
        elif ok and not v.weak:
            strong_ok = True
    if any_false:
        return {"status": "rejected", "reasons": ["a verifier judged the cited text does not support the claim"],
                "anchors": anchors, "checks": checks}
    status = "verified" if strong_ok else "weakly_verified"
    return {"status": status, "reasons": [], "anchors": anchors, "checks": checks,
            "needs_human_review": status == "verified" and _needs_review(claim["text"])}
