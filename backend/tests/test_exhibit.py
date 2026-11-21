"""M5: exhibit export. A package is only worth something if someone who did not run the
investigation can check it, so most of these tests export a real case through the API and
then attack the bundle (alter a file, an audit entry, the manifest, the signature) and
require the standalone verifier to notice."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from astral import exhibit_verify, signing

from test_ai import DOCS, IBAN, ask_run, make_client, seeded  # noqa: F401  (make_client is a fixture)

STATEMENT = "I collected these three documents on 2026-11-07 from the client's shared folder. I did not alter them."


def export(c, case, **body):
    r = c.post(f"/cases/{case}/exhibit", json={"statement": STATEMENT, **body})
    return r


def unpack(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {n: z.read(n) for n in z.namelist()}


def pack(files: dict[str, bytes], path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return path


@pytest.fixture()
def bundle(make_client, tmp_path):
    c = make_client()
    case = seeded(c)
    p, r = ask_run(c, case, "Which IBAN receives payments for Acme Holdings?")
    assert r.status_code == 200
    resp = export(c, case, plan_ids=[p["id"]])
    assert resp.status_code == 200, resp.text
    return {"client": c, "case": case, "plan": p["id"], "zip": resp.content, "path": pack(unpack(resp.content), tmp_path / "b.zip"),
            "files": unpack(resp.content)}


def test_a_fresh_bundle_verifies_and_contains_what_the_spec_lists(bundle):
    rep = exhibit_verify.verify(bundle["path"])
    assert rep["ok"], [c for c in rep["checks"] if not c["ok"]]
    f = bundle["files"]
    assert {"MANIFEST.json", "SHA256SUMS", "verify.py", "report.md", "operator_statement.md", "assertions.jsonl",
            "capture_log.jsonl", "tools.json", "audit.jsonl", "ai_runs.jsonl", "LIMITS.md", "README.txt"} <= set(f)
    man = json.loads(f["MANIFEST.json"])
    assert man["integrity"]["ok"] and man["counts"]["artifacts_included"] >= 3
    for a in man["artifacts"]:
        assert a["path"] == f"artifacts/{a['sha256']}" and a["path"] in f      # named by the hash of its own bytes
    assert STATEMENT in f["operator_statement.md"].decode()
    report = f["report.md"].decode()
    assert IBAN in report and "## Gaps and limits" in report and "## Source appendix" in report
    assert "admissib" in f["LIMITS.md"].decode().lower()
    tools = json.loads(f["tools.json"])
    assert any(p["id"].endswith("doc-ingest") or "ingest" in p["id"] for p in tools["plugins"]) and tools["platform"]["astral_lens"]
    assert json.loads(f["ai_runs.jsonl"].splitlines()[0])["result"]["claims"]


def test_the_export_is_itself_recorded_in_the_audit_log(bundle, db):
    with db.connection() as conn:
        rows = conn.execute("SELECT detail FROM audit_log WHERE case_id=%s AND action='exhibit.export'", (bundle["case"],)).fetchall()
    assert len(rows) == 1 and rows[0]["detail"]["integrity_ok"] is True and rows[0]["detail"]["bundle_sha256"]
    # ...and it came after the slice that was exported, which says so
    assert b"exhibit.export" not in bundle["files"]["audit.jsonl"]


def test_the_shipped_verifier_runs_standalone_with_no_astral_install(bundle, tmp_path):
    v = tmp_path / "elsewhere" / "verify.py"
    v.parent.mkdir()
    v.write_bytes(bundle["files"]["verify.py"])
    out = subprocess.run([sys.executable, "-I", str(v), str(bundle["path"])], capture_output=True, text=True, cwd=v.parent)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "ALL CHECKS PASSED" in out.stdout and "integrity only" in out.stdout


@pytest.mark.parametrize("what", ["artifact", "audit", "manifest", "extra_file", "missing_file", "sums"])
def test_tampering_is_detected(bundle, tmp_path, what):
    files = dict(bundle["files"])
    some_artifact = next(n for n in files if n.startswith("artifacts/"))
    if what == "artifact":
        files[some_artifact] = files[some_artifact] + b" altered"
    elif what == "audit":
        lines = files["audit.jsonl"].decode().splitlines()
        e = json.loads(lines[1])
        e["fields"]["action"] = "case.read"                       # rewrite history in one entry
        lines[1] = json.dumps(e)
        files["audit.jsonl"] = ("\n".join(lines) + "\n").encode()
    elif what == "manifest":
        m = json.loads(files["MANIFEST.json"])
        m["case"]["name"] = "something else"
        files["MANIFEST.json"] = json.dumps(m).encode()
    elif what == "extra_file":
        files["artifacts/planted.txt"] = b"not in the manifest"
    elif what == "missing_file":
        del files[some_artifact]
    elif what == "sums":
        files["SHA256SUMS"] = files["SHA256SUMS"].replace(b"a", b"b", 1)
    rep = exhibit_verify.verify(pack(files, tmp_path / f"{what}.zip"))
    assert not rep["ok"], f"{what} went unnoticed"


def test_a_corrupted_stored_blob_blocks_the_export_unless_forced(make_client):
    c = make_client()
    case = seeded(c)
    sha = c.get(f"/cases/{case}/documents").json()[0]["text_sha"]
    blob = c.app.state.store._path(bytes.fromhex(sha))
    blob.chmod(0o666)                                                 # blobs are read-only; bit rot does not ask
    blob.write_bytes(b"bit rot")
    r = export(c, case, include_all_artifacts=True)
    assert r.status_code == 409 and any("hash" in p for p in r.json()["detail"]["problems"])
    forced = export(c, case, include_all_artifacts=True, allow_integrity_failures=True)
    assert forced.status_code == 200
    files = unpack(forced.content)
    assert json.loads(files["MANIFEST.json"])["integrity"]["ok"] is False
    assert "INTEGRITY PROBLEMS" in files["README.txt"].decode() and "INTEGRITY PROBLEMS" in files["report.md"].decode()
    rep = exhibit_verify.verify(pack(files, Path(c.app.state.store.root) / "forced.zip"))
    assert not rep["ok"]                                              # a forced export never verifies as clean


def test_refuses_without_an_operator_statement_and_for_unfinished_plans(make_client):
    c = make_client()
    case = seeded(c)
    assert export(c, case, statement="   ").status_code == 409        # the platform cannot write the operator's account
    p = c.post(f"/cases/{case}/ask", json={"question": "Which IBAN receives payments for Acme?"}).json()
    r = export(c, case, plan_ids=[p["id"]])                           # proposed, never approved or run
    assert r.status_code == 409 and "finished answer" in r.json()["detail"]["message"]


def test_a_bundle_holds_only_its_own_cases_evidence(make_client):
    c = make_client()
    a = seeded(c, "a")
    b = c.post("/cases", json={"name": "b"}).json()["id"]
    secret = b"Only case B holds this sentence about Zanzibar."
    sha_b = c.post(f"/cases/{b}/artifacts", content=secret, headers={"content-type": "text/plain"}).json()["sha256"]
    files = unpack(export(c, a, include_all_artifacts=True).content)
    assert f"artifacts/{sha_b}" not in files and secret not in b"".join(files.values())


def test_signed_bundles_verify_with_the_right_key_and_not_with_another(make_client, tmp_path):
    priv, pub = signing.generate_keypair()
    key = tmp_path / "k.pem"
    key.write_bytes(priv)
    c = make_client(signing_key=str(key))
    case = seeded(c)
    resp = export(c, case, sign=True)
    assert resp.status_code == 200
    files = unpack(resp.content)
    p = pack(files, tmp_path / "s.zip")
    ok = exhibit_verify.verify(p, pub)
    assert ok["ok"] and ok["signature"]["valid"] and ok["signature"]["key_fingerprint"] == signing.fingerprint(pub)
    other = signing.generate_keypair()[1]
    assert not exhibit_verify.verify(p, other)["ok"]                  # a key you pinned yourself, not the one in the bundle
    assert "pin the fingerprint" in exhibit_verify.verify(p)["signature"]["key_source"]
    files["MANIFEST.json"] = files["MANIFEST.json"].replace(b'"signed": true', b'"signed": tru3')
    assert not exhibit_verify.verify(pack(files, tmp_path / "s2.zip"), pub)["ok"]
    assert c.post(f"/cases/{case}/exhibit", json={"statement": "x", "sign": True}).status_code == 200
    unsigned_client = make_client()
    assert unsigned_client.post(f"/cases/{seeded(unsigned_client, 'u')}/exhibit", json={"statement": "x", "sign": True}).status_code == 400


def test_signing_primitives():
    priv, pub = signing.generate_keypair("pw")
    assert b"ENCRYPTED" in priv
    s = signing.sign(priv, b"data", "pw")
    assert signing.verify(pub, b"data", s) and not signing.verify(pub, b"datb", s)
    with pytest.raises(signing.SigningError):
        signing.sign(priv, b"data", "wrong")
