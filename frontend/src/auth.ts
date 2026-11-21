// Browser sign-in for the team profile: OpenID Connect authorization code + PKCE as a public client, run entirely here.
// The API holds no session and never sees a code, a verifier or a password; it validates the bearer token on every request.
// The token lives in sessionStorage (gone when the tab closes), is never written to a URL, and is dropped on 401 or expiry.
//
// Needs, at the identity provider: a public client (no secret) with the redirect URI `<origin>/ui/` registered, the code
// flow with PKCE S256 allowed, and CORS allowed from this origin on the token endpoint. Not verified against a real provider.

export type AuthConfig = {
  enabled: boolean;
  browser_login?: boolean;
  client_id?: string;
  authorization_endpoint?: string;
  token_endpoint?: string;
  scope?: string;
  token_use?: "id_token" | "access_token";
};

export type AuthState =
  | { kind: "open" }                                   // no sign-in needed here (solo profile, or a proxy signs people in)
  | { kind: "ready" }                                  // signed in
  | { kind: "signin"; cfg: AuthConfig }                // must sign in
  | { kind: "error"; message: string };

const TOKEN = "astral.auth.token";
const PENDING = "astral.auth.pending";

const store = {
  get: (k: string) => { try { return sessionStorage.getItem(k); } catch { return null; } },
  set: (k: string, v: string) => { try { sessionStorage.setItem(k, v); } catch { /* private mode: sign-in will not stick */ } },
  del: (k: string) => { try { sessionStorage.removeItem(k); } catch { /* ignore */ } },
};

const b64url = (b: ArrayBuffer | Uint8Array) =>
  btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const random = (n: number) => b64url(crypto.getRandomValues(new Uint8Array(n)));

function claims(jwt: string): Record<string, any> | null {
  try {
    const p = jwt.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(atob(p + "=".repeat((4 - (p.length % 4)) % 4)));
  } catch { return null; }
}

/** The bearer token, or null when there is none or it is about to expire. */
export function getToken(): string | null {
  try {
    const t = JSON.parse(store.get(TOKEN) ?? "null") as { token: string; exp: number } | null;
    return t && t.exp - 30 > Date.now() / 1000 ? t.token : null;
  } catch { return null; }
}

/** True when this tab signed in earlier and the token has since expired (or is about to): the next request would go out
 *  unauthenticated, so the caller sends the person back to the sign-in prompt instead of failing silently. */
export function sessionExpired(): boolean {
  return store.get(TOKEN) !== null && getToken() === null;
}

export const authHeaders =(): Record<string, string> => { const t = getToken(); return t ? { Authorization: `Bearer ${t}` } : {}; };

export function dropToken(): void { store.del(TOKEN); }

export function signOut(): void { dropToken(); store.del(PENDING); location.assign(redirectUri()); }

const redirectUri = () => `${location.origin}/ui/`;

export async function startLogin(cfg: AuthConfig): Promise<void> {
  const verifier = random(48), state = random(16), nonce = random(16);
  const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  store.set(PENDING, JSON.stringify({ verifier, state, nonce }));
  const q = new URLSearchParams({
    response_type: "code", client_id: cfg.client_id!, redirect_uri: redirectUri(), scope: cfg.scope ?? "openid profile email",
    state, nonce, code_challenge: challenge, code_challenge_method: "S256",
  });
  location.assign(`${cfg.authorization_endpoint}?${q}`);
}

async function finish(cfg: AuthConfig, code: string, state: string): Promise<AuthState> {
  const pending = JSON.parse(store.get(PENDING) ?? "null") as { verifier: string; state: string; nonce: string } | null;
  store.del(PENDING);
  history.replaceState(null, "", redirectUri());                   // the code must not stay in the address bar or history
  if (!pending || pending.state !== state) return { kind: "error", message: "The sign-in response did not match this browser's request (state). Try again." };
  let r: Response;
  try {
    r = await fetch(cfg.token_endpoint!, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" },
      body: new URLSearchParams({ grant_type: "authorization_code", code, redirect_uri: redirectUri(), client_id: cfg.client_id!, code_verifier: pending.verifier }),
    });
  } catch {
    return { kind: "error", message: "Could not reach the identity provider's token endpoint (it may not allow requests from this origin)." };
  }
  const body = await r.json().catch(() => null);
  if (!r.ok || !body) return { kind: "error", message: `The identity provider refused the sign-in${body?.error ? `: ${body.error}` : ""}.` };
  const token: string | undefined = cfg.token_use === "access_token" ? body.access_token : body.id_token;
  if (!token) return { kind: "error", message: `The identity provider returned no ${cfg.token_use ?? "id_token"}.` };
  const c = claims(token);                                          // the API verifies the signature; this only catches a mixed-up response
  if (cfg.token_use !== "access_token" && (!c || c.nonce !== pending.nonce)) return { kind: "error", message: "The sign-in response did not match this browser's request (nonce)." };
  const exp = c?.exp ?? Date.now() / 1000 + (Number(body.expires_in) || 300);
  store.set(TOKEN, JSON.stringify({ token, exp }));
  return { kind: "ready" };
}

/** Decide what to show: the app, a sign-in prompt, or an error. Completes a sign-in that is returning from the provider. */
export async function loadAuth(): Promise<AuthState> {
  let cfg: AuthConfig;
  try {
    const r = await fetch("/auth/config");
    if (r.status === 503) return { kind: "error", message: "The identity provider is unavailable, so sign-in is not possible right now." };
    if (!r.ok) return { kind: "open" };                              // an older server with no such endpoint
    cfg = await r.json();
  } catch { return { kind: "open" }; }
  if (!cfg.enabled || !cfg.browser_login) return { kind: "open" };
  const q = new URLSearchParams(location.search);
  if (q.get("error")) {
    history.replaceState(null, "", redirectUri());
    return { kind: "error", message: `Sign-in was not completed: ${q.get("error")}${q.get("error_description") ? ` (${q.get("error_description")})` : ""}` };
  }
  if (q.get("code") && q.get("state")) return finish(cfg, q.get("code")!, q.get("state")!);
  return getToken() ? { kind: "ready" } : { kind: "signin", cfg };
}
