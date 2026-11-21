"""Document parsing, entity extraction and web capture (M1)."""

from __future__ import annotations

import io
import zipfile

import pytest

from astral import capture, ner, projector, resolution
from astral.plugins import registry
from astral.plugins.host import PluginHost

from conftest import REPO

CORE = REPO / "plugins" / "core"
IBAN = "GB82 WEST 1234 5698 7654 32"
LEI = "5493001KJTIIGC8Y1R12"
BTC = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
BECH = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"


def enable(db, kit, *names):
    with db.connection() as conn:
        for n in names:
            m = registry.install_from_dir(conn, kit.actor, CORE / n)
            registry.set_state(conn, kit.actor, m.id, m.version, "enabled")
    return PluginHost(writer=kit.writer)


def ingest(db, kit, data: bytes, media_type: str, name="doc") -> str:
    with db.connection() as conn:
        return kit.store.ingest(conn, kit.actor, kit.case, data, acquired_by="upload",
                                media_type=media_type, acquired_from=name).hex


def run_pipeline(host, conn, kit, sha):
    r1 = host.invoke(conn, kit.actor, kit.case, "core.doc-ingest", {"artifact": sha})
    if "text_artifact" not in r1.stats:
        return r1, None
    r2 = host.invoke(conn, kit.actor, kit.case, "core.ner",
                     {"artifact": sha, "text_artifact": r1.stats["text_artifact"]})
    return r1, r2


# ---------------------------------------------------------------- checksums
def test_checksums_accept_real_identifiers_and_reject_corrupted_ones():
    assert ner.iban_valid(IBAN) and not ner.iban_valid("GB82 WEST 1234 5698 7654 33")
    assert ner.lei_valid(LEI) and not ner.lei_valid("5493001KJTIIGC8Y1R13")
    assert ner.btc_legacy_valid(BTC) and not ner.btc_legacy_valid(BTC[:-1] + "b")
    assert ner.bech32_valid(BECH) and not ner.bech32_valid(BECH[:-1] + "p")


def test_rules_ner_extracts_validated_identifiers_with_exact_spans():
    text = (f"Pay {IBAN} or {BTC}. LEI {LEI}. Contact j.doe@example.org, +44 20 7946 0958, "
            f"https://example.org/x. Dr Jane Doe of Acme Holdings Ltd. Bad IBAN GB00 0000 0000 0000 0000 00.")
    ms = ner.RulesNer().run(text=text)
    by = {(m.label, m.text) for m in ms}
    assert ("iban", IBAN) in by and ("btc", BTC) in by and ("lei", LEI) in by
    assert ("email", "j.doe@example.org") in by and ("person", "Jane Doe") in by
    assert ("organization", "Acme Holdings Ltd") in by
    assert not any(m.label == "iban" and "0000 0000 0000 0000 00" in m.text for m in ms)
    for m in ms:
        assert text[m.start:m.end] == m.text                      # spans are exact
    assert all(m.validated for m in ms if m.label in ("iban", "lei", "btc"))
    assert not any(m.label == "url" and "example.org" in m.text and "@" in text[m.start - 1:m.start]
                   for m in ms)                                   # an e-mail's domain is not a URL


