import { useEffect, useMemo, useRef, useState } from "react";
import cytoscape from "cytoscape";
import { api, post, put } from "./api";
import { Card, ErrorBox, Outcome, short, useAsync } from "./ui";
import { colorOf } from "./viewsB";

// The analyst workbench: select a node, run a connector on it, watch the graph grow, annotate, analyse.
// Everything a transform adds goes through the assertion log; notes, colours and saved views are workspace
// data and are never evidence.

const LAYOUTS: Record<string, any> = {
  organic: { name: "cose", animate: false, nodeRepulsion: () => 9000, idealEdgeLength: () => 90 },
  circle: { name: "circle", animate: false },
  grid: { name: "grid", animate: false },
  hierarchy: { name: "breadthfirst", animate: false, directed: true, spacingFactor: 1.1 },
  concentric: { name: "concentric", animate: false, concentric: (n: any) => n.degree(), levelWidth: () => 2 },
};
const PALETTE = ["#4c78a8", "#f58518", "#54a24b", "#e45756", "#72b7b2", "#b279a2", "#9d755d", "#bab0ac"];

export function WorkbenchView({ caseId, bump, onChanged }: { caseId: string; bump: number; onChanged: () => void }) {
  const graph = useAsync(() => api<any>(`/cases/${caseId}/graph?documents=false`), [caseId, bump]);
  const notes = useAsync(() => api<any[]>(`/cases/${caseId}/annotations`), [caseId, bump]);
  const analysis = useAsync(() => api<any>(`/cases/${caseId}/analysis`), [caseId, bump]);
  const views = useAsync(() => api<any[]>(`/cases/${caseId}/views`), [caseId, bump]);
  const box = useRef<HTMLDivElement>(null);
  const cyRef = useRef<cytoscape.Core | null>(null);
  const [layout, setLayout] = useState("organic");
  const [sizeBy, setSizeBy] = useState(false);
  const [byCommunity, setByCommunity] = useState(false);
  const [sel, setSel] = useState<string[]>([]);
  const [fresh, setFresh] = useState<string[]>([]);
  const [panel, setPanel] = useState<"entity" | "analysis" | "machines" | "import" | "cypher">("entity");
  const [saved, setSaved] = useState<any>(null);

  const communityOf = useMemo(() => {
    const m = new Map<string, number>();
    (analysis.data?.communities ?? []).forEach((c: any, i: number) => c.members.forEach((id: string) => m.set(id, i)));
    return m;
  }, [analysis.data]);
  const between = useMemo(() => new Map<string, number>((analysis.data?.betweenness ?? []).map((r: any) => [r.id, r.score])), [analysis.data]);
  const maxB = Math.max(0.0001, ...between.values());

  useEffect(() => {
    if (!graph.data || !box.current) return;
    const ann = notes.data ?? [];
    const by = (kind: string, id: string) => ann.find((a) => a.kind === kind && a.cluster_id === id)?.body;
    const cy = cytoscape({
      container: box.current,
      elements: [
        ...graph.data.nodes.map((n: any) => ({
          data: {
            id: n.id, label: by("label", n.id) ?? n.caption ?? n.schema, sanc: n.listed_in.length > 0, pinned: !!by("pin", n.id),
            color: by("colour", n.id) ?? (byCommunity ? PALETTE[(communityOf.get(n.id) ?? 0) % PALETTE.length] : colorOf(n.schema)),
            size: sizeBy ? 12 + 30 * ((between.get(n.id) ?? 0) / maxB) : 16, noted: ann.some((a) => a.kind === "note" && a.cluster_id === n.id),
          },
        })),
        ...graph.data.edges.map((e: any) => ({ data: { id: e.id, source: e.source, target: e.target, label: e.label,
          w: 1 + Math.min(4, e.independent_sources), weak: e.info_grade > 3 || e.source_grade > "C" } })),
      ],
      layout: saved ? { name: "preset", positions: (n: any) => ({ x: saved.pos[n.id()]?.[0] ?? 0, y: saved.pos[n.id()]?.[1] ?? 0 }) } as any : LAYOUTS[layout],
      style: [
        { selector: "node", style: { label: "data(label)", "background-color": "data(color)", color: getComputedStyle(document.body).color, "font-size": 10, "text-valign": "bottom", "text-margin-y": 3, width: "data(size)", height: "data(size)", "text-wrap": "ellipsis", "text-max-width": "110px" } },
        { selector: "node[?sanc]", style: { "border-width": 3, "border-color": "#c0392b" } },
        { selector: "node[?noted]", style: { "border-width": 2, "border-color": "#e0a800", "border-style": "double" } },
        { selector: "node[?pinned]", style: { shape: "diamond" } },
        { selector: ".fresh", style: { "border-width": 3, "border-color": "#1e9e55" } },
        { selector: ".onpath", style: { "line-color": "#d35400", "target-arrow-color": "#d35400", "border-width": 3, "border-color": "#d35400", width: 4 } },
        { selector: "edge", style: { width: "data(w)", "curve-style": "bezier", "target-arrow-shape": "triangle", "line-color": "#8a93a3", "target-arrow-color": "#8a93a3", label: "data(label)", "font-size": 8, color: "#8a93a3", "text-rotation": "autorotate" } },
        { selector: "edge[?weak]", style: { "line-style": "dashed" } },
        { selector: ":selected", style: { "overlay-color": "#2f5fd0", "overlay-opacity": 0.25 } },
      ],
    });
    cy.nodes("[?pinned]").lock();
    cy.on("select unselect", "node", () => setSel(cy.$("node:selected").map((n) => n.id())));
    cyRef.current = cy;
    return () => { cy.destroy(); cyRef.current = null; };
  }, [graph.data, notes.data, layout, sizeBy, byCommunity, communityOf, saved]);

  useEffect(() => {
    cyRef.current?.nodes().removeClass("fresh");
    fresh.forEach((id) => cyRef.current?.getElementById(id).addClass("fresh"));
  }, [fresh, graph.data]);

  const one = sel.length === 1 ? sel[0] : null;

  const saveView = async () => {
    const name = prompt("Name this view");
    if (!name || !cyRef.current) return;
    const pos: Record<string, number[]> = {};
    cyRef.current.nodes().forEach((n) => { pos[n.id()] = [Math.round(n.position("x")), Math.round(n.position("y"))]; });
    await put(`/cases/${caseId}/views`, { name, layout, state: { pos } });
    views.reload();
  };

  return (
    <div className="split" style={{ gridTemplateColumns: "2fr 1fr" }}>
      <div>
        <div className="row" style={{ marginBottom: 6, flexWrap: "wrap", gap: 8 }}>
          <label>Layout <select value={layout} onChange={(e) => { setSaved(null); setLayout(e.target.value); }}>{Object.keys(LAYOUTS).map((l) => <option key={l}>{l}</option>)}</select></label>
          <label><input type="checkbox" checked={sizeBy} onChange={(e) => setSizeBy(e.target.checked)} /> size by betweenness</label>
          <label><input type="checkbox" checked={byCommunity} onChange={(e) => setByCommunity(e.target.checked)} /> colour by community</label>
          <select value="" onChange={(e) => { const v = views.data?.find((x) => x.id === e.target.value); if (v) setSaved(v.state); }}>
            <option value="">Saved views…</option>{(views.data ?? []).map((v) => <option key={v.id} value={v.id}>{v.name}</option>)}
          </select>
          <button onClick={saveView}>Save view</button>
          <span className="muted">Export:</span>
          {["graphml", "json", "nodes.csv", "edges.csv"].map((f) => <a key={f} href={`/cases/${caseId}/export/graph?format=${f}`} download>{f}</a>)}
          <a href={`/cases/${caseId}/report.html`} target="_blank" rel="noreferrer">report</a>
          <a href={`/cases/${caseId}/report.pdf`} download>pdf</a>
        </div>
        <ErrorBox e={graph.error} />
        <div className="graph" ref={box} />
        <div className="muted" style={{ marginTop: 4 }}>Click a node to select it (shift-click for several). Green ring = new from the last transform · double ring = has a note · diamond = pinned · red ring = on a list.</div>
      </div>
      <div>
        <nav className="tabs" style={{ marginBottom: 6 }}>
          {(["entity", "analysis", "machines", "import", "cypher"] as const).map((p) => <button key={p} className={panel === p ? "on" : ""} onClick={() => setPanel(p)}>{p === "entity" ? "Selected" : p[0].toUpperCase() + p.slice(1)}</button>)}
        </nav>
        {panel === "entity" && <Selected caseId={caseId} ids={sel} one={one} notes={notes.data ?? []} onAdded={(ids: string[]) => { setFresh(ids); onChanged(); }} onNote={() => notes.reload()} />}
        {panel === "analysis" && <Analysis caseId={caseId} data={analysis.data} sel={sel} cy={cyRef} />}
        {panel === "machines" && <Machines caseId={caseId} sel={sel} onDone={onChanged} />}
        {panel === "import" && <ImportTable caseId={caseId} onDone={onChanged} />}
        {panel === "cypher" && <CypherPanel caseId={caseId} bump={bump} />}
      </div>
    </div>
  );
}

