// Thin fetch wrapper. Every state-changing request carries X-Astral-Client: the
// solo API refuses anything without it (CSRF guard, backend/src/astral/api.py).

import { authHeaders, dropToken, getToken } from "./auth";

/** A 401 while signed in means the token expired or was refused: drop it and start again at the sign-in prompt. */
function unauthorized(status: number): void {
  if (status === 401 && getToken()) {
    dropToken();
    location.reload();
  }
}

export class ApiError extends Error {
  constructor(public status: number, message: string, public body?: unknown) {
    super(message);
  }
}

export async function api<T = any>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const headers: Record<string, string> = { "X-Astral-Client": "ui", ...authHeaders(), ...(init.headers as Record<string, string>) };
  let body = init.body;
  if (init.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(init.json);
  }
  const r = await fetch(path, { ...init, headers, body });
  const text = await r.text();
  let data: any = text;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    /* keep text */
  }
  if (!r.ok) {
    unauthorized(r.status);
    throw new ApiError(r.status, detailOf(data, r.statusText), data);
  }
  return data as T;
}

/** Fetch a file the API serves (an artifact, an export, a report) with the bearer token, for a link a browser cannot add a header to.
 *  `download` saves it under that name; otherwise it opens in a new tab. */
export async function openWithAuth(path: string, download?: string): Promise<void> {
  const r = await fetch(path, { headers: { "X-Astral-Client": "ui", ...authHeaders() } });
  if (!r.ok) {
    unauthorized(r.status);
    throw new ApiError(r.status, r.statusText || `HTTP ${r.status}`);
  }
  const url = URL.createObjectURL(await r.blob());
  if (download) {
    const a = document.createElement("a");
    a.href = url;
    a.download = download;
    a.click();
  } else {
    window.open(url, "_blank", "noopener");
  }
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

function detailOf(data: any, fallback: string): string {
  if (typeof data !== "object" || !data || !("detail" in data)) return fallback;
  const d = data.detail;
  if (typeof d === "string") return d;
  if (d && typeof d === "object" && "message" in d)
    return d.message + (Array.isArray(d.problems) && d.problems.length ? `: ${d.problems.join("; ")}` : "");
  return String(d);
}

/** POST and keep the response as a file (the exhibit export). */
export async function postForFile(path: string, json: unknown): Promise<{ blob: Blob; name: string; sha256: string | null }> {
  const r = await fetch(path, { method: "POST", headers: { "X-Astral-Client": "ui", ...authHeaders(), "Content-Type": "application/json" }, body: JSON.stringify(json) });
  if (!r.ok) {
    unauthorized(r.status);
    const text = await r.text();
    let data: unknown = text;
    try { data = JSON.parse(text); } catch { /* keep text */ }
    throw new ApiError(r.status, detailOf(data, r.statusText), data);
  }
  const m = /filename="?([^";]+)"?/.exec(r.headers.get("content-disposition") ?? "");
  return { blob: await r.blob(), name: m?.[1] ?? "exhibit.zip", sha256: r.headers.get("x-exhibit-sha256") };
}

export const post = <T = any>(path: string, json?: unknown) => api<T>(path, { method: "POST", json: json ?? {} });
export const put = <T = any>(path: string, json: unknown) => api<T>(path, { method: "PUT", json });

export async function sha256Hex(buf: ArrayBuffer): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", buf);
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
