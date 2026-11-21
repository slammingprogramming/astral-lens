import { useState } from "react";
import { api, post, put, sha256Hex } from "./api";
import { authHeaders } from "./auth";
import { Card, ErrorBox, Outcome, short, useAsync, AuthLink } from "./ui";
import { Support } from "./support";

// ------------------------------------------------------------- Investigate
export function Investigate({ caseId, onDone }: { caseId: string; onDone: () => void }) {
  const [name, setName] = useState("");
  const [country, setCountry] = useState("");
  const [schema, setSchema] = useState("");
  const [advanced, setAdvanced] = useState("{}");
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState<any>(null);
  const [err, setErr] = useState<unknown>(null);
  const [note, setNote] = useState<string | null>(null);
  const arts = useAsync(() => api<any[]>(`/cases/${caseId}/artifacts`), [caseId, report]);
  const plugins = useAsync(() => api<any[]>("/plugins"), []);

  async function run() {
    setBusy(true);
    setErr(null);
    try {
      const extra = JSON.parse(advanced || "{}");
      const r = await post(`/cases/${caseId}/investigate/name`, {
        name, country: country || null, schema: schema || null, plugin_inputs: extra.plugin_inputs ?? {},
        plugin_configs: extra.plugin_configs ?? {},
      });
      setReport(r);
      onDone();
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  }

  async function upload(f: File) {
    setErr(null);
    try {
      const buf = await f.arrayBuffer();
      const up = await api(`/cases/${caseId}/artifacts`, {
        method: "POST", body: buf,
        headers: { "Content-Type": f.type || "application/octet-stream", "X-Astral-Filename": f.name },
      });
      const text = /^(text\/|audio\/|video\/|image\/|application\/(pdf|json|xml)|message\/rfc822|application\/vnd\.openxml)/.test(f.type || "");
      setNote(null);
      if (text) {
        const r = await post(`/cases/${caseId}/artifacts/${up.sha256}/ingest`);
        if (r.steps) // media: say what ran, and be explicit about every step that could not
          setNote(Object.entries<any>(r.steps).map(([k, s]) =>
            `${k}: ${s.status}` + (s.coverage ?? []).filter((c: any) => c.outcome === "unknown").map((c: any) => ` (${c.source} unknown: ${c.note})`).join("")).join(" · "));
      }
      arts.reload();
      onDone();
    } catch (e) {
      setErr(e);
    }
  }

  const connectors = (plugins.data ?? []).filter((p) => p.state === "enabled" && (p.consumes ?? []).some((c: any) => c.type === "Query"));
  return (
    <>
      <Card title="Name to network">
        <div className="row">
          <input className="grow" placeholder="Person or company name" value={name} onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && name && run()} />
          <input style={{ width: 80 }} placeholder="Country" value={country} onChange={(e) => setCountry(e.target.value)} />
          <select value={schema} onChange={(e) => setSchema(e.target.value)}>
            <option value="">any type</option><option>Person</option><option>Company</option>
          </select>
          <button className="primary" disabled={!name || busy} onClick={run}>{busy ? "Searching…" : "Search"}</button>
        </div>
        <p className="muted">
          Queries {connectors.length} enabled connector(s). Sources that cannot be reached are reported as <b>unknown</b>, never as
          “no results”. Footprint: third-party services only; nothing contacts the subject.
        </p>
        <details>
          <summary>Advanced: per-plugin inputs and config (JSON)</summary>
          <textarea style={{ width: "100%", height: 90 }} className="mono" value={advanced} onChange={(e) => setAdvanced(e.target.value)} />
          <div className="muted">e.g. {`{"plugin_inputs":{"connector.ofac-sdn":{"dataset_artifact":"<sha256>"}}}`}</div>
        </details>
        <ErrorBox e={err} />
      </Card>

      {report && <Report r={report} />}

      <Card title="Evidence in this case" right={
        <label className="row"><span className="muted">Add a file (documents, audio, video and images are processed)</span>
          <input type="file" onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} /></label>}>
        {note && <div className="note">{note}</div>}
        <table><thead><tr><th>sha256</th><th>type</th><th>size</th><th>from</th></tr></thead><tbody>
          {(arts.data ?? []).slice(0, 8).map((a) => (
            <tr key={a.sha256}><td className="mono" title={a.sha256}>{short(a.sha256, 14)}</td><td>{a.media_type}</td>
              <td>{a.byte_size}</td><td>{short(a.acquired_from, 50)} <span className="muted">{a.acquired_by}</span></td></tr>
          ))}
        </tbody></table>
      </Card>
    </>
  );
}

