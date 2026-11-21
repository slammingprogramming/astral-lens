"""Backends for the media capabilities, best-first per capability.

Verification status, stated plainly:
- EnergyVad and TesseractOcr are exercised by the test suite.
- Every other adapter here (Parakeet, faster-whisper, Silero, pyannote, MADLAD) is
  the documented call surface only. None has been run: their libraries and model
  weights are not installed in the development environment.

No adapter downloads anything. Each activates only when the operator points it at
a LOCAL model path through an environment variable, so a model fetch is never a
side effect of running an investigation, and a `local-only` case cannot leak one.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from astral import media
from astral.backends import module_available


def _dir_from_env(var: str) -> Path | None:
    v = os.environ.get(var)
    return Path(v) if v and Path(v).exists() else None


class _LocalModel:
    """Availability = library importable AND an operator-supplied local path exists."""

    modules: tuple[str, ...] = ()
    env_var = ""

    def available(self) -> tuple[bool, str]:
        for m in self.modules:
            if not module_available(m):
                return False, f"{m} is not installed (and {self.env_var} must point at local model files)"
        if not _dir_from_env(self.env_var):
            return False, f"{self.env_var} is not set to an existing local model path"
        return True, ""

    def supports(self, **_: Any) -> bool:
        return True


# ------------------------------------------------------------------------ ASR
class ParakeetOnnx(_LocalModel):
    """NVIDIA Parakeet-TDT via ONNX on CPU. The INT8 claim for the community
    conversion is unverified at file level (models.yaml)."""

    name, capability, model_id = "parakeet-onnx", "asr.transcribe", "parakeet-tdt-0.6b-v3"
    modules, env_var = ("onnx_asr",), "ASTRAL_PARAKEET_DIR"

    def run(self, *, wav_path: Path, language: str | None = None, **_: Any) -> dict[str, Any]:  # pragma: no cover
        import onnx_asr

        model = onnx_asr.load_model("nemo-parakeet-tdt-0.6b-v3", str(_dir_from_env(self.env_var)))
        res = model.with_timestamps().recognize(str(wav_path))
        res = res if isinstance(res, list) else [res]
        segs = []
        for r in res:
            text = getattr(r, "text", str(r)).strip()
            if text:
                ts = getattr(r, "timestamps", None) or []
                segs.append({"start_ms": int(ts[0] * 1000) if ts else 0,
                             "end_ms": int(ts[-1] * 1000) if ts else 0, "text": text, "confidence": None})
        return {"segments": segs, "language": language}


class FasterWhisper(_LocalModel):
    name, capability, model_id = "faster-whisper", "asr.transcribe", "faster-whisper-large-v3"
    modules, env_var = ("faster_whisper",), "ASTRAL_WHISPER_DIR"

    def run(self, *, wav_path: Path, language: str | None = None, **_: Any) -> dict[str, Any]:  # pragma: no cover
        from faster_whisper import WhisperModel

        model = WhisperModel(str(_dir_from_env(self.env_var)), device="auto", compute_type="int8")
        it, info = model.transcribe(str(wav_path), language=language, vad_filter=True)
        segs = [{"start_ms": int(s.start * 1000), "end_ms": int(s.end * 1000), "text": s.text.strip(),
                 "confidence": round(2.718281828 ** s.avg_logprob, 3)} for s in it if s.text.strip()]
        return {"segments": segs, "language": info.language}


# ------------------------------------------------------------------------ VAD
class SileroVad(_LocalModel):
    name, capability, model_id = "silero-vad", "audio.vad", "silero-vad"
    modules, env_var = ("onnxruntime", "numpy"), "ASTRAL_SILERO_VAD_PATH"

    def run(self, *, wav_path: Path, **_: Any) -> dict[str, Any]:  # pragma: no cover
        import wave

        import numpy as np
        import onnxruntime as ort

        sess = ort.InferenceSession(str(_dir_from_env(self.env_var)))
        with wave.open(str(wav_path), "rb") as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        state = np.zeros((2, 1, 128), dtype=np.float32)
        sr = np.array(16000, dtype=np.int64)
        probs = []
        for i in range(0, len(pcm) - 511, 512):
            out, state = sess.run(None, {"input": pcm[i:i + 512][None, :], "state": state, "sr": sr})
            probs.append(float(out[0][0]))
        regions, cur = [], None
        for i, p in enumerate(probs):
            t = int(i * 512 / 16)
            if p >= 0.5 and cur is None:
                cur = t
            elif p < 0.35 and cur is not None:
                regions.append({"start_ms": cur, "end_ms": t})
                cur = None
        if cur is not None:
            regions.append({"start_ms": cur, "end_ms": int(len(probs) * 512 / 16)})
        return {"regions": regions}


class EnergyVad:
    name, capability, model_id = "energy-vad", "audio.vad", None

    def available(self) -> tuple[bool, str]:
        return True, ""

    def supports(self, **_: Any) -> bool:
        return True

    def run(self, *, wav_path: Path, **_: Any) -> dict[str, Any]:
        return {"regions": media.energy_vad(wav_path)}


# ----------------------------------------------------------------- diarization
class PyannoteDiarizer(_LocalModel):
    name, capability, model_id = "pyannote", "diarize.speakers", "pyannote-community-1"
    modules, env_var = ("pyannote.audio",), "ASTRAL_PYANNOTE_DIR"

    def run(self, *, wav_path: Path, **_: Any) -> dict[str, Any]:  # pragma: no cover
        from pyannote.audio import Pipeline

        pipe = Pipeline.from_pretrained(str(_dir_from_env(self.env_var)))
        dia = pipe(str(wav_path))
        turns = [{"start_ms": int(t.start * 1000), "end_ms": int(t.end * 1000), "speaker": spk}
                 for t, _, spk in dia.itertracks(yield_label=True)]
        return {"turns": turns}


# ----------------------------------------------------------------- translation
class MadladTranslator(_LocalModel):
    name, capability, model_id = "madlad400", "translate.text", "madlad400-3b-mt"
    modules, env_var = ("transformers", "torch", "sentencepiece"), "ASTRAL_MADLAD_DIR"

    def run(self, *, texts: list[str], target: str, **_: Any) -> dict[str, Any]:  # pragma: no cover
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

        p = str(_dir_from_env(self.env_var))
        tok, model = AutoTokenizer.from_pretrained(p), AutoModelForSeq2SeqLM.from_pretrained(p)
        out = []
        for t in texts:
            ids = tok(f"<2{target}> {t}", return_tensors="pt", truncation=True, max_length=512)
            gen = model.generate(**ids, max_new_tokens=512)
            out.append(tok.decode(gen[0], skip_special_tokens=True))
        return {"texts": out}


# ------------------------------------------------------------------------- OCR
class TesseractOcr:
    """Tesseract through its command line. Exercised by the test suite."""

    name, capability, model_id = "tesseract", "ocr.image", None

    def available(self) -> tuple[bool, str]:
        return (True, "") if shutil.which("tesseract") else (False, "tesseract is not installed or not on PATH")

    def supports(self, **_: Any) -> bool:
        return True

    def languages(self) -> list[str]:
        p = subprocess.run([shutil.which("tesseract") or "tesseract", "--list-langs"], capture_output=True, timeout=30)
        return [x.strip() for x in p.stdout.decode("utf-8", "replace").splitlines()[1:] if x.strip()]

    def run(self, *, image_path: Path, lang: str = "eng", **_: Any) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z_]{2,12}(\+[A-Za-z_]{2,12})*", lang):
            raise ValueError("invalid OCR language code")
        missing = [x for x in lang.split("+") if x not in self.languages()]
        if missing:
            raise ValueError(f"tesseract language data not installed: {', '.join(missing)}")
        p = subprocess.run([shutil.which("tesseract"), str(image_path), "stdout", "-l", lang, "tsv"],
                           capture_output=True, timeout=300)
        if p.returncode != 0:
            raise RuntimeError(p.stderr.decode("utf-8", "replace").strip()[:300])
        lines: dict[tuple[int, int, int], list[str]] = {}
        confs: list[float] = []
        for row in p.stdout.decode("utf-8", "replace").splitlines()[1:]:
            c = row.split("\t")
            if len(c) < 12 or not c[11].strip():
                continue
            try:
                conf = float(c[10])
            except ValueError:
                continue
            if conf < 0:
                continue
            lines.setdefault((int(c[2]), int(c[3]), int(c[4])), []).append(c[11].strip())
            confs.append(conf)
        text = "\n".join(" ".join(w) for _, w in sorted(lines.items()))
        return {"text": text, "confidence": round(sum(confs) / len(confs) / 100, 3) if confs else None,
                "words": len(confs), "lang": lang}


def register_media_backends(reg) -> None:
    reg.register(ParakeetOnnx())
    reg.register(FasterWhisper())
    reg.register(SileroVad())
    reg.register(EnergyVad())
    reg.register(PyannoteDiarizer())
    reg.register(MadladTranslator())
    reg.register(TesseractOcr())
