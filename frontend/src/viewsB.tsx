import cytoscape from "cytoscape";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, post } from "./api";
import { Card, ErrorBox, Grade, Scope, short, useAsync } from "./ui";
import { fmtMs } from "./viewsC";

const SCHEMA_COLOR: Record<string, string> = {
  Person: "#c2549a", Company: "#2f5fd0", Organization: "#2f5fd0", LegalEntity: "#4a76c9", Document: "#8a93a3",
  Address: "#b58b2a", BankAccount: "#d98a2b", CryptoWallet: "#d98a2b", EmailAddress: "#3f9b5a", WebPage: "#7b6bd1",
  WebSnapshot: "#a99be0", Vessel: "#2a8c99", Airplane: "#2a8c99",
};
export const colorOf = (s: string | null) => SCHEMA_COLOR[s ?? ""] ?? "#777";

// ----------------------------------------------------------------- Entities
export function Entities({ caseId, bump, onChanged }: { caseId: string; bump: number; onChanged: () => void }) {
  const { data, error } = useAsync(() => api<any[]>(`/cases/${caseId}/entities`), [caseId, bump]);
  const [q, setQ] = useState("");
  const [sel, setSel] = useState<string | null>(null);
  const rows = (data ?? []).filter((e) => !q || (e.caption ?? "").toLowerCase().includes(q.toLowerCase()) || (e.schema_name ?? "").toLowerCase().includes(q.toLowerCase()));
  return (
    <div className="split">
      <Card title={`Entities (${data?.length ?? 0})`}>
        <ErrorBox e={error} />
        <input placeholder="filter" value={q} onChange={(e) => setQ(e.target.value)} style={{ width: "100%", marginBottom: 8 }} />
        <table><tbody>
          {rows.map((e) => (
            <tr key={e.cluster_id} onClick={() => setSel(e.cluster_id)} style={{ cursor: "pointer", background: sel === e.cluster_id ? "var(--line)" : undefined }}>
              <td><span style={{ color: colorOf(e.schema_name) }}>●</span> {e.caption ?? <i className="muted">(unnamed)</i>}</td>
              <td className="muted">{e.schema_name}</td><td>{e.member_count > 1 && <span className="badge">{e.member_count} merged</span>}</td>
            </tr>
          ))}
        </tbody></table>
      </Card>
      {sel ? <Profile key={sel} caseId={caseId} cluster={sel} onChanged={onChanged} /> : <Card><span className="muted">Select an entity.</span></Card>}
    </div>
  );
}

