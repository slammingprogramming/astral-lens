"""Write-once preservation. HONEST SCOPE: the S3 service is an in-process FAKE that implements the parts of
Object Lock this code relies on (checksum verification on put, versions, retention, refusing early
deletion). That proves this code's logic and its refusals. It does NOT prove that any real S3-compatible
server enforces Object Lock: the one test that would (test_against_a_real_s3_server) runs only when
ASTRAL_TEST_S3 points at one, and it did not run in this repository's development environment."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from astral import worm
from astral.api import create_app
from astral.config import Settings


class Denied(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self, lock=True, bucket="b"):
        self.lock, self.bucket, self.objs, self.n = lock, bucket, {}, 0

    def _need(self, Bucket):
        if Bucket != self.bucket:
            raise Denied("NoSuchBucket")

    def head_bucket(self, Bucket):
        self._need(Bucket)

    def get_object_lock_configuration(self, Bucket):
        self._need(Bucket)
        if not self.lock:
            raise Denied("ObjectLockConfigurationNotFoundError")
        return {"ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled"}}

    def put_object(self, Bucket, Key, Body, ChecksumSHA256, ObjectLockMode, ObjectLockRetainUntilDate, Metadata):
        self._need(Bucket)
        if base64.b64encode(hashlib.sha256(Body).digest()).decode() != ChecksumSHA256:
            raise Denied("BadDigest")
        self.n += 1
        vid = f"v{self.n}"
        self.objs[(Key, vid)] = {"data": Body, "mode": ObjectLockMode, "until": ObjectLockRetainUntilDate}
        return {"VersionId": vid}

    def head_object(self, Bucket, Key, VersionId):
        o = self.objs.get((Key, VersionId)) or (_ for _ in ()).throw(Denied("NoSuchKey"))
        return {"ObjectLockMode": o["mode"], "ObjectLockRetainUntilDate": o["until"], "ContentLength": len(o["data"])}

    def get_object(self, Bucket, Key, VersionId):
        o = self.objs.get((Key, VersionId)) or (_ for _ in ()).throw(Denied("NoSuchKey"))
        import io
        return {"Body": io.BytesIO(o["data"])}

    def put_object_retention(self, Bucket, Key, VersionId, Retention):
        o = self.objs[(Key, VersionId)]
        if o["mode"] == "COMPLIANCE" and Retention["RetainUntilDate"] < o["until"]:
            raise Denied("AccessDenied")
        o["until"] = Retention["RetainUntilDate"]

    def delete_object(self, Bucket, Key, VersionId):                   # what compliance mode is for
        o = self.objs[(Key, VersionId)]
        if o["mode"] == "COMPLIANCE" and o["until"] > datetime.now(timezone.utc):
            raise Denied("AccessDenied")
        del self.objs[(Key, VersionId)]


def store(fake, days=30, mode="COMPLIANCE"):
    return worm.WormStore(worm.WormConfig("http://fake", "b", "k", "s", mode=mode, retain_days=days), client=fake)


@pytest.fixture()
def rig(db, dsn, tmp_path):
    fake = FakeS3()
    app = create_app(Settings(dsn=dsn, data_dir=tmp_path), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        app.state.worm = store(fake)
        case = c.post("/cases", json={"name": "worm"}).json()["id"]
        yield c, app, fake, case, tmp_path


def upload(c, case, data):
    r = c.post(f"/cases/{case}/artifacts", content=data, headers={"content-type": "application/octet-stream", "x-astral-filename": "f.bin"})
    assert r.status_code == 201, r.text
    return r.json()["sha256"]


def test_a_bucket_without_object_lock_is_refused_not_used_as_plain_storage():
    s = store(FakeS3(lock=False))
    assert s.status()["object_lock"] is False
    with pytest.raises(worm.WormError, match="not created with Object Lock"):
        s.require_lock()
    with pytest.raises(worm.WormError, match="cannot be reached"):
        worm.WormStore(worm.WormConfig("http://fake", "other", "k", "s"), client=FakeS3()).require_lock()
    for bad in (dict(mode="DELETE"), dict(retain_days=0)):
        with pytest.raises(worm.WormError):
            worm.WormStore(worm.WormConfig("http://fake", "b", "k", "s", **bad))


def test_put_refuses_bytes_that_do_not_match_their_digest():
    s = store(FakeS3())
    with pytest.raises(worm.WormError, match="do not match their digest"):
        s.put("k", b"abc", hashlib.sha256(b"other").hexdigest(), retain_until=datetime.now(timezone.utc))


def test_mirroring_preserves_each_artifact_once_under_a_compliance_lock(rig):
    c, app, fake, case, _ = rig
    a, b = upload(c, case, b"first evidence"), upload(c, case, b"second evidence")
    r = c.post(f"/cases/{case}/worm/mirror").json()
    assert r["preserved"] == 2 and r["failed"] == [] and r["mode"] == "COMPLIANCE" and "cannot be deleted before the retention date" in r["note"]
    assert all(o["mode"] == "COMPLIANCE" and o["until"] > datetime.now(timezone.utc) for o in fake.objs.values())
    again = c.post(f"/cases/{case}/worm/mirror").json()
    assert again["preserved"] == 0 and again["already_preserved"] == 2 and len(fake.objs) == 2
    copies = c.get(f"/cases/{case}/worm").json()["copies"]
    assert {x["sha256"] for x in copies} == {a, b}
    key, vid = next(iter(fake.objs))
    with pytest.raises(Denied):                                              # the point of the mode: nobody can delete it early
        fake.delete_object("b", key, vid)
    assert c.get("/worm").json()["object_lock"] is True


def test_a_corrupt_local_blob_is_never_preserved_as_if_it_were_good(rig):
    c, app, fake, case, tmp = rig
    sha = upload(c, case, b"will be damaged")
    p = app.state.store._path(bytes.fromhex(sha))
    os.chmod(p, 0o666)
    p.write_bytes(b"damaged!")
    r = c.post(f"/cases/{case}/worm/mirror").json()
    assert r["preserved"] == 0 and len(r["failed"]) == 1 and fake.objs == {}


def test_verify_reads_the_bytes_back_and_notices_a_change_or_an_expired_lock(rig):
    c, app, fake, case, _ = rig
    upload(c, case, b"keep me")
    c.post(f"/cases/{case}/worm/mirror")
    ok = c.post(f"/cases/{case}/worm/verify").json()
    assert ok["checked"] == 1 and ok["failed"] == 0
    (key, vid), obj = next(iter(fake.objs.items()))
    obj["data"] = b"someone altered this"                                    # a storage service that lied about immutability
    bad = c.post(f"/cases/{case}/worm/verify").json()
    assert bad["failed"] == 1 and "no longer match the digest" in bad["results"][0]["problems"][0]
    obj["data"] = b"keep me"
    obj["until"] = datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert "retention ended" in c.post(f"/cases/{case}/worm/verify").json()["results"][0]["problems"][0]
    obj["mode"] = None
    assert any("no retention lock" in p for p in c.post(f"/cases/{case}/worm/verify").json()["results"][0]["problems"])
    assert c.get(f"/cases/{case}/worm").json()["copies"][0]["last_verified_ok"] is False


def test_a_lost_local_blob_is_restored_from_the_preserved_copy_after_a_digest_check(rig):
    c, app, fake, case, _ = rig
    data = b"disaster recovery material"
    sha = upload(c, case, data)
    c.post(f"/cases/{case}/worm/mirror")
    p = app.state.store._path(bytes.fromhex(sha))
    os.chmod(p, 0o666)
    p.unlink()
    r = c.post(f"/cases/{case}/worm/restore").json()
    assert r["restored"] == 1 and p.read_bytes() == data
    (key, vid), obj = next(iter(fake.objs.items()))
    obj["data"] = b"tampered"
    os.chmod(p, 0o666)                                                          # the restored blob is read-only, like any stored blob
    p.unlink()
    assert c.post(f"/cases/{case}/worm/restore").json()["restored"] == 0 and not p.exists()      # a bad copy is not restored


def test_erasing_a_case_with_write_once_copies_needs_an_explicit_acknowledgement(rig):
    c, app, fake, case, _ = rig
    upload(c, case, b"subject to retention")
    c.post(f"/cases/{case}/worm/mirror")
    refused = c.post(f"/cases/{case}/erase", json={"legal_basis": "court order"})
    assert refused.status_code == 409 and refused.json()["detail"]["worm_copies"] and "cannot be deleted" in refused.json()["detail"]["message"]
    assert c.get(f"/cases/{case}").json()["state"] != "erased"
    done = c.post(f"/cases/{case}/erase", json={"legal_basis": "court order", "acknowledge_worm": True}).json()
    assert done["worm_retained"] and done["worm_retained"][0]["mode"] == "COMPLIANCE" and done["worm_retained"][0]["retain_until"]
    assert fake.objs                                                           # the preserved copy is still there
    with app.state.db.connection() as conn:
        left = conn.execute("SELECT (SELECT count(*) FROM worm_case WHERE case_id=%s) wc, (SELECT count(*) FROM worm_copy) c", (case,)).fetchone()
    assert left["wc"] == 0 and left["c"] >= 1                                 # the link to the case goes; the record that a copy exists stays


def test_erasing_a_case_without_copies_is_unchanged(rig):
    c, app, fake, case, _ = rig
    upload(c, case, b"plain")
    r = c.post(f"/cases/{case}/erase", json={"legal_basis": "request"})
    assert r.status_code == 200 and r.json()["worm_retained"] == []


def test_an_exhibit_can_be_preserved_as_it_is_exported(rig):
    c, app, fake, case, _ = rig
    upload(c, case, b"exhibit material")
    r = c.post(f"/cases/{case}/exhibit", json={"statement": "prepared by the operator", "worm": True, "include_all_artifacts": True})
    assert r.status_code == 200, r.text
    assert r.headers["x-worm-version"] and r.headers["x-worm-retain-until"]
    key = next(k for (k, v) in fake.objs if k.startswith(f"exhibit/{case}/"))
    assert hashlib.sha256(r.content).hexdigest() in key and fake.objs[(key, r.headers["x-worm-version"])]["data"] == r.content


def test_unconfigured_storage_says_so(db, dsn, tmp_path):
    app = create_app(Settings(dsn=dsn, data_dir=tmp_path), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        case = c.post("/cases", json={"name": "x"}).json()["id"]
        assert c.get("/worm").json() == {"configured": False}
        r = c.post(f"/cases/{case}/worm/mirror")
        assert r.status_code == 400 and "not configured" in r.json()["detail"]


@pytest.mark.skipif(not os.environ.get("ASTRAL_TEST_S3"), reason="ASTRAL_TEST_S3 (endpoint, with ASTRAL_TEST_S3_KEY/_SECRET) not set: no real S3 server was available")
def test_against_a_real_s3_server(tmp_path):
    import boto3
    from datetime import timedelta

    bucket = f"astral-{uuid.uuid4().hex[:10]}"
    cfg = worm.WormConfig(os.environ["ASTRAL_TEST_S3"], bucket, os.environ["ASTRAL_TEST_S3_KEY"], os.environ["ASTRAL_TEST_S3_SECRET"], retain_days=1)
    s = worm.WormStore(cfg)
    s.s3.create_bucket(Bucket=bucket, ObjectLockEnabledForBucket=True)
    s.require_lock()
    data = b"real object lock test"
    h = hashlib.sha256(data).hexdigest()
    put = s.put("t/obj", data, h, retain_until=datetime.now(timezone.utc) + timedelta(days=1))
    assert s.verify("t/obj", put["version_id"], h)["ok"]
    with pytest.raises(worm.WormError):                                      # the server must refuse an early delete
        s._call("delete_object", Bucket=bucket, Key="t/obj", VersionId=put["version_id"])


@pytest.mark.skipif(not os.environ.get("ASTRAL_TEST_S3"), reason="ASTRAL_TEST_S3 not set: no real S3 server was available")
def test_the_api_flow_against_a_real_s3_server(db, dsn, tmp_path):
    """mirror -> verify -> erase refused without acknowledgement -> erase with it, all against a real Object Lock server."""
    bucket = f"astral-{uuid.uuid4().hex[:10]}"
    cfg = worm.WormConfig(os.environ["ASTRAL_TEST_S3"], bucket, os.environ["ASTRAL_TEST_S3_KEY"], os.environ["ASTRAL_TEST_S3_SECRET"], retain_days=1)
    real = worm.WormStore(cfg)
    real.s3.create_bucket(Bucket=bucket, ObjectLockEnabledForBucket=True)
    app = create_app(Settings(dsn=dsn, data_dir=tmp_path), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        app.state.worm = real
        case = c.post("/cases", json={"name": "real-s3"}).json()["id"]
        sha = upload(c, case, b"evidence kept under a real compliance lock")
        m = c.post(f"/cases/{case}/worm/mirror").json()
        assert m["preserved"] == 1 and m["failed"] == [], m
        v = c.post(f"/cases/{case}/worm/verify").json()
        assert v["checked"] == 1 and v["failed"] == 0, v
        copy = c.get(f"/cases/{case}/worm").json()["copies"][0]
        assert copy["sha256"] == sha and copy["mode"] == "COMPLIANCE"
        refused = c.post(f"/cases/{case}/erase", json={"legal_basis": "court order"})
        assert refused.status_code == 409
        assert c.post(f"/cases/{case}/erase", json={"legal_basis": "court order", "acknowledge_worm": True}).status_code == 200
        from botocore.exceptions import ClientError

        with pytest.raises(ClientError) as e:                                                         # still there, still locked
            real.s3.delete_object(Bucket=bucket, Key=copy["key"], VersionId=copy["version_id"])
        assert e.value.response["Error"]["Code"] == "AccessDenied"
