import { useEffect, useState } from "react";
import { api, post } from "./api";
import { Card, ErrorBox, Scope, useAsync } from "./ui";
import { Coverage, Evidence, Investigate, Settings } from "./viewsA";
import { Documents, Entities, GraphView, Review, TimelineView } from "./viewsB";
import { HubView, MapView, WorkbenchView } from "./viewsH";
import { MediaView } from "./viewsC";
import { FacesView } from "./viewsD";
import { AskView } from "./viewsE";
import { ExhibitView } from "./viewsF";
import { MonitorView } from "./viewsG";

const TABS = ["Investigate", "Ask", "Review", "Entities", "Graph", "Documents", "Media", "Faces", "Timeline", "Evidence", "Coverage", "Monitor", "Exhibit", "Workbench", "Map", "Hub"] as const;
type Tab = (typeof TABS)[number] | "Settings";

export default function App() {
  const cases = useAsync(() => api<any[]>("/cases"), []);
  const [caseId, setCaseId] = useState<string | null>(localStorage.getItem("case"));
  const [tab, setTab] = useState<Tab>("Investigate");
  const [bump, setBump] = useState(0);
  const [scope, setScope] = useState<Scope>({ from: "", to: "" });
  const [name, setName] = useState("");
  const [err, setErr] = useState<unknown>(null);
  const changed = () => setBump((b) => b + 1);

  useEffect(() => { if (caseId) localStorage.setItem("case", caseId); }, [caseId]);
  // A remembered case that no longer exists (new database, erased case) must not leave the views fetching a 404.
  useEffect(() => {
    if (caseId && cases.data && !cases.data.some((c) => c.id === caseId && c.state !== "erased")) setCaseId(null);
  }, [cases.data, caseId]);

  async function create() {
    setErr(null);
    try {
      const r = await post("/cases", { name: name || "Untitled case" });
      setName("");
      cases.reload();
      setCaseId(r.id);
    } catch (e) { setErr(e); }
  }

  return (
    <div className="app">
      <aside className="side">
        <h1>Astral Lens</h1>
        <div className="row" style={{ marginBottom: 8 }}>
          <input className="grow" placeholder="New case name" value={name} onChange={(e) => setName(e.target.value)} />
          <button onClick={create}>+</button>
        </div>
        <ErrorBox e={err ?? cases.error} />
        {(cases.data ?? []).filter((c) => c.state !== "erased").map((c) => (
          <button key={c.id} className={"case" + (c.id === caseId ? " on" : "")} onClick={() => setCaseId(c.id)}>{c.name}</button>
        ))}
        <hr style={{ borderColor: "var(--line)" }} />
        <button className="case" onClick={() => setTab("Settings")}>Sources, plugins, keys</button>
      </aside>
      <main className="main">
        <nav className="tabs">
          {TABS.map((t) => <button key={t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>{t}</button>)}
          <button className={tab === "Settings" ? "on" : ""} onClick={() => setTab("Settings")}>Settings</button>
        </nav>
        <div className="view">
          {tab === "Settings" && <Settings />}
          {tab !== "Settings" && !caseId && <Card><span className="muted">Create or select a case to begin.</span></Card>}
          {tab !== "Settings" && caseId && (
            <>
              {tab === "Investigate" && <Investigate caseId={caseId} onDone={changed} />}
              {tab === "Ask" && <AskView caseId={caseId} bump={bump} onChanged={changed} />}
              {tab === "Review" && <Review caseId={caseId} bump={bump} onChanged={changed} />}
              {tab === "Entities" && <Entities caseId={caseId} bump={bump} onChanged={changed} />}
              {tab === "Graph" && <GraphView caseId={caseId} bump={bump} scope={scope} />}
              {tab === "Documents" && <Documents caseId={caseId} bump={bump} />}
              {tab === "Media" && <MediaView caseId={caseId} bump={bump} />}
              {tab === "Faces" && <FacesView caseId={caseId} bump={bump} onChanged={changed} />}
              {tab === "Timeline" &&<TimelineView caseId={caseId} bump={bump} scope={scope} />}
              {tab === "Evidence" && <Evidence key={bump} caseId={caseId} />}
              {tab === "Coverage" && <Coverage key={bump} caseId={caseId} />}
              {tab === "Monitor" && <MonitorView caseId={caseId} bump={bump} onChanged={changed} />}
              {tab === "Exhibit" && <ExhibitView caseId={caseId} bump={bump} />}
              {tab === "Workbench" && <WorkbenchView caseId={caseId} bump={bump} onChanged={changed} />}
              {tab === "Map" && <MapView caseId={caseId} bump={bump} />}
              {tab === "Hub" && <HubView />}
            </>
          )}
        </div>
        {caseId && <TimeScope scope={scope} onChange={setScope} />}
      </main>
    </div>
  );
}

// One time scope for the whole workspace: it filters the graph and the timeline together.
function TimeScope({ scope, onChange }: { scope: Scope; onChange: (s: Scope) => void }) {
  return (
    <div className="timebar scope">
      <b>Time scope</b>
      <span className="muted">valid time</span>
      <input type="date" value={scope.from} onChange={(e) => onChange({ ...scope, from: e.target.value })} />
      <span>→</span>
      <input type="date" value={scope.to} onChange={(e) => onChange({ ...scope, to: e.target.value })} />
      <button onClick={() => onChange({ from: "", to: "" })} disabled={!scope.from && !scope.to}>clear</button>
      <span className="muted">Facts with no dates are always shown: unknown validity is not invalidity.</span>
    </div>
  );
}
