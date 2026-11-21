"""Entity-resolution candidate generation with per-field explanation (03 §5).

Why this is not Splink: Splink wants DuckDB and a training step (EM) over a large
record set. A single investigation has hundreds to thousands of entities and the
design requires something Splink does not give a reviewer: for every proposed
pair, a field-by-field comparison vector naming the assertions on each side.
So this module is a small, deterministic Fellegi-Sunter-style comparator with
FIXED, documented weights (log2 Bayes-factor style) and no learning. The design's
caveat stands either way: scores are RANKINGS for human review, never
probabilities, and nothing here ever merges (invariant 10). A Splink adapter can
replace `find_candidates` later; the output contract is the point.

Missing data contributes 0. Absence of a field is not a mismatch (invariant 3).
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import psycopg

from astral import names
from astral.assertions import AssertionRejected
from astral.db import Actor
from astral.resolver import record_judgment

NAME_PREDS = ("name", "alias", "previousName", "weakAlias")
STRONG_UNIQUE = ("leiCode", "imoNumber", "wikidataId", "passportNumber", "idNumber",
                 "taxNumber", "vatCode")
ID_PREDS = STRONG_UNIQUE + ("registrationNumber", "email", "phone", "cryptoAddress",
                            "swiftBic", "iban", "opencorporatesUrl")
DATE_PREDS = ("birthDate", "incorporationDate")
COUNTRY_PREDS = ("country", "nationality", "jurisdiction", "citizenship")
ADDRESS_PREDS = ("address",)

PERSON = {"Person"}
ORG = {"Company", "Organization", "LegalEntity", "PublicBody"}
MAX_BLOCK = 200                       # bigger blocks are skipped and reported, not guessed at
MIN_SCORE = 6.0


@dataclass
class Rec:
    cluster: uuid.UUID
    member: uuid.UUID                 # a representative entity id (judgments name entities)
    schema: str | None
    caption: str | None
    values: dict[str, list[tuple[Any, int]]] = field(default_factory=dict)   # pred -> [(value, assertion)]

    def vals(self, *preds: str) -> list[tuple[str, int]]:
        out = []
        for p in preds:
            for v, aid in self.values.get(p, []):
                if isinstance(v, (str, int, float)):
                    out.append((str(v), aid))
        return out

    @property
    def is_org(self) -> bool:
        return self.schema in ORG


@dataclass
class Candidate:
    a: Rec
    b: Rec
    score: float
    fields: list[dict[str, Any]]
    flags: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {"a_cluster": str(self.a.cluster), "b_cluster": str(self.b.cluster),
                "a_entity": str(self.a.member), "b_entity": str(self.b.member),
                "a_caption": self.a.caption, "b_caption": self.b.caption,
                "schema": self.a.schema or self.b.schema, "score": round(self.score, 2),
                "flags": self.flags, "fields": self.fields}


# ------------------------------------------------------------------ loading
def load_records(conn: psycopg.Connection, case_id: uuid.UUID) -> dict[uuid.UUID, Rec]:
    recs: dict[uuid.UUID, Rec] = {}
    for r in conn.execute("SELECT e.cluster_id, e.schema_name, e.caption,"
                          " (SELECT min(entity_id::text)::uuid FROM proj_entity_member m"
                          "  WHERE m.case_id = e.case_id AND m.cluster_id = e.cluster_id) AS member"
                          " FROM proj_entity e WHERE e.case_id = %s", (case_id,)).fetchall():
        recs[r["cluster_id"]] = Rec(r["cluster_id"], r["member"], r["schema_name"], r["caption"])
    for p in conn.execute("SELECT cluster_id, predicate, value, assertion_id FROM proj_property"
                          " WHERE case_id = %s ORDER BY assertion_id", (case_id,)).fetchall():
        rec = recs.get(p["cluster_id"])
        if rec is not None:
            rec.values.setdefault(p["predicate"], []).append((p["value"], p["assertion_id"]))
    return recs


def _family_ok(a: Rec, b: Rec) -> bool:
    if a.schema is None or b.schema is None:
        return True
    if a.schema == b.schema:
        return True
    return (a.schema in PERSON and b.schema in PERSON) or (a.schema in ORG and b.schema in ORG)


def _blocked_keys(rec: Rec) -> set[str]:
    keys: set[str] = set()
    for v, _ in rec.vals(*NAME_PREDS):
        keys |= names.blocking_keys(v, org=rec.is_org)
    for p in STRONG_UNIQUE:
        for v, _ in rec.vals(p):
            keys.add(f"id:{p}:{v.lower()}")
    return keys


# ----------------------------------------------------------------- compare
def _norm_date(v: str) -> str:
    m = re.match(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", v.strip())
    return "-".join(g for g in m.groups() if g) if m else v.strip()


# Country values arrive as ISO codes ("gb"), names ("United Kingdom") and
# demonyms ("British"). Only values we can map are compared; an unmappable value
# is UNCOMPARABLE, not a mismatch (a missing mapping must not veto a match).
_COUNTRY = {
    "gb": "gb|uk|united kingdom|great britain|britain|british|english|scottish|welsh|england|scotland|wales",
    "us": "us|usa|united states|united states of america|america|american",
    "de": "de|germany|german|deutschland", "fr": "fr|france|french", "es": "es|spain|spanish",
    "it": "it|italy|italian", "nl": "nl|netherlands|dutch|holland", "ch": "ch|switzerland|swiss",
    "ru": "ru|russia|russian|russian federation", "ua": "ua|ukraine|ukrainian",
    "by": "by|belarus|belarusian", "cn": "cn|china|chinese", "hk": "hk|hong kong",
    "in": "in|india|indian", "ae": "ae|uae|united arab emirates|emirati", "tr": "tr|turkey|turkish",
    "ie": "ie|ireland|irish", "lu": "lu|luxembourg", "cy": "cy|cyprus|cypriot", "mt": "mt|malta|maltese",
    "pa": "pa|panama|panamanian", "vg": "vg|british virgin islands|bvi",
    "ky": "ky|cayman islands|cayman", "je": "je|jersey", "gg": "gg|guernsey", "im": "im|isle of man",
    "sg": "sg|singapore", "jp": "jp|japan|japanese", "ir": "ir|iran|iranian",
    "kp": "kp|north korea", "sy": "sy|syria|syrian", "ve": "ve|venezuela|venezuelan",
    "cu": "cu|cuba|cuban", "br": "br|brazil|brazilian", "mx": "mx|mexico|mexican",
    "ca": "ca|canada|canadian", "au": "au|australia|australian", "se": "se|sweden|swedish",
    "no": "no|norway|norwegian", "pl": "pl|poland|polish", "at": "at|austria|austrian",
    "be": "be|belgium|belgian",
}
_COUNTRY_LOOKUP = {alias: code for code, aliases in _COUNTRY.items() for alias in aliases.split("|")}

def norm_country(v: str) -> str | None:
    t = names.normalize(v)
    if t in _COUNTRY_LOOKUP:
        return _COUNTRY_LOOKUP[t]
    return None


def compare(a: Rec, b: Rec) -> Candidate:
    fields: list[dict[str, Any]] = []
    flags: list[str] = []
    total = 0.0

    def add(name: str, level: str, weight: float, av: list, bv: list, **extra: Any) -> None:
        nonlocal total
        total += weight
        fields.append({"field": name, "level": level, "weight": weight,
                       "a": [{"value": v, "assertion": i} for v, i in av],
                       "b": [{"value": v, "assertion": i} for v, i in bv], **extra})

    # -- names
    an, bn = a.vals(*NAME_PREDS), b.vals(*NAME_PREDS)
    if an and bn:
        org = a.is_org or b.is_org
        best = max(((names.score(x, y, org=org), x, y, ia, ib)
                    for x, ia in an for y, ib in bn), key=lambda t: t[0].score)
        s = best[0]
        if s.score >= 0.999:
            lvl, w = "exact", 6.0
        elif s.score >= 0.93:
            lvl, w = "very_close", 4.5
        elif s.score >= 0.85:
            lvl, w = "close", 2.5
        elif s.score >= 0.75:
            lvl, w = "weak", 0.5
        else:
            lvl, w = "different", -2.0
        add("name", lvl, w, [(best[1], best[3])], [(best[2], best[4])],
            similarity=s.score, compared_forms=[s.a_form, s.b_form])

    # -- identifiers: a shared value is strong; differing UNIQUE ids are strong too
    for pred in ID_PREDS:
        av, bv = a.vals(pred), b.vals(pred)
        if not av or not bv:
            continue                                   # missing is not mismatch
        norm = lambda x: re.sub(r"[\s.-]", "", x).lower()   # noqa: E731
        shared = {norm(v) for v, _ in av} & {norm(v) for v, _ in bv}
        if shared:
            add(pred, "shared", 12.0 if pred in STRONG_UNIQUE else 8.0, av, bv)
        elif pred in STRONG_UNIQUE:
            add(pred, "conflict", -6.0, av, bv)
            flags.append(f"{pred}_conflict")

    # -- dates
    for pred in DATE_PREDS:
        av, bv = a.vals(pred), b.vals(pred)
        if not av or not bv:
            continue
        da, db_ = {_norm_date(v) for v, _ in av}, {_norm_date(v) for v, _ in bv}
        if da & db_:
            add(pred, "exact", 5.0, av, bv)
        elif any(x[:4] == y[:4] for x in da for y in db_) and \
                any(len(x) < 10 or len(y) < 10 for x in da for y in db_):
            add(pred, "same_year", 2.0, av, bv)       # one side is only a year
        else:
            add(pred, "conflict", -5.0, av, bv)
            flags.append(f"{pred}_conflict")

    # -- countries (multi-valued, so a mismatch is only mildly negative)
    ac, bc = a.vals(*COUNTRY_PREDS), b.vals(*COUNTRY_PREDS)
    if ac and bc:
        sa = {c for c in (norm_country(v) for v, _ in ac) if c}
        sb = {c for c in (norm_country(v) for v, _ in bc) if c}
        if sa and sb:                                   # compare only what we can map
            if sa & sb:
                add("country", "overlap", 1.5, ac, bc)
            else:
                add("country", "disjoint", -1.5, ac, bc)

    # -- addresses
    aa, ba = a.vals(*ADDRESS_PREDS), b.vals(*ADDRESS_PREDS)
    if aa and ba:
        ta = [set(names.normalize(v).split()) for v, _ in aa]
        tb = [set(names.normalize(v).split()) for v, _ in ba]
        best_o = max((len(x & y) / max(1, min(len(x), len(y))) for x in ta for y in tb), default=0)
        if best_o >= 0.8:
            add("address", "match", 3.0, aa, ba, overlap=round(best_o, 2))

    name_field = next((f for f in fields if f["field"] == "name"), None)
    others = [f for f in fields if f["field"] != "name" and f["weight"] > 0]
    if name_field and name_field["weight"] > 0 and not others:
        flags.append("name_only")           # common names produce many of these; review with care
    return Candidate(a, b, total, fields, flags)


# ------------------------------------------------------------------- search
@dataclass
class CandidateReport:
    candidates: list[Candidate]
    compared: int
    skipped_blocks: list[str]


def find_candidates(
    conn: psycopg.Connection, case_id: uuid.UUID, *, min_score: float = MIN_SCORE,
    limit: int = 200, recs: dict[uuid.UUID, Rec] | None = None,
) -> CandidateReport:
    recs = recs or load_records(conn, case_id)
    blocks: dict[str, list[uuid.UUID]] = {}
    for cid, rec in recs.items():
        for k in _blocked_keys(rec):
            blocks.setdefault(k, []).append(cid)
    seen: set[tuple[uuid.UUID, uuid.UUID]] = set()
    skipped: list[str] = []
    out: list[Candidate] = []
    for key, members in sorted(blocks.items()):
        if len(members) < 2:
            continue
        if len(members) > MAX_BLOCK:
            skipped.append(f"{key} ({len(members)} records)")
            continue
        for i, x in enumerate(members):
            for y in members[i + 1:]:
                pair = (x, y) if str(x) < str(y) else (y, x)
                if pair in seen:
                    continue
                seen.add(pair)
                a, b = recs[pair[0]], recs[pair[1]]
                if _family_ok(a, b):
                    c = compare(a, b)
                    if c.score >= min_score:
                        out.append(c)
    decided = _decided_pairs(conn, case_id)
    out = [c for c in out if frozenset((c.a.cluster, c.b.cluster)) not in decided]
    out.sort(key=lambda c: (-c.score, str(c.a.cluster), str(c.b.cluster)))
    return CandidateReport(out[:limit], len(seen), skipped)


def _decided_pairs(conn: psycopg.Connection, case_id: uuid.UUID) -> set[frozenset[uuid.UUID]]:
    """Cluster pairs that already carry a live judgment of any verdict: a
    human has looked, or a proposal is already waiting. Do not re-propose."""
    cl = {r["entity_id"]: r["cluster_id"] for r in conn.execute(
        "SELECT entity_id, cluster_id FROM proj_entity_member WHERE case_id = %s", (case_id,))}
    pairs: set[frozenset[uuid.UUID]] = set()
    for j in conn.execute(
            "SELECT j.entity_a, j.entity_b FROM judgment j LEFT JOIN judgment_retraction r"
            " ON r.judgment_id = j.id WHERE j.case_id = %s AND r.id IS NULL", (case_id,)):
        if j["entity_a"] in cl and j["entity_b"] in cl:
            pairs.add(frozenset((cl[j["entity_a"]], cl[j["entity_b"]])))
    return pairs


def propose(
    conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID, candidates: list[Candidate],
) -> list[int]:
    """Record each candidate as an `undecided` judgment carrying its comparison
    vector. This is a proposal waiting for a human; it changes no cluster."""
    ids = []
    for c in candidates:
        try:
            ids.append(record_judgment(
                conn, actor, case_id, c.a.member, c.b.member, "undecided",
                comparison=c.as_dict(),
                reasoning=f"proposed by resolution.find_candidates (score {c.score:.1f}; "
                          "a ranking for review, not a probability)"))
        except AssertionRejected:
            continue
    return ids


# -------------------------------------------------------------- explanation
def explain_pair(
    conn: psycopg.Connection, case_id: uuid.UUID, cluster_a: uuid.UUID, cluster_b: uuid.UUID,
) -> dict[str, Any]:
    """The merge-explanation view: every compared field with both sides' values,
    the assertion behind each, its grades, who produced it and its evidence."""
    recs = load_records(conn, case_id)
    if cluster_a not in recs or cluster_b not in recs:
        raise LookupError("entity not found")
    cand = compare(recs[cluster_a], recs[cluster_b])
    ids = sorted({x["assertion"] for f in cand.fields for side in ("a", "b") for x in f[side]})
    meta = {}
    if ids:
        for r in conn.execute(
                "SELECT id, source_grade, info_grade, derived_by, evidence FROM assertion"
                " WHERE case_id = %s AND id = ANY(%s)", (case_id, ids)):
            meta[r["id"]] = {"source_grade": r["source_grade"], "info_grade": r["info_grade"],
                             "plugin": r["derived_by"].get("plugin"),
                             "version": r["derived_by"].get("version"),
                             "evidence": [bytes(e).hex() for e in r["evidence"]]}
    for f in cand.fields:
        for side in ("a", "b"):
            for x in f[side]:
                x.update(meta.get(x["assertion"], {}))
    d = cand.as_dict()
    d["unseen_fields"] = sorted(
        {p for p in (*NAME_PREDS, *ID_PREDS, *DATE_PREDS, *COUNTRY_PREDS, *ADDRESS_PREDS)}
        - {f["field"] for f in cand.fields} - {"alias", "previousName", "weakAlias"})
    d["note"] = ("Fields listed in unseen_fields were not compared because one or both sides "
                 "have no value. That is missing data, not evidence of difference.")
    return d


def to_json(c: Candidate) -> str:
    return json.dumps(c.as_dict(), default=str)
