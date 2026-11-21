"""Account enumeration from declarative site packs (doc 04 section 4).

One plugin interprets many packs. A pack is a JSON file saying how to ask one site whether a
username exists and what that site's answers mean. The engine below is the only code; adding a
site adds no code.

The rule that matters: three outcomes, never two. A site is reported

  present  only if the pack's present_when rule matches the response;
  absent   only if the pack's absent_when rule matches the response -- and, if control_probe is on,
           only after the same pack still recognises a known-present account;
  unknown  in every other case: no rule matched, both matched, the response was truncated or not
           text, the site rate-limited or blocked us (403/429/5xx), or the request failed.

The surveyed tools treat "did not look present" as "absent". This one cannot: a pack with no
absent_when rule can never say absent. A username that exists on a site says nothing about whether
it is the same person.

Pack format (JSON; stdlib only, so it runs in any runtime):

  {"site": "github.com", "category": "code", "licence": "AGPL-3.0-or-later", "attribution": "...",
   "username_regex": "^[A-Za-z0-9-]{1,39}$",
   "check": {"url": "https://github.com/{username}", "method": "GET",
             "present_when": {"status": 200}, "absent_when": {"status": 404}},
   "extract": {"name": {"regex": "<title>(.*?)</title>"}},
   "known": {"present": "someone-real", "absent": "no-such-account-zz9"},
   "fixtures": {"present": {"status": 200, "body": "..."}, "absent": {"status": 404, "body": "..."}}}

Conditions take any of: status (int or list), body_contains, body_absent, final_url_contains; all
that are given must hold. Fixtures are required and are run through the same classifier at load
(and by selfcheck): a pack whose own present/absent fixtures do not classify correctly is not used.
This catches an inconsistent pack. It cannot detect that the real site changed; that is what the
control probe is for at run time.
"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
from pathlib import Path

from astral import sdk

BUNDLED = Path(__file__).parent / "packs"
USERNAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
COND_KEYS = {"status", "body_contains", "body_absent", "final_url_contains"}
EXTRACT_PREDICATES = {"name", "summary", "location", "website"}
UNRELIABLE = {429, 500, 502, 503, 504}            # a rule may not read these as an answer about the account
MAX_BODY = 400_000


class PackError(ValueError):
    pass


# ------------------------------------------------------------------ packs
def _cond(c, where):
    if not isinstance(c, dict) or not c or set(c) - COND_KEYS:
        raise PackError(f"{where}: needs one or more of {sorted(COND_KEYS)}")
    st = c.get("status")
    if st is not None:
        codes = st if isinstance(st, list) else [st]
        if not codes or any(not isinstance(x, int) or not 100 <= x <= 599 for x in codes):
            raise PackError(f"{where}: status must be an HTTP code or a list of them")
        if set(codes) & UNRELIABLE:
            raise PackError(f"{where}: a {sorted(set(codes) & UNRELIABLE)} response says nothing about the account")
    for k in ("body_contains", "body_absent", "final_url_contains"):
        if k in c and (not isinstance(c[k], str) or not c[k]):
            raise PackError(f"{where}.{k} must be a non-empty string")


def validate(p):
    if not isinstance(p, dict):
        raise PackError("a pack is a JSON object")
    site = p.get("site")
    if not isinstance(site, str) or not re.fullmatch(r"[a-z0-9.-]{3,100}", site):
        raise PackError("site must be a lowercase host-like name")
    if not isinstance(p.get("licence"), str) or not p["licence"].strip():
        raise PackError("a pack must state its licence (third-party packs are licence-segregated)")
    chk = p.get("check")
    if not isinstance(chk, dict) or "{username}" not in str(chk.get("url", "")) or not str(chk["url"]).startswith(("http://", "https://")):
        raise PackError("check.url must be an http(s) URL containing {username}")
    if chk.get("method", "GET") not in ("GET", "HEAD"):
        raise PackError("check.method must be GET or HEAD")
    _cond(chk.get("present_when"), "check.present_when")
    if chk.get("absent_when") is not None:
        _cond(chk["absent_when"], "check.absent_when")
    if chk.get("method") == "HEAD" and any(k.startswith("body") for c in (chk["present_when"], chk.get("absent_when") or {}) for k in c):
        raise PackError("a HEAD check has no body to test")
    for pred, spec in (p.get("extract") or {}).items():
        if pred not in EXTRACT_PREDICATES or not isinstance(spec, dict) or not isinstance(spec.get("regex"), str):
            raise PackError(f"extract.{pred}: only {sorted(EXTRACT_PREDICATES)} with a regex are allowed")
        try:
            re.compile(spec["regex"])
        except re.error as e:
            raise PackError(f"extract.{pred}: bad regex ({e})") from e
    if p.get("username_regex"):
        try:
            re.compile(p["username_regex"])
        except re.error as e:
            raise PackError(f"username_regex: {e}") from e
    fx = p.get("fixtures") or {}
    if not all(isinstance(fx.get(k), dict) and isinstance(fx[k].get("status"), int) for k in ("present", "absent")):
        raise PackError("fixtures.present and fixtures.absent (status, body) are required: a pack must be able to test itself")
    # the self-test: the pack must recognise its own fixtures, and refuse to guess about a rate limit
    for kind, want in (("present", "present"), ("absent", "absent" if chk.get("absent_when") else "unknown")):
        got, why = classify(p, fx[kind]["status"], fx[kind].get("body"), fx[kind].get("final_url", chk["url"]), truncated=False)
        if got != want:
            raise PackError(f"fixture '{kind}' classified {got!r} ({why}), expected {want!r}")
    got, _ = classify(p, 429, "", chk["url"], truncated=False)
    if got != "unknown":
        raise PackError("a 429 must classify as unknown")
    return p


def load(dirs):
    """([valid packs], [(file, reason)]). A bad pack is excluded and named, never silently dropped."""
    packs, bad, seen = [], [], set()
    for d in dirs:
        for f in sorted(Path(d).glob("*.json")):
            try:
                p = validate(json.loads(f.read_text(encoding="utf-8")))
                if p["site"] in seen:
                    raise PackError(f"duplicate site {p['site']}")
                seen.add(p["site"])
                p["_file"] = str(f)
                packs.append(p)
            except (OSError, ValueError) as e:                 # PackError and JSON errors are ValueErrors
                bad.append((str(f), str(e)))
    return packs, bad


# ------------------------------------------------------------------ classification
def _holds(c, status, body, final_url, truncated):
    """True/False, or None when the response cannot answer (the body is needed and is missing or cut)."""
    if "status" in c:
        codes = c["status"] if isinstance(c["status"], list) else [c["status"]]
        if status not in codes:
            return False
    if "final_url_contains" in c and c["final_url_contains"] not in (final_url or ""):
        return False
    if "body_contains" in c:
        if body is None:
            return None
        if c["body_contains"] not in body:
            return None if truncated else False
    if "body_absent" in c:
        if body is None or truncated:
            return None                      # absence of text in a body we did not fully read proves nothing
        if c["body_absent"] in body:
            return False
    return True


def classify(pack, status, body, final_url, *, truncated):
    """('present'|'absent'|'unknown', reason)."""
    if status in UNRELIABLE or status == 403:
        return "unknown", f"HTTP {status}: rate-limited or blocked, which says nothing about the account"
    chk = pack["check"]
    pres = _holds(chk["present_when"], status, body, final_url, truncated)
    absn = _holds(chk["absent_when"], status, body, final_url, truncated) if chk.get("absent_when") else False
    if pres is None or absn is None:
        return "unknown", "the response body was missing or truncated, so the rule cannot be decided"
    if pres and absn:
        return "unknown", "the present and absent rules both match: the pack is ambiguous"
    if pres:
        return "present", "present_when matched"
    if absn:
        return "absent", "absent_when matched"
    if not chk.get("absent_when"):
        return "unknown", "present_when did not match and this pack has no absent_when rule, so it cannot say absent"
    return "unknown", f"HTTP {status} matched neither rule: the site may have changed"


def _extract(pack, body):
    out = {}
    for pred, spec in (pack.get("extract") or {}).items():
        m = re.search(spec["regex"], body or "", re.S)
        if m:
            v = html.unescape(re.sub(r"<[^>]+>", "", m.group(1) if m.groups() else m.group(0))).strip()
            if v:
                out[pred] = v[:300]
    return out


# ------------------------------------------------------------------ plugin
def _url(pack, username):
    return pack["check"]["url"].replace("{username}", urllib.parse.quote(username, safe=""))


def _ask(pack, username):
    chk = pack["check"]
    res = yield from sdk.fetch(_url(pack, username), method=chk.get("method", "GET"), max_bytes=MAX_BODY)
    return res, classify(pack, res["status"], res.get("text"), res.get("final_url"), truncated=bool(res.get("truncated")))


def invoke(request):
    inp, cfg = request["input"], request["config"]
    ns = request["context"]["entity_namespace"]
    username = str(inp["username"]).strip()
    if not USERNAME.match(username):
        yield sdk.result(status="error", error="username must be 1-64 letters, digits, '.', '_' or '-'")
        return
    dirs = [BUNDLED] + ([Path(cfg["packs_dir"])] if cfg.get("packs_dir") else [])
    packs, bad = load(dirs)
    wanted = set(cfg.get("sites") or [])
    cov = [sdk.coverage(Path(f).name, unknown=f"pack not used: {why}") for f, why in bad]
    if wanted:
        packs = [p for p in packs if p["site"] in wanted]
        cov += [sdk.coverage(s, unknown="no valid pack for this site") for s in sorted(wanted - {p["site"] for p in packs})]
    skipped = max(0, len(packs) - cfg["max_sites"])
    packs = packs[: cfg["max_sites"]]
    if skipped:
        cov.append(sdk.coverage("site-pack limit", unknown=f"{skipped} more site(s) not asked (max_sites={cfg['max_sites']})"))

    out, counts = [], {"present": 0, "absent": 0, "unknown": len(bad)}
    for p in packs:
        if not re.fullmatch(p.get("username_regex") or ".*", username):
            cov.append(sdk.coverage(p["site"], unknown=f"not applicable: '{username}' is not a valid username on this site"))
            counts["unknown"] += 1
            continue
        try:
            res, (verdict, why) = yield from _ask(p, username)
        except sdk.CapabilityFailed as e:
            cov.append(sdk.coverage(p["site"], unknown=str(e)))
            counts["unknown"] += 1
            continue
        url = _url(p, username)
        if verdict == "absent" and cfg["control_probe"]:
            known = (p.get("known") or {}).get("present")
            if not known:
                verdict, why = "unknown", "no known-present control account in the pack, so an absence cannot be trusted"
            else:
                try:
                    _, (cv, cwhy) = yield from _ask(p, known)
                except sdk.CapabilityFailed as e:
                    cv, cwhy = "unknown", str(e)
                if cv != "present":
                    verdict, why = "unknown", f"control check failed ({cwhy}): the site or the pack may have changed"
        counts[verdict] += 1
        if verdict == "present":
            ev = [res["sha256"]]
            acct = sdk.entity_id(ns, "account", f"{p['site']}:{username.lower()}")
            out += [sdk.a_value(acct, "schema", "UserAccount", ev, 3), sdk.a_value(acct, "username", username, ev, 3),
                    sdk.a_value(acct, "service", p["site"], ev, 3), sdk.a_value(acct, "sourceUrl", url, ev, 3)]
            for pred, val in _extract(p, res.get("text")).items():      # self-reported profile text: doubtful by default
                out.append(sdk.a_value(acct, pred, val, ev, 4))
            cov.append(sdk.coverage(p["site"], hits=1, note="an account with this username exists; that does not show it is the same person"))
        elif verdict == "absent":
            cov.append(sdk.coverage(p["site"], hits=0, note=f"{why}; says nothing about other sites or other spellings"))
        else:
            cov.append(sdk.coverage(p["site"], unknown=why))
    if out:
        yield sdk.emit(out)
    yield sdk.result(stats={**counts, "pack_licences": {p["site"]: p["licence"] for p in packs}}, coverage=cov)


def selfcheck():
    """Offline: every bundled pack loads and classifies its own fixtures. Does not touch the network,
    so it cannot see a site changing; the run-time control probe does that."""
    packs, bad = load([BUNDLED])
    if bad or not packs:
        return {"ok": False, "detail": "; ".join(f"{Path(f).name}: {w}" for f, w in bad) or "no bundled packs"}
    return {"ok": True, "detail": f"{len(packs)} bundled pack(s) consistent with their fixtures (offline)"}
