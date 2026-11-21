"""Default backend registry and the capabilities built on it."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astral import capture, egress, media
from astral.backends import BackendRegistry, NoBackend
from astral.capabilities import CallContext, Capabilities, CapabilityError
from astral.docparse import BuiltinParser, DoclingParser, TikaParser
from astral.faces import register_face_backends
from astral.mediamodels import register_media_backends
from astral.ner import GlinerNer, RulesNer, SpacyNer
from astral.geo import register_geo_backends
from astral.objects import register_object_backends


def default_backends(tika_url: str | None = None) -> BackendRegistry:
    """Best-first per capability. Order encodes quality, so choosing a later
    entry because an earlier one is not installed is reported as degradation."""
    reg = BackendRegistry()
    reg.register(DoclingParser())
    reg.register(TikaParser(tika_url or os.environ.get("ASTRAL_TIKA_URL")))
    reg.register(BuiltinParser())
    reg.register(GlinerNer())
    reg.register(SpacyNer())
    reg.register(RulesNer())
    register_media_backends(reg)
    register_face_backends(reg)
    register_object_backends(reg)
    register_geo_backends(reg)
    return reg


def _backend_info(res) -> dict[str, Any]:
    return {"name": res.backend.name, "degraded": res.degraded,
            "degraded_from": res.degraded_from, "model": getattr(res.backend, "model_id", None),
            "skipped": res.skipped}


def _text_extract(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    from astral.policy import PermissionDenied

    sha = bytes.fromhex(args["sha256"])
    try:
        data = ctx.store.read(ctx.conn, ctx.actor, ctx.case_id, sha)
    except (PermissionDenied, FileNotFoundError) as e:
        raise CapabilityError("artifact not found in this case") from e
    row = ctx.conn.execute("SELECT media_type FROM artifact WHERE sha256=%s", (sha,)).fetchone()
    media_type = args.get("media_type") or row["media_type"]
    try:
        res = ctx.host.backends.resolve("doc.parse", prefer=args.get("prefer"), media_type=media_type)
    except NoBackend as e:
        raise CapabilityError(f"cannot parse {media_type}: {e}") from e
    try:
        out = res.backend.run(data=data, media_type=media_type)
    except Exception as e:                           # noqa: BLE001 - corrupt file, backend crash
        raise CapabilityError(f"{res.backend.name} failed on {media_type}: "
                              f"{type(e).__name__}: {e}") from e
    return {"text": out["text"], "meta": out.get("meta", {}), "media_type": media_type,
            "backend": _backend_info(res)}


def _ner_extract(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        res = ctx.host.backends.resolve("ner.extract", prefer=args.get("prefer"))
    except NoBackend as e:
        raise CapabilityError(str(e)) from e
    text = str(args.get("text") or "")
    try:
        mentions = res.backend.run(text=text, labels=args.get("labels"))
    except Exception as e:                           # noqa: BLE001
        raise CapabilityError(f"{res.backend.name} failed: {type(e).__name__}: {e}") from e
    return {"mentions": [m.as_dict() for m in mentions], "backend": _backend_info(res)}


def _web_capture(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    if ctx.manifest.footprint == "none":
        raise CapabilityError("a plugin declaring footprint 'none' may not capture web pages")
    url = str(args.get("url") or "")
    started = datetime.now(timezone.utc)
    try:
        res = ctx.host.fetcher.fetch(ctx.egress, url, headers=args.get("headers") or {})
    except egress.EgressDenied as e:
        raise CapabilityError(str(e)) from e
    except OSError as e:
        raise CapabilityError(f"network error: {type(e).__name__}: {e}") from e
    ctx.hosts_contacted[egress.check_url(ctx.egress, url)] += 1
    safe_url = ctx.redactor.url(res.final_url)
    warc = capture.build_warc(
        url=safe_url, status=res.status, response_headers=res.headers,
        request_headers={"User-Agent": ctx.egress.user_agent, "Accept-Encoding": "identity"},
        body=res.body, captured_at=started, operator=str(ctx.actor.principal_id),
        egress_mode=ctx.egress.mode)
    log = {"url_requested": ctx.redactor.url(url), "url_final": safe_url, "status": res.status,
           "captured_at": started.isoformat(), "egress": ctx.egress.mode,
           "user_agent": ctx.egress.user_agent,
           "redirects": [ctx.redactor.url(u) for u in res.redirects],
           "rendering": "none", "truncated": res.truncated, "tool": capture.TOOL,
           "elapsed_ms": res.elapsed_ms}
    w = ctx.store.ingest(ctx.conn, ctx.actor, ctx.case_id, warc, acquired_by=ctx.acquired_by,
                         media_type="application/warc", acquired_from=safe_url,
                         acquisition_ctx=log)
    ctype = res.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
    p = ctx.store.ingest(ctx.conn, ctx.actor, ctx.case_id, res.body, acquired_by=ctx.acquired_by,
                         media_type=ctype, acquired_from=safe_url, acquisition_ctx=log,
                         parent_sha256=w.sha256, derivation="warc-payload")
    return {"warc_sha256": w.hex, "payload_sha256": p.hex, "status": res.status,
            "final_url": safe_url, "media_type": ctype, "log": log,
            "warc_verified": capture.verify_warc(warc)["ok"]}


# --------------------------------------------------------------------- media
def _media_path(ctx: CallContext, sha: str, *, verify: bool = False) -> Path:
    from astral.policy import PermissionDenied

    try:
        b = bytes.fromhex(sha)
        p = ctx.store.path_for(ctx.conn, ctx.actor, ctx.case_id, b)
        if verify and not ctx.store.verify_file(b):
            raise CapabilityError("stored evidence does not match its SHA-256; refusing to process it")
    except (PermissionDenied, FileNotFoundError, ValueError) as e:
        raise CapabilityError("artifact not found in this case") from e
    return p


def _guard(fn):
    try:
        return fn()
    except media.MediaError as e:
        raise CapabilityError(str(e)) from e


def _resolve(ctx: CallContext, capability: str, args: dict[str, Any]):
    try:
        return ctx.host.backends.resolve(capability, prefer=args.get("prefer"))
    except NoBackend as e:
        raise CapabilityError(str(e)) from e


def _media_probe(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    return _guard(lambda: media.probe(_media_path(ctx, args["sha256"], verify=True)))


def _exif_extract(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    with _media_path(ctx, args["sha256"]).open("rb") as f:
        return media.parse_exif(f.read(4 << 20))


def _c2pa_check(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    p = _media_path(ctx, args["sha256"])
    scan = media.c2pa_scan(p)
    rep = media.c2patool_report(p) if scan.get("present") else None
    return {**scan, "validation": rep, "verifier": "c2patool" if rep is not None else None}


def _media_audio(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    src = _media_path(ctx, args["sha256"], verify=True)
    info = _guard(lambda: media.probe(src))
    if not info["has_audio"]:
        raise CapabilityError("this file has no audio stream")
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "audio.wav"
        _guard(lambda: media.extract_audio(src, out))
        w = ctx.store.ingest(ctx.conn, ctx.actor, ctx.case_id, out, acquired_by=ctx.acquired_by,
                             media_type="audio/wav", parent_sha256=bytes.fromhex(args["sha256"]),
                             derivation="audio-extract:16k-mono")
    return {"sha256": w.hex, "duration_ms": int((info["duration_s"] or 0) * 1000)}


def _wav_call(capability: str):
    def handler(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        res = _resolve(ctx, capability, args)
        wav = _media_path(ctx, args["sha256"])
        try:
            out = res.backend.run(wav_path=wav, language=args.get("language"))
        except Exception as e:                       # noqa: BLE001 - model/library failure
            raise CapabilityError(f"{res.backend.name} failed: {type(e).__name__}: {e}") from e
        return {**out, "backend": _backend_info(res)}
    return handler


def _translate_text(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    res = _resolve(ctx, "translate.text", args)
    try:
        out = res.backend.run(texts=[str(t) for t in args["texts"]], target=args.get("target", "en"))
    except Exception as e:                           # noqa: BLE001
        raise CapabilityError(f"{res.backend.name} failed: {type(e).__name__}: {e}") from e
    return {**out, "backend": _backend_info(res)}


def _ocr_image(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    res = _resolve(ctx, "ocr.image", args)
    img = _media_path(ctx, args["sha256"])
    try:
        out = res.backend.run(image_path=img, lang=args.get("lang") or "eng")
    except Exception as e:                           # noqa: BLE001
        raise CapabilityError(f"{res.backend.name} failed: {type(e).__name__}: {e}") from e
    return {**out, "backend": _backend_info(res)}


def _media_scenes(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    src = _media_path(ctx, args["sha256"], verify=True)
    info = _guard(lambda: media.probe(src))
    if not info["has_video"]:
        raise CapabilityError("this file has no video stream")
    thr = float(args.get("threshold", 0.4))
    cuts = _guard(lambda: media.scene_cuts(src, thr))
    end = int((info["duration_s"] or 0) * 1000)
    bounds = [0] + [c for c in cuts if 0 < c < end] + [end]
    scenes = [{"start_ms": a, "end_ms": b} for a, b in zip(bounds, bounds[1:]) if b > a]
    cap = int(args.get("max_scenes", 200))
    return {"scenes": scenes[:cap], "total": len(scenes), "truncated": len(scenes) > cap,
            "method": f"ffmpeg scene score > {thr}", "duration_ms": end}


def _media_frame(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    src = _media_path(ctx, args["sha256"])
    t = int(args["t_ms"])
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "frame.jpg"
        _guard(lambda: media.extract_frame(src, t, out))
        w = ctx.store.ingest(ctx.conn, ctx.actor, ctx.case_id, out, acquired_by=ctx.acquired_by,
                             media_type="image/jpeg", parent_sha256=bytes.fromhex(args["sha256"]),
                             derivation=f"keyframe:{t}ms")
    return {"sha256": w.hex, "t_ms": t}


# ---------------------------------------------------------------------- faces
def _licence_of(model_id: str) -> dict[str, Any]:
    from astral.compute.models import load_registry

    m = load_registry().get(model_id)
    return ({"model_id": model_id, "licence": m.licence, "licence_class": m.licence_class,
             "commercial_use": m.commercial_use} if m else {"model_id": model_id, "licence_class": "unknown"})


def _face_embed(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        res = ctx.host.backends.resolve("face.embed", prefer=args.get("prefer"),
                                        allow_noncommercial=bool(args.get("allow_noncommercial")))
    except NoBackend as e:
        raise CapabilityError(str(e)) from e
    path = _media_path(ctx, args["sha256"])
    try:
        out = res.backend.run(data=path.read_bytes(), max_faces=int(args.get("max_faces", 20)),
                              min_score=float(args.get("min_score", 0.7)))
    except Exception as e:                           # noqa: BLE001 - undecodable image, model failure
        raise CapabilityError(f"{res.backend.name} failed: {type(e).__name__}: {e}") from e
    b = res.backend
    return {**out, "backend": _backend_info(res), "model_id": b.model_id, "detector_id": b.detector_id,
            "licences": {"detector": _licence_of(b.detector_id), "embedder": _licence_of(b.model_id)}}


def _face_index(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    from astral import faces

    n = faces.index_faces(ctx.conn, ctx.actor, ctx.case_id, artifact_sha=bytes.fromhex(args["sha256"]),
                          faces=args["faces"], model_id=args["model_id"], backend=args["backend"],
                          frame_ms=args.get("frame_ms"))
    return {"indexed": n}


# -------------------------------------------------------------------- objects
def _object_backend(ctx: CallContext, args: dict[str, Any]):
    try:
        return ctx.host.backends.resolve("object.detect", prefer=args.get("prefer"))
    except NoBackend as e:
        raise CapabilityError(str(e)) from e


def _object_detect(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    res = _object_backend(ctx, args)
    path = _media_path(ctx, args["sha256"])
    try:
        out = res.backend.run(data=path.read_bytes(), min_score=float(args.get("min_score", 0.4)),
                              max_objects=int(args.get("max_objects", 100)))
    except Exception as e:                           # noqa: BLE001 - undecodable image, model failure
        raise CapabilityError(f"{res.backend.name} failed: {type(e).__name__}: {e}") from e
    return {**out, "backend": _backend_info(res), "model_id": res.backend.model_id, "licence": _licence_of(res.backend.model_id)}


def _object_track(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    from astral import objects, tracking

    res = _object_backend(ctx, args)
    src = _media_path(ctx, args["sha256"])
    try:
        out = objects.track_video(src, res.backend, fps=float(args.get("fps", 4.0)), max_frames=int(args.get("max_frames", 600)),
                                  min_score=float(args.get("min_score", 0.3)), tracker=tracking.Tracker())
    except Exception as e:                           # noqa: BLE001 - unreadable video, model failure
        raise CapabilityError(f"{res.backend.name} failed: {type(e).__name__}: {e}") from e
    return {**out, "backend": _backend_info(res), "model_id": res.backend.model_id, "licence": _licence_of(res.backend.model_id)}


def _geo_region(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        res = ctx.host.backends.resolve("geo.region", prefer=args.get("prefer"),
                                        allow_unverified_licence=bool(args.get("allow_unverified_licence")))
    except NoBackend as e:
        hint = "" if args.get("allow_unverified_licence") else (
            " (this model's licence is unverified, so it runs only when the caller sets allow_unverified_licence)")
        raise CapabilityError(f"{e}{hint}") from e
    path = _media_path(ctx, args["sha256"])
    try:
        out = res.backend.run(data=path.read_bytes(), top_k=int(args.get("top_k", 100)),
                              cell_degrees=float(args.get("cell_degrees", 5.0)), max_regions=int(args.get("max_regions", 5)))
    except Exception as e:                           # noqa: BLE001 - undecodable image, model failure
        raise CapabilityError(f"{res.backend.name} failed: {type(e).__name__}: {e}") from e
    return {**out, "backend": _backend_info(res), "model_id": res.backend.model_id,
            "licences": {"geoclip": _licence_of("geoclip"), "backbone": _licence_of("clip-vit-large-patch14")}}


def _image_hash(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
    from astral import pdq
    from astral.backends import module_available

    if not module_available("cv2"):
        raise CapabilityError("opencv is not installed (pip install astral-lens[faces])")
    sha = bytes.fromhex(args["sha256"])
    try:
        h, q = pdq.hash_image(_media_path(ctx, args["sha256"]).read_bytes())
    except ValueError as e:
        raise CapabilityError(str(e)) from e
    ctx.conn.execute(
        "INSERT INTO image_hash (workspace_id, case_id, artifact_sha, algo, hash, quality) VALUES (%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (case_id, artifact_sha, algo) DO NOTHING",
        (ctx.actor.workspace_id, ctx.case_id, sha, pdq.ALGO, h, q))
    return {"hash": h, "quality": q, "algo": pdq.ALGO}


def register_extras(caps: Capabilities) -> None:
    caps.register("face.embed", _face_embed)
    caps.register("face.index", _face_index)
    caps.register("image.hash", _image_hash)
    caps.register("object.detect", _object_detect)
    caps.register("object.track", _object_track)
    caps.register("geo.region", _geo_region)
    caps.register("text.extract", _text_extract)
    caps.register("ner.extract", _ner_extract)
    caps.register("web.capture", _web_capture)
    caps.register("media.probe", _media_probe)
    caps.register("exif.extract", _exif_extract)
    caps.register("c2pa.check", _c2pa_check)
    caps.register("media.audio", _media_audio)
    caps.register("media.scenes", _media_scenes)
    caps.register("media.frame", _media_frame)
    caps.register("audio.vad", _wav_call("audio.vad"))
    caps.register("asr.transcribe", _wav_call("asr.transcribe"))
    caps.register("diarize.speakers", _wav_call("diarize.speakers"))
    caps.register("translate.text", _translate_text)
    caps.register("ocr.image", _ocr_image)
