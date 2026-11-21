import { ReactNode, useEffect, useState } from "react";
import { AuthState, loadAuth, startLogin } from "./auth";

// Shows the app when no sign-in is needed (solo profile) or the person has signed in; otherwise a sign-in prompt.
export default function Gate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState | null>(null);
  useEffect(() => { loadAuth().then(setState); }, []);
  if (!state) return <div className="app"><main className="main"><span className="muted">Loading…</span></main></div>;
  if (state.kind === "open" || state.kind === "ready") return <>{children}</>;
  return (
    <div className="app">
      <main className="main" style={{ maxWidth: 520, margin: "10vh auto" }}>
        <div className="card">
          <h3>Astral Lens</h3>
          {state.kind === "error" ? (
            <>
              <div className="note">{state.message}</div>
              <button onClick={() => { location.assign("/ui/"); }}>Try again</button>
            </>
          ) : (
            <>
              <p className="muted">This workspace uses your organisation's identity provider. You will be sent there to sign in and brought back here.</p>
              <button onClick={() => startLogin(state.cfg)}>Sign in</button>
            </>
          )}
        </div>
      </main>
    </div>
  );
}
