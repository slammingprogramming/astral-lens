"""Document parsing backends for the `doc.parse` capability (M1).

Best-first: Docling (layout-aware, optional, heavy), Apache Tika (a separate
Java server the operator runs, optional), then the built-in parsers (stdlib +
pypdf), which are always available and cover plain text, Markdown, CSV, HTML,
e-mail, DOCX and text-layer PDFs. Scanned documents need OCR; no built-in
backend does that (M2/M3 add an OCR backend), so an image-only PDF yields an
explicit empty-text gap rather than silence.
"""

from __future__ import annotations

import email
import email.policy
import io
import re
import urllib.request
import zipfile
from html.parser import HTMLParser
from typing import Any
from xml.etree import ElementTree as ET

from astral.backends import module_available

BUILTIN_TYPES = ("text/plain", "text/markdown", "text/csv", "text/html", "application/xhtml+xml",
                 "message/rfc822", "application/pdf",
                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document")


class _Text(HTMLParser):
    SKIP = {"script", "style", "head", "noscript", "template"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section",
             "article", "table", "ul", "ol", "blockquote", "pre", "hr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self._skip += 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    p = _Text()
    p.feed(html)
    text = "".join(p.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(), p.title.strip()


def _decode(data: bytes, hint: str | None = None) -> str:
    for enc in ([hint] if hint else []) + ["utf-8-sig", "utf-16"]:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1", "replace")


def _docx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paras = []
    for p in root.iter(ns + "p"):
        paras.append("".join(t.text or "" for t in p.iter(ns + "t")))
    return "\n".join(paras).strip()


def _pdf(data: bytes) -> tuple[str, int]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = [(page.extract_text() or "") for page in reader.pages]
    return "\n\f\n".join(pages).strip(), len(pages)


def _eml(data: bytes) -> tuple[str, dict[str, str]]:
    msg = email.message_from_bytes(data, policy=email.policy.default)
    headers = {h: str(msg[h]) for h in ("From", "To", "Cc", "Subject", "Date", "Message-ID") if msg[h]}
    body = msg.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        text = body.get_content()
        if body.get_content_type() == "text/html":
            text, _ = html_to_text(text)
    head = "\n".join(f"{k}: {v}" for k, v in headers.items())
    return (head + "\n\n" + text).strip(), headers


class BuiltinParser:
    name = "builtin"
    capability = "doc.parse"
    model_id = None

    def available(self) -> tuple[bool, str]:
        return True, ""

    def supports(self, media_type: str = "", **_: Any) -> bool:
        return media_type.split(";")[0].strip().lower() in BUILTIN_TYPES

    def run(self, *, data: bytes, media_type: str, **_: Any) -> dict[str, Any]:
        mt = media_type.split(";")[0].strip().lower()
        meta: dict[str, Any] = {}
        if mt in ("text/plain", "text/markdown", "text/csv"):
            text = _decode(data)
        elif mt in ("text/html", "application/xhtml+xml"):
            text, title = html_to_text(_decode(data))
            meta["title"] = title
        elif mt == "message/rfc822":
            text, headers = _eml(data)
            meta["headers"] = headers
        elif mt.endswith("wordprocessingml.document"):
            text = _docx(data)
        elif mt == "application/pdf":
            text, pages = _pdf(data)
            meta["pages"] = pages
            if not text.strip():
                meta["warning"] = ("no text layer: this PDF is image-only and needs OCR, "
                                   "which no installed backend provides")
        else:                                           # pragma: no cover - guarded by supports()
            raise ValueError(f"builtin parser does not handle {mt}")
        return {"text": text, "meta": meta}


class TikaParser:
    """Apache Tika server (`PUT /tika`). The operator runs it (Java); core only
    talks HTTP to a URL from `ASTRAL_TIKA_URL`. Egress is the operator's own
    host, so it is allowed even in local-only mode when that URL is local."""

    name = "tika"
    capability = "doc.parse"
    model_id = None

    def __init__(self, url: str | None) -> None:
        self.url = url

    def available(self) -> tuple[bool, str]:
        return (True, "") if self.url else (False, "ASTRAL_TIKA_URL is not set")

    def supports(self, media_type: str = "", **_: Any) -> bool:
        # The broad fallback for office/ebook formats the built-in parsers lack.
        # It is not claimed for plain text / HTML / e-mail, so choosing the
        # built-in parser for those is not reported as a degradation.
        return media_type.split(";")[0].strip().lower() in (
            "application/pdf", "application/rtf", "application/msword",
            "application/vnd.ms-excel", "application/vnd.ms-powerpoint",
            "application/epub+zip", "application/vnd.oasis.opendocument.text",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    def run(self, *, data: bytes, media_type: str, **_: Any) -> dict[str, Any]:
        req = urllib.request.Request(self.url.rstrip("/") + "/tika", data=data, method="PUT",
                                     headers={"Accept": "text/plain", "Content-Type": media_type})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=120) as r:
            return {"text": r.read().decode("utf-8", "replace"), "meta": {}}


class DoclingParser:
    """Layout-aware conversion via the `docling` package. NOT exercised by the
    test suite (it needs a multi-hundred-MB install and model download); the
    adapter is the documented call surface only."""

    name = "docling"
    capability = "doc.parse"
    model_id = "docling"

    def available(self) -> tuple[bool, str]:
        return (True, "") if module_available("docling") else (False, "docling is not installed")

    def supports(self, media_type: str = "", **_: Any) -> bool:
        return media_type.split(";")[0].strip().lower() in (
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "image/png", "image/jpeg", "image/tiff")

    def run(self, *, data: bytes, media_type: str, **_: Any) -> dict[str, Any]:  # pragma: no cover
        import tempfile
        from pathlib import Path

        from docling.document_converter import DocumentConverter

        suffix = {"application/pdf": ".pdf", "text/html": ".html"}.get(media_type, ".bin")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / f"in{suffix}"
            p.write_bytes(data)
            doc = DocumentConverter().convert(str(p)).document
            return {"text": doc.export_to_markdown(), "meta": {"format": "markdown"}}