function Report({ r }: { r: any }) {
  const s = r.coverage_summary;
  return (
    <Card title="What was searched">
      <div className="row">
        <Outcome o="hits" /> {s.hits} <Outcome o="no_hits" /> {s.no_hits} <Outcome o="unknown" /> {s.unknown}
        <span className="muted">· {r.projection.entities} entities · {r.candidates_proposed} match proposal(s) awaiting review</span>
      </div>
      <p className="note">{r.note}</p>
      <table><thead><tr><th>source</th><th>outcome</th><th>detail</th><th>records</th></tr></thead><tbody>
        {r.sources.flatMap((src: any) =>
          (src.coverage?.length ? src.coverage : [{ source: src.plugin, outcome: "unknown", note: src.gaps?.join("; ") }]).map((c: any, i: number) => (
            <tr key={src.plugin + i}><td>{c.source}<div className="muted mono">{src.plugin}</div></td>
              <td><Outcome o={c.outcome} /></td><td>{c.note}</td><td>{src.assertions}</td></tr>
          )),
        )}
      </tbody></table>
    </Card>
  );
}

// ---------------------------------------------------------------- Coverage
export function Coverage({ caseId }: { caseId: string }) {
  const { data, error } = useAsync(() => api<any[]>(`/cases/${caseId}/coverage`), [caseId]);
  const runs = new Map<string, any[]>();
  (data ?? []).forEach((r) => runs.set(r.run_id, [...(runs.get(r.run_id) ?? []), r]));
  return (
    <>
      <ErrorBox e={error} />
      {[...runs.entries()].map(([id, rows]) => (
        <Card key={id} title={`Search run ${new Date(rows[0].ran_at).toLocaleString()} — “${rows[0].query.name}”`}>
          <table><thead><tr><th>plugin</th><th>status</th><th>outcome</th><th>note</th></tr></thead><tbody>
            {rows.flatMap((r: any) => (r.coverage.length ? r.coverage : [{ outcome: "unknown", note: r.gaps.join("; ") }]).map((c: any, i: number) => (
              <tr key={r.plugin_id + i}><td className="mono">{r.plugin_id}</td><td>{r.status}</td><td><Outcome o={c.outcome} /></td><td>{c.note}</td></tr>
            )))}
          </tbody></table>
        </Card>
      ))}
      {data?.length === 0 && <p className="muted">No searches yet.</p>}
    </>
  );
}

// ---------------------------------------------------------------- Evidence
export function Evidence({ caseId }: { caseId: string }) {
  const { data, error } = useAsync(() => api<any[]>(`/cases/${caseId}/artifacts`), [caseId]);
  const [verdict, setVerdict] = useState<Record<string, string>>({});
  async function verify(sha: string) {
    const r = await fetch(`/cases/${caseId}/artifacts/${sha}`, { headers: authHeaders() });
    const ok = (await sha256Hex(await r.arrayBuffer())) === sha;
    setVerdict((v) => ({ ...v, [sha]: ok ? "✓ bytes match their SHA-256" : "✗ MISMATCH" }));
  }
  return (
    <Card title="Artifacts (as captured, never re-fetched)">
      <ErrorBox e={error} />
      <table><thead><tr><th>sha256</th><th>type</th><th>size</th><th>acquired by / from</th><th>derived</th><th /></tr></thead><tbody>
        {(data ?? []).map((a) => (
          <tr key={a.sha256}>
            <td className="mono"><AuthLink href={`/cases/${caseId}/artifacts/${a.sha256}`} title={a.sha256}>{short(a.sha256, 16)}</AuthLink></td>
            <td>{a.media_type}</td><td>{a.byte_size}</td>
            <td>{a.acquired_by} <div className="muted" style={{ wordBreak: "break-all" }}>{a.acquired_from}</div></td>
            <td>{a.derivation ? `${a.derivation} ← ${short(a.parent_sha256, 10)}` : ""}</td>
            <td><button onClick={() => verify(a.sha256)}>verify</button> <span className="muted">{verdict[a.sha256]}</span></td>
          </tr>
        ))}
      </tbody></table>
    </Card>
  );
}