# --------------------------------------------------------------- end to end
def test_html_document_to_entities_with_citable_spans(db, kit):
    html = (f"<html><head><title>Board minutes</title><style>x{{}}</style></head><body>"
            f"<p>Dr Jane Doe approved payment to {IBAN}.</p><p>Write to j.doe@example.org</p>"
            f"<script>var a=1</script></body></html>").encode()
    host = enable(db, kit, "doc-ingest", "ner")
    sha = ingest(db, kit, html, "text/html")
    with db.connection() as conn:
        r1, r2 = run_pipeline(host, conn, kit, sha)
        assert r1.status == "ok" and r1.stats["backend"] == "builtin" and not r1.stats["degraded"]
        text_sha = r1.stats["text_artifact"]
        text = kit.store.verified_bytes(bytes.fromhex(text_sha)).decode()
        assert "var a" not in text and "x{}" not in text       # script and style are dropped
        art = conn.execute("SELECT parent_sha256, derivation FROM artifact WHERE sha256=%s",
                           (bytes.fromhex(text_sha),)).fetchone()
        assert bytes(art["parent_sha256"]).hex() == sha and art["derivation"] == "text-extract:builtin"
        assert r2.status == "ok" and r2.stats["entities"] >= 3
        rows = conn.execute(
            "SELECT derived_by FROM assertion WHERE case_id=%s AND predicate='mentions'",
            (kit.case,)).fetchall()
        assert rows
        for r in rows:
            p = r["derived_by"]["params"]
            assert text[p["span"][0]:p["span"][1]] == p["text"]
        projector.rebuild_case(conn, kit.case)
        schemas = {r["schema_name"] for r in conn.execute(
            "SELECT schema_name FROM proj_entity WHERE case_id=%s", (kit.case,))}
    assert {"Document", "Person", "BankAccount", "EmailAddress"} <= schemas


def test_a_pdf_is_parsed_with_the_builtin_backend_and_reports_the_skipped_better_one(db, kit):
    pdf = _simple_pdf("Registered in Zug. Contact dana@example.org")
    host = enable(db, kit, "doc-ingest")
    sha = ingest(db, kit, pdf, "application/pdf")
    with db.connection() as conn:
        res = host.invoke(conn, kit.actor, kit.case, "core.doc-ingest", {"artifact": sha})
        by = conn.execute("SELECT derived_by FROM assertion WHERE case_id=%s AND predicate='schema'",
                          (kit.case,)).fetchone()["derived_by"]
    assert res.status == "ok" and res.stats["degraded"] is True
    assert by["backend"] == "builtin" and by["degraded_from"] in ("docling", "tika")
    assert any("not available" in l for l in res.logs)


def test_an_image_only_pdf_is_unknown_not_empty(db, kit):
    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(200, 200)
    buf = io.BytesIO()
    w.write(buf)
    host = enable(db, kit, "doc-ingest")
    sha = ingest(db, kit, buf.getvalue(), "application/pdf")
    with db.connection() as conn:
        res = host.invoke(conn, kit.actor, kit.case, "core.doc-ingest", {"artifact": sha})
    assert res.coverage[0]["outcome"] == "unknown" and "OCR" in res.coverage[0]["note"]
    assert "text_artifact" not in res.stats


