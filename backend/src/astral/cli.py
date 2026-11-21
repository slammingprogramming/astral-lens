"""`astral` command line: migrate, preflight, serve, secrets, keygen, exhibit."""

from __future__ import annotations

import argparse
import json
import sys

from astral.compute.preflight import run_preflight
from astral.config import Settings
from astral.db import Database


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="astral")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply database migrations")
    pf = sub.add_parser("preflight", help="probe the host and report capability and licence posture")
    pf.add_argument("--json", action="store_true")
    sc = sub.add_parser("secrets", help="manage the encrypted secret store (ASTRAL_SECRETS_PASSPHRASE)")
    sc.add_argument("action", choices=["set", "list", "delete", "rotate"])
    sc.add_argument("name", nargs="?")
    sv = sub.add_parser("serve", help="run the API (binds to localhost only)")
    sv.add_argument("--port", type=int, default=8000)
    kg = sub.add_parser("keygen", help="create an Ed25519 signing key pair (passphrase: ASTRAL_SIGNING_PASSPHRASE)")
    kg.add_argument("--out", required=True, help="path for the private key; the public key is written beside it as .pub")
    sub.add_parser("mcp", help="run the read-only MCP server on stdio (team profile: ASTRAL_MCP_TOKEN)")
    pl = sub.add_parser("plugin", help="sign a plugin, verify its signature, or trust a publisher's key")
    pl.add_argument("action", choices=["sign", "verify", "trust"])
    pl.add_argument("target", help="sign/verify: the plugin directory; trust: a public key .pub file")
    pl.add_argument("--key", help="sign: the Ed25519 private key (passphrase: ASTRAL_SIGNING_PASSPHRASE)")
    pl.add_argument("--name", help="trust: a short name for the key")
    rp = sub.add_parser("repo", help="build, check or search a plugin / site-pack repository")
    rp.add_argument("action", choices=["build", "verify", "search"])
    rp.add_argument("target", help="build: the output directory; verify/search: a repository directory or URL")
    rp.add_argument("--plugin", action="append", default=[], help="build: a plugin directory (repeatable)")
    rp.add_argument("--pack", action="append", default=[], help="build: a site-pack .json file (repeatable)")
    rp.add_argument("--package", action="append", default=[], help="build: an exported lens or machine package .json (repeatable)")
    rp.add_argument("--name", default="repository", help="build: the repository's name")
    rp.add_argument("--key", help="build: sign every entry with this Ed25519 private key")
    rp.add_argument("--query", default="", help="search: text to look for")
    ex = sub.add_parser("exhibit", help="export a case as a self-verifying package, or verify one")
    ex.add_argument("action", choices=["export", "verify"])
    ex.add_argument("target", help="export: the case id; verify: a bundle .zip or unzipped folder")
    ex.add_argument("--out", help="export: where to write the .zip")
    ex.add_argument("--statement-file", help="export: the operator's own statement (required)")
    ex.add_argument("--plan", action="append", default=[], help="export: include a finished AI answer (plan id); repeatable")
    ex.add_argument("--all-artifacts", action="store_true", help="export: every artifact in the case, not only those relied on")
    ex.add_argument("--sign", action="store_true", help="export: sign the manifest with ASTRAL_SIGNING_KEY")
    ex.add_argument("--public-key", help="verify: a public key PEM you trust, instead of the one in the bundle")
    args = p.parse_args(argv)
    settings = Settings.from_env()

    if args.cmd == "preflight":
        rpt = run_preflight(deployment_commercial=settings.commercial)
        print(json.dumps(rpt.to_dict(), indent=2, default=str) if args.json else rpt.render())
        return 0
    if args.cmd == "secrets":
        return _secrets_cmd(args, settings)
    if args.cmd == "mcp":
        from astral import mcp
        from astral.evidence import EvidenceStore

        with Database(settings.dsn) as db:
            db.migrate()
            if settings.profile == "team":
                from astral import auth

                with db.connection() as conn:
                    auth.bootstrap_team(conn)
            else:
                db.bootstrap_solo()
            mcp.build(settings, db, EvidenceStore(settings.data_dir / "evidence")).serve()
        return 0
    if args.cmd == "keygen":
        return _keygen_cmd(args)
    if args.cmd == "plugin":
        return _plugin_cmd(args, settings)
    if args.cmd == "exhibit":
        return _exhibit_cmd(args, settings)
    if args.cmd == "repo":
        return _repo_cmd(args, settings)
    if args.cmd == "migrate":
        with Database(settings.dsn) as db:
            applied = db.migrate()
            db.bootstrap_solo()
        print("applied:", ", ".join(applied) or "nothing to do")
        return 0
    if args.cmd == "serve":
        import uvicorn

        from astral.api import create_app

        print(run_preflight(deployment_commercial=settings.commercial).render(), file=sys.stderr)
        uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port)
        return 0
    return 2