function Profile({ caseId, cluster, onChanged }: { caseId: string; cluster: string; onChanged: () => void }) {
  const ent = useAsync(() => api<any>(`/cases/${caseId}/entities/${cluster}`), [caseId, cluster]);
  const mem = useAsync(() => api<any>(`/cases/${caseId}/entities/${cluster}/members`), [caseId, cluster]);
  const plugins = useAsync(() => api<any[]>("/plugins"), []);
  const [open, setOpen] = useState<number | null>(null);
  const [ex, setEx] = useState<any>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const e = ent.data;
  const groups = useMemo(() => {
    const g = new Map<string, any[]>();
    (e?.properties ?? []).forEach((p: any) => g.set(p.predicate, [...(g.get(p.predicate) ?? []), p]));
    return [...g.entries()];
  }, [e]);

  async function explain(aid: number) {
    setOpen(aid);
    setEx(await api(`/cases/${caseId}/assertions/${aid}/explain`));
  }
  async function pivot(pluginId: string) {
    setErr(null);
    setMsg("searching…");
    try {
      const schema = e.schema === "Person" ? "Person" : "Company";
      const r = await post(`/cases/${caseId}/investigate/name`, { name: e.caption, schema, only: [pluginId] });
      setMsg(r.sources.map((s: any) => s.coverage.map((c: any) => `${c.source}: ${c.outcome}`).join("; ")).join("; "));
      onChanged();
    } catch (x) { setErr(x); setMsg(null); }
  }
  async function split(jid: number) {
    setErr(null);
    try {
      await post(`/cases/${caseId}/judgments/${jid}/retract`, { reason: "split from entity profile" });
      await post(`/cases/${caseId}/rebuild`);
      onChanged();
    } catch (x) { setErr(x); }
  }
  const connectors = (plugins.data ?? []).filter((p) => p.state === "enabled" && (p.consumes ?? []).some((c: any) => c.type === "Query") &&
    !(p.consumes ?? []).some((c: any) => (c.required ?? []).includes("url")) && p.footprint !== "target_contact");
  if (!e) return <Card><ErrorBox e={ent.error} /></Card>;
  return (
    <Card title={`${e.schema ?? "Entity"} — ${e.caption ?? "(unnamed)"}`}>
      <ErrorBox e={err} />
      {(e.schema === "Person" || e.schema === "Company" || e.schema === "Organization") && connectors.length > 0 && (
        <div className="row" style={{ marginBottom: 8 }}>
          <span className="muted">Pivot (from installed connectors):</span>
          {connectors.map((c) => <button key={c.id} onClick={() => pivot(c.id)}>{c.title}</button>)}
        </div>
      )}
      {msg && <div className="note">{msg}</div>}
      {mem.data && mem.data.judgments.length > 0 && (
        <div className="note">
          <b>Merged from {mem.data.members.length} references</b> by your decision(s):
          {mem.data.judgments.map((j: any) => (
            <div key={j.id} className="row">#{j.id}: {j.reasoning ?? "no reason recorded"}
              <button onClick={() => split(j.id)}>Split (retract this judgment)</button></div>
          ))}
        </div>
      )}
      <table><tbody>
        {groups.map(([pred, ps]) => (
          <tr key={pred}><th style={{ width: 150 }}>{pred}</th><td>
            {ps.map((p: any) => (
              <div key={p.assertion_id}>
                {String(typeof p.value === "object" ? JSON.stringify(p.value) : p.value)}{" "}
                <Grade s={p.source_grade} i={p.info_grade} />{" "}
                <span className="muted" title="distinct upstream origins that agree">×{p.independent_sources}</span>{" "}
                <a href="#" onClick={(ev) => { ev.preventDefault(); explain(p.assertion_id); }}>why?</a>
                {open === p.assertion_id && ex && <Explain ex={ex} caseId={caseId} />}
              </div>
            ))}
          </td></tr>
        ))}
      </tbody></table>
      {e.edges.length > 0 && <>
        <h3 style={{ marginTop: 12 }}>Links</h3>
        {e.edges.map((x: any) => <div key={x.assertion_id}>{x.predicate} → <span className="mono">{short(x.object_cluster, 8)}</span> <Grade s={x.source_grade} i={x.info_grade} /></div>)}
      </>}
    </Card>
  );
}

export function Explain({ ex, caseId }: { ex: any; caseId: string }) {
  const t = ex.tree;
  return (
    <div className="note" style={{ fontSize: 12 }}>
      produced by <b>{t.derived_by.plugin}@{t.derived_by.version}</b> via <b>{t.derived_by.backend}</b>
      {t.derived_by.degraded && <span className="badge"> degraded from {t.derived_by.degraded_from}</span>}
      {t.derived_by.kind === "model" && <span className="badge"> model-extracted</span>}
      {(t.artifacts ?? []).map((a: any) => (
        <div key={a.sha256}>evidence: <a href={`/cases/${caseId}/artifacts/${a.sha256}`} target="_blank" rel="noreferrer" className="mono">{short(a.sha256, 14)}</a>
          {" "}{a.media_type} — acquired by {a.acquisitions?.[0]?.acquired_by}
          <div className="muted" style={{ wordBreak: "break-all" }}>{a.acquisitions?.[0]?.acquired_from}</div></div>
      ))}
    </div>
  );
}

