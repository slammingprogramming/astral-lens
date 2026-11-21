import { useState } from "react";

// Where to send a donation. The same details are in README.md and .github/FUNDING.yml.
// This panel only displays text and one ordinary link the user may click: nothing here
// calls out to any service on its own, and nothing in the app depends on it.
export const KOFI = "https://ko-fi.com/scenprogramming";
export const CONTACT = "https://smp14.simplex.im/a#3gZ-zeHs4QrFZKLAN0o3SC_XQJXhj1eYBVTO_c0FAtg";
export const WALLETS: { coin: string; name: string; address: string }[] = [
  { coin: "BTC", name: "Bitcoin", address: "sp1qq2637t6ve9zp4wh0e4ly2u7h7jwfn0k6c4535lxpp3wrqvmkua6xjqkwevyz0wwqx5ly2f0q29qh48wgf9gl0zt049za9kr9m8xgkey4cyggxa0x" },
  { coin: "ETH", name: "Ethereum", address: "0x8F238db898102125Fd968ed7CaD959Cb594a7e93" },
  { coin: "XMR", name: "Monero", address: "8AYK93mQEzsFFEFkuPN7wC9rPcXCyEXa1Ye5bH9U2GR3PpaZiYc84SxjMcdSdVMaaQUGmCXYkuxdDSG5bM3EVyqR25nbumV" },
];

function CopyButton({ text }: { text: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setState("copied");
    } catch {
      setState("failed"); // no clipboard permission: the address is still selectable on screen
    }
    setTimeout(() => setState("idle"), 2000);
  }
  return <button onClick={copy}>{state === "copied" ? "copied" : state === "failed" ? "select and copy by hand" : "copy"}</button>;
}

// A collapsed section at the foot of Settings. It is closed by default and never shown anywhere else.
export function Support() {
  return (
    <details style={{ marginBottom: 12 }}>
      <summary className="muted">About, contact and supporting Astral Lens</summary>
      <div className="card" style={{ marginTop: 8 }}>
        <p>
          Astral Lens is free software (AGPL-3.0-or-later) and stays that way. Nothing on this panel is needed to use it, and
          nothing here contacts any service unless you click a link.
        </p>
        <p>
          <b>Contact:</b> <a href={CONTACT} target="_blank" rel="noopener noreferrer">SimpleX</a> (opens in the SimpleX app).
          Use it for questions and for reporting a security problem; leave case data, evidence and keys out of what you send.
        </p>
        <p>
          <b>Donate (optional):</b> fiat through <a href={KOFI} target="_blank" rel="noopener noreferrer">Ko-fi</a>, or crypto:
        </p>
        <table><tbody>
          {WALLETS.map((w) => (
            <tr key={w.coin}>
              <td><b>{w.name}</b> ({w.coin})</td>
              <td className="mono" style={{ wordBreak: "break-all", userSelect: "all" }}>{w.address}</td>
              <td><CopyButton text={w.address} /></td>
            </tr>
          ))}
        </tbody></table>
        <p className="muted">Check an address against the project README before sending anything; a transfer cannot be undone.</p>
      </div>
    </details>
  );
}
