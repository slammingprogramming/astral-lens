"""Turn the speech in an audio or video file into a timecoded transcript.

Two derived artifacts carry the result, so every later citation points at stable
bytes: the transcript text, and a segments index mapping each character range back
to a time range in the audio. A mention found in the text is therefore citable as
(media file, start_ms, end_ms).

What this does NOT claim:
- A transcript is machine output: information grade 4 until a person has checked it.
- No voiced energy is "nothing detected", never "silence", and ASR is not run on
  it (recognisers invent text for noise).
- Speakers are only labelled when a diarization backend ran. Labels are anonymous
  (SPEAKER_00); they are never matched to a real person here.
"""

import json

from astral import sdk


def _ts(ms):
    s, _ = divmod(int(ms), 1000)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _speaker_for(seg, turns):
    best, best_ov = None, 0
    for t in turns:
        ov = min(seg["end_ms"], t["end_ms"]) - max(seg["start_ms"], t["start_ms"])
        if ov > best_ov:
            best, best_ov = t["speaker"], ov
    return best


def invoke(request):
    sha = request["input"]["artifact"]
    ns = request["context"]["entity_namespace"]
    cfg = request["config"]
    cov_name = "speech transcript"
    try:
        audio = yield from sdk.call("media.audio", sha256=sha)
    except sdk.CapabilityFailed as e:
        if "no audio stream" in str(e):
            yield sdk.result(stats={"speech": False}, coverage=[sdk.coverage(cov_name, note=str(e))])
        else:
            yield sdk.result(status="error", error=str(e), coverage=[sdk.coverage(cov_name, unknown=str(e))])
        return
    wav = audio["sha256"]

    vad, vad_be = None, None
    try:
        vad = yield from sdk.call("audio.vad", sha256=wav)
        vad_be = vad["backend"]
    except sdk.CapabilityFailed as e:
        yield sdk.log(f"voice-activity detection unavailable ({e}); transcribing the whole track", "warning")
    if vad is not None and not vad["regions"]:
        yield sdk.result(stats={"speech": False, "vad": vad_be["name"]}, coverage=[sdk.coverage(
            cov_name, note=f"no voiced energy found by {vad_be['name']}; this is not proof the file is silent")])
        return

    try:
        asr = yield from sdk.call("asr.transcribe", sha256=wav, language=cfg.get("language"))
    except sdk.CapabilityFailed as e:
        yield sdk.result(status="error", error=str(e), coverage=[sdk.coverage(cov_name, unknown=str(e))])
        return
    be = asr["backend"]
    segs = [s for s in asr["segments"] if (s.get("text") or "").strip()]

    turns, dia_note = [], None
    if cfg.get("diarize", True) and segs:
        try:
            d = yield from sdk.call("diarize.speakers", sha256=wav)
            turns = d["turns"]
        except sdk.CapabilityFailed as e:
            dia_note = str(e)

    lines, index, pos = [], [], 0
    for s in segs:
        spk = _speaker_for(s, turns) if turns else None
        head = f"[{_ts(s['start_ms'])}] " + (f"{spk}: " if spk else "")
        text = s["text"].strip()
        lines.append(head + text)
        index.append({"start_ms": s["start_ms"], "end_ms": s["end_ms"], "speaker": spk, "text": text,
                      "confidence": s.get("confidence"), "char_start": pos + len(head),
                      "char_end": pos + len(head) + len(text)})
        pos += len(head) + len(text) + 1
    body = "\n".join(lines)
    if not body:
        yield sdk.result(stats={"speech": False}, coverage=[sdk.coverage(
            cov_name, note=f"{be['name']} returned no words; not proof nothing was said")])
        return

    t = yield from sdk.put_artifact(body, media_type="text/plain", parent_sha256=sha,
                                    derivation=f"asr-transcript:{be['name']}")
    idx = yield from sdk.put_artifact(
        json.dumps({"version": 1, "media_sha256": sha, "audio_sha256": wav, "text_sha256": t["sha256"],
                    "segments": index}, ensure_ascii=False),
        media_type="application/json", parent_sha256=t["sha256"], derivation="asr-segments")

    confs = [s["confidence"] for s in index if s.get("confidence") is not None]
    by = {"backend": be["name"], "degraded": be["degraded"], "degraded_from": be["degraded_from"],
          "kind": "model", "model": be.get("model"),
          "params": {"audio_sha256": wav, "vad": vad_be["name"] if vad_be else None,
                     "diarization": bool(turns), "mean_confidence": round(sum(confs) / len(confs), 3) if confs else None}}
    doc = sdk.entity_id(ns, "document", sha)
    media = sdk.entity_id(ns, "media", sha)
    ev = [sha, t["sha256"]]
    out = [sdk.a_value(doc, "schema", "Document", ev, 2, derived_by=by),
           sdk.a_value(doc, "title", "Transcript", ev, 4, derived_by=by),
           sdk.a_value(doc, "mimeType", "text/plain", ev, 2, derived_by=by),
           sdk.a_value(doc, "hasText", t["sha256"], ev, 2, derived_by=by),
           sdk.a_value(doc, "hasSegments", idx["sha256"], ev + [idx["sha256"]], 2, derived_by=by),
           sdk.a_edge(doc, "textOf", media, ev, 4, derived_by=by)]
    if asr.get("language"):
        out.append(sdk.a_value(doc, "language", asr["language"], ev, 4, derived_by=by))
    if be["degraded"]:
        yield sdk.log(f"transcribed with {be['name']}; {be['degraded_from']} was not available "
                      f"({'; '.join(be['skipped'])})", "warning")
    cov = [sdk.coverage(cov_name, hits=len(index),
                        note=f"machine transcript ({be['name']}); unreviewed. Timecodes are exact to the audio track")]
    if turns:
        cov.append(sdk.coverage("speaker labels", hits=len({s["speaker"] for s in index if s["speaker"]}),
                                note="anonymous labels, not matched to anyone"))
    else:
        cov.append(sdk.coverage("speaker labels",
                                unknown=dia_note or "diarization was not requested; speakers are unlabelled"))
    yield sdk.emit(out)
    yield sdk.result(stats={"speech": True, "text_artifact": t["sha256"], "segments_artifact": idx["sha256"],
                            "segments": len(index), "backend": be["name"], "language": asr.get("language"),
                            "speech_ms": sum(r["end_ms"] - r["start_ms"] for r in vad["regions"]) if vad else None},
                     coverage=cov)


def selfcheck():
    return {"ok": True}
