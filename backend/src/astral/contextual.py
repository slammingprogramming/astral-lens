"""Contextual chunk headers: one model-written sentence per passage that says where it sits in its document.

Why: a passage such as "Payments are made to IBAN ..." never names the company the document is about, so a search for the
company misses it. Prepending a situating sentence at index time is the cheapest fix (docs/08 section 5).

What it is NOT: evidence. The header is model output used only to find a passage. It is stored beside the chunk, never
replaces or alters the chunk's text, is never offered as a quotation, and is never written as an assertion; a citation is
still checked against the stored bytes of the passage (invariant 10). The request goes through the same endpoint policy as
every model call (a local-only case refuses a non-local endpoint).

The model may not invent: a header is rejected if it contains a number or a capitalised word that the document does not
contain. A model that cannot be reached stops the run with a visible count; nothing is half-written.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import psycopg

from astral.db import Actor
from astral.evidence import EvidenceStore
from astral.llm import LlmError

MAX_PER_RUN = 500
MAX_HEADER = 300
WHOLE_DOC = 6000
SCHEMA = {"type": "object", "properties": {"header": {"type": "string", "maxLength": MAX_HEADER}}, "required": ["header"],
          "additionalProperties": False}
SYSTEM = ("You write one short sentence that says where a passage sits in its source document and what it is about, so a search "
          "engine can find it. Use only what the document says. Never add a name, number, date or fact that the document does not "
          "contain. Do not answer questions and do not quote the passage.")
_NUM = re.compile(r"\d[\d.,/\-]*\d|\d")
_CAP = re.compile(r"\b[A-Z][A-Za-z]{2,}\b")


def excerpt(doc: str, start: int, end: int) -> str:
    """The whole document when it is short, else its opening plus the window around the passage."""
    if len(doc) <= WHOLE_DOC:
        return doc
    lo, hi = max(0, start - 1500), min(len(doc), end + 1500)
    return doc[:1500] + ("\n[...]\n" if lo > 1500 else "") + doc[max(lo, 1500):hi]


def grounded(header: str, doc: str) -> bool:
    """True when every number and every capitalised word after the first in the header also occurs in the document."""
    low = doc.lower()
    if any(n.lower() not in low for n in _NUM.findall(header)):
        return False
    words = _CAP.findall(header)
    first = header.split(None, 1)[0].strip(".,;:") if header.split() else ""
    return all(w.lower() in low for w in words if w != first)


def clean(raw: Any) -> str | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("header"), str):
        return None
    h = " ".join(raw["header"].split())
    return h if 1 <= len(h) <= MAX_HEADER else None


def generate(conn: psycopg.Connection, store: EvidenceStore, actor: Actor, case_id: uuid.UUID, client: Any, *, limit: int = 50) -> dict[str, Any]:
    """Write headers for up to `limit` chunks that have none. Returns counts; never raises for one bad answer."""
    limit = max(1, min(int(limit), MAX_PER_RUN))
    rows = conn.execute(
        "SELECT c.id, c.text_sha, c.start_char, c.end_char, c.body FROM text_chunk c"
        " LEFT JOIN text_chunk_context x ON x.chunk_id = c.id WHERE c.case_id=%s AND x.chunk_id IS NULL ORDER BY c.id LIMIT %s",
        (case_id, limit)).fetchall()
    docs: dict[bytes, str] = {}
    out = {"written": 0, "ungrounded": 0, "unusable": 0, "stopped": None, "model": client.model}
    for r in rows:
        sha = bytes(r["text_sha"])
        if sha not in docs:
            docs[sha] = store.read(conn, actor, case_id, sha).decode("utf-8", "replace")
        doc = docs[sha]
        user = (f"<document>\n{excerpt(doc, r['start_char'], r['end_char'])}\n</document>\n<passage>\n{r['body']}\n</passage>\n"
                "Write one sentence of at most 40 words.")
        try:
            header = clean(client.chat_json(SYSTEM, user, SCHEMA, max_tokens=120))
        except LlmError as e:
            out["stopped"] = f"the model could not be reached or answered badly: {str(e)[:160]}"
            break
        if header is None:
            out["unusable"] += 1
        elif not grounded(header, doc):
            out["ungrounded"] += 1
        else:
            out["written"] += conn.execute(
                "INSERT INTO text_chunk_context (chunk_id, case_id, header, model) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (r["id"], case_id, header, client.model)).rowcount
    out["remaining"] = conn.execute(
        "SELECT count(*) AS n FROM text_chunk c LEFT JOIN text_chunk_context x ON x.chunk_id = c.id WHERE c.case_id=%s AND x.chunk_id IS NULL",
        (case_id,)).fetchone()["n"]
    return out
