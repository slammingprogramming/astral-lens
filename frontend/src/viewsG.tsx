import { useState } from "react";
import { api, post, put } from "./api";
import { Card, ErrorBox, short, useAsync } from "./ui";

function FactList({ title, items, empty }: { title: string; items: any[]; empty: string }) {
  return (
    <div>
      <h4>{title} ({items.length})</h4>
      {items.length === 0 ? <span className="muted">{empty}</span> : <ul>{items.map((f, i) => <li key={i}>{f.label} <span className="muted">· {f.plugin}</span></li>)}</ul>}
    </div>
  );
}

function Diff({ run }: { run: any }) {
  const d = run.diff;
  return (
    <div className="note">
      <div className="muted">{new Date(run.ran_at).toLocaleString()} · {d.first_run ? "first run: everything reported is new" : "compared with the last time each source answered"}</div>
      <FactList title="New" items={d.new_facts} empty="Nothing new reported." />
      <FactList title="No longer reported" items={d.no_longer_reported} empty="Nothing that a source reported before has disappeared." />
      {d.not_asked_this_time.length > 0 && (
        <p><b>Could not be asked this time:</b> {d.not_asked_this_time.join(", ")}. That is <b>unknown</b>, not "gone" and not "nothing found".</p>
      )}
      {d.new_watch_matches.length > 0 && <p><b>{d.new_watch_matches.length} new watch-term match(es)</b> since the last run (see below).</p>}
      <div className="muted">{d.note}</div>
    </div>
  );
}

export function MonitorView({ caseId, bump, onChanged }: { caseId: string; bump: number; onChanged: () => void }) {
  const mons = useAsync(() => api<any[]>(`/cases/${caseId}/monitors`), [caseId, bump]);
  const terms = useAsync(() => api<any[]>(`/cases/${caseId}/watch-terms`), [caseId, bump]);
  const matches = useAsync(() => api<any>(`/cases/${caseId}/watch/matches`), [caseId, bump]);
  const [name, setName] = useState("");
  const [query, setQuery] = useState("");
  const [every, setEvery] = useState("");
  const [kind, setKind] = useState("name");
  const [value, setValue] = useState("");
  const [shown, setShown] = useState<{ id: string; runs: any[] } | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function guarded(fn: () => Promise<unknown>) {
    setErr(null); setBusy(true);
    try { await fn(); } catch (e) { setErr(e); } finally { setBusy(false); }
  }
  const create = () => guarded(async () => {
    await post(`/cases/${caseId}/monitors`, { name: name || query, query: { name: query }, interval_minutes: every ? Number(every) : null });
    setName(""); setQuery(""); setEvery(""); mons.reload();
  });
  const run = (id: string) => guarded(async () => {
    await post(`/cases/${caseId}/monitors/${id}/run`);
    setShown({ id, runs: await api(`/cases/${caseId}/monitors/${id}/runs`) });
    mons.reload(); matches.reload(); onChanged();
  });
  const history = (id: string) => guarded(async () => setShown({ id, runs: await api(`/cases/${caseId}/monitors/${id}/runs`) }));
  const addTerm = () => guarded(async () => { await post(`/cases/${caseId}/watch-terms`, { kind, value }); setValue(""); terms.reload(); matches.reload(); });
  const dropTerm = (id: number) => guarded(async () => { await api(`/cases/${caseId}/watch-terms/${id}`, { method: "DELETE" }); terms.reload(); matches.reload(); });

  return (
    <>
      <Card title="Monitors (saved searches)">
        <p className="muted">
          A monitor re-runs a name search across your enabled sources and shows what changed. A source that could not be asked is
          <b> unknown</b>, never "gone". Scheduled runs contact third-party sources, so the server only runs them if its operator turned the
          schedule on (ASTRAL_MONITOR_SWEEP_SECONDS).
        </p>
        <div className="row">
          <input placeholder="name to search" value={query} onChange={(e) => setQuery(e.target.value)} />
          <input placeholder="label (optional)" value={name} onChange={(e) => setName(e.target.value)} />
          <input style={{ width: 150 }} type="number" min={5} placeholder="every N minutes" value={every} onChange={(e) => setEvery(e.target.value)} />
          <button disabled={busy || !query.trim()} onClick={create}>Save monitor</button>
        </div>
        <ErrorBox e={err} />
        <table><thead><tr><th>monitor</th><th>search</th><th>schedule</th><th>last run</th><th></th></tr></thead><tbody>
          {(mons.data ?? []).map((m) => (
            <tr key={m.id}>
              <td>{m.name}{!m.active && <span className="badge"> paused</span>}</td><td>{m.query.name}</td>
              <td>{m.interval_minutes ? `every ${m.interval_minutes} min` : "manual"}</td>
              <td className="muted">{m.last_run ? new Date(m.last_run.ran_at).toLocaleString() : "never"}</td>
              <td className="row">
                <button disabled={busy} onClick={() => run(m.id)}>Run now</button>
                <button disabled={busy} onClick={() => history(m.id)}>History</button>
                <button disabled={busy} onClick={() => guarded(async () => { await put(`/cases/${caseId}/monitors/${m.id}`, { active: !m.active }); mons.reload(); })}>{m.active ? "Pause" : "Resume"}</button>
              </td>
            </tr>))}
        </tbody></table>
        {(mons.data ?? []).length === 0 && <span className="muted">No monitors yet.</span>}
        {shown && (shown.runs.length === 0 ? <span className="muted">No runs yet.</span> : shown.runs.slice(0, 5).map((r) => <Diff key={r.id} run={r} />))}
      </Card>

      <Card title="Watch terms">
        <p className="muted">Names and identifiers to flag wherever they appear in this case's entities and document text. Name matches are <b>candidates</b>: a similar name is not the same person.</p>
        <div className="row">
          <select value={kind} onChange={(e) => setKind(e.target.value)}><option value="name">name</option><option value="identifier">identifier (IBAN, e-mail, phone…)</option></select>
          <input placeholder="term" value={value} onChange={(e) => setValue(e.target.value)} />
          <button disabled={busy || value.trim().length < 2} onClick={addTerm}>Watch</button>
        </div>
        <table><tbody>{(terms.data ?? []).map((t) => (
          <tr key={t.id}><td>{t.kind}</td><td>{t.value}</td><td><button disabled={busy} onClick={() => dropTerm(t.id)}>remove</button></td></tr>))}</tbody></table>
        <h4>Matches in this case ({matches.data?.matches.length ?? 0})</h4>
        {(matches.data?.matches ?? []).map((m: any) => (
          <div key={m.key} className="note">
            <b>{m.term}</b> → {m.kind === "entity"
              ? <>entity <b>{m.caption ?? short(m.cluster_id, 8)}</b>{m.via ? ` (via ${m.via})` : ""} · similarity {m.similarity}</>
              : <>document {short(m.text_artifact, 10)} characters {m.start}–{m.end}: <i>“…{m.snippet}…”</i></>}
            <div className="muted">{m.caveat}</div>
          </div>))}
      </Card>
    </>
  );
}
