"""Entity extraction backends for the `ner.extract` capability (M1).

Best-first: GLiNER multi v2.1 (Apache-2.0, zero-shot labels), spaCy (optional
pipeline package), then the built-in RULES backend, which is always available
and is deliberately conservative:

* identifiers (e-mail, IBAN, LEI, BTC, ETH, phone, URL) are matched by pattern
  AND, where the format defines one, validated by checksum. A checksum-valid
  IBAN or LEI is far stronger evidence than a bare regex hit, and the result
  says which it was (`validated`).
* organisations are found only by a legal-form suffix ("Acme Holdings Ltd").
* persons are found only after an honorific ("Dr Jane Doe"). That is weak by
  construction; surname-only or title-less persons need a model backend and the
  rules backend does not pretend otherwise.

Every mention carries its character span in the text it was extracted from, so
a reviewer can see the hit in context and a citation can be span-checked.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from astral.backends import module_available

LABEL_SCHEMA = {
    "person": "Person", "organization": "Organization", "location": "Address",
    "email": "EmailAddress", "phone": "PhoneNumber", "url": "Url", "iban": "BankAccount",
    "lei": "LegalEntity", "btc": "CryptoWallet", "eth": "CryptoWallet",
}
IDENTIFIER_LABELS = {"email", "phone", "url", "iban", "lei", "btc", "eth"}


@dataclass(frozen=True)
class Mention:
    label: str
    text: str
    start: int
    end: int
    score: float
    validated: bool = False
    kind: str = "name"             # 'identifier' | 'name'

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "text": self.text, "start": self.start, "end": self.end,
                "score": self.score, "validated": self.validated, "kind": self.kind,
                "schema": LABEL_SCHEMA.get(self.label)}


# ------------------------------------------------------------- checksums
def _mod97(s: str) -> int:
    return int("".join(str(int(c, 36)) for c in s)) % 97


def iban_valid(raw: str) -> bool:
    s = re.sub(r"\s", "", raw).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", s):
        return False
    return _mod97(s[4:] + s[:4]) == 1


def lei_valid(raw: str) -> bool:
    s = raw.upper()
    return bool(re.fullmatch(r"[0-9A-Z]{18}\d{2}", s)) and _mod97(s) == 1


_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def btc_legacy_valid(addr: str) -> bool:
    n = 0
    for c in addr:
        i = _B58.find(c)
        if i < 0:
            return False
        n = n * 58 + i
    raw = n.to_bytes(25, "big") if n.bit_length() <= 200 else b""
    if len(raw) != 25:
        return False
    return hashlib.sha256(hashlib.sha256(raw[:-4]).digest()).digest()[:4] == raw[-4:]


_BECH_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def bech32_valid(addr: str) -> bool:
    if addr.lower() != addr and addr.upper() != addr:
        return False
    a = addr.lower()
    pos = a.rfind("1")
    if pos < 1 or pos + 7 > len(a) or len(a) > 90:
        return False
    if any(c not in _BECH_CHARSET for c in a[pos + 1:]):
        return False
    hrp, data = a[:pos], [_BECH_CHARSET.index(c) for c in a[pos + 1:]]

    def polymod(values: list[int]) -> int:
        gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
        chk = 1
        for v in values:
            b = chk >> 25
            chk = (chk & 0x1FFFFFF) << 5 ^ v
            for i in range(5):
                chk ^= gen[i] if (b >> i) & 1 else 0
        return chk

    exp = [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]
    return polymod(exp + data) in (1, 0x2BC830A3)         # bech32 / bech32m


# ----------------------------------------------------------------- rules
_EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_URL = re.compile(r"\bhttps?://[^\s<>\"')\]]+[^\s<>\"')\].,;:!?]")
_PHONE = re.compile(r"(?<![\w/])\+\d{1,3}[\s().-]?(?:\(?\d{1,4}\)?[\s.-]?){2,5}\d(?!\d)")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,3})?\b")
_LEI = re.compile(r"\b[0-9A-Z]{18}\d{2}\b")
_BTC = re.compile(r"\b[13][a-km-zA-HJ-NP-Z1-9]{25,34}\b")
_BECH = re.compile(r"\b(?:bc1|BC1)[a-zA-HJ-NP-Z0-9]{11,87}\b")
_ETH = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
_SUFFIX = (r"(?:Ltd\.?|Limited|LLC|L\.L\.C\.|LLP|Inc\.?|Incorporated|Corp\.?|Corporation|PLC|plc|"
           r"GmbH|AG|KG|S\.A\.|SA|S\.A\.S\.|SARL|S\.r\.l\.|SRL|B\.V\.|BV|N\.V\.|NV|Oy|AB|ApS|"
           r"OOO|ООО|АО|ЗАО|JSC|PJSC|Pty|Pte\.? Ltd\.?|Co\.|Holdings|Group)")
_CAP = r"[A-ZÀ-ÖØ-Þ][\w&'’.-]*"
_ORG = re.compile(rf"\b((?:{_CAP}\s+){{1,5}}{_SUFFIX})(?=\W|$)")
_HONORIFIC = re.compile(
    r"\b(?:Mr|Mrs|Ms|Miss|Dr|Prof|Sir|Dame|Herr|Frau|Mme|M\.)\.?\s+"
    r"([A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'’-]+(?:\s+[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'’-]+){1,2})")


class RulesNer:
    name = "rules"
    capability = "ner.extract"
    model_id = None

    def available(self) -> tuple[bool, str]:
        return True, ""

    def supports(self, **_: Any) -> bool:
        return True

    def run(self, *, text: str, labels: list[str] | None = None, **_: Any) -> list[Mention]:
        want = set(labels or LABEL_SCHEMA)
        found: list[Mention] = []

        def add(label: str, m: re.Match, *, group: int = 0, score: float, validated: bool = False,
                text_override: str | None = None) -> None:
            if label in want:
                s, e = m.span(group)
                found.append(Mention(label, text_override or m.group(group), s, e, score,
                                     validated, "identifier" if label in IDENTIFIER_LABELS else "name"))

        for m in _EMAIL.finditer(text):
            add("email", m, score=0.95)
        for m in _URL.finditer(text):
            add("url", m, score=0.95)
        for m in _IBAN.finditer(text):
            if iban_valid(m.group(0)):
                add("iban", m, score=0.99, validated=True)
        for m in _LEI.finditer(text):
            if lei_valid(m.group(0)):
                add("lei", m, score=0.99, validated=True)
        for m in _BTC.finditer(text):
            if btc_legacy_valid(m.group(0)):
                add("btc", m, score=0.99, validated=True)
        for m in _BECH.finditer(text):
            if bech32_valid(m.group(0)):
                add("btc", m, score=0.99, validated=True)
        for m in _ETH.finditer(text):
            add("eth", m, score=0.9)                     # format only; EIP-55 case checksum not verified
        for m in _PHONE.finditer(text):
            digits = re.sub(r"\D", "", m.group(0))
            if 8 <= len(digits) <= 15:
                add("phone", m, score=0.7)
        for m in _ORG.finditer(text):
            s, e = m.span(1)
            if text[e - 1] == ".":
                e -= 1            # "Ltd." vs a sentence-final stop is ambiguous; names normalise it away
            if "organization" in want:
                found.append(Mention("organization", text[s:e], s, e, 0.6))
        for m in _HONORIFIC.finditer(text):
            add("person", m, group=1, score=0.5)
        return _dedupe(found)


def _dedupe(ms: list[Mention]) -> list[Mention]:
    """Drop mentions wholly inside a stronger one (an e-mail's domain is not
    also a URL; digits of an IBAN are not also a phone number)."""
    ms = sorted(ms, key=lambda m: (-m.score, -(m.end - m.start), m.start))
    kept: list[Mention] = []
    for m in ms:
        if not any(k.start <= m.start and m.end <= k.end and k is not m for k in kept):
            kept.append(m)
    return sorted(kept, key=lambda m: (m.start, m.end))


# --------------------------------------------------- model backends (optional)
class GlinerNer:
    """GLiNER multi v2.1, zero-shot. NOT exercised by the test suite (needs the
    `gliner` package and a ~1 GB weight download, which the operator approves);
    the adapter is the documented call surface only. Loading goes through the
    residency manager in a real deployment (invariant 5)."""

    name = "gliner"
    capability = "ner.extract"
    model_id = "gliner-multi-v2.1"
    MODEL = "urchade/gliner_multi-v2.1"

    def __init__(self) -> None:
        self._model = None

    def available(self) -> tuple[bool, str]:
        return (True, "") if module_available("gliner") else (False, "gliner is not installed")

    def supports(self, **_: Any) -> bool:
        return True

    def run(self, *, text: str, labels: list[str] | None = None, **_: Any) -> list[Mention]:  # pragma: no cover
        from gliner import GLiNER

        if self._model is None:
            self._model = GLiNER.from_pretrained(self.MODEL)
        want = [l for l in (labels or ["person", "organization", "location"])
                if l in ("person", "organization", "location")]
        out = []
        for chunk_start, chunk in _chunks(text, 1500):
            for ent in self._model.predict_entities(chunk, want, threshold=0.5):
                out.append(Mention(ent["label"], ent["text"], chunk_start + ent["start"],
                                   chunk_start + ent["end"], float(ent["score"])))
        return _dedupe(out)


class SpacyNer:
    """spaCy pipeline package (e.g. en_core_web_sm). Not exercised by the tests."""

    name = "spacy"
    capability = "ner.extract"
    model_id = "spacy-en_core_web_sm"
    PIPELINE = "en_core_web_sm"
    MAP = {"PERSON": "person", "ORG": "organization", "GPE": "location", "LOC": "location"}

    def __init__(self) -> None:
        self._nlp = None

    def available(self) -> tuple[bool, str]:
        if not module_available("spacy"):
            return False, "spacy is not installed"
        return (True, "") if module_available(self.PIPELINE) else (
            False, f"spaCy pipeline {self.PIPELINE} is not installed")

    def supports(self, **_: Any) -> bool:
        return True

    def run(self, *, text: str, labels: list[str] | None = None, **_: Any) -> list[Mention]:  # pragma: no cover
        import spacy

        if self._nlp is None:
            self._nlp = spacy.load(self.PIPELINE)
        want = set(labels or ["person", "organization", "location"])
        out = []
        for start, chunk in _chunks(text, 100_000):
            for ent in self._nlp(chunk).ents:
                lab = self.MAP.get(ent.label_)
                if lab in want:
                    out.append(Mention(lab, ent.text, start + ent.start_char, start + ent.end_char, 0.6))
        return _dedupe(out)


def _chunks(text: str, size: int):
    for i in range(0, len(text), size):
        yield i, text[i:i + size]