function Selected({ caseId, ids, one, notes, onAdded, onNote }: any) {
  const t = useAsync(() => (one ? api<any>(`/cases/${caseId}/entities/${one}/transforms`) : Promise.resolve(null)), [caseId, one]);
  const [out, setOut] = useState<any>(null);
  const [err, setErr] = useState<unknown>(null);
  const [ok, setOk] = useState<Record<string, boolean>>({});
  const [text, setText] = useState("");
  const [busy, setBusy] = useState("");
  if (!ids.length) return <Card><span className="muted">Select a node to see what you can ask about it.</span></Card>;
  if (!one) return <Card title={`${ids.length} nodes selected`}><span className="muted">Select a single node to run connectors on it. Analysis tab: find paths between two.</span></Card>;
  const run = async (p: any) => {
    setBusy(p.plugin); setErr(null);
    try {
      const r = await post(`/cases/${caseId}/entities/${one}/transforms/${p.plugin}/run`, { approved: !!ok[p.plugin] });
      setOut(r); setOk({ ...ok, [p.plugin]: false });
      onAdded(r.new_entities.map((n: any) => n.id));
    } catch (e) { setErr(e); } finally { setBusy(""); }
  };
  const mine = notes.filter((a: any) => a.cluster_id === one);
  const add = async (kind: string, body: string) => { setErr(null); try { await post(`/cases/${caseId}/annotations`, { kind, body, cluster_id: one }); onNote(); setText(""); } catch (e) { setErr(e); } };
  return (
    <>
      <Card title={t.data?.entity.caption ?? "…"}>
        <div className="muted">{t.data?.entity.schema}</div>
        <h4>Run a connector on this entity</h4>
        {(t.data?.transforms ?? []).map((p: any) => (
          <div key={p.plugin} style={{ borderTop: "1px solid var(--line)", padding: "4px 0" }}>
            <div className="row"><b>{p.title}</b> <span className="badge">{p.footprint}</span></div>
            {p.ready ? <div className="muted">will ask: {Object.entries(p.inputs).map(([k, v]) => `${k} = ${v}`).join(", ")}</div> : <div className="muted">needs {p.missing.join(", ")} (this entity has none)</div>}
            {p.ready && p.needs_approval && <label><input type="checkbox" checked={!!ok[p.plugin]} onChange={(e) => setOk({ ...ok, [p.plugin]: e.target.checked })} /> I approve contacting the subject, this once</label>}
            {p.ready && <button disabled={!!busy || (p.needs_approval && !ok[p.plugin])} onClick={() => run(p)}>{busy === p.plugin ? "Running…" : "Run"}</button>}
          </div>
        ))}
        <ErrorBox e={err} />
        {out && (
          <div className="note">
            <b>{out.plugin}</b>: {out.status} · {out.new_entities.length} new entit{out.new_entities.length === 1 ? "y" : "ies"}, {out.new_edges} new link(s)
            {out.coverage.map((c: any, i: number) => <div key={i}><Outcome o={c.outcome} /> {c.source} <span className="muted">{c.note}</span></div>)}
            <div className="muted">{out.note}</div>
          </div>
        )}
      </Card>
      <Card title="Notes and marks (not evidence)">
        {mine.map((a: any) => <div key={a.id} className="row"><span className="badge">{a.kind}</span> {short(a.body, 60)} <button onClick={async () => { await api(`/cases/${caseId}/annotations/${a.id}`, { method: "DELETE" }); onNote(); }}>×</button></div>)}
        <textarea value={text} onChange={(e) => setText(e.target.value)} rows={2} style={{ width: "100%" }} placeholder="A note, label or #rrggbb colour" />
        <div className="row">
          <button disabled={!text} onClick={() => add("note", text)}>Add note</button>
          <button disabled={!text} onClick={() => add("label", text)}>Set label</button>
          <button disabled={!/^#[0-9a-fA-F]{6}$/.test(text)} onClick={() => add("colour", text)}>Set colour</button>
          <button onClick={() => add("pin", "pinned")}>Pin</button>
        </div>
      </Card>
    </>
  );
}

function Analysis({ caseId, data, sel, cy }: any) {
  const [res, setRes] = useState<any>(null);
  const [err, setErr] = useState<unknown>(null);
  const find = async () => {
    setErr(null);
    try {
      const r = await api<any>(`/cases/${caseId}/analysis/path?a=${sel[0]}&b=${sel[1]}&limit=5`);
      setRes(r);
      cy.current?.elements().removeClass("onpath");
      const ids = new Set<string>(r.paths.flat());
      ids.forEach((id) => cy.current?.getElementById(id).addClass("onpath"));
      cy.current?.edges().forEach((e: any) => { if (ids.has(e.source().id()) && ids.has(e.target().id())) e.addClass("onpath"); });
    } catch (e) { setErr(e); }
  };
  const Rank = ({ title, rows }: { title: string; rows: any[] }) => (
    <div><h4>{title}</h4>{rows.length === 0 ? <span className="muted">none</span> : rows.slice(0, 5).map((r) => <div key={r.id}>{r.caption} <span className="muted">{r.score}</span></div>)}</div>
  );
  return (
    <>
      <Card title="Paths between two selected nodes">
        <button disabled={sel.length !== 2} onClick={find}>Find shortest paths</button>
        <ErrorBox e={err} />
        {res && (res.paths.length === 0 ? <p>{res.note}</p> : <>
          <p>{res.paths.length}{res.truncated ? "+" : ""} path(s) of length {res.length}</p>
          {res.named.map((p: any[], i: number) => <div key={i}>{p.join(" → ")}</div>)}
        </>)}
      </Card>
      {data && (
        <Card title="Shape of the recorded graph">
          <div>{data.nodes} nodes · {data.edges} links · {data.components.length} connected group(s) · {data.communities.length} communit{data.communities.length === 1 ? "y" : "ies"}</div>
          <Rank title="Most links" rows={data.degree} />
          <Rank title="Brokers (betweenness)" rows={data.betweenness} />
          <Rank title="Influence (pagerank)" rows={data.pagerank} />
          <p className="muted">{data.note}</p>
        </Card>
      )}
    </>
  );
}

// Read-only Cypher over the optional Apache AGE projection. The server refuses anything that writes; names shown here come
// from Postgres, not from the graph copy, and a vertex Postgres no longer holds is flagged stale.
const CYPHER_EXAMPLE = "MATCH (a)-[r]->(b) RETURN a, r, b LIMIT 25";

function cell(v: any): string {
  if (v === null || v === undefined) return "";
  if (Array.isArray(v)) return v.map(cell).join(", ");
  if (typeof v === "object") {
    if (v.kind === "node") return `${v.caption ?? "(unknown)"} [${v.schema}]${v.stale ? " STALE" : ""}`;
    if (v.kind === "edge") return `-${v.label}-${v.source_grade ? ` ${v.source_grade}${v.info_grade ?? ""}` : ""}`;
    if (v.kind === "path") return v.elements.map(cell).join(" ");
    return JSON.stringify(v);
  }
  return String(v);
}

function CypherPanel({ caseId, bump }: { caseId: string; bump: number }) {
  const svc = useAsync(() => api<any>("/services"), [bump]);
  const [q, setQ] = useState(CYPHER_EXAMPLE);
  const [res, setRes] = useState<any>(null);
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const graph = svc.data?.graph;
  if (svc.data && !graph?.configured) {
    return <Card title="Cypher"><span className="muted">No graph projection is configured (set ASTRAL_AGE_DSN to a database with the Apache AGE extension). The path, neighbourhood and analysis tools work without it.</span></Card>;
  }
  return (
    <Card title="Cypher (read-only, Apache AGE)">
      <textarea rows={4} value={q} onChange={(e) => setQ(e.target.value)} style={{ width: "100%", fontFamily: "monospace" }} spellCheck={false} />
      <div className="row">
        <button disabled={busy || !q.trim()} onClick={async () => {
          setBusy(true); setErr(null);
          try { setRes(await post(`/cases/${caseId}/graph/cypher`, { query: q })); } catch (e) { setErr(e); setRes(null); } finally { setBusy(false); }
        }}>{busy ? "Running…" : "Run"}</button>
        <span className="muted">Vertex label = schema (Person, Company…), edge label = predicate. No writes, no RETURN *. A changed case is re-projected first.</span>
      </div>
      <ErrorBox e={err} />
      {graph && graph.reachable === false && <div className="note">The graph service is unreachable: {graph.error}</div>}
      {res && (
        <>
          <table>
            <tbody>{res.rows.map((r: any[], i: number) => <tr key={i}>{r.map((c, j) => <td key={j}>{cell(c)}</td>)}</tr>)}</tbody>
          </table>
          {!res.rows.length && <div className="muted">No rows. That means the recorded links do not match, not that nothing exists.</div>}
          {res.truncated && <div className="note">Only the first {res.rows.length} rows are shown.</div>}
          {res.stale_vertices ? <div className="note">{res.note}</div> : null}
          <div className="muted">{res.projection.rebuilt_for_this_query ? "Projection rebuilt for this query" : "Projection was current"} · {res.projection.vertices} vertices, {res.projection.edges} edges</div>
        </>
      )}
    </Card>
  );
}

function Machines({ caseId, sel, onDone }: { caseId: string; sel: string[]; onDone: () => void }) {
  const list = useAsync(() => api<any[]>("/machines"), []);
  const [name, setName] = useState("");
  const [run, setRun] = useState<any>(null);
  const [allow, setAllow] = useState(false);
  const [result, setResult] = useState<any>(null);
  const [err, setErr] = useState<unknown>(null);
  const [every, setEvery] = useState(60);
  const schedules = useAsync(() => api<any[]>(`/cases/${caseId}/machine-schedules`), [caseId]);
  const guard = async (f: () => Promise<void>) => { setErr(null); try { await f(); } catch (e) { setErr(e); } };
  return (
    <>
      <Card title="Machines (multi-step chains)">
        <select value={name} onChange={(e) => { setName(e.target.value); setRun(null); setResult(null); }}>
          <option value="">Choose a machine…</option>{(list.data ?? []).map((m) => <option key={m.name + m.version} value={m.name}>{m.name} v{m.version} ({m.trust})</option>)}
        </select>
        <button disabled={!name || !sel.length} onClick={() => guard(async () => { setResult(null); setRun(await post(`/cases/${caseId}/machines/${name}/propose`, { seeds: sel })); })}>Propose for {sel.length} selected</button>
        <ErrorBox e={err} />
        {run && (
          <div className="note">
            <b>Proposed</b>: at most {run.plan.max_calls} call(s) over {run.plan.stages.length} stage(s)
            <ol>{run.plan.stages.map((s: any) => <li key={s.id}>{s.transform} on {s.on.join("/")} ({s.from})</li>)}</ol>
            <label><input type="checkbox" checked={allow} onChange={(e) => setAllow(e.target.checked)} /> allow connectors that contact the subject</label>
            <button onClick={() => guard(async () => {
              await post(`/cases/${caseId}/machine-runs/${run.id}/approve`, { allow_target_contact: allow });
              setResult(await post(`/cases/${caseId}/machine-runs/${run.id}/run`)); setRun(null); onDone();
            })}>Approve and run</button>
          </div>
        )}
        {result && (
          <div className="note"><b>{result.status}</b> · {result.calls} call(s) · {result.coverage_summary.hits} hit(s), {result.coverage_summary.unknown} unknown
            {result.trace.filter((t: any) => !t.plan).map((t: any, i: number) => <div key={i} className="muted">{t.stage}: {t.transform} {t.status ?? t.outcome ?? t.event} {t.note ?? ""}</div>)}
          </div>
        )}
      </Card>
      <Card title="Scheduled runs (passive connectors only)">
        <div className="row">
          every <input type="number" min={5} value={every} onChange={(e) => setEvery(Number(e.target.value))} style={{ width: 70 }} /> minutes
          <button disabled={!name || !sel.length} onClick={() => guard(async () => { await post(`/cases/${caseId}/machine-schedules`, { machine: name, seeds: sel, interval_minutes: every }); schedules.reload(); })}>
            Schedule “{name || "choose a machine"}” for {sel.length} selected</button>
        </div>
        <div className="muted">Creating a schedule is the approval, and it only ever runs connectors that do not contact the subject. Runs appear in the case like any other.</div>
        {(schedules.data ?? []).map((s) => (
          <div key={s.id} className="row">
            <b>{s.machine}</b> every {s.interval_minutes} min · {s.seeds.length} seed(s) · {s.enabled ? "on" : "off"} · last run {s.last_run_at ?? "never"}
            <button onClick={() => guard(async () => { await post(`/cases/${caseId}/machine-schedules/${s.id}/${s.enabled ? "disable" : "enable"}`); schedules.reload(); })}>{s.enabled ? "Disable" : "Enable"}</button>
          </div>
        ))}
        {!(schedules.data ?? []).length && <div className="muted">No schedules in this case.</div>}
      </Card>
      <MachineEditor onSaved={() => list.reload()} />
    </>
  );
}

type Stage = { id: string; transform: string; on: string; from: string; max_entities: number };

// A form for what is otherwise hand-written JSON. It builds the same astral.machine/v1 document the API validates
// (the server still checks it), and shows that document so it can be shared as a package.
function MachineEditor({ onSaved }: { onSaved: () => void }) {
  const plugins = useAsync(() => api<any[]>("/plugins"), []);
  const [name, setName] = useState("my-machine");
  const [desc, setDesc] = useState("");
  const [seed, setSeed] = useState("Company");
  const [maxCalls, setMaxCalls] = useState(10);
  const [stages, setStages] = useState<Stage[]>([{ id: "s1", transform: "", on: "Company", from: "seed", max_entities: 5 }]);
  const [err, setErr] = useState<unknown>(null);
  const [saved, setSaved] = useState("");
  const transforms: string[] = (plugins.data ?? []).filter((p) => p.id.startsWith("connector.") && p.state === "enabled").map((p) => p.id);
  const split = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);
  const defn = {
    schema: "astral.machine/v1", machine: name, version: 1, description: desc, seed: { schemas: split(seed) },
    stages: stages.map((s, i) => ({ id: s.id, transform: s.transform, on: split(s.on), from: i === 0 ? "seed" : s.from, max_entities: s.max_entities })),
    limits: { max_calls: maxCalls },
  };
  const set = (i: number, patch: Partial<Stage>) => setStages(stages.map((s, j) => (j === i ? { ...s, ...patch } : s)));
  return (
    <Card title="Define a machine">
      <div className="row">
        name <input value={name} onChange={(e) => setName(e.target.value)} style={{ width: 150 }} />
        description <input value={desc} onChange={(e) => setDesc(e.target.value)} style={{ width: 240 }} />
        starts from <input value={seed} onChange={(e) => setSeed(e.target.value)} style={{ width: 130 }} title="entity types the machine may start from, comma separated" />
        at most <input type="number" min={1} max={200} value={maxCalls} onChange={(e) => setMaxCalls(Number(e.target.value))} style={{ width: 60 }} /> calls
      </div>
      {stages.map((s, i) => (
        <div key={i} className="row">
          <b>{i + 1}.</b> id <input value={s.id} onChange={(e) => set(i, { id: e.target.value })} style={{ width: 60 }} />
          ask <select value={s.transform} onChange={(e) => set(i, { transform: e.target.value })}>
            <option value="">choose a connector…</option>{transforms.map((t) => <option key={t}>{t}</option>)}
          </select>
          about <input value={s.on} onChange={(e) => set(i, { on: e.target.value })} style={{ width: 110 }} title="entity types" />
          {i > 0 && <select value={s.from} onChange={(e) => set(i, { from: e.target.value })}><option value="new">only what the last stage found</option><option value="all">everything so far</option><option value="seed">the starting entities</option></select>}
          max <input type="number" min={1} max={50} value={s.max_entities} onChange={(e) => set(i, { max_entities: Number(e.target.value) })} style={{ width: 55 }} />
          <button disabled={stages.length < 2} onClick={() => setStages(stages.filter((_, j) => j !== i))}>remove</button>
        </div>
      ))}
      <div className="row">
        <button disabled={stages.length >= 8} onClick={() => setStages([...stages, { id: `s${stages.length + 1}`, transform: "", on: "Company", from: "new", max_entities: 5 }])}>Add stage</button>
        <button onClick={async () => { setErr(null); try { await post("/machines", defn); setSaved(`saved ${name} v1`); onSaved(); } catch (e) { setErr(e); setSaved(""); } }}>Save machine</button>
        {saved && <span className="muted">{saved}</span>}
      </div>
      <ErrorBox e={err} />
      <details><summary className="muted">The document this saves (shareable as a package)</summary><pre>{JSON.stringify(defn, null, 2)}</pre></details>
      <div className="muted">A machine cannot approve itself: contacting the subject is decided when a run is approved. The server validates every field.</div>
    </Card>
  );
}

