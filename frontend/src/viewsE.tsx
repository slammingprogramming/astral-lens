import { useState } from "react";
import { api, post } from "./api";
import { Card, ErrorBox, short, useAsync } from "./ui";

const STATUS: Record<string, string> = {
  verified: "verified", weakly_verified: "weakly verified (not entailment-checked)", rejected: "rejected",
};

function Claim({ c, evidence }: { c: any; evidence: Record<string, any> }) {
  return (
    <li style={{ marginBottom: 12 }}>
      <div>{c.text}</div>
      <div className="muted">
        confidence <b>{c.confidence}</b> · check <b>{STATUS[c.status] ?? c.status}</b>
        {c.needs_human_review && <> · <span className="badge">flagged for human review</span></>}
      </div>
      {c.anchors.map((a: any, i: number) => (
        <div key={i} className="muted">
          {a.kind === "span"
            ? <>source {a.evidence_id}: document {short(a.text_artifact, 10)} characters {a.start}–{a.end}</>
            : <>source {a.evidence_id}: assertion #{a.assertion_id}</>}
          {evidence[a.evidence_id]?.text && <> — <i>“{evidence[a.evidence_id].text.slice(0, 160)}”</i></>}
        </div>
      ))}
      <details><summary className="muted">checks ({c.checks.length})</summary>
        <ul>{c.checks.map((k: any, i: number) => (
          <li key={i}>{k.verifier}{k.weak ? " (weak)" : ""}: {k.result ? "passed" : "failed"} — {k.note}</li>))}</ul>
      </details>
    </li>
  );
}

export function AskView({ caseId, bump, onChanged }: { caseId: string; bump: number; onChanged: () => void }) {
  const status = useAsync(() => api<any>(`/ai/status`), []);
  const past = useAsync(() => api<any[]>(`/cases/${caseId}/plans`), [caseId, bump]);
  const [q, setQ] = useState("");
  const [plan, setPlan] = useState<any>(null);
  const [res, setRes] = useState<any>(null);
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function propose() {
    setErr(null); setRes(null); setBusy(true);
    try { setPlan(await post(`/cases/${caseId}/ask`, { question: q })); past.reload(); }
    catch (e) { setErr(e); } finally { setBusy(false); }
  }
  async function approveAndRun() {
    setErr(null); setBusy(true);
    try {
      await post(`/cases/${caseId}/plans/${plan.id}/approve`, {});
      setRes(await post(`/cases/${caseId}/plans/${plan.id}/run`, {}));
      setPlan(null); past.reload(); onChanged();
    } catch (e) { setErr(e); } finally { setBusy(false); }
  }
  function download() {
    const url = URL.createObjectURL(new Blob([res.report_md], { type: "text/markdown" }));
    const a = document.createElement("a"); a.href = url; a.download = "assessment.md"; a.click(); URL.revokeObjectURL(url);
  }

  const s = status.data;
  const evidence: Record<string, any> = Object.fromEntries((res?.evidence ?? []).map((e: any) => [e.id, e]));
  return (
    <>
      <Card title="Ask the case">
        {s && (
          <div className="note">
            planner <b>{s.planner}</b> · composer <b>{s.composer}</b> · retrieval {s.retrieval.join(" + ")} ·{" "}
            {s.model_configured ? `language model egress: ${s.egress_mode}` : "no language model configured"}
            <div className="muted">{s.not_configured.length ? `Not configured: ${s.not_configured.join(", ")}. ` : ""}{s.note}</div>
          </div>
        )}
        <p className="muted">
          Questions are answered only from this case's own evidence. You see the plan and what it will touch before anything runs.
          The assistant never merges entities, never invents identifiers, never raises a grade and writes no assertions.
        </p>
        <div className="row">
          <input style={{ flex: 1 }} value={q} placeholder="e.g. Which IBAN receives payments for Acme Holdings?"
            onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && q.trim() && !busy && propose()} />
          <button disabled={busy || !q.trim()} onClick={propose}>Plan</button>
        </div>
        <ErrorBox e={err} />
      </Card>

      {plan && (
        <Card title="Proposed plan — not yet run">
          <div className="muted">planned by {plan.planner}</div>
          <ol>{plan.plan.steps.map((st: any, i: number) => (
            <li key={i}><b>{st.type}</b>{" "}
              <span className="mono">{Object.entries(st.args ?? {}).map(([k, v]) => `${k}: ${typeof v === "string" ? `“${v}”` : JSON.stringify(v)}`).join(" · ")}</span>
              {st.depends_on?.length > 0 && <span className="muted"> (after {st.depends_on.join(", ")})</span>}</li>))}</ol>
          <div className="note">
            {plan.plan.estimate.steps} step(s) · {plan.plan.estimate.llm_calls} language-model call(s) · egress <b>{plan.plan.estimate.egress}</b> ·
            footprint <b>{plan.plan.estimate.footprint}</b> · writes assertions: <b>{plan.plan.estimate.writes_assertions ? "yes" : "no"}</b>
            <div className="muted">{plan.plan.estimate.note}</div>
          </div>
          <div className="row"><button disabled={busy} onClick={approveAndRun}>Approve and run</button>
            <button disabled={busy} onClick={() => setPlan(null)}>Discard</button></div>
        </Card>
      )}

      {res && (
        <Card title="Answer" right={<button onClick={download}>Download report (.md)</button>}>
          <div className="muted">
            composed by {res.composer} · rankers {res.rankers.join(" + ")} · {res.latency_ms} ms ·
            verifiers {res.verifiers.map((v: any) => v.name + (v.weak ? " (weak)" : "")).join(", ")}
          </div>
          {res.claims.length === 0
            ? <p className="note">No statement could be supported by the evidence in this case.</p>
            : <ol>{res.claims.map((c: any, i: number) => <Claim key={i} c={c} evidence={evidence} />)}</ol>}
          <h4>Gaps</h4>
          <ul>{(res.gaps.length ? res.gaps : ["No coverage gaps were recorded for the last search run."]).map((g: string, i: number) => <li key={i}>{g}</li>)}</ul>
          {res.rejected.length > 0 && (
            <details><summary className="muted">{res.rejected.length} dropped statement(s) and why</summary>
              <ul>{res.rejected.map((r: any, i: number) => <li key={i}>{r.text} — {r.reasons.join("; ")}</li>)}</ul></details>
          )}
        </Card>
      )}

      <Card title="Earlier questions">
        {(past.data ?? []).length === 0 && <span className="muted">None yet.</span>}
        <table><tbody>{(past.data ?? []).map((p) => (
          <tr key={p.id}><td>{p.question}</td><td>{p.status}</td><td className="muted">{p.planner}</td>
            <td className="muted">{p.result?.claims ? `${p.result.claims.length} claim(s)` : ""}</td></tr>))}</tbody></table>
      </Card>
    </>
  );
}