// -------------------------------------------------------------------- Graph
export function GraphView({ caseId, bump, scope }: { caseId: string; bump: number; scope: Scope }) {
  const [docs, setDocs] = useState(false);
  const { data, error } = useAsync(() => api<any>(`/cases/${caseId}/graph?documents=${docs}`), [caseId, bump, docs]);
  const box = useRef<HTMLDivElement>(null);
  const [edge, setEdge] = useState<any>(null);
  const [exs, setExs] = useState<any[]>([]);

  const visible = useMemo(() => {
    if (!data) return null;
    // Unknown validity is not invalidity: an edge with no dates is always shown.
    const edges = data.edges.filter((e: any) => {
      const s = e.properties.startDate as string | undefined, t = e.properties.endDate as string | undefined;
      if (scope.to && s && s > scope.to) return false;
      if (scope.from && t && t < scope.from) return false;
      return true;
    });
    const used = new Set(edges.flatMap((e: any) => [e.source, e.target]));
    const nodes = data.nodes.filter((n: any) => used.has(n.id) || n.degree === 0);
    return { nodes, edges };
  }, [data, scope]);

  useEffect(() => {
    if (!visible || !box.current) return;
    const cy = cytoscape({
      container: box.current,
      elements: [
        ...visible.nodes.map((n: any) => ({ data: { id: n.id, label: n.caption ?? n.schema, color: colorOf(n.schema), sanc: n.listed_in.length > 0 } })),
        ...visible.edges.map((e: any) => ({ data: { id: e.id, source: e.source, target: e.target, label: e.label,
          w: 1 + Math.min(4, e.independent_sources), weak: e.info_grade > 3 || e.source_grade > "C" } })),
      ],
      layout: { name: "cose", animate: false, nodeRepulsion: () => 9000, idealEdgeLength: () => 90 } as any,
      style: [
        { selector: "node", style: { label: "data(label)", "background-color": "data(color)", color: getComputedStyle(document.body).color, "font-size": 10, "text-valign": "bottom", "text-margin-y": 3, width: 16, height: 16, "text-wrap": "ellipsis", "text-max-width": "110px" } },
        { selector: "node[?sanc]", style: { "border-width": 3, "border-color": "#c0392b" } },
        { selector: "edge", style: { width: "data(w)", "curve-style": "bezier", "target-arrow-shape": "triangle", "line-color": "#8a93a3", "target-arrow-color": "#8a93a3", label: "data(label)", "font-size": 8, color: "#8a93a3", "text-rotation": "autorotate" } },
        { selector: "edge[?weak]", style: { "line-style": "dashed" } },
        { selector: ":selected", style: { "overlay-color": "#2f5fd0", "overlay-opacity": 0.25 } },
      ],
    });
    cy.on("tap", "edge", (ev) => setEdge(visible.edges.find((x: any) => x.id === ev.target.id())));
    cy.on("tap", (ev) => ev.target === cy && setEdge(null));
    return () => cy.destroy();
  }, [visible]);

  useEffect(() => {
    setExs([]);
    if (!edge) return;
    Promise.all(edge.assertions.slice(0, 4).map((a: number) => api(`/cases/${caseId}/assertions/${a}/explain`))).then(setExs).catch(() => setExs([]));
  }, [edge, caseId]);

  const names = new Map<string, string>((data?.nodes ?? []).map((n: any) => [n.id, n.caption]));
  return (
    <div className="split" style={{ gridTemplateColumns: "2fr 1fr" }}>
      <div>
        <div className="row" style={{ marginBottom: 6 }}>
          <label><input type="checkbox" checked={docs} onChange={(e) => setDocs(e.target.checked)} /> show documents</label>
          <span className="muted">solid = corroborated, dashed = weak grade · thickness = independent sources · red ring = on a list</span>
        </div>
        <ErrorBox e={error} />
        <div className="graph" ref={box} />
      </div>
      <Card title="Edge provenance">
        {!edge && <span className="muted">Click an edge to see what backs it.</span>}
        {edge && <>
          <div><b>{names.get(edge.source)}</b> — {edge.label} → <b>{names.get(edge.target)}</b></div>
          <div className="row">weakest link <Grade s={edge.source_grade} i={edge.info_grade} /> · {edge.independent_sources} independent source(s)</div>
          {Object.entries(edge.properties).map(([k, v]) => <div key={k}>{k}: {String(v)}</div>)}
          <h3 style={{ marginTop: 10 }}>Backing assertions</h3>
          {exs.map((x, i) => <div key={i}><span className="mono">#{edge.assertions[i]}</span><Explain ex={x} caseId={caseId} /></div>)}
        </>}
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------- Documents
export function Documents({ caseId, bump }: { caseId: string; bump: number }) {
  const list = useAsync(() => api<any[]>(`/cases/${caseId}/documents`), [caseId, bump]);
  const [sel, setSel] = useState<string | null>(null);
  const doc = useAsync(() => (sel ? api<any>(`/cases/${caseId}/documents/${sel}`) : Promise.resolve(null)), [caseId, sel]);
  const [hit, setHit] = useState<number | null>(null);
  const parts = useMemo(() => {
    if (!doc.data) return [];
    const out: any[] = [];
    let pos = 0;
    const ms = [...doc.data.mentions].sort((a: any, b: any) => a.start - b.start || b.end - a.end);
    for (const m of ms) {
      if (m.start < pos) continue; // overlapping mention: the earlier one wins on screen, both stay in the list
      if (m.start > pos) out.push(doc.data.text.slice(pos, m.start));
      out.push(m);
      pos = m.end;
    }
    out.push(doc.data.text.slice(pos));
    return out;
  }, [doc.data]);
  return (
    <div className="split">
      <Card title="Documents">
        <ErrorBox e={list.error} />
        <table><tbody>
          {(list.data ?? []).map((d) => (
            <tr key={d.cluster_id} style={{ cursor: d.text_sha ? "pointer" : "default", background: sel === d.text_sha && d.text_sha ? "var(--line)" : undefined }}
              onClick={() => d.text_sha && setSel(d.text_sha)}>
              <td>{d.title || d.caption || short(d.cluster_id, 8)}<div className="muted">{d.mime}</div></td>
              <td>{d.status === "no_text_extracted" ? <span className="badge" title="no text could be read; this is unknown, not empty">unreadable (needs OCR)</span> : `${d.mentions} mentions`}</td>
            </tr>
          ))}
        </tbody></table>
      </Card>
      <Card title="Hits in context">
        {!sel && <span className="muted">Select a document.</span>}
        <ErrorBox e={doc.error} />
        {doc.data && <>
          <div className="docview">{parts.map((p, i) => typeof p === "string" ? p : (
            <mark key={i} className={`m-${p.label}`} style={hit === p.assertion_id ? { outline: "2px solid var(--accent)" } : undefined}
              title={`${p.label}${p.validated ? " (checksum valid)" : ""} · ${p.backend}${p.degraded ? " (degraded)" : ""} · grade ${p.source_grade}${p.info_grade} · → ${p.entity_caption ?? "?"}`}
              onClick={() => setHit(p.assertion_id)}>{p.text}</mark>
          ))}</div>
          <h3 style={{ marginTop: 12 }}>Mentions ({doc.data.mentions.length})</h3>
          <table><tbody>{doc.data.mentions.map((m: any) => (
            <tr key={m.assertion_id} onClick={() => setHit(m.assertion_id)} style={{ cursor: "pointer" }}>
              <td>{m.text}{m.t_ms != null && <span className="muted mono"> @{fmtMs(m.t_ms)}</span>}</td><td className="muted">{m.label}{m.validated ? " ✓" : ""}</td><td><Grade s={m.source_grade} i={m.info_grade} /></td>
              <td className="muted">{m.backend}{m.degraded ? " · degraded" : ""}</td></tr>))}</tbody></table>
        </>}
      </Card>
    </div>
  );
}

// ----------------------------------------------------------------- Timeline
export function TimelineView({ caseId, bump, scope }: { caseId: string; bump: number; scope: Scope }) {
  const { data, error } = useAsync(() => api<any[]>(`/cases/${caseId}/timeline`), [caseId, bump]);
  const rows = (data ?? []).filter((e) => (!scope.from || e.date >= scope.from) && (!scope.to || e.date.slice(0, scope.to.length) <= scope.to));
  const byYear = new Map<string, any[]>();
  rows.forEach((e) => byYear.set(e.date.slice(0, 4), [...(byYear.get(e.date.slice(0, 4)) ?? []), e]));
  return (
    <Card title="Timeline (valid time)">
      <ErrorBox e={error} />
      <div className="timeline">
        {[...byYear.entries()].map(([y, evs]) => (
          <div key={y}><div className="year">{y}</div>
            {evs.map((e) => (
              <div className="ev" key={e.assertion_id}>
                <span className="mono">{e.date}{e.precision !== "day" && <span className="muted"> ({e.precision})</span>}</span>
                <span>{e.what}</span><span>{e.label}</span><Grade s={e.source_grade} i={e.info_grade} />
              </div>
            ))}</div>
        ))}
        {rows.length === 0 && <span className="muted">No dated facts in scope.</span>}
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------- Review
export function Review({ caseId, bump, onChanged }: { caseId: string; bump: number; onChanged: () => void }) {
  const { data, error, reload } = useAsync(() => api<any[]>(`/cases/${caseId}/proposals`), [caseId, bump]);
  const [err, setErr] = useState<unknown>(null);
  async function decide(p: any, verdict: string) {
    setErr(null);
    try {
      await post(`/cases/${caseId}/judgments`, { entity_a: p.a_entity, entity_b: p.b_entity, verdict,
        comparison: p.comparison, reasoning: `${verdict} — reviewed in the merge view (score ${p.comparison.score})` });
      await post(`/cases/${caseId}/rebuild`);
      reload();
      onChanged();
    } catch (x) { setErr(x); }
  }
  return (
    <>
      <p className="muted">These pairs were <b>proposed</b>, not merged. Scores rank pairs for you; they are not probabilities. Missing fields count for nothing either way.</p>
      <ErrorBox e={error ?? err} />
      {(data ?? []).map((p) => {
        const c = p.comparison;
        return (
          <Card key={p.judgment_id} title={`${c.a_caption} ↔ ${c.b_caption}  ·  score ${c.score}`}
            right={<span className="row">{c.flags.map((f: string) => <span className="badge" key={f}>{f.replace("_", " ")}</span>)}</span>}>
            <table className="fields"><thead><tr><th>field</th><th>match</th><th>A</th><th>B</th><th>weight</th></tr></thead><tbody>
              {c.fields.map((f: any) => (
                <tr key={f.field}><td>{f.field}</td><td>{f.level}</td>
                  <td>{f.a.map((x: any) => <div key={x.assertion}>{String(x.value)} <span className="muted">#{x.assertion}</span></div>)}</td>
                  <td>{f.b.map((x: any) => <div key={x.assertion}>{String(x.value)} <span className="muted">#{x.assertion}</span></div>)}</td>
                  <td className={f.weight < 0 ? "neg" : "pos"}>{f.weight > 0 ? "+" : ""}{f.weight}</td></tr>
              ))}</tbody></table>
            {c.flags.includes("name_only") && <div className="note">Only the name matches. Common names produce many look-alikes; look for a second identifier before merging.</div>}
            <div className="row" style={{ marginTop: 8 }}>
              <button className="primary" onClick={() => decide(p, "same")}>Same entity</button>
              <button onClick={() => decide(p, "not_same")}>Not the same</button>
              <span className="muted">Undo later by splitting from the entity profile.</span>
            </div>
          </Card>
        );
      })}
      {data?.length === 0 && <p className="muted">Nothing awaiting review.</p>}
    </>
  );
}