function ImportTable({ caseId, onDone }: { caseId: string; onDone: () => void }) {
  const [text, setText] = useState("");
  const [file, setFile] = useState("table.csv");
  const [schema, setSchema] = useState("Person");
  const [key, setKey] = useState("");
  const [nameCol, setNameCol] = useState("");
  const [cols, setCols] = useState("");
  const [links, setLinks] = useState("");
  const [sg, setSg] = useState("F");
  const [ig, setIg] = useState(6);
  const [res, setRes] = useState<any>(null);
  const [err, setErr] = useState<unknown>(null);
  const header = text.split(/\r?\n/)[0] ?? "";
  const go = async () => {
    setErr(null);
    try {
      const columns: Record<string, string> = {};
      cols.split("\n").map((l) => l.trim()).filter(Boolean).forEach((l) => { const [c, p] = l.split("="); columns[c.trim()] = (p ?? "").trim(); });
      const lk = links.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => { const [column, predicate, sch] = l.split(":"); return { column: column.trim(), predicate: (predicate ?? "").trim(), schema: (sch ?? "").trim() }; });
      setRes(await post(`/cases/${caseId}/import/table`, { csv_text: text, filename: file, source_grade: sg, info_grade: ig,
        mapping: { schema, key, name_column: nameCol || undefined, columns, links: lk } }));
      onDone();
    } catch (e) { setErr(e); }
  };
  return (
    <Card title="Import a spreadsheet as entities">
      <input type="file" accept=".csv,text/csv" onChange={async (e) => { const f = e.target.files?.[0]; if (f) { setFile(f.name); setText(await f.text()); } }} />
      {header && <div className="muted">columns: {header}</div>}
      <div className="row"><label>Entity type <input value={schema} onChange={(e) => setSchema(e.target.value)} size={10} /></label>
        <label>Key column <input value={key} onChange={(e) => setKey(e.target.value)} size={10} /></label>
        <label>Name column <input value={nameCol} onChange={(e) => setNameCol(e.target.value)} size={10} /></label></div>
      <textarea rows={3} style={{ width: "100%" }} placeholder={"properties, one per line: column=predicate\ncountry=jurisdiction"} value={cols} onChange={(e) => setCols(e.target.value)} />
      <textarea rows={3} style={{ width: "100%" }} placeholder={"links, one per line: column:predicate:TargetType\nemployer:employer:Company"} value={links} onChange={(e) => setLinks(e.target.value)} />
      <div className="row"><label>Source grade <select value={sg} onChange={(e) => setSg(e.target.value)}>{"ABCDEF".split("").map((g) => <option key={g}>{g}</option>)}</select></label>
        <label>Info grade <select value={ig} onChange={(e) => setIg(+e.target.value)}>{[1, 2, 3, 4, 5, 6].map((g) => <option key={g}>{g}</option>)}</select></label>
        <span className="muted">F/6 = cannot be judged. Raise only if you can say why.</span></div>
      <button disabled={!text || !key} onClick={go}>Import</button>
      <ErrorBox e={err} />
      {res && <div className="note">{res.rows} rows → {res.entities} entities, {res.assertions} assertions. File stored as evidence <span className="mono">{short(res.artifact, 12)}</span>.</div>}
    </Card>
  );
}

