// Thin fetch wrapper. Every state-changing request carries X-Astral-Client: the
// solo API refuses anything without it (CSRF guard, backend/src/astral/api.py).

export class ApiError extends Error {
  constructor(public status: number, message: string, public body?: unknown) {
    super(message);
  }
}

export async function api<T = any>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const headers: Record<string, string> = { "X-Astral-Client": "ui", ...(init.headers as Record<string, string>) };
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
    throw new ApiError(r.status, detailOf(data, r.statusText), data);
  }
  return data as T;
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
  const r = await fetch(path, { method: "POST", headers: { "X-Astral-Client": "ui", "Content-Type": "application/json" }, body: JSON.stringify(json) });
  if (!r.ok) {
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
