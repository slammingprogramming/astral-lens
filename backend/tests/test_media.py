"""M2 media pipeline: probing, EXIF, C2PA presence, VAD, scenes, transcripts with
timecodes, OCR, translation, and the honest handling of everything that is absent.

HONEST SCOPE. ASR, diarization and translation have no model installed here, so
those steps run against small STUB backends that return canned text. That proves
the plumbing (audio extraction, VAD gating, segment index, timecode anchors,
entity extraction on the transcript, degradation flags) and NOT that any real
recogniser works. ffmpeg, ffprobe and Tesseract are real.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from astral import media
from astral.api import create_app
from astral.config import Settings

from conftest import REPO

CORE = ["media-probe", "transcribe", "translate", "video-scenes", "ocr", "ner", "doc-ingest"]
need_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg not installed")
IBAN = "GB82WEST12345698765432"


# ------------------------------------------------------------------ builders
def run_ffmpeg(*args: str) -> None:
    p = subprocess.run(["ffmpeg", "-v", "error", "-y", *args], capture_output=True)
    if p.returncode != 0:
        pytest.skip("ffmpeg could not build the fixture: " + p.stderr.decode()[:200])


def make_video(path: Path, *, audio: str | None = "tone", creation_time: str | None = None) -> None:
    """Three 2-second colour scenes (hard cuts at 2 s and 4 s)."""
    cmd = []
    for c in ("red", "blue", "green"):
        cmd += ["-f", "lavfi", "-i", f"color=c={c}:s=320x240:d=2:r=10"]
    fc = "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]"
    maps = ["-filter_complex", fc, "-map", "[v]"]
    if audio == "tone":
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=6"]
        maps += ["-map", "3:a"]
    elif audio == "silence":
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "6"]
        maps += ["-map", "3:a", "-t", "6"]
    meta = ["-metadata", f"creation_time={creation_time}"] if creation_time else []
    run_ffmpeg(*cmd, *maps, *meta, "-pix_fmt", "yuv420p", str(path))


def write_wav(path: Path, parts: list[tuple[float, int]]) -> None:
    """parts: (seconds, amplitude). amplitude 0 = silence, else a 440 Hz tone."""
    frames = bytearray()
    for secs, amp in parts:
        for i in range(int(16000 * secs)):
            frames += struct.pack("<h", int(amp * math.sin(2 * math.pi * 440 * i / 16000)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(bytes(frames))


def _ifd(entries, base):
    n = len(entries)
    data_off = base + 2 + 12 * n + 4
    body, data = b"", b""
    for tag, typ, cnt, val in entries:
        if len(val) <= 4:
            field = val.ljust(4, b"\0")
        else:
            field = struct.pack("<I", data_off + len(data))
            data += val + (b"\0" if len(val) % 2 else b"")
        body += struct.pack("<HHI", tag, typ, cnt) + field
    return struct.pack("<H", n) + body + struct.pack("<I", 0) + data


def _ascii(s):
    return (len(s) + 1, s.encode() + b"\0")


def _rat(vals):
    return (len(vals), b"".join(struct.pack("<II", int(round(v * 100)), 100) for v in vals))


def build_jpeg_with_exif(*, make="ACME", model="Cam 1", taken="2024:03:05 10:20:30",
                         lat=(37, 46, 29.64), lat_ref="N", lon=(122, 25, 9.84), lon_ref="W") -> bytes:
    def ifd0(exif_ptr, gps_ptr):
        return [(0x010F, 2, *_ascii(make)), (0x0110, 2, *_ascii(model)),
                (0x8769, 4, 1, struct.pack("<I", exif_ptr)), (0x8825, 4, 1, struct.pack("<I", gps_ptr))]

    def row(e):
        return (e[0], e[1], e[2], e[3])

    exif = [(0x9003, 2, *_ascii(taken))]
    gps = [(1, 2, *_ascii(lat_ref)), (2, 5, *_rat(lat)), (3, 2, *_ascii(lon_ref)), (4, 5, *_rat(lon))]
    l0 = len(_ifd([row(e) for e in ifd0(0, 0)], 8))
    exif_base = 8 + l0
    gps_base = exif_base + len(_ifd([row(e) for e in exif], exif_base))
    tiff = (b"II*\x00" + struct.pack("<I", 8) + _ifd([row(e) for e in ifd0(exif_base, gps_base)], 8)
            + _ifd([row(e) for e in exif], exif_base) + _ifd([row(e) for e in gps], gps_base))
    app1 = b"Exif\0\0" + tiff
    return b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + b"\xff\xd9"


# ------------------------------------------------------------------ stubs
class StubAsr:
    name, capability, model_id = "stub-asr", "asr.transcribe", None

    def __init__(self):
        self.calls = 0

    def available(self):
        return True, ""

    def supports(self, **_):
        return True

    def run(self, *, wav_path, language=None, **_):
        assert Path(wav_path).exists()
        self.calls += 1
        return {"language": "en", "segments": [
            {"start_ms": 500, "end_ms": 2500, "text": f"Please wire the funds to IBAN {IBAN} today.", "confidence": 0.9},
            {"start_ms": 3000, "end_ms": 5200, "text": "Contact Acme Holdings Ltd in London.", "confidence": 0.8}]}


class StubTranslator:
    name, capability, model_id = "stub-translate", "translate.text", None

    def __init__(self, drop=False):
        self.drop = drop

    def available(self):
        return True, ""

    def supports(self, **_):
        return True

    def run(self, *, texts, target, **_):
        out = [f"<{target}> {t}" if t.strip() else t for t in texts]
        return {"texts": out[:-1] if self.drop else out}


# ---------------------------------------------------------------- fixtures
@pytest.fixture()
def client(db, dsn, tmp_path):
    app = create_app(Settings(dsn=dsn, data_dir=tmp_path), db=db)
    with TestClient(app, headers={"x-astral-client": "1"}) as c:
        for n in CORE:
            r = c.post("/plugins", json={"path": str(REPO / "plugins" / "core" / n)})
            assert r.status_code == 201, r.text
            c.post(f"/plugins/{r.json()['id']}/{r.json()['version']}/enabled")
        yield c


def upload(client, case, data: bytes, mime: str) -> str:
    r = client.post(f"/cases/{case}/artifacts", content=data, headers={"content-type": mime})
    assert r.status_code == 201, r.text
    return r.json()["sha256"]


def new_case(client, name="media"):
    return client.post("/cases", json={"name": name}).json()["id"]


# ------------------------------------------------------------- unit: parsers
def test_exif_parser_reads_claims_and_gps():
    ex = media.parse_exif(build_jpeg_with_exif())
    assert ex["make"] == "ACME" and ex["model"] == "Cam 1"
    assert ex["captured"] == "2024-03-05T10:20:30"
    assert ex["gps"] == {"lat": pytest.approx(37.7749, abs=1e-3), "lon": pytest.approx(-122.4194, abs=1e-3)}


def test_exif_absent_and_malformed_are_distinguished():
    assert media.parse_exif(b"\xff\xd8\xff\xd9") == {}
    broken = build_jpeg_with_exif()[:60] + b"\xff\xd9"
    assert "warning" in media.parse_exif(broken)


def test_c2pa_scan_reports_presence_only(tmp_path):
    jpg = tmp_path / "c.jpg"
    app11 = b"JP\x00\x00jumb" + b"\x00" * 8 + b"c2pa" + b"\x00" * 20
    jpg.write_bytes(b"\xff\xd8\xff\xeb" + struct.pack(">H", len(app11) + 2) + app11 + b"\xff\xd9")
    assert media.c2pa_scan(jpg) == {"container": "jpeg", "present": True}
    plain = tmp_path / "p.jpg"
    plain.write_bytes(build_jpeg_with_exif())
    assert media.c2pa_scan(plain)["present"] is False
    png = tmp_path / "c.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I4s", 0, b"caBX") + b"\0\0\0\0"
                    + struct.pack(">I4s", 0, b"IEND") + b"\0\0\0\0")
    assert media.c2pa_scan(png)["present"] is True
    mp4 = tmp_path / "c.mp4"
    mp4.write_bytes(struct.pack(">I4s", 16, b"ftyp") + b"isom\0\0\0\0"
                    + struct.pack(">I4s", 8 + 16 + 4, b"uuid") + media.C2PA_UUID + b"\0\0\0\0")
    assert media.c2pa_scan(mp4)["present"] is True


def test_energy_vad_finds_loud_regions_and_nothing_in_silence(tmp_path):
    w = tmp_path / "a.wav"
    write_wav(w, [(1.0, 0), (1.0, 8000), (1.0, 0), (0.6, 8000), (0.5, 0)])
    r = media.energy_vad(w)
    assert len(r) == 2
    assert 900 <= r[0]["start_ms"] <= 1100 and 1900 <= r[0]["end_ms"] <= 2150
    s = tmp_path / "s.wav"
    write_wav(s, [(2.0, 0)])
    assert media.energy_vad(s) == []
    loud = tmp_path / "l.wav"
    write_wav(loud, [(2.0, 8000)])
    assert len(media.energy_vad(loud)) == 1           # steady loud audio is not mistaken for noise floor


# --------------------------------------------------- end to end through the API
@need_ffmpeg
def test_video_to_timecoded_dossier(client, tmp_path):
    asr = StubAsr()
    client.app.state.host.backends.register(asr)
    vid = tmp_path / "v.mp4"
    make_video(vid, creation_time="2024-03-05T10:20:30Z")
    case = new_case(client)
    sha = upload(client, case, vid.read_bytes(), "video/mp4")
    rep = client.post(f"/cases/{case}/artifacts/{sha}/ingest").json()
    assert rep["kind"] == "video"
    assert {k: v["status"] for k, v in rep["steps"].items() if k != "faces"} == {
        "probe": "ok", "transcribe": "partial", "extract": "ok", "scenes": "ok"}   # partial: speakers unknown
    assert asr.calls == 1

    # the ASR in play was not the best backend: that must be visible, not silent
    tr_cov = {c["source"]: c for c in rep["steps"]["transcribe"]["coverage"]}
    assert tr_cov["speech transcript"]["outcome"] == "hits" and "unreviewed" in tr_cov["speech transcript"]["note"]
    assert tr_cov["speaker labels"]["outcome"] == "unknown"          # no diarizer here: not "no speakers"

    ml = client.get(f"/cases/{case}/media").json()
    assert len(ml) == 1 and ml[0]["kind"] == "video" and ml[0]["has_audio"] is True
    assert ml[0]["sha256"] == sha and 5.5 < ml[0]["duration_s"] < 6.6

    v = client.get(f"/cases/{case}/media/{sha}").json()
    # metadata dates are claims by the file, graded as doubtful
    created = next(p for p in v["properties"] if p["predicate"] == "dateCreated")
    assert created["claimed_by_file"] is True and created["info_grade"] == 4
    assert next(p for p in v["properties"] if p["predicate"] == "hasAudio")["value"] is True

    # transcript with an exact time index and a mention carrying its timecode
    assert v["transcript"]["language"] == "en" and len(v["transcript"]["segments"]) == 2
    iban = next(m for m in v["mentions"] if m["text"] == IBAN)
    assert iban["validated"] is True and iban["t_ms"] == 500
    item = next(e for e in v["timeline"] if e["kind"] == "mention" and e["text"] == IBAN)
    assert item["anchor"]["artifact"] == sha and (item["anchor"]["start_ms"], item["anchor"]["end_ms"]) == (500, 2500)
    seg = v["transcript"]["segments"][0]
    text = client.get(f"/cases/{case}/artifacts/{v['transcript']['text_sha']}").text
    assert text[seg["char_start"]:seg["char_end"]] == seg["text"]       # the index points at real bytes

    # scenes: two hard cuts -> three scenes, each with a keyframe artifact that verifies
    assert [(s["start_ms"] // 1000, s["end_ms"] // 1000) for s in v["scenes"]] == [(0, 2), (2, 4), (4, 6)] or \
        len(v["scenes"]) == 3
    kf = v["scenes"][1]["keyframe"]
    img = client.get(f"/cases/{case}/artifacts/{kf}")
    assert img.status_code == 200 and hashlib.sha256(img.content).hexdigest() == kf
    assert [e["kind"] for e in v["timeline"]][0] == "scene"

    # the creation claim reaches the case timeline, labelled as a claim
    tl = client.get(f"/cases/{case}/timeline").json()
    assert any(e["date"].startswith("2024-03-05") and "claims" in e["what"] for e in tl)

    # derived artifacts are children of the original, so provenance walks back to the file
    arts = {a["derivation"]: a for a in client.get(f"/cases/{case}/artifacts").json() if a["derivation"]}
    assert arts["audio-extract:16k-mono"]["parent_sha256"] == sha
    assert arts["asr-transcript:stub-asr"]["parent_sha256"] == sha

    # the player route serves the original with range support
    r = client.get(f"/cases/{case}/media/{sha}/stream", headers={"range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100 and r.headers["content-type"].startswith("video/")
    assert client.get("/audit/verify").json()["ok"] is True


@need_ffmpeg
def test_no_asr_backend_means_unknown_not_silence(client, tmp_path):
    vid = tmp_path / "v.mp4"
    make_video(vid)
    case = new_case(client)
    sha = upload(client, case, vid.read_bytes(), "video/mp4")
    rep = client.post(f"/cases/{case}/artifacts/{sha}/ingest").json()
    tr = rep["steps"]["transcribe"]
    assert tr["status"] == "error"
    c = tr["coverage"][0]
    assert c["outcome"] == "unknown" and "asr.transcribe" in c["note"] and "ASTRAL_WHISPER_DIR" in c["note"]
    assert rep["steps"]["probe"]["status"] == "ok" and rep["steps"]["scenes"]["status"] == "ok"   # the rest still ran
    assert client.get(f"/cases/{case}/media/{sha}").json()["transcript"] is None


@need_ffmpeg
def test_silence_is_not_sent_to_the_recogniser_and_is_not_called_empty(client, tmp_path):
    asr = StubAsr()
    client.app.state.host.backends.register(asr)
    vid = tmp_path / "quiet.mp4"
    make_video(vid, audio="silence")
    case = new_case(client)
    sha = upload(client, case, vid.read_bytes(), "video/mp4")
    rep = client.post(f"/cases/{case}/artifacts/{sha}/ingest").json()
    assert asr.calls == 0                                          # recognisers invent text for noise
    c = rep["steps"]["transcribe"]["coverage"][0]
    assert c["outcome"] == "no_hits" and "not proof" in c["note"]


@need_ffmpeg
def test_video_without_audio_is_reported_as_such(client, tmp_path):
    vid = tmp_path / "mute.mp4"
    make_video(vid, audio=None)
    case = new_case(client)
    sha = upload(client, case, vid.read_bytes(), "video/mp4")
    rep = client.post(f"/cases/{case}/artifacts/{sha}/ingest").json()
    c = rep["steps"]["transcribe"]["coverage"][0]
    assert c["outcome"] == "no_hits" and "no audio stream" in c["note"]
    assert rep["steps"]["scenes"]["status"] == "ok"


@need_ffmpeg
def test_corrupt_or_unreadable_media_is_unknown(client):
    case = new_case(client)
    sha = upload(client, case, b"this is not a video at all" * 40, "video/mp4")
    rep = client.post(f"/cases/{case}/artifacts/{sha}/ingest").json()
    assert rep["steps"]["probe"]["status"] == "error"
    assert rep["steps"]["probe"]["coverage"][0]["outcome"] == "unknown"
    assert "transcribe" not in rep["steps"]                          # nothing is guessed about a file we cannot read


@need_ffmpeg
def test_exif_image_claims_are_recorded_as_claims(client, tmp_path):
    base = tmp_path / "plain.jpg"
    run_ffmpeg("-f", "lavfi", "-i", "color=c=gray:s=64x48:d=1", "-frames:v", "1", str(base))
    exif = build_jpeg_with_exif()[2:-2]                      # the APP1 segment only
    jpg = b"\xff\xd8" + exif + base.read_bytes()[2:]         # a real image with EXIF inserted after SOI
    case = new_case(client)
    sha = upload(client, case, jpg, "image/jpeg")
    rep = client.post(f"/cases/{case}/artifacts/{sha}/ingest").json()
    cov = {c["source"]: c for c in rep["steps"]["probe"]["coverage"]}
    assert cov["EXIF"]["outcome"] == "hits" and "trivially edited" in cov["EXIF"]["note"]
    assert cov["C2PA Content Credentials"]["outcome"] == "no_hits"
    assert "proves nothing" in cov["C2PA Content Credentials"]["note"]
    v = client.get(f"/cases/{case}/media/{sha}").json()
    loc = next(p for p in v["properties"] if p["predicate"] == "claimedLocation")
    assert loc["claimed_by_file"] is True and loc["info_grade"] == 4
    assert {p["predicate"] for p in v["properties"]} >= {"deviceMake", "deviceModel", "dateCreated"}


@pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")
@need_ffmpeg
def test_ocr_reads_text_from_an_image_and_feeds_entity_extraction(client, tmp_path):
    font = Path("C:/Windows/Fonts/arial.ttf")
    if not font.exists():
        pytest.skip("no font for drawtext")
    png = tmp_path / "sign.png"
    run_ffmpeg("-f", "lavfi", "-i", "color=c=white:s=900x220:d=1",
               "-vf", f"drawtext=fontfile='C\\:/Windows/Fonts/arial.ttf':text='Invoice 48213 Acme Holdings Ltd':"
                      f"fontsize=48:fontcolor=black:x=20:y=80", "-frames:v", "1", str(png))
    case = new_case(client)
    sha = upload(client, case, png.read_bytes(), "image/png")
    rep = client.post(f"/cases/{case}/artifacts/{sha}/ingest").json()
    assert rep["kind"] == "image" and rep["steps"]["ocr"]["status"] == "ok"
    oc = rep["steps"]["ocr"]["coverage"][0]
    assert oc["outcome"] == "hits" and "mean confidence" in oc["note"]
    docs = client.get(f"/cases/{case}/documents").json()
    d = next(x for x in docs if x["title"] == "Text recognised in image")
    view = client.get(f"/cases/{case}/documents/{d['text_sha']}").json()
    assert "48213" in view["text"].replace(" ", "") or "ACME" in view["text"].upper()
    assert any("ACME" in m["text"].upper() for m in view["mentions"])
    # OCR output is graded as doubtful
    props = client.get(f"/cases/{case}/entities/{d['cluster_id']}").json()["properties"]
    assert next(p for p in props if p["predicate"] == "hasText")["info_grade"] == 4


@need_ffmpeg
def test_translation_keeps_the_original_and_refuses_misaligned_output(client, tmp_path):
    client.app.state.host.backends.register(StubAsr())
    tr = StubTranslator()
    client.app.state.host.backends.register(tr)
    vid = tmp_path / "v.mp4"
    make_video(vid)
    case = new_case(client)
    sha = upload(client, case, vid.read_bytes(), "video/mp4")
    client.post(f"/cases/{case}/artifacts/{sha}/ingest")
    v = client.get(f"/cases/{case}/media/{sha}").json()
    text_sha = v["transcript"]["text_sha"]

    host = client.app.state.host
    from astral.db import SOLO_ACTOR
    import uuid
    with client.app.state.db.connection() as conn:
        r = host.invoke(conn, SOLO_ACTOR, uuid.UUID(case), "core.translate",
                        {"artifact": sha, "text_artifact": text_sha}, config={"target": "de"})
        assert r.status == "ok"
        from astral import projector
        projector.rebuild_case(conn, uuid.UUID(case))
    v = client.get(f"/cases/{case}/media/{sha}").json()
    t = v["transcript"]["translations"][0]
    assert t["language"] == "de"
    translated = client.get(f"/cases/{case}/artifacts/{t['text_sha']}").text
    assert translated.startswith("<de> [00:00:00]") or "<de>" in translated
    assert client.get(f"/cases/{case}/artifacts/{text_sha}").status_code == 200       # original untouched

    tr.drop = True
    with client.app.state.db.connection() as conn:
        r = host.invoke(conn, SOLO_ACTOR, uuid.UUID(case), "core.translate",
                        {"artifact": sha, "text_artifact": text_sha}, config={"target": "fr"})
    assert r.status == "error" and any("misaligned" in g for g in r.gaps + [c["note"] for c in r.coverage])


def test_media_backends_never_activate_without_a_local_model_path(monkeypatch):
    from astral.defaults import default_backends

    for var in ("ASTRAL_PARAKEET_DIR", "ASTRAL_WHISPER_DIR", "ASTRAL_PYANNOTE_DIR", "ASTRAL_MADLAD_DIR",
                "ASTRAL_SILERO_VAD_PATH"):
        monkeypatch.delenv(var, raising=False)
    reg = default_backends()
    for cap in ("asr.transcribe", "diarize.speakers", "translate.text"):
        assert all(not b.available()[0] for b in reg.backends(cap)), cap
    res = reg.resolve("audio.vad")
    assert res.backend.name == "energy-vad" and res.degraded and res.degraded_from == "silero-vad"