// ---------------------------------------------------------------- Map
// A plain equirectangular plot with a graticule and NO background tiles: loading tiles would tell a third
// party exactly which area the analyst is looking at. Positions are what sources reported.
export function MapView({ caseId, bump }: { caseId: string; bump: number }) {
  const { data, error } = useAsync(() => api<any>(`/cases/${caseId}/geo`), [caseId, bump]);
  const pts: any[] = data?.points ?? [];
  const [sel, setSel] = useState<string | null>(null);
  const [view, setView] = useState<{ x: number; y: number; w: number } | null>(null);
  const drag = useRef<{ x: number; y: number } | null>(null);
  const fit = useMemo(() => {
    if (!pts.length) return { x: -180, y: -90, w: 360 };
    const la = pts.map((p) => p.lat), lo = pts.map((p) => p.lon);
    const w = Math.max(0.02, Math.max(...lo) - Math.min(...lo), (Math.max(...la) - Math.min(...la)) * 2) * 1.4;
    return { x: (Math.min(...lo) + Math.max(...lo)) / 2 - w / 2, y: -((Math.min(...la) + Math.max(...la)) / 2) - w / 4, w };
  }, [data]);
  const v = view ?? fit;
  const h = v.w / 2;
  const step = v.w > 120 ? 30 : v.w > 40 ? 10 : v.w > 12 ? 5 : v.w > 3 ? 1 : v.w > 0.6 ? 0.2 : 0.05;
  const lines: number[] = [];
  for (let g = Math.ceil(v.x / step) * step; g <= v.x + v.w; g += step) lines.push(g);
  const rows: number[] = [];
  for (let g = Math.ceil(-(v.y + h) / step) * step; g <= -v.y; g += step) rows.push(g);
  const chosen = pts.find((p) => p.id === sel);
  const r = v.w / 140;
  return (
    <div className="split" style={{ gridTemplateColumns: "2fr 1fr" }}>
      <Card title={`${pts.length} located`} right={<span className="muted">no basemap: tiles would reveal what you are looking at</span>}>
        <ErrorBox e={error} />
        <svg viewBox={`${v.x} ${v.y} ${v.w} ${h}`} style={{ width: "100%", height: 460, background: "var(--panel, #f4f6f9)", border: "1px solid var(--line)", cursor: drag.current ? "grabbing" : "grab" }}
          onWheel={(e) => { const f = e.deltaY > 0 ? 1.25 : 0.8; setView({ x: v.x + (v.w - v.w * f) / 2, y: v.y + (h - h * f) / 2, w: v.w * f }); }}
          onMouseDown={(e) => { drag.current = { x: e.clientX, y: e.clientY }; }}
          onMouseUp={() => { drag.current = null; }} onMouseLeave={() => { drag.current = null; }}
          onMouseMove={(e) => { if (!drag.current) return; const k = v.w / e.currentTarget.getBoundingClientRect().width; setView({ ...v, x: v.x - (e.clientX - drag.current.x) * k, y: v.y - (e.clientY - drag.current.y) * k }); drag.current = { x: e.clientX, y: e.clientY }; }}>
          {lines.map((g) => <g key={"x" + g}><line x1={g} x2={g} y1={v.y} y2={v.y + h} stroke="#c4cad4" strokeWidth={v.w / 1200} /><text x={g} y={v.y + r * 2} fontSize={r * 1.8} fill="#8a93a3">{+g.toFixed(2)}°</text></g>)}
          {rows.map((g) => <g key={"y" + g}><line y1={-g} y2={-g} x1={v.x} x2={v.x + v.w} stroke="#c4cad4" strokeWidth={v.w / 1200} /><text y={-g - r * 0.4} x={v.x + r * 0.5} fontSize={r * 1.8} fill="#8a93a3">{+g.toFixed(2)}°</text></g>)}
          {pts.map((p) => <circle key={p.id} cx={p.lon} cy={-p.lat} r={p.id === sel ? r * 1.8 : r} fill={colorOf(p.schema)} stroke="#222" strokeWidth={r / 6} onClick={(e) => { e.stopPropagation(); setSel(p.id); }} style={{ cursor: "pointer" }}><title>{p.caption}</title></circle>)}
        </svg>
        <div className="row"><button onClick={() => setView(null)}>Fit to points</button><span className="muted">scroll to zoom, drag to pan</span></div>
      </Card>
      <Card title={chosen?.caption ?? "Selected point"}>
        {!chosen && <span className="muted">Click a point.</span>}
        {chosen && <div>{chosen.schema}<br />{chosen.lat.toFixed(5)}, {chosen.lon.toFixed(5)}<br />{chosen.date && <>reported {chosen.date}<br /></>}<span className="muted">weakest-link grade of the position: {chosen.grade}</span></div>}
        {data?.unplaced?.length > 0 && <p className="muted">{data.unplaced.length} entit{data.unplaced.length === 1 ? "y has" : "ies have"} a coordinate that is incomplete or out of range and is not drawn.</p>}
        <p className="muted">{data?.note}</p>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------- Hub (plugin registry and community repositories)
export function HubView() {
  const status = useAsync(() => api<any>("/hub"), []);
  const [q, setQ] = useState("");
  const [kind, setKind] = useState("");
  const found = useAsync(() => api<any>(`/hub/search?q=${encodeURIComponent(q)}${kind ? `&kind=${kind}` : ""}`), [q, kind]);
  const updates = useAsync(() => api<any[]>("/hub/updates"), []);
  const [done, setDone] = useState<Record<string, any>>({});
  const [err, setErr] = useState<unknown>(null);
  const install = async (r: any) => {
    setErr(null);
    try { setDone({ ...done, [r.kind + r.id]: await post("/hub/install", { repository: r.repository, kind: r.kind, id: r.id, version: r.version }) }); } catch (e) { setErr(e); }
  };
  return (
    <>
      <Card title="Repositories">
        {(status.data?.repositories ?? []).length === 0 && <span className="muted">None configured. Set ASTRAL_REPOSITORIES to directories or URLs that serve an index.json (remote ones also need ASTRAL_HUB_EGRESS=direct).</span>}
        {(status.data?.repositories ?? []).map((r: any) => <div key={r.repository}>{r.error ? <span>⚠ {r.repository}: {r.error}</span> : <span><b>{r.name}</b> · {r.packages} package(s) · <span className="muted">{r.repository}</span></span>}</div>)}
        <div className="muted">Install policy: {status.data?.trust}. A signature says who published a package, never that it is safe. Plugins install disabled.</div>
      </Card>
      {(updates.data ?? []).filter((u) => u.available).length > 0 && (
        <Card title="Updates available">{updates.data!.filter((u) => u.available).map((u) => <div key={u.id}>{u.id}: {u.installed} → {u.available}</div>)}</Card>
      )}
      <Card title="Browse">
        <div className="row"><input placeholder="search" value={q} onChange={(e) => setQ(e.target.value)} />
          <select value={kind} onChange={(e) => setKind(e.target.value)}><option value="">all kinds</option>{["plugin", "site-pack", "lens", "machine"].map((k) => <option key={k}>{k}</option>)}</select></div>
        <ErrorBox e={err} />
        {(found.data?.results ?? []).map((r: any) => {
          const d = done[r.kind + r.id];
          return (
            <div key={r.repository + r.kind + r.id + r.version} style={{ borderTop: "1px solid var(--line)", padding: "4px 0" }}>
              <b>{r.title || r.id}</b> <span className="badge">{r.kind}</span> v{r.version} <span className="muted">[{r.licence}] {r.signed ? "signed" : "UNSIGNED"}</span>
              <div className="muted">{r.description}</div>
              {d ? <div className="note">Installed: publisher {d.publisher.status}. {d.note}</div> : <button onClick={() => install(r)}>Install</button>}
            </div>
          );
        })}
        {(found.data?.errors ?? []).map((e: any) => <div key={e.repository}>⚠ {e.repository}: {e.error}</div>)}
      </Card>
    </>
  );
}