def _keygen_cmd(args) -> int:
    import os
    from pathlib import Path

    from astral import signing

    out = Path(args.out)
    if out.exists():
        print(f"error: {out} exists; refusing to overwrite a key", file=sys.stderr)
        return 1
    priv, pub = signing.generate_keypair(os.environ.get("ASTRAL_SIGNING_PASSPHRASE") or None)
    out.write_bytes(priv)
    out.with_suffix(out.suffix + ".pub").write_bytes(pub)
    try:
        out.chmod(0o600)
    except OSError:
        pass
    print(f"private key: {out}")
    print(f"public key:  {out.with_suffix(out.suffix + '.pub')}")
    print(f"fingerprint: {signing.fingerprint(pub)}")
    if not os.environ.get("ASTRAL_SIGNING_PASSPHRASE"):
        print("note: the private key is NOT encrypted (set ASTRAL_SIGNING_PASSPHRASE before keygen to encrypt it)", file=sys.stderr)
    return 0


def _repo_cmd(args, settings) -> int:
    import os
    from pathlib import Path

    from astral import egress, hub
    from astral.plugins import trust

    try:
        if args.action == "build":
            key = (Path(args.key).read_bytes(), os.environ.get("ASTRAL_SIGNING_PASSPHRASE") or None) if args.key else None
            idx = hub.build(Path(args.target), plugins=[Path(p) for p in args.plugin], packs=[Path(p) for p in args.pack],
                            packages=[Path(p) for p in args.package], name=args.name, signing_key=key)
            print(f"wrote {len(idx['packages'])} package(s) to {args.target}" + (" (signed)" if key else " (UNSIGNED)"))
            return 0
        repo = hub.Repository(args.target, remote_policy=egress.EgressPolicy("direct" if settings.hub_egress == "direct" else "local-only"))
        store = trust.TrustStore(settings.data_dir / "trusted-keys")
        if args.action == "search":
            for e in repo.search(args.query):
                print(f"{e['kind']:<10} {e['id']} {e['version']}  [{e['licence']}] {'signed' if e['signed'] else 'unsigned'}  {e['title']}")
            return 0
        bad = 0                                                  # verify: every package's hash, size and signature
        for e in repo.index["packages"]:
            try:
                repo.fetch(e)
                v = hub.verdict(e, store)
                print(f"ok    {e['kind']:<10} {e['id']} {e['version']}  {v['status']}")
            except hub.HubError as ex:
                bad += 1
                print(f"FAIL  {e['kind']:<10} {e['id']} {e['version']}  {ex}")
        return 1 if bad else 0
    except hub.HubError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _plugin_cmd(args, settings) -> int:
    import os
    from pathlib import Path

    from astral import signing
    from astral.plugins import registry, trust

    store = trust.TrustStore(settings.data_dir / "trusted-keys")
    try:
        if args.action == "trust":
            if not args.name:
                print("trust needs --name", file=sys.stderr)
                return 2
            fp = store.add(Path(args.target).read_bytes(), args.name)
            print(f"trusted '{args.name}' (fingerprint {fp}) in {store.dir}")
            print("Confirm this fingerprint with the publisher by a channel other than the one you got the key from.")
            return 0
        d = Path(args.target)
        m = registry.load_manifest(d)
        if args.action == "sign":
            if not args.key:
                print("sign needs --key", file=sys.stderr)
                return 2
            rec = trust.sign_package(d, m.id, m.version, Path(args.key).read_bytes(), os.environ.get("ASTRAL_SIGNING_PASSPHRASE") or None)
            print(f"signed {m.id}@{m.version}: {len(rec['files'])} files, key {rec['key_fingerprint']}")
            return 0
        v = trust.verify_package(d, m.id, m.version, store)
        print(f"{m.id}@{m.version}: {v.status}" + (f" — {v.detail}" if v.detail else ""))
        return 0 if v.status in ("trusted", "signed-untrusted") else 1
    except (trust.TrustError, signing.SigningError, registry.PluginError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _exhibit_cmd(args, settings) -> int:
    import os
    import uuid
    from pathlib import Path

    from astral import exhibit, exhibit_verify
    from astral.evidence import EvidenceStore

    if args.action == "verify":
        pub = Path(args.public_key).read_bytes() if args.public_key else None
        rep = exhibit_verify.verify(args.target, pub)
        for c in rep["checks"]:
            print(f"[{'ok' if c['ok'] else 'FAIL'}] {c['check']}" + (f" — {c['detail']}" if c["detail"] else ""))
        print()
        print("ALL CHECKS PASSED" if rep["ok"] else "VERIFICATION FAILED")
        return 0 if rep["ok"] else 1
    if not (args.out and args.statement_file):
        print("export needs --out and --statement-file (the operator's own account, which the platform cannot write)", file=sys.stderr)
        return 2
    key = None
    if args.sign:
        if not settings.signing_key:
            print("set ASTRAL_SIGNING_KEY to the private key to sign with (astral keygen)", file=sys.stderr)
            return 2
        key = (Path(settings.signing_key).read_bytes(), os.environ.get("ASTRAL_SIGNING_PASSPHRASE") or None)
    with Database(settings.dsn) as db:
        actor = db.bootstrap_solo()
        store = EvidenceStore(settings.data_dir / "evidence")
        with db.connection() as conn:
            try:
                s = exhibit.build(conn, store, actor, uuid.UUID(args.target), args.out,
                                  operator_statement=Path(args.statement_file).read_text(encoding="utf-8"),
                                  plan_ids=[uuid.UUID(x) for x in args.plan], include_all_artifacts=args.all_artifacts,
                                  signing_key=key)
            except exhibit.ExhibitError as e:
                print(f"error: {e}", file=sys.stderr)
                for p in e.problems:
                    print(f"  - {p}", file=sys.stderr)
                return 1
    print(json.dumps(s, indent=2))
    return 0


def _secrets_cmd(args, settings) -> int:
    import getpass

    from astral.secrets import SecretStore, SecretsError, passphrase_from_environment

    pw = passphrase_from_environment()
    if not pw:
        print("set ASTRAL_SECRETS_PASSPHRASE first (it is never written to disk)", file=sys.stderr)
        return 2
    try:
        st = SecretStore(settings.data_dir / "secrets.json", pw)
        if args.action == "list":
            print("\n".join(st.names()) or "(no secrets)")
        elif args.action == "set":
            st.set(args.name, getpass.getpass(f"value for {args.name}: "))
        elif args.action == "delete":
            print("deleted" if st.delete(args.name) else "no such secret")
        elif args.action == "rotate":
            st.rotate(getpass.getpass("new passphrase: "))
            print("re-encrypted; update ASTRAL_SECRETS_PASSPHRASE")
    except SecretsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
