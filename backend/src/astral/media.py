"""Media primitives that need no model: probing, audio extraction, scene cuts,
frames, a crude voice-activity detector, EXIF and a C2PA presence scan.

Everything here is honest about what it can and cannot show:
- EXIF and container tags are what the FILE CLAIMS. They are trivially edited, so
  callers grade them as doubtful (info 4), never as the time or place of the event.
- Finding no C2PA manifest proves nothing; finding one without validating it
  proves little. `c2pa_scan` only reports presence.
- The energy VAD sees loudness, not speech.
"""

from __future__ import annotations

import array
import json
import math
import re
import shutil
import struct
import subprocess
import wave
from pathlib import Path
from typing import Any

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


class MediaError(Exception):
    pass


def tools_available() -> tuple[bool, str]:
    if FFMPEG and FFPROBE:
        return True, ""
    return False, "ffmpeg/ffprobe not found on PATH"


def _run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise MediaError(f"{Path(cmd[0]).name} timed out after {timeout}s") from e
    except OSError as e:
        raise MediaError(f"cannot run {cmd[0]}: {e}") from e


def fmt_ms(ms: int) -> str:
    s, ms = divmod(int(ms), 1000)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ------------------------------------------------------------------- ffprobe
def probe(path: Path) -> dict[str, Any]:
    ok, why = tools_available()
    if not ok:
        raise MediaError(why)
    p = _run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], 60)
    if p.returncode != 0:
        raise MediaError("ffprobe could not read this file: " + p.stderr.decode("utf-8", "replace").strip()[:300])
    raw = json.loads(p.stdout or b"{}")
    fmt, streams = raw.get("format", {}), raw.get("streams", [])
    out_streams = []
    for s in streams:
        t = s.get("codec_type")
        item: dict[str, Any] = {"type": t, "codec": s.get("codec_name")}
        if t == "video":
            item.update(width=s.get("width"), height=s.get("height"), fps=_fps(s.get("avg_frame_rate")),
                        attached_pic=bool(s.get("disposition", {}).get("attached_pic")))
        elif t == "audio":
            item.update(sample_rate=int(s["sample_rate"]) if s.get("sample_rate") else None,
                        channels=s.get("channels"))
        out_streams.append(item)
    real_video = [s for s in out_streams if s["type"] == "video" and not s.get("attached_pic")]
    audio = [s for s in out_streams if s["type"] == "audio"]
    names = (fmt.get("format_name") or "").split(",")
    still = any(n.endswith("_pipe") or n in ("image2", "png_pipe", "tiff_pipe") for n in names)
    kind = ("image" if still and real_video else "video" if real_video else "audio" if audio else "other")
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    for s in streams:
        for k, v in (s.get("tags") or {}).items():
            tags.setdefault(k.lower(), v)
    try:
        duration = float(fmt["duration"]) if fmt.get("duration") not in (None, "N/A") else None
    except (ValueError, TypeError):
        duration = None
    return {"kind": kind, "container": fmt.get("format_long_name") or fmt.get("format_name"),
            "format_names": names, "duration_s": duration, "bit_rate": _int(fmt.get("bit_rate")),
            "streams": out_streams, "has_audio": bool(audio), "has_video": bool(real_video),
            "tags": {k: tags[k] for k in ("creation_time", "title", "artist", "encoder", "location",
                                          "com.apple.quicktime.location.iso6709", "make", "model") if k in tags}}


def _fps(rate: str | None) -> float | None:
    try:
        a, b = (rate or "0/1").split("/")
        return round(int(a) / int(b), 3) if int(b) else None
    except (ValueError, ZeroDivisionError):
        return None


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------- audio / frames
def extract_audio(src: Path, dst: Path) -> None:
    """16 kHz mono PCM: what every ASR/VAD/diarization backend expects."""
    p = _run([FFMPEG, "-v", "error", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
              "-c:a", "pcm_s16le", "-f", "wav", str(dst)])
    if p.returncode != 0 or not dst.exists() or dst.stat().st_size < 64:
        raise MediaError("audio extraction failed: " + p.stderr.decode("utf-8", "replace").strip()[:300])


def scene_cuts(src: Path, threshold: float = 0.4) -> list[int]:
    """Timestamps (ms) where the picture changes sharply, via ffmpeg's scene score.
    A cut detector, not a semantic one: fades and slow pans are missed."""
    p = _run([FFMPEG, "-v", "info", "-i", str(src), "-an", "-vf", f"select='gt(scene,{threshold})',showinfo",
              "-f", "null", "-"], 1800)
    if p.returncode != 0:
        raise MediaError("scene detection failed: " + p.stderr.decode("utf-8", "replace").strip()[-300:])
    out = []
    for m in re.finditer(rb"pts_time:([0-9.]+)", p.stderr):
        out.append(int(round(float(m.group(1)) * 1000)))
    return sorted(set(out))


