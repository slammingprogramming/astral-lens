"""Wall-clock timing of the M2 media pipeline stages on this machine.

    python tests/bench_media.py [minutes] [width] [threads]      (default 10 1280 4)

WHAT THIS MEASURES: real ffmpeg decode/scene detection/audio extraction, the
energy VAD, keyframe extraction, Tesseract on keyframes, and the whole ingest
through the API (hashing, database, plugin overhead).
WHAT IT DOES NOT: speech recognition. No ASR model is installed here, so the
recogniser is a stub that returns instantly and its cost is NOT in any number.
The video is SYNTHETIC (ffmpeg testsrc2), far simpler than real footage; decode
and scene-detection times for real camera video will be higher. Compare the
figures per media-minute, not as a forecast.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from astral import media  # noqa: E402
from astral.api import create_app  # noqa: E402
from astral.config import Settings  # noqa: E402
from astral.db import Database  # noqa: E402
from astral.mediamodels import TesseractOcr  # noqa: E402

from conftest import ADMIN_DSN, REPO  # noqa: E402
from test_media import CORE, StubAsr  # noqa: E402


def timed(label: str, rows: list, fn):
    t = time.perf_counter()
    out = fn()
    rows.append((label, time.perf_counter() - t))
    return out


def main() -> None:
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 10
    width = int(sys.argv[2]) if len(sys.argv) > 2 else 1280
    threads = sys.argv[3] if len(sys.argv) > 3 else "4"
    height = width * 9 // 16 // 2 * 2
    work = Path(tempfile.mkdtemp(prefix="astral-bench-"))
    src = work / "bench.mp4"
    secs = int(minutes * 60)

    t = time.perf_counter()
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-threads", threads,
                    "-f", "lavfi", "-i", f"testsrc2=s={width}x{height}:r=25:d={secs}",
                    "-f", "lavfi", "-i", f"sine=frequency=300:duration={secs}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], check=True)
    gen = time.perf_counter() - t
    rows: list[tuple[str, float]] = []

    info = timed("ffprobe", rows, lambda: media.probe(src))
    wav = work / "a.wav"
    timed("audio extract (16 kHz mono)", rows, lambda: media.extract_audio(src, wav))
    regions = timed("energy VAD", rows, lambda: media.energy_vad(wav))
    cuts = timed("scene cuts (ffmpeg, full decode)", rows, lambda: media.scene_cuts(src, 0.4))
    n = min(len(cuts) + 1, 50)
    frames = []
    t = time.perf_counter()
    for i in range(n):
        f = work / f"f{i}.jpg"
        media.extract_frame(src, int((i + 0.5) * secs * 1000 / n), f)
        frames.append(f)
    rows.append((f"keyframes x{n}", time.perf_counter() - t))
    ocr = TesseractOcr()
    ocr_ok = ocr.available()[0]
    if ocr_ok:
        t = time.perf_counter()
        for f in frames:
            ocr.run(image_path=f)
        rows.append((f"Tesseract OCR x{n}", time.perf_counter() - t))

    # whole ingest through the API (stub ASR)
    name = f"astral_bench_{uuid.uuid4().hex[:6]}"
    admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    admin.execute(f'CREATE DATABASE "{name}"')
    dsn = ADMIN_DSN.rsplit("/", 1)[0] + "/" + name
    db = Database(dsn).open()
    db.migrate()
    db.bootstrap_solo()
    try:
        app = create_app(Settings(dsn=dsn, data_dir=work / "data"), db=db)
        with TestClient(app, headers={"x-astral-client": "1"}) as c:
            for p in CORE:
                r = c.post("/plugins", json={"path": str(REPO / "plugins" / "core" / p)}).json()
                c.post(f"/plugins/{r['id']}/{r['version']}/enabled")
            app.state.host.backends.register(StubAsr())
            case = c.post("/cases", json={"name": "bench"}).json()["id"]
            t = time.perf_counter()
            sha = c.post(f"/cases/{case}/artifacts", content=src.read_bytes(), headers={"content-type": "video/mp4"}).json()["sha256"]
            rows.append(("upload + SHA-256 + store", time.perf_counter() - t))
            t = time.perf_counter()
            rep = c.post(f"/cases/{case}/artifacts/{sha}/ingest").json()
            ingest = time.perf_counter() - t
            steps = {k: v["status"] for k, v in rep["steps"].items()}
    finally:
        db.close()
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()

    mb = src.stat().st_size / 1e6
    print(f"\nHost: {platform.platform()}, {os.cpu_count()} logical CPUs, ffmpeg threads={threads}")
    print(f"Synthetic video: {minutes:g} min, {width}x{height}@25, {mb:.1f} MB, audio 16k-mono wav {wav.stat().st_size/1e6:.1f} MB")
    print(f"(generation took {gen:.1f}s; not part of the pipeline)")
    print(f"Scenes found: {len(cuts) + 1}; voiced regions: {len(regions)}; duration {info['duration_s']:.1f}s\n")
    print(f"{'stage':<42}{'seconds':>9}{'x realtime':>12}")
    for label, s in rows:
        print(f"{label:<42}{s:>9.2f}{(secs / s if s else 0):>12.0f}")
    print(f"{'FULL INGEST via API (stub ASR)':<42}{ingest:>9.2f}{secs / ingest:>12.1f}   steps={steps}")
    print("\nNot measured: speech recognition, diarization, translation (no models installed); peak memory.")


if __name__ == "__main__":
    main()
