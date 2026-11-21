import { useState } from "react";
import { api, postForFile } from "./api";
import { Card, ErrorBox, useAsync } from "./ui";

/** Case to exhibit (acceptance scenario 5). The operator writes their own statement; the platform
 *  assembles the package and says plainly what it does not establish. */
export function ExhibitView({ caseId, bump }: { caseId: string; bump: number }) {
  const plans = useAsync(() => api<any[]>(`/cases/${caseId}/plans`), [caseId, bump]);
  const [statement, setStatement] = useState("");
  const [chosen, setChosen] = useState<string[]>([]);
  const [all, setAll] = useState(false);
  const [sign, setSign] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<{ name: string; sha256: string | null } | null>(null);

  const answers = (plans.data ?? []).filter((p) => p.status === "done");
  const toggle = (id: string) => setChosen((c) => (c.includes(id) ? c.filter((x) => x !== id) : [...c, id]));

  async function run() {
    setErr(null); setDone(null); setBusy(true);
    try {
      const f = await postForFile(`/cases/${caseId}/exhibit`, { statement, plan_ids: chosen, include_all_artifacts: all, sign });
      const url = URL.createObjectURL(f.blob);
      const a = document.createElement("a"); a.href = url; a.download = f.name; a.click(); URL.revokeObjectURL(url);
      setDone({ name: f.name, sha256: f.sha256 });
    } catch (e) { setErr(e); } finally { setBusy(false); }
  }

  return (
    <Card title="Export an exhibit">
      <p className="muted">
        Builds one .zip: the evidence the case relies on (named by its SHA-256), every assertion with its grades and retractions, the
        capture log, tool and model versions, this case's audit entries, any checked answers you pick, a readable record with a source
        appendix, and a <code>verify.py</code> that anyone can run without this software.{" "}
        <b>The platform assembles the package; it never asserts admissibility.</b> The package says, in <code>LIMITS.md</code>, what it
        does not establish.
      </p>
      <label>Your statement (required). What you did, when, and how the material came to you. It is included verbatim; the platform cannot
        write it for you and does not verify it.
        <textarea style={{ width: "100%", minHeight: 120 }} value={statement} onChange={(e) => setStatement(e.target.value)} />
      </label>
      <h4>Checked answers to include</h4>
      {answers.length === 0 && <span className="muted">No finished answers yet. Ask a question in the Ask tab first, if you want one included.</span>}
      {answers.map((p) => (
        <label key={p.id} className="row">
          <input type="checkbox" checked={chosen.includes(p.id)} onChange={() => toggle(p.id)} />
          <span>{p.question} <span className="muted">({p.result?.claims?.length ?? 0} claim(s), {p.planner})</span></span>
        </label>
      ))}
      <div className="row">
        <label className="row"><input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> include every artifact in the case, not only those relied on</label>
      </div>
      <div className="row">
        <label className="row"><input type="checkbox" checked={sign} onChange={(e) => setSign(e.target.checked)} /> sign the manifest (needs ASTRAL_SIGNING_KEY on the server)</label>
      </div>
      <ErrorBox e={err} />
      <div className="row"><button disabled={busy || !statement.trim()} onClick={run}>{busy ? "Building…" : "Build and download"}</button></div>
      {done && (
        <div className="note">
          Downloaded <b>{done.name}</b>. Package SHA-256: <span className="mono">{done.sha256 || "not computed (large file)"}</span>.
          Record this value separately; it is also in the audit log. Check the package with <code>python verify.py {done.name}</code>.
        </div>
      )}
    </Card>
  );
}