def extract_frame(src: Path, t_ms: int, dst: Path, max_width: int = 960) -> None:
    p = _run([FFMPEG, "-v", "error", "-y", "-ss", f"{t_ms / 1000:.3f}", "-i", str(src), "-frames:v", "1",
              "-vf", f"scale='min({max_width},iw)':-2", "-q:v", "3", str(dst)], 120)
    if p.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise MediaError(f"no frame at {t_ms} ms: " + p.stderr.decode("utf-8", "replace").strip()[:200])


# ---------------------------------------------------------------- energy VAD
def energy_vad(wav_path: Path, *, frame_ms: int = 30, min_speech_ms: int = 250,
               min_gap_ms: int = 400) -> list[dict[str, int]]:
    """Voiced-energy regions: frames louder than an adaptive floor. It cannot tell
    speech from music or a door slam; it exists to skip silence cheaply."""
    with wave.open(str(wav_path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise MediaError("energy VAD expects 16-bit mono PCM")
        rate, raw = w.getframerate(), w.readframes(w.getnframes())
    samples = array.array("h")
    samples.frombytes(raw)
    step = max(1, rate * frame_ms // 1000)
    rms = []
    for i in range(0, len(samples) - step + 1, step):
        seg = samples[i:i + step]
        rms.append(math.sqrt(sum(x * x for x in seg) / len(seg)))
    if not rms:
        return []
    ordered = sorted(rms)
    floor = ordered[len(ordered) // 10]                        # quietest decile = noise floor
    peak = ordered[-1]
    thr = max(min(floor * 3.0, peak * 0.5), peak * 0.05, 50.0)    # capped: steady loud audio is still voiced
    regions: list[list[int]] = []
    for i, v in enumerate(rms):
        t0 = i * frame_ms
        if v >= thr:
            if regions and t0 - regions[-1][1] <= min_gap_ms:
                regions[-1][1] = t0 + frame_ms
            else:
                regions.append([t0, t0 + frame_ms])
    return [{"start_ms": a, "end_ms": b} for a, b in regions if b - a >= min_speech_ms]


# ---------------------------------------------------------------------- EXIF
_TYPES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}


def parse_exif(data: bytes) -> dict[str, Any]:
    """Minimal, defensive EXIF reader for JPEG (APP1) and bare TIFF: make, model,
    software, orientation, capture time and GPS. Returns {} when there is none."""
    tiff = _find_tiff(data)
    if tiff is None:
        return {}
    try:
        return _read_tiff(tiff)
    except (struct.error, IndexError, ValueError, OverflowError):
        return {"warning": "EXIF block is present but malformed"}


def _find_tiff(data: bytes) -> bytes | None:
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 4 <= len(data) and data[i] == 0xFF:
            marker = data[i + 1]
            if marker in (0xDA, 0xD9):
                break
            (ln,) = struct.unpack(">H", data[i + 2:i + 4])
            seg = data[i + 4:i + 2 + ln]
            if marker == 0xE1 and seg[:6] == b"Exif\x00\x00":
                return seg[6:]
            i += 2 + ln
        return None
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return data
    return None


def _read_tiff(t: bytes) -> dict[str, Any]:
    e = "<" if t[:2] == b"II" else ">"
    (first,) = struct.unpack(e + "I", t[4:8])

    def ifd(off: int) -> dict[int, Any]:
        (n,) = struct.unpack(e + "H", t[off:off + 2])
        out: dict[int, Any] = {}
        for k in range(min(n, 512)):
            tag, typ, cnt, val = struct.unpack(e + "HHI4s", t[off + 2 + 12 * k:off + 14 + 12 * k])
            size = _TYPES.get(typ, 1) * cnt
            raw = val[:size] if size <= 4 else t[struct.unpack(e + "I", val)[0]:][:size]
            out[tag] = _value(e, typ, cnt, raw)
        return out

    i0 = ifd(first)
    res: dict[str, Any] = {}
    for tag, key in ((0x010F, "make"), (0x0110, "model"), (0x0131, "software"), (0x0112, "orientation")):
        if tag in i0:
            res[key] = i0[tag]
    dt = i0.get(0x0132)
    if 0x8769 in i0:
        ex = ifd(i0[0x8769])
        dt_orig = ex.get(0x9003) or ex.get(0x9004)
        if dt_orig:
            res["captured"] = _exif_time(dt_orig)
        if ex.get(0x9011):
            res["offset_time"] = ex[0x9011]
    if "captured" not in res and dt:
        res["modified"] = _exif_time(dt)
    if 0x8825 in i0:
        g = ifd(i0[0x8825])
        lat, lon = _gps(g.get(2), g.get(1)), _gps(g.get(4), g.get(3))
        if lat is not None and lon is not None:
            res["gps"] = {"lat": round(lat, 6), "lon": round(lon, 6)}
    return {k: v for k, v in res.items() if v not in (None, "")}


def _value(e: str, typ: int, cnt: int, raw: bytes) -> Any:
    if typ == 2:
        return raw.split(b"\x00")[0].decode("ascii", "replace").strip()
    if typ == 3:
        v = list(struct.unpack(e + "H" * cnt, raw[:2 * cnt]))
    elif typ == 4:
        v = list(struct.unpack(e + "I" * cnt, raw[:4 * cnt]))
    elif typ in (5, 10):
        f = e + ("II" if typ == 5 else "ii")
        v = [struct.unpack(f, raw[8 * k:8 * k + 8]) for k in range(cnt)]
        v = [(a / b) if b else None for a, b in v]
    elif typ == 1:
        v = list(raw[:cnt])
    else:
        return raw
    return v[0] if len(v) == 1 else v


def _gps(dms: Any, ref: Any) -> float | None:
    if not isinstance(dms, list) or len(dms) != 3 or any(x is None for x in dms):
        return None
    deg = dms[0] + dms[1] / 60 + dms[2] / 3600
    return -deg if str(ref).upper() in ("S", "W") else deg


def _exif_time(s: str) -> str | None:
    m = re.fullmatch(r"(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", s.strip())
    return f"{m[1]}-{m[2]}-{m[3]}T{m[4]}:{m[5]}:{m[6]}" if m else None


# ---------------------------------------------------------------------- C2PA
C2PA_UUID = bytes.fromhex("d8fec3d61b0e483c92975828877ec481")


def c2pa_scan(path: Path) -> dict[str, Any]:
    """Is a Content Credentials manifest embedded? Presence only; no signature or
    hash is checked here. Returns {present, container}."""
    with path.open("rb") as f:
        head = f.read(12)
        f.seek(0)
        if head[:2] == b"\xff\xd8":
            return {"container": "jpeg", "present": _jpeg_has_c2pa(f)}
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return {"container": "png", "present": _png_has_c2pa(f)}
        if head[4:8] in (b"ftyp", b"moov", b"mdat", b"free") or head[4:8] == b"wide":
            return {"container": "isobmff", "present": _bmff_has_c2pa(f, path.stat().st_size)}
    return {"container": None, "present": False, "unsupported": True}


def _jpeg_has_c2pa(f) -> bool:
    f.seek(2)
    while True:
        h = f.read(4)
        if len(h) < 4 or h[0] != 0xFF or h[1] in (0xDA, 0xD9):
            return False
        (ln,) = struct.unpack(">H", h[2:4])
        body = f.read(ln - 2)
        if h[1] == 0xEB and b"jumb" in body[:16] and b"c2pa" in body[:256]:
            return True


def _png_has_c2pa(f) -> bool:
    f.seek(8)
    while True:
        h = f.read(8)
        if len(h) < 8:
            return False
        ln, typ = struct.unpack(">I4s", h)
        if typ == b"caBX":
            return True
        if typ == b"IEND":
            return False
        f.seek(ln + 4, 1)


def _bmff_has_c2pa(f, size: int) -> bool:
    pos = 0
    while pos + 8 <= size:
        f.seek(pos)
        h = f.read(8)
        ln, typ = struct.unpack(">I4s", h)
        hdr = 8
        if ln == 1:
            (ln,) = struct.unpack(">Q", f.read(8))
            hdr = 16
        elif ln == 0:
            ln = size - pos
        if typ == b"uuid" and f.read(16) == C2PA_UUID:
            return True
        if ln < hdr:
            return False
        pos += ln
    return False


def c2patool_report(path: Path) -> dict[str, Any] | None:
    """Validate with the `c2patool` binary when installed. None = no verifier here."""
    exe = shutil.which("c2patool")
    if not exe:
        return None
    p = _run([exe, str(path)], 120)
    try:
        doc = json.loads(p.stdout or b"{}")
    except ValueError:
        return {"validated": False, "error": (p.stderr or p.stdout).decode("utf-8", "replace")[:300]}
    status = doc.get("validation_status") or doc.get("validation_results") or []
    return {"validated": p.returncode == 0 and not status, "problems": status,
            "active_manifest": doc.get("active_manifest")}
