import { useState } from "react";
import { api, post } from "./api";
import { Card, ErrorBox, short, useAsync } from "./ui";

async function uploadImage(caseId: string, f: File): Promise<string> {
  const up = await api(`/cases/${caseId}/artifacts`, {
    method: "POST", body: await f.arrayBuffer(), headers: { "Content-Type": f.type || "application/octet-stream" },
  });
  return up.sha256;
}

export function FacesView({ caseId, bump, onChanged }: { caseId: string; bump: number; onChanged: () => void }) {
  const faces = useAsync(() => api<any[]>(`/cases/${caseId}/faces`), [caseId, bump]);
  const dups = useAsync(() => api<any[]>(`/cases/${caseId}/images/near-duplicates`), [caseId, bump]);
  const [res, setRes] = useState<any>(null);
  const [scope, setScope] = useState("all");
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [label, setLabel] = useState("");
  const [basis, setBasis] = useState("");

  async function search(body: any) {
    setErr(null); setBusy(true);
    try { setRes(await post(`/cases/${caseId}/faces/search`, { scope, k: 10, ...body })); }
    catch (e) { setErr(e); setRes(null); } finally { setBusy(false); }
  }
  async function probe(f: File) {
    try { await search({ artifact: await uploadImage(caseId, f) }); onChanged(); } catch (e) { setErr(e); }
  }
  async function enrol(f: File) {
    setErr(null);
    try {
      await post(`/cases/${caseId}/watchlist`, { artifact: await uploadImage(caseId, f), label, basis });
      setLabel(""); setBasis(""); faces.reload(); onChanged();
    } catch (e) { setErr(e); }
  }
  async function remove(id: string) { await api(`/cases/${caseId}/watchlist/${id}`, { method: "DELETE" }); faces.reload(); }

  const all = faces.data ?? [];
  const watch = all.filter((f) => f.watchlist);
  const found = all.filter((f) => !f.watchlist);
  return (
    <>
      <Card title="Find similar faces">
        <p className="muted">
          Ranks faces in this case's media (and your watchlist) by similarity to a photo. <b>Results are candidates, not identifications.</b>{" "}
          Every search is recorded in the audit log. There is no index of the general public; the gallery is only what is in this case.
        </p>
        <div className="row">
          <label className="row"><span className="muted">Probe photo</span>
            <input type="file" accept="image/*" disabled={busy} onChange={(e) => e.target.files?.[0] && probe(e.target.files[0])} /></label>
          <select value={scope} onChange={(e) => setScope(e.target.value)}>
            <option value="all">case media + watchlist</option><option value="case">case media only</option><option value="watchlist">watchlist only</option>
          </select>
        </div>
        <ErrorBox e={err} />
        {res && (
          <>
            <div className="note">
              backend <b>{res.backend}</b> · model <b>{res.model_id}</b> ({res.licences[res.model_id]?.licence}, {res.licences[res.model_id]?.licence_class})
              · {res.faces_in_probe} face(s) in the probe · {res.notice}
            </div>
            {res.candidates.length === 0 && <span className="muted">No candidates in this scope. That is not evidence the person is absent from other media.</span>}
            <table><thead><tr><th></th><th>similarity</th><th>source</th><th>detail</th></tr></thead><tbody>
              {res.candidates.map((c: any) => (
                <tr key={c.face_id}>
                  <td><img src={`/cases/${caseId}/faces/${c.face_id}/crop`} alt="" style={{ height: 56 }} /></td>
                  <td><b>{c.score.toFixed(3)}</b>
                    <div className="muted" title="OpenCV's published SFace operating point on the LFW benchmark. It does not transfer to poor-quality images.">
                      {c.above_reference ? "above" : "below"} reference {c.reference_threshold}</div></td>
                  <td>{c.watchlist ? <span className="badge">watchlist: {c.label}</span> : <a href={`/cases/${caseId}/artifacts/${c.artifact}`} target="_blank" rel="noreferrer" className="mono">{short(c.artifact, 12)}</a>}
                    {c.frame_ms != null && <span className="muted"> @{Math.round(c.frame_ms / 1000)}s</span>}</td>
                  <td className="muted">{c.backend} · detector score {c.det_score}</td>
                </tr>
              ))}
            </tbody></table>
          </>
        )}
      </Card>

      <div className="split">
        <Card title={`Faces found in case media (${found.length})`}>
          <div className="row" style={{ flexWrap: "wrap" }}>
            {found.slice(0, 60).map((f) => (
              <div key={f.id} style={{ textAlign: "center" }}>
                <img src={`/cases/${caseId}/faces/${f.id}/crop`} alt="" style={{ height: 72, display: "block" }} />
                <button disabled={busy} onClick={() => search({ face_id: f.id })}>similar</button>
              </div>
            ))}
          </div>
          {found.length === 0 && <span className="muted">None yet. Add images or video on the Investigate tab. If detection is unavailable the Coverage tab says why.</span>}
          <p className="muted">Face embeddings are biometric data and are deleted automatically when their retention period ends (case policy, at most 3 years).</p>
        </Card>
        <Card title={`Watchlist (${watch.length})`}>
          <p className="muted">Reference photos you add yourself. Each needs exactly one face. The reason is recorded with it; it is not judged.</p>
          <div className="row">
            <input placeholder="Label" value={label} onChange={(e) => setLabel(e.target.value)} />
            <input className="grow" placeholder="Basis / purpose" value={basis} onChange={(e) => setBasis(e.target.value)} />
            <input type="file" accept="image/*" disabled={!label} onChange={(e) => e.target.files?.[0] && enrol(e.target.files[0])} />
          </div>
          <table><tbody>{watch.map((f) => (
            <tr key={f.id}><td><img src={`/cases/${caseId}/faces/${f.id}/crop`} alt="" style={{ height: 48 }} /></td>
              <td>{f.label}</td><td><button onClick={() => remove(f.id)}>remove</button></td></tr>))}</tbody></table>
        </Card>
      </div>

      <Card title="Near-duplicate images">
        <p className="muted">Perceptual-hash screening (distance out of 256 bits). A small distance means likely the same picture re-encoded; it is not proof.</p>
        <table><tbody>{(dups.data ?? []).map((d, i) => (
          <tr key={i}><td className="mono">{short(d.a, 10)}</td><td className="mono">{short(d.b, 10)}</td><td>distance {d.distance}</td>
            <td className="muted">{d.low_quality ? "low-quality hash" : ""}</td></tr>))}</tbody></table>
        {dups.data?.length === 0 && <span className="muted">No near-duplicates found.</span>}
      </Card>
    </>
  );
}
