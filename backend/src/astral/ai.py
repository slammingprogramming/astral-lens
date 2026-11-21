"""The AI layer: a question becomes a bounded plan, the operator approves it, it runs
over the case's own data, and the answer is only what survives citation checking.

What the layer will not do (invariant 10, docs/08): write assertions, merge entities,
invent identifiers, raise a grade, or fetch from the network. Its plans are made only of
READ steps over the case, so `footprint` is always none. With a model configured it
plans and composes through `llm.LlmClient` (egress-checked); without one it falls back
to a rule planner and an EXTRACTIVE composer that quotes passages, and says so.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

import psycopg

from astral import citations, retrieval
from astral.db import Actor
from astral.evidence import EvidenceStore

MAX_STEPS, MAX_K = 6, 20
STEP_TYPES = {"search_text": {"query": str, "k": int}, "entity_facts": {"name": str}}
SKIP_PREDICATES = {"schema", "hasText", "hasSegments", "contentHash", "mimeType", "container"}
_NAME = re.compile(r"\b[A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*)+\b")          # allows middle initials: Jane Q Doe


class PlanError(ValueError):
    pass


# ----------------------------------------------------------------------- plans
def validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise PlanError("a plan needs at least one step")
    if len(steps) > MAX_STEPS:
        raise PlanError(f"a plan may have at most {MAX_STEPS} steps")
    ids: set[str] = set()
    for s in steps:
        t = s.get("type")
        if t not in STEP_TYPES:
            raise PlanError(f"unknown step type {t!r}; allowed: {sorted(STEP_TYPES)}")
        args = s.get("args") or {}
        for k, typ in STEP_TYPES[t].items():
            if k == "k":
                args["k"] = min(int(args.get("k", 8)), MAX_K)
            elif not isinstance(args.get(k), typ) or not args[k].strip():
                raise PlanError(f"step {s.get('id')} ({t}) needs a non-empty {k!r}")
        s["args"] = {k: args[k] for k in STEP_TYPES[t]}
        if s.get("id") in ids:
            raise PlanError(f"duplicate step id {s.get('id')!r}")
        ids.add(s["id"])
        s["depends_on"] = [d for d in s.get("depends_on", []) if d in ids and d != s["id"]]
    return plan


def rules_plan(question: str) -> dict[str, Any]:
    steps = [{"id": "s1", "type": "search_text", "args": {"query": question, "k": 8}, "depends_on": []}]
    for i, name in enumerate(dict.fromkeys(_NAME.findall(question)), 2):
        if len(steps) < MAX_STEPS:
            steps.append({"id": f"s{i}", "type": "entity_facts", "args": {"name": name}, "depends_on": []})
    return validate_plan({"steps": steps})


def llm_plan(client: Any, question: str) -> dict[str, Any]:
    schema = {"type": "object", "required": ["steps"], "additionalProperties": False, "properties": {"steps": {
        "type": "array", "maxItems": MAX_STEPS, "items": {"type": "object", "required": ["id", "type", "args"], "properties": {
            "id": {"type": "string"}, "type": {"type": "string", "enum": sorted(STEP_TYPES)},
            "args": {"type": "object"}}}}}}
    sys_p = ("You plan how to answer a question from an investigation case. Steps: search_text{query,k} searches the "
             "case's documents; entity_facts{name} fetches recorded facts about a named entity. Use at most "
             f"{MAX_STEPS} steps. You cannot browse or contact anyone.")
    return validate_plan(client.chat_json(sys_p, question, schema, max_tokens=400))


def store_plan(conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID, question: str, plan: dict[str, Any], planner: str,
               client: Any | None) -> uuid.UUID:
    """Record a proposed plan with its estimate. Nothing runs until it is approved."""
    import psycopg.types.json

    from astral import audit

    plan["estimate"] = estimate(plan, client)
    pid = uuid.uuid4()
    conn.execute("INSERT INTO ai_plan (id, workspace_id, case_id, question, planner, plan, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                 (pid, actor.workspace_id, case_id, question, planner, psycopg.types.json.Jsonb(plan), actor.principal_id))
    audit.record(conn, actor, "ai.plan", case_id=case_id, target_kind="ai_plan", target_id=str(pid),
                 detail={"planner": planner, "steps": len(plan["steps"])})
    return pid


def estimate(plan: dict[str, Any], client: Any | None) -> dict[str, Any]:
    """Shown to the operator BEFORE anything runs."""
    return {"steps": len(plan["steps"]), "llm_calls": 1 if client else 0, "egress": (client.mode if client else "none"),
            "footprint": "none", "writes_assertions": False,
            "note": "plans only read this case; nothing is fetched and no one is contacted"}


# ------------------------------------------------------------------- execution
def gather(conn: psycopg.Connection, store: EvidenceStore, actor: Actor, case_id: uuid.UUID,
           plan: dict[str, Any], services: Any | None = None) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
    retrieval.ensure_indexed(conn, store, actor, case_id)
    evidence: dict[str, dict[str, Any]] = {}
    notes: list[str] = []
    rankers: list[str] = []
    for s in plan["steps"]:
        if s["type"] == "search_text":
            r = retrieval.search(conn, case_id, s["args"]["query"], k=s["args"]["k"], services=services)
            rankers = r["rankers"]
            notes += [n for n in r["notes"] if n not in notes]
            for h in r["hits"]:
                if not any(e.get("kind") == "passage" and (e["text_sha"], e["start"]) == (h["text_sha"], h["start"])
                           for e in evidence.values()):
                    eid = f"E{len(evidence) + 1}"
                    evidence[eid] = {"id": eid, "kind": "passage", "text_sha": h["text_sha"], "start": h["start"],
                                     "end": h["end"], "text": h["text"], "rrf": h["rrf"]}
        else:
            name = s["args"]["name"]
            for ent in conn.execute(
                    "SELECT cluster_id, caption, schema_name FROM proj_entity WHERE case_id=%s AND caption ILIKE %s"
                    " ORDER BY member_count DESC LIMIT 3", (case_id, f"%{name}%")).fetchall():
                for p in conn.execute(
                        "SELECT predicate, value, assertion_id, source_grade, info_grade, independent_sources FROM proj_property"
                        " WHERE case_id=%s AND cluster_id=%s AND NOT (predicate = ANY(%s)) ORDER BY assertion_id LIMIT 12",
                        (case_id, ent["cluster_id"], list(SKIP_PREDICATES))):
                    v = p["value"] if not isinstance(p["value"], (dict, list)) else str(p["value"])
                    eid = f"E{len(evidence) + 1}"
                    evidence[eid] = {"id": eid, "kind": "fact", "assertion_id": p["assertion_id"],
                                     "text": f"{ent['caption']} — {p['predicate']}: {v}", "entity": ent["caption"],
                                     "source_grade": p["source_grade"], "info_grade": p["info_grade"],
                                     "independent_sources": p["independent_sources"]}
    return evidence, notes, rankers


_WH = re.compile(r"^\s*(who|what|which|when|where|why|how)\b", re.I)


def _question_entities(question: str) -> list[set[str]]:
    """Token sets of the multi-word proper names in the question (the subject, not what is asked)."""
    return [t for t in (set(retrieval.content_tokens(m.group(0))) for m in _NAME.finditer(_WH.sub("", question, count=1))) if t]


def _best_sentences(text: str, query: str, n: int = 2, strict: bool = False) -> list[str]:
    """The sentences that best answer the question (ties keep document order).

    Words that ask something ("director", "IBAN") count in full; words that only name the
    subject count half, so the sentence that merely repeats the company name does not
    outrank the one that answers. When the question names a subject, a sentence is a
    candidate only if its paragraph (line) names it: a crude stand-in for pronoun
    reference ("Its director is ..."), which keeps a different company's sentence from
    being quoted just because it shares the question's other words. A text with no line
    breaks is one paragraph, so there the rule changes nothing. If no paragraph names
    the subject, scoring falls back to plain word overlap, unless `strict` (then nothing is
    returned, so a passage about someone else is not quoted)."""
    toks = set(retrieval.content_tokens(query))
    ents = _question_entities(query)
    ask = toks - set().union(*ents) if ents else toks
    pieces = [(pi, s.strip()) for pi, para in enumerate(text.split("\n")) for s in re.split(r"(?<=[.!?])\s+", para)
              if len(s.strip()) > 20]
    pieces = [(pi, re.sub(r"^\[\d\d:\d\d:\d\d\]\s*(SPEAKER_\d+:\s*)?", "", s)) for pi, s in pieces] or [(0, text.strip()[:200])]
    stoks = [set(retrieval.content_tokens(s)) for _, s in pieces]
    named = [any(e <= st for e in ents) for st in stoks]
    named_paras = {pi for (pi, _), nm in zip(pieces, named) if nm}
    scored = [(len(ask & st) + (0.5 if named[i] else 0.0), i, s) for i, ((pi, s), st) in enumerate(zip(pieces, stoks))
              if pi in named_paras]
    if not scored and strict:
        return []
    if not scored:
        scored = [(len(toks & st), i, s) for i, ((_, s), st) in enumerate(zip(pieces, stoks))]
    return [s for sc, _, s in sorted(scored, key=lambda x: (-x[0], x[1])) if sc > 0][:n]


def compose_extractive(question: str, evidence: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """No model: quote what was found. Every claim IS a quotation, so it can only be as
    good as retrieval; it will not synthesise or infer."""
    claims = []
    passages = [x for x in evidence.values() if x["kind"] == "passage"][:4]
    for strict in ([True, False] if _question_entities(question) else [False]):
        for e in passages:
            for sent in _best_sentences(e["text"], question, strict=strict):
                claims.append({"text": sent, "cites": [{"evidence_id": e["id"], "quote": sent}]})
        if claims:
            break
    for e in [x for x in evidence.values() if x["kind"] == "fact"][:8]:
        claims.append({"text": e["text"], "cites": [{"evidence_id": e["id"], "quote": e["text"]}]})
    return claims


def compose_llm(client: Any, question: str, evidence: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    schema = {"type": "object", "required": ["claims"], "additionalProperties": False, "properties": {"claims": {
        "type": "array", "items": {"type": "object", "required": ["text", "cites"], "properties": {
            "text": {"type": "string"}, "cites": {"type": "array", "items": {"type": "object",
                "required": ["evidence_id", "quote"], "properties": {"evidence_id": {"type": "string"},
                                                                     "quote": {"type": "string"}}}}}}}}}
    ev = "\n\n".join(f"[{e['id']}] ({e['kind']}) {e['text'][:1200]}" for e in evidence.values())
    sys_p = ("Answer ONLY from the evidence. Each claim must cite evidence ids and QUOTE the exact words that support "
             "it. Do not add identifiers, names or numbers that are not in the quotes. If the evidence does not answer "
             "the question, return no claims.")
    return client.chat_json(sys_p, f"QUESTION: {question}\n\nEVIDENCE:\n{ev}", schema, max_tokens=1500)["claims"]


def _confidence(anchors: list[dict[str, Any]], evidence: dict[str, dict[str, Any]], status: str) -> str:
    if status != "verified" and not all(a["kind"] == "assertion" for a in anchors):
        return "low"
    worst = "high"
    for a in anchors:
        e = evidence[a["evidence_id"]]
        if a["kind"] == "span":
            return "low"                                  # a document says it; whether it is true is unassessed
        if e["source_grade"] > "C" or e["info_grade"] > 3:
            lvl = "low"
        elif e["source_grade"] <= "B" and e["info_grade"] <= 2 and e["independent_sources"] >= 2:
            lvl = "high"
        else:
            lvl = "moderate"
        worst = min(worst, lvl, key=["low", "moderate", "high"].index)
    return "low" if status == "weakly_verified" and worst == "high" else worst


def run_plan(conn: psycopg.Connection, store: EvidenceStore, actor: Actor, case_id: uuid.UUID, question: str,
             plan: dict[str, Any], client: Any | None, verifiers: list[Any], services: Any | None = None) -> dict[str, Any]:
    t0 = time.perf_counter()
    evidence, notes, rankers = gather(conn, store, actor, case_id, plan, services)
    composer, raw = "extractive (no model)", []
    if client is not None and evidence:
        try:
            raw, composer = compose_llm(client, question, evidence), f"model via {client.name}"
        except Exception as e:                            # noqa: BLE001 - any model failure degrades visibly
            notes.append(f"model composer failed ({e}); fell back to extractive quoting")
    if not raw:
        raw = compose_extractive(question, evidence)
    accepted, rejected = [], []
    for c in raw:
        v = citations.verify_claim(conn, store, actor, case_id, c, evidence, verifiers)
        item = {"text": c["text"], "status": v["status"], "anchors": v["anchors"], "checks": v["checks"],
                "reasons": v["reasons"], "needs_human_review": v.get("needs_human_review", False)}
        if v["status"] == "rejected":
            rejected.append(item)
        else:
            item["confidence"] = _confidence(v["anchors"], evidence, v["status"])
            accepted.append(item)
    weak_only = not any(not v.weak for v in verifiers)
    if weak_only:
        notes.append("no model verifier is available: claims can be at most 'weakly verified' (existence, verbatim span, "
                     "identifiers and word overlap were checked; entailment was not)")
    gaps = _gaps(conn, case_id, evidence, accepted, rejected, notes)
    out = {"question": question, "composer": composer, "claims": accepted, "rejected": rejected,
           "evidence": list(evidence.values()), "rankers": rankers, "notes": notes, "gaps": gaps,
           "verifiers": [{"name": v.name, "weak": v.weak} for v in verifiers],
           "latency_ms": int((time.perf_counter() - t0) * 1000)}
    out["report_md"] = render_report(out)
    return out


def _gaps(conn, case_id, evidence, accepted, rejected, notes) -> list[str]:
    g = []
    if not evidence:
        g.append("Nothing in this case matched the question. That is not evidence that nothing exists elsewhere.")
    last = conn.execute("SELECT run_id FROM search_run WHERE case_id=%s ORDER BY id DESC LIMIT 1", (case_id,)).fetchone()
    if last:
        for r in conn.execute("SELECT plugin_id, coverage FROM search_run WHERE case_id=%s AND run_id=%s", (case_id, last["run_id"])):
            for c in r["coverage"]:
                if c["outcome"] == "unknown":
                    g.append(f"Source not consulted: {c['source']} — {c['note']}")
    if rejected:
        g.append(f"{len(rejected)} candidate statement(s) were dropped because their citations failed checking.")
    g += notes
    return g


def render_report(r: dict[str, Any]) -> str:
    """ICD 203-shaped, with the sections the standard requires of an honest product. No
    estimative-probability language is used: statements say what sources say."""
    L = [f"# Assessment: {r['question']}", "",
         "**Basis.** Statements below are drawn from this case's own evidence and checked against it. "
         "Analytic confidence reflects source grading and corroboration, not model certainty. "
         f"Composed by: {r['composer']}.", "", "## Key judgments", ""]
    if not r["claims"]:
        L.append("_No statement could be supported by the evidence in this case._")
    for i, c in enumerate(r["claims"], 1):
        cites = ", ".join(f"{a['evidence_id']}" + (f" [{a['start']}–{a['end']}]" if a["kind"] == "span" else f" (assertion #{a['assertion_id']})")
                          for a in c["anchors"])
        flag = " — **flagged for human review**" if c["needs_human_review"] else ""
        L.append(f"{i}. {c['text']}  \n   _Confidence: {c['confidence']}; check: {c['status'].replace('_', ' ')}; sources: {cites}_{flag}")
    L += ["", "## Gaps", ""] + [f"- {x}" for x in (r["gaps"] or ["No coverage gaps were recorded for the last search run."])]
    ids = sorted({a["assertion_id"] for c in r["claims"] for a in c["anchors"] if a["kind"] == "assertion"})
    L += ["", "## Indicators", "",
          "What would change this assessment:",
          "- Independent corroboration of any statement marked low confidence.",
          "- Consulting the sources listed under Gaps as not consulted.",
          "- " + (f"Retraction or revision of the cited assertions ({', '.join('#' + str(i) for i in ids)})."
                  if ids else "New documents or media added to the case."),
          "", "## Method and limits", "",
          f"- Retrieval: {', '.join(r['rankers']) or 'none'}. Verifiers: {', '.join(v['name'] for v in r['verifiers']) or 'none'}.",
          f"- {len(r['rejected'])} statement(s) rejected at citation checking; nothing rejected appears above.",
          "- Model output is never recorded as evidence; this report writes no assertions."]
    return "\n".join(L)
