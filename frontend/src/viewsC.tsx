import { useRef, useState } from "react";
import { api } from "./api";
import { Card, ErrorBox, Grade, short, useAsync, AuthLink } from "./ui";

export const fmtMs = (ms: number | null | undefined) => {
  if (ms == null) return "";
  const s = Math.floor(ms / 1000);
  return [Math.floor(s / 3600), Math.floor((s % 3600) / 60), s % 60].map((n) => String(n).padStart(2, "0")).join(":");
};

const C2PA_TEXT: Record<string, string> = {
  present_unvalidated: "Content Credentials present, NOT validated. Presence alone proves little.",
  present_valid: "Content Credentials present and valid. They vouch for the signer's claims, not for the scene.",
  present_invalid: "Content Credentials present but FAILED validation.",
};

export function MediaView({ caseId, bump }: { caseId: string; bump: number }) {
  const list = useAsync(() => api<any[]>(`/cases/${caseId}/media`), [caseId, bump]);
  const [sel, setSel] = useState<string | null>(null);
  return (
    <div className="split" style={{ gridTemplateColumns: "1fr 2.2fr" }}>
      <Card title={`Media (${list.data?.length ?? 0})`}>
        <ErrorBox e={list.error} />
        <table><tbody>
          {(list.data ?? []).map((m) => (
            <tr key={m.cluster_id} onClick={() => m.sha256 && setSel(m.sha256)} style={{ cursor: "pointer", background: sel === m.sha256 ? "var(--line)" : undefined }}>
              <td>{m.name}<div className="muted">{m.kind}{m.duration_s != null ? ` · ${fmtMs(m.duration_s * 1000)}` : ""}</div></td>
              <td className="muted">{m.device}</td>
            </tr>
          ))}
        </tbody></table>
        {list.data?.length === 0 && <span className="muted">No media yet. Add a video, audio or image on the Investigate tab.</span>}
      </Card>
      {sel ? <Detail key={sel} caseId={caseId} sha={sel} /> : <Card><span className="muted">Select a media file.</span></Card>}
    </div>
  );
}

function Detail({ caseId, sha }: { caseId: string; sha: string }) {
  const v = useAsync(() => api<any>(`/cases/${caseId}/media/${sha}`), [caseId, sha]);
  const player = useRef<HTMLVideoElement & HTMLAudioElement>(null);
  const [show, setShow] = useState<Record<string, boolean>>({ scene: true, speech: true, mention: true });
  const d = v.data;
  if (!d) return <Card><ErrorBox e={v.error} /></Card>;
  const src = `/cases/${caseId}/media/${sha}/stream`;
  const seek = (ms: number) => { if (player.current) { player.current.currentTime = ms / 1000; player.current.play?.().catch(() => {}); } };
  const c2pa = d.properties.find((p: any) => p.predicate === "contentCredentials");
  return (
    <div>
      <Card title={`${d.name}`}>
        {d.kind === "video" && <video ref={player} src={src} controls style={{ width: "100%", maxHeight: 340, background: "#000" }} />}
        {d.kind === "audio" && <audio ref={player} src={src} controls style={{ width: "100%" }} />}
        {d.kind === "image" && <img src={src} style={{ maxWidth: "100%", maxHeight: 340 }} alt="" />}
        <div className="note">
          {c2pa ? C2PA_TEXT[c2pa.value] ?? c2pa.value : "No Content Credentials found. Most media carries none, so this proves nothing about whether it is authentic."}
        </div>
        <table><tbody>
          {d.properties.filter((p: any) => p.predicate !== "contentHash" && p.predicate !== "schema").map((p: any) => (
            <tr key={p.assertion_id}>
              <th style={{ width: 150 }}>{p.predicate}</th>
              <td>{typeof p.value === "object" ? JSON.stringify(p.value) : String(p.value)}
                {p.claimed_by_file && <span className="badge" title="Written by whoever made or edited the file. Trivially changed; not a fact about the event."> claimed by file</span>}
              </td>
              <td><Grade s={p.source_grade} i={p.info_grade} /></td>
            </tr>
          ))}
        </tbody></table>
        <div className="muted mono" title={sha}>sha256 {short(sha, 20)}</div>
      </Card>

      {d.transcript?.translations?.length > 0 && (
        <Card title="Translations (machine)">
          {d.transcript.translations.map((t: any) => (
            <div key={t.text_sha}><AuthLink href={`/cases/${caseId}/artifacts/${t.text_sha}`}>{t.language}</AuthLink>
              <span className="muted"> · machine output, read the original</span></div>
          ))}
        </Card>
      )}

      <Card title="Timeline" right={
        <span className="row">{["scene", "speech", "mention"].map((k) => (
          <label key={k}><input type="checkbox" checked={show[k]} onChange={(e) => setShow({ ...show, [k]: e.target.checked })} /> {k}</label>))}</span>}>
        {d.timeline.length === 0 && <span className="muted">Nothing time-coded yet. If this file has speech, a transcription backend may be missing: see the Coverage tab.</span>}
        <div className="timeline">
          {d.timeline.filter((e: any) => show[e.kind]).map((e: any, i: number) => (
            <div className="ev" key={i} style={{ cursor: "pointer" }} onClick={() => seek(e.t_ms)}
              title={`anchor: ${short(e.anchor.artifact, 10)} ${e.anchor.start_ms}–${e.anchor.end_ms} ms`}>
              <span className="mono">{fmtMs(e.t_ms)}</span>
              <span>
                {e.kind === "scene" && e.keyframe && <img src={`/cases/${caseId}/artifacts/${e.keyframe}`} alt="" style={{ height: 36, marginRight: 8, verticalAlign: "middle" }} />}
                {e.kind === "speech" && (e.speaker ? <b>{e.speaker}: </b> : null)}
                {e.kind === "mention" ? <mark className={`m-${e.label}`}>{e.text}</mark> : e.text}
                {e.ocr_text && <div className="muted">on screen: {e.ocr_text}</div>}
              </span>
              <span className="muted">{e.kind}{e.kind === "mention" && e.entity_caption ? ` → ${e.entity_caption}` : ""}</span>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
