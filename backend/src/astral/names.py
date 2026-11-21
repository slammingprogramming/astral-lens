"""Names across scripts (03 §6).

Pipeline: normalise per script -> transliterate (recording the standard) ->
score with token-based similarity -> use phonetic codes as BLOCKING KEYS ONLY.
Original-script and romanised forms are both retained; a romanised form is an
additional value, never a replacement.

Transliteration uses `anyascii` (ISC). ICU's `Any-Latin` is the design's first
choice but needs native ICU libraries that are painful on Windows; the standard
used is recorded on every form (`standard`) so scores stay interpretable.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version

from anyascii import anyascii
from rapidfuzz import fuzz

try:
    TRANSLIT_STANDARD = f"anyascii-{version('anyascii')}"
except PackageNotFoundError:           # pragma: no cover
    TRANSLIT_STANDARD = "anyascii"

_SCRIPTS = ("LATIN", "CYRILLIC", "GREEK", "ARABIC", "HEBREW", "DEVANAGARI", "THAI", "HANGUL",
            "HIRAGANA", "KATAKANA", "CJK", "ARMENIAN", "GEORGIAN", "BENGALI", "TAMIL")

# Legal-form words that carry no identity for matching. Deliberately small and
# language-agnostic: removing too much would merge unrelated firms.
LEGAL_SUFFIXES = frozenset({
    "ltd", "limited", "llc", "llp", "lp", "inc", "incorporated", "corp", "corporation", "co",
    "company", "plc", "gmbh", "ag", "kg", "sa", "sarl", "sas", "srl", "spa", "nv", "bv", "oy",
    "ab", "as", "aps", "sp", "zoo", "ooo", "ao", "zao", "oao", "pao", "pjsc", "jsc", "kk",
    "pte", "pty", "sdn", "bhd", "ltda", "oü", "uab", "sia", "se", "ou",
})
_TITLES = frozenset({"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "dame", "lord", "lady",
                     "mx", "hon", "rt", "sheikh", "shaikh", "sri", "shri", "herr", "frau"})


def script_of(text: str) -> str:
    """Dominant Unicode script of the letters in `text` ('LATIN', 'CYRILLIC', ...)."""
    counts: dict[str, int] = {}
    for ch in text:
        if not ch.isalpha():
            continue
        name = unicodedata.name(ch, "")
        for s in _SCRIPTS:
            if name.startswith(s) or (s == "CJK" and name.startswith("CJK UNIFIED")):
                counts[s] = counts.get(s, 0) + 1
                break
        else:
            counts["OTHER"] = counts.get("OTHER", 0) + 1
    return max(counts, key=counts.get) if counts else "NONE"


def normalize(text: str) -> str:
    """NFKC, casefold, drop combining marks, punctuation -> spaces, collapse."""
    t = unicodedata.normalize("NFKC", text)
    t = unicodedata.normalize("NFKD", t.casefold())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE).replace("_", " ")
    return re.sub(r"\s+", " ", t).strip()


def romanize(text: str) -> str:
    return normalize(anyascii(unicodedata.normalize("NFKC", text)))


def tokens(norm: str, *, org: bool = False) -> list[str]:
    toks = norm.split()
    toks = [t for t in toks if t not in _TITLES]
    if org:
        stripped = [t for t in toks if t not in LEGAL_SUFFIXES]
        toks = stripped or toks
    return toks


@dataclass(frozen=True)
class NameForm:
    form: str                 # the value
    kind: str                 # 'original' | 'normalized' | 'romanized'
    script: str
    standard: str | None = None


def name_forms(name: str) -> list[NameForm]:
    """Every searchable form of a name. The original is always first."""
    script = script_of(name)
    forms = [NameForm(name, "original", script)]
    norm = normalize(name)
    if norm and norm != name:
        forms.append(NameForm(norm, "normalized", script))
    if script not in ("LATIN", "NONE"):
        rom = romanize(name)
        if rom and rom not in {f.form for f in forms}:
            forms.append(NameForm(rom, "romanized", "LATIN", TRANSLIT_STANDARD))
    return forms


@dataclass(frozen=True)
class NameScore:
    score: float              # 0..1
    method: str
    a_form: str
    b_form: str


def score(a: str, b: str, *, org: bool = False) -> NameScore:
    """Best token-based similarity over all form pairs of two names. Compares
    like with like: original vs original, romanised vs romanised, so a Cyrillic
    and a Latin spelling are matched through the romanised forms."""
    best = NameScore(0.0, "none", a, b)
    for fa in name_forms(a):
        for fb in name_forms(b):
            if fa.kind == "original" and fb.kind == "original" and fa.script != fb.script:
                continue                      # different scripts: only romanised forms compare
            ta = " ".join(tokens(normalize(fa.form), org=org))
            tb = " ".join(tokens(normalize(fb.form), org=org))
            if not ta or not tb:
                continue
            if ta == tb:
                return NameScore(1.0, "exact", fa.form, fb.form)
            sort = fuzz.token_sort_ratio(ta, tb) / 100
            sett = fuzz.token_set_ratio(ta, tb) / 100
            # token_set alone overrates a short name contained in a long one
            # ("john smith" vs "john smith holdings"); damp it by length ratio.
            ratio = min(len(ta), len(tb)) / max(len(ta), len(tb))
            s = max(sort, sett * (0.75 + 0.25 * ratio))
            if s > best.score:
                best = NameScore(round(s, 4), "token", fa.form, fb.form)
    return best


# ------------------------------------------------------------ blocking keys
def soundex(word: str) -> str:
    """English-oriented Soundex. A BLOCKING KEY only: it narrows candidates
    within one language and never decides a match (03 §6; it collapses
    Christine, Christina and Christopher)."""
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return ""
    codes = {c: d for d, cs in {"1": "bfpv", "2": "cgjkqsxz", "3": "dt", "4": "l",
                                "5": "mn", "6": "r"}.items() for c in cs}
    out, last = w[0].upper(), codes.get(w[0], "")
    for c in w[1:]:
        d = codes.get(c, "")
        if d and d != last:
            out += d
        if c not in "hw":
            last = d
    return (out + "000")[:4]


def blocking_keys(name: str, *, org: bool = False) -> set[str]:
    """Cheap keys that put plausible duplicates in the same bucket. Computed on
    the romanised form so cross-script duplicates share a block."""
    keys: set[str] = set()
    for form in name_forms(name):
        toks = tokens(normalize(form.form), org=org)
        if form.kind == "original" and form.script not in ("LATIN", "NONE"):
            continue                          # compare via romanised forms
        if not toks:
            continue
        for t in toks:
            if len(t) >= 3:
                keys.add("p:" + t[:4])        # token prefix
        keys.add("s:" + soundex(toks[-1]) + toks[0][:1])   # last-name sound + first initial
    return keys