// ---------------------------------------------------------------- Settings
export function Settings() {
  const pre = useAsync(() => api("/preflight"), []);
  const plugins = useAsync(() => api<any[]>("/plugins"), []);
  const sources = useAsync(() => api<any[]>("/sources"), []);
  const secrets = useAsync(() => api("/secrets"), []);
  const [err, setErr] = useState<unknown>(null);
  const [val, setVal] = useState<Record<string, string>>({});
  const p = pre.data as any;

  async function sweep() {
    setErr(null);
    try { await post("/plugins/health-sweep"); plugins.reload(); } catch (e) { setErr(e); }
  }
  async function setState(pl: any, state: string) {
    setErr(null);
    try { await post(`/plugins/${pl.id}/${pl.version}/${state}`); plugins.reload(); } catch (e) { setErr(e); }
  }
  async function grade(id: string, g: string) {
    try { await put(`/sources/${id}/grade`, { grade: g }); sources.reload(); } catch (e) { setErr(e); }
  }
  async function saveSecret(name: string) {
    try { await put(`/secrets/${name}`, { value: val[name] }); setVal({ ...val, [name]: "" }); secrets.reload(); } catch (e) { setErr(e); }
  }
  return (
    <>
      <ErrorBox e={err} />
      <Card title="Capability panel">
        {p && (
          <>
            <div>Profile <b>{p.profile}</b> <span className="muted">({p.profile_reasons.join("; ")})</span></div>
            <div className="muted">{p.host.os} · {p.host.cpu_threads} threads · {Math.round(p.host.ram_mb / 1024)} GB RAM · GPU: {p.host.gpus.map((g: any) => `${g.name} ${g.vram_mb} MB`).join(", ") || "none"}</div>
            {(["available", "degraded", "unavailable"] as const).map((k) => (
              <details key={k} open={k !== "available"}><summary>{k} ({p[k].length})</summary><ul>{p[k].map((x: string) => <li key={x}>{x}</li>)}</ul></details>
            ))}
            {p.licence_findings.length > 0 && <details open><summary>licence posture ({p.licence_findings.length})</summary>
              <ul>{p.licence_findings.map((f: any, i: number) => <li key={i}><span className="badge">{f.level}</span> {f.model_id}: {f.message}</li>)}</ul></details>}
          </>
        )}
      </Card>

      <Card title="Plugins and source health" right={<button onClick={sweep}>Run self-checks now</button>}>
        <table><thead><tr><th>plugin</th><th>footprint</th><th>data licence</th><th>last check</th><th>state</th></tr></thead><tbody>
          {(plugins.data ?? []).map((pl) => (
            <tr key={pl.id + pl.version}>
              <td>{pl.title}<div className="muted mono">{pl.id}@{pl.version}</div></td>
              <td>{pl.footprint}</td>
              <td>{pl.licence.data}{pl.licence.commercial_use === false && <span className="badge"> non-commercial</span>}
                {pl.licence.commercial_use == null && <span className="badge"> commercial use unconfirmed</span>}</td>
              <td>{pl.last_check ? (pl.last_check.ok ? "ok" : `FAILING (${pl.last_check.error_class ?? "other"})`) : "never"}</td>
              <td>{pl.state === "quarantined" ? <b title={pl.quarantine_reason}>quarantined → results UNKNOWN</b> : pl.state}
                {" "}<button onClick={() => setState(pl, pl.state === "enabled" ? "disabled" : "enabled")}>{pl.state === "enabled" ? "disable" : "enable"}</button></td>
            </tr>
          ))}
        </tbody></table>
      </Card>

      <Card title="Source reliability (you decide; a new source is F = no basis to judge)">
        <table><thead><tr><th>source</th><th>origin</th><th>grade</th></tr></thead><tbody>
          {(sources.data ?? []).map((s) => (
            <tr key={s.id}><td>{s.name}</td><td className="muted">{s.origin_key ?? "—"}</td>
              <td><select value={s.default_source_grade} onChange={(e) => grade(s.id, e.target.value)}>{"ABCDEF".split("").map((g) => <option key={g}>{g}</option>)}</select></td></tr>
          ))}
        </tbody></table>
        <p className="muted">A grade applies to what the source records from now on; assertions already written keep the grade they were recorded with.</p>
      </Card>

      <Card title="API keys">
        {secrets.data && !(secrets.data as any).enabled && <div className="note">{(secrets.data as any).note}</div>}
        <table><thead><tr><th>secret</th><th>used by</th><th>status</th><th /></tr></thead><tbody>
          {((secrets.data as any)?.needed ?? []).map((n: any) => (
            <tr key={n.name}><td className="mono">{n.name}</td><td>{n.plugins.join(", ")}</td><td>{n.set ? "set" : "not set — dependent sources report UNKNOWN"}</td>
              <td>{(secrets.data as any).enabled && <span className="row">
                <input type="password" value={val[n.name] ?? ""} onChange={(e) => setVal({ ...val, [n.name]: e.target.value })} />
                <button disabled={!val[n.name]} onClick={() => saveSecret(n.name)}>save</button></span>}</td></tr>
          ))}
        </tbody></table>
      </Card>

      <Support />
    </>
  );
}
