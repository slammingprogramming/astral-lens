import { ReactNode, useEffect, useState } from "react";
import { openWithAuth } from "./api";
import { getToken } from "./auth";

/** A link to something the API serves. A browser cannot add the bearer token to a plain link, so when someone is signed in
 *  (team profile) the file is fetched with the token and opened or saved; otherwise it is an ordinary link. */
export function AuthLink({ href, download, className, title, children }: { href: string; download?: string; className?: string; title?: string; children: ReactNode }) {
  const [failed, setFailed] = useState(false);
  return (
    <>
      <a href={href} download={download} className={className} title={title} target={download ? undefined : "_blank"} rel="noreferrer"
        onClick={(e) => {
          if (!getToken()) return;
          e.preventDefault();
          setFailed(false);
          openWithAuth(href, download).catch(() => setFailed(true));
        }}>{children}</a>
      {failed && <span className="badge" title="The server refused or could not serve this file"> failed</span>}
    </>
  );
}

export function Grade({ s, i }: { s: string; i: number }) {
  // Admiralty: source reliability A-F, information credibility 1-6. Weak = worse than C3.
  const weak = s > "C" || i > 3;
  return (
    <span className={"grade" + (weak ? " weak" : "")} title={`source ${s} (A best, F = no basis to judge) / information ${i} (1 best, 6 = cannot be judged)`}>
      {s}
      {i}
    </span>
  );
}

export function Outcome({ o }: { o: string }) {
  const label = o === "hits" ? "hits" : o === "no_hits" ? "no hits" : "unknown";
  const tip =
    o === "unknown"
      ? "This source could not be asked. This is NOT a negative result."
      : o === "no_hits"
        ? "The source answered with nothing. That is not evidence of absence."
        : "The source returned records.";
  return (
    <span className={"pill " + o} title={tip}>
      {label}
    </span>
  );
}

export function ErrorBox({ e }: { e: unknown }) {
  return e ? <div className="err">{String((e as Error).message ?? e)}</div> : null;
}

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]): { data: T | null; error: unknown; loading: boolean; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [n, setN] = useState(0);
  useEffect(() => {
    let live = true;
    setLoading(true);
    fn()
      .then((d) => live && (setData(d), setError(null)))
      .catch((e) => live && setError(e))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, n]);
  return { data, error, loading, reload: () => setN((x) => x + 1) };
}

export function Card({ title, children, right }: { title?: string; children: ReactNode; right?: ReactNode }) {
  return (
    <div className="card">
      {(title || right) && (
        <div className="row" style={{ justifyContent: "space-between" }}>
          {title && <h3>{title}</h3>}
          {right}
        </div>
      )}
      {children}
    </div>
  );
}

export const short = (s: string | null | undefined, n = 12) => (s ? s.slice(0, n) + (s.length > n ? "…" : "") : "");

export interface Scope {
  from: string; // ISO date prefix; "" = unbounded
  to: string;
}