def test_docx_and_eml_are_parsed(db, kit):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml",
                   '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   "<w:body><w:p><w:r><w:t>Hello from Word, mail a@example.org</w:t></w:r></w:p></w:body></w:document>")
    eml = (b"From: Dana <dana@example.org>\r\nTo: Lee <lee@example.net>\r\nSubject: Wire\r\n"
           b"Content-Type: text/plain\r\n\r\nPlease pay " + IBAN.encode() + b"\r\n")
    host = enable(db, kit, "doc-ingest")
    docx = ingest(db, kit, buf.getvalue(),
                  "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    mail = ingest(db, kit, eml, "message/rfc822")
    with db.connection() as conn:
        d = host.invoke(conn, kit.actor, kit.case, "core.doc-ingest", {"artifact": docx})
        m = host.invoke(conn, kit.actor, kit.case, "core.doc-ingest", {"artifact": mail})
        dtext = kit.store.verified_bytes(bytes.fromhex(d.stats["text_artifact"])).decode()
        mtext = kit.store.verified_bytes(bytes.fromhex(m.stats["text_artifact"])).decode()
    assert "Hello from Word" in dtext and "Subject: Wire" in mtext and IBAN in mtext


def test_same_name_in_two_documents_is_two_entities_until_a_human_decides(db, kit):
    host = enable(db, kit, "doc-ingest", "ner")
    a = ingest(db, kit, b"Dr Jane Doe attended the Zurich meeting.", "text/plain", "a.txt")
    b = ingest(db, kit, b"Letter signed by Dr Jane Doe of Basel.", "text/plain", "b.txt")
    with db.connection() as conn:
        run_pipeline(host, conn, kit, a)
        run_pipeline(host, conn, kit, b)
        projector.rebuild_case(conn, kit.case)
        people = conn.execute("SELECT cluster_id FROM proj_entity WHERE case_id=%s AND schema_name='Person'",
                              (kit.case,)).fetchall()
        assert len(people) == 2                                    # never merged by name alone
        cands = resolution.find_candidates(conn, kit.case).candidates
    assert len(cands) == 1 and "name_only" in cands[0].flags


def test_the_same_identifier_in_two_documents_is_one_entity(db, kit):
    host = enable(db, kit, "doc-ingest", "ner")
    a = ingest(db, kit, f"pay {IBAN} now".encode(), "text/plain", "a.txt")
    b = ingest(db, kit, f"again {IBAN.replace(' ', '')} thanks".encode(), "text/plain", "b.txt")
    with db.connection() as conn:
        for sha in (a, b):
            run_pipeline(host, conn, kit, sha)
        projector.rebuild_case(conn, kit.case)
        n = conn.execute("SELECT count(*) AS n FROM proj_entity WHERE case_id=%s AND schema_name='BankAccount'",
                         (kit.case,)).fetchone()["n"]
    # spaced and unspaced spellings are different strings here; documented limit, one per spelling
    assert n == 2


# ---------------------------------------------------------------- capture
def test_web_capture_produces_a_verifiable_warc_and_a_derived_payload(db, kit, server):
    server.add("/page", "<html><body>Hello captured world</body></html>", ctype="text/html")
    host = enable(db, kit, "web-capture")
    with db.connection() as conn:
        res = host.invoke(conn, kit.actor, kit.case, "core.web-capture", {"url": server.url + "/page"})
        warc = kit.store.verified_bytes(bytes.fromhex(res.stats["warc_sha256"]))
        pay = conn.execute("SELECT parent_sha256, derivation, media_type FROM artifact WHERE sha256=%s",
                           (bytes.fromhex(res.stats["payload_sha256"]),)).fetchone()
        acq = conn.execute("SELECT acquisition_ctx FROM artifact_acquisition WHERE sha256=%s AND case_id=%s",
                           (bytes.fromhex(res.stats["warc_sha256"]), kit.case)).fetchone()
    assert res.status == "ok" and res.stats["warc_verified"] and res.stats["status"] == 200
    v = capture.verify_warc(warc)
    assert v["ok"] and v["records"] == 3
    assert capture.payload_of(warc) == b"<html><body>Hello captured world</body></html>"
    assert bytes(pay["parent_sha256"]).hex() == res.stats["warc_sha256"] and pay["derivation"] == "warc-payload"
    assert acq["acquisition_ctx"]["rendering"] == "none" and acq["acquisition_ctx"]["status"] == 200


def test_a_tampered_warc_fails_verification(db, kit, server):
    server.add("/p", "original body text", ctype="text/plain")
    warc = _capture_bytes(server.url + "/p", b"original body text")
    assert capture.verify_warc(warc)["ok"]
    raw = bytearray(_ungz_all(warc))
    i = raw.index(b"original body text")
    raw[i:i + 8] = b"tampered"
    assert capture.verify_warc(bytes(raw))["ok"] is False


def _ungz_all(data: bytes) -> bytes:
    import gzip

    return gzip.decompress(data)


def _capture_bytes(url: str, body: bytes) -> bytes:
    return capture.build_warc(url=url, status=200, response_headers={"content-type": "text/plain"},
                              request_headers={"User-Agent": "t"}, body=body)


def _simple_pdf(text: str) -> bytes:
    """A minimal one-page PDF with a text layer, offsets computed properly."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        None,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = f"BT /F1 12 Tf 10 100 Td ({text}) Tj ET".encode()
    objs[3] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for o in offsets:
        out += b"%010d 00000 n \n" % o
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)
