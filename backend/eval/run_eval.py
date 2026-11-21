"""Evaluation of the AI layer's text pipeline. Run:  python eval/run_eval.py [n_docs] [seed]

WHAT THIS IS. A seeded, SYNTHETIC corpus with gold labels, run through the real HTTP
API: ingestion, entity extraction, retrieval, planning, extractive answering, citation
verification. It reports
  * extraction precision/recall/F1 per label (the rules backend; no model installed),
  * answer recall and WRONG-answer rate for two question types,
  * how many injected bad claims (fabricated identifier, invented quote, unknown
    evidence id) citation checking rejects,
  * latency per question.
WHAT IT IS NOT. Documents come from templates, so every number here is friendlier than
real text would give. It is a regression baseline for the model-free path. It says
NOTHING about a language model: none is installed, so planning, composing and the model
verifiers were not exercised. Treat these as floor numbers for the plumbing, not as a
claim about answer quality.
"""

from __future__ import annotations

import os
import random
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path

import psycopg
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "tests"))

from astral import citations  # noqa: E402
from astral.api import create_app  # noqa: E402
from astral.config import Settings  # noqa: E402
from astral.db import Database  # noqa: E402

from conftest import ADMIN_DSN  # noqa: E402

STEMS = ["Acme", "Borealis", "Cobalt", "Dunmore", "Ember", "Fenwick", "Garnet", "Halcyon", "Ironbark", "Juniper",
         "Kestrel", "Lumen", "Marlow", "Nimbus", "Orchard", "Pinnacle", "Quarry", "Redwood", "Solace", "Tidewater"]
KINDS = ["Holdings Ltd", "Trading LLC", "Capital GmbH", "Logistics Inc", "Partners LLP"]
FIRST = ["Jane", "Marta", "Omar", "Priya", "Lucas", "Ingrid", "Tomas", "Sofia", "Diego", "Hana"]
LAST = ["Doe", "Novak", "Haddad", "Rao", "Meyer", "Larsen", "Kral", "Costa", "Ruiz", "Sato"]


def make_iban(rng: random.Random) -> str:
    bban = "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(4)) + "".join(rng.choice("0123456789") for _ in range(14))
    num = "".join(str(int(c, 36)) for c in bban + "GB00")
    return f"GB{98 - int(num) % 97:02d}{bban}"


def corpus(n: int, seed: int):
    rng = random.Random(seed)
    orgs = [f"{rng.choice(STEMS)}{i} {rng.choice(KINDS)}" for i in range(n)]
    docs = []
    for i, org in enumerate(orgs):
        person, iban = f"{rng.choice(FIRST)} {rng.choice(LAST)}", make_iban(rng)
        other = orgs[(i + 1) % n]
        docs.append({"org": org, "person": person, "iban": iban, "email": f"{person.split()[0].lower()}.{person.split()[1].lower()}@{org.split()[0].lower()}.example",
                     "text": (f"{org} was registered in {rng.choice(['London', 'Dublin', 'Zurich', 'Lisbon'])} in {rng.randint(1998, 2022)}. "
                              f"Its director is {person}. Payments for {org} are made to IBAN {iban}.\n"
                              f"Correspondence goes to {person.split()[0].lower()}.{person.split()[1].lower()}@{org.split()[0].lower()}.example.\n"
                              f"The firm occasionally trades with {other}, a separate company, which banks elsewhere.\n"
                              f"Quarterly volume was {rng.randint(10_000, 99_999)} units across {rng.randint(2, 9)} sites.")})
    return docs


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return round(p, 3), round(r, 3), round(2 * p * r / (p + r), 3) if p + r else 0.0


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    llm_url = os.environ.get("ASTRAL_EVAL_LLM") or None          # e.g. http://127.0.0.1:58080 (a local llama.cpp server)
    llm_model = os.environ.get("ASTRAL_EVAL_LLM_MODEL", "local")
    docs = corpus(n, seed)
    name = f"astral_eval_{uuid.uuid4().hex[:6]}"
    admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    admin.execute(f'CREATE DATABASE "{name}"')
    dsn = ADMIN_DSN.rsplit("/", 1)[0] + "/" + name
    db = Database(dsn).open()
    db.migrate()
    db.bootstrap_solo()
    try:
        app = create_app(Settings(dsn=dsn, data_dir=Path(tempfile.mkdtemp(prefix="astral-eval-")), llm_url=llm_url, llm_model=llm_model), db=db)
        with TestClient(app, headers={"x-astral-client": "1"}) as c:
            for p in ("doc-ingest", "ner"):
                r = c.post("/plugins", json={"path": str(ROOT / "plugins" / "core" / p)}).json()
                c.post(f"/plugins/{r['id']}/{r['version']}/enabled")
            case = c.post("/cases", json={"name": "eval"}).json()["id"]
            t = time.perf_counter()
            for d in docs:
                sha = c.post(f"/cases/{case}/artifacts", content=d["text"].encode(), headers={"content-type": "text/plain"}).json()["sha256"]
                d["sha"] = sha
                d["text_sha"] = c.post(f"/cases/{case}/artifacts/{sha}/ingest").json()["parse"]["stats"]["text_artifact"]
            ingest_s = time.perf_counter() - t

            # ---- extraction: what the rules backend found vs what was planted
            found = {"iban": set(), "email": set(), "organization": set()}
            for dv in docs:
                view = c.get(f"/cases/{case}/documents/{dv['text_sha']}").json()
                for m in view["mentions"]:
                    if m["label"] in found:
                        found[m["label"]].add(m["text"].strip().rstrip("."))
            gold = {"iban": {d["iban"] for d in docs}, "email": {d["email"] for d in docs}, "organization": {d["org"] for d in docs}}
            ext = {}
            for lab in gold:
                tp = len(gold[lab] & found[lab])
                ext[lab] = prf(tp, len(found[lab] - gold[lab]), len(gold[lab] - found[lab]))

            # ---- question answering (extractive, verified)
            qa = {"iban": [0, 0, 0], "director": [0, 0, 0]}                  # answered, wrong, total
            model_stats: dict[str, dict[str, int]] = {}
            lat = []
            for d in docs:
                for kind, q, gold_ans in (("iban", f"Which IBAN receives payments for {d['org']}?", d["iban"]),
                                          ("director", f"Who is the director of {d['org']}?", d["person"])):
                    t = time.perf_counter()
                    p = c.post(f"/cases/{case}/ask", json={"question": q}).json()
                    c.post(f"/cases/{case}/plans/{p['id']}/approve")
                    out = c.post(f"/cases/{case}/plans/{p['id']}/run").json()
                    lat.append(time.perf_counter() - t)
                    texts = " ".join(x["text"] for x in out["claims"])
                    m = model_stats.setdefault(kind, {"questions": 0, "model_composed": 0, "claims_made": 0, "claims_accepted": 0,
                                                       "no_claims": 0, "fell_back": 0, "planner_model": 0})
                    m["questions"] += 1
                    m["model_composed"] += out["composer"].startswith("model")
                    m["fell_back"] += any("model composer failed" in x for x in out["notes"])
                    m["planner_model"] += p.get("planner") not in (None, "rules")
                    m["claims_made"] += len(out["claims"]) + len(out["rejected"])
                    m["claims_accepted"] += len(out["claims"])
                    m["no_claims"] += not (out["claims"] or out["rejected"])
                    qa[kind][2] += 1
                    qa[kind][0] += gold_ans in texts
                    if kind == "iban":                                       # a different company's IBAN offered as the answer
                        others = {o["iban"] for o in docs} - {gold_ans}
                        qa[kind][1] += any(o in texts for o in others)
                    else:
                        others = {o["person"] for o in docs} - {gold_ans}
                        qa[kind][1] += any(o in texts for o in others)

            # ---- adversarial claims: every one of these must be rejected
            ev = {"E1": {"id": "E1", "kind": "passage", "text_sha": docs[0]["sha"], "start": 0, "end": len(docs[0]["text"]),
                         "text": docs[0]["text"]}}
            bad = []
            for d in docs[:20]:
                ev["E1"] = {"id": "E1", "kind": "passage", "text_sha": d["sha"], "start": 0, "end": len(d["text"]), "text": d["text"]}
                real_quote = f"Payments for {d['org']} are made to IBAN {d['iban']}"
                swapped = make_iban(random.Random(len(bad) + 99))
                variants = [{"text": f"Payments go to IBAN {swapped}.", "cites": [{"evidence_id": "E1", "quote": real_quote}]},
                            {"text": f"{d['org']} was founded in 1850.", "cites": [{"evidence_id": "E1", "quote": f"{d['org']} was founded in 1850"}]},
                            {"text": "Payments go to IBAN X.", "cites": [{"evidence_id": "E7", "quote": "X"}]}]
                bad.append(variants[len(bad) % 3])
            with db.connection() as conn:
                from astral.db import SOLO_ACTOR
                rejected = 0
                for cl in bad:
                    v = citations.verify_claim(conn, app.state.store, SOLO_ACTOR, uuid.UUID(case), cl, ev, [citations.LexicalVerifier()])
                    rejected += v["status"] == "rejected"

        print(f"\nSynthetic corpus: {n} documents (seed {seed}); ingest {ingest_s:.1f}s ({ingest_s / n * 1000:.0f} ms/doc)")
        print("\nEntity extraction (rules backend; P / R / F1)")
        for lab, (p, r, f) in ext.items():
            print(f"  {lab:<13} {p:.3f} / {r:.3f} / {f:.3f}")
        print("  person        not extracted: the rules backend has no person recogniser (GLiNER/spaCy not installed)")
        print("\nQuestion answering (extractive; claims are verbatim quotations that passed citation checks)")
        for k, (a, w, t) in qa.items():
            print(f"  {k:<10} answer present {a}/{t} ({a / t:.0%});  a different company's answer offered {w}/{t} ({w / t:.0%})")
        print(f"\nInjected bad claims rejected by citation checking: {rejected}/{len(bad)}")
        lat.sort()
        print(f"Latency per question (plan + approve + run): median {statistics.median(lat) * 1000:.0f} ms, "
              f"p95 {lat[int(len(lat) * 0.95) - 1] * 1000:.0f} ms, max {lat[-1] * 1000:.0f} ms")
        if llm_url:
            used = sum(m["model_composed"] for m in model_stats.values())
            total = sum(m["questions"] for m in model_stats.values())
            print(f"\nLanguage model configured: {llm_url} (model name {llm_model!r}); it composed the answer for {used}/{total} questions"
                  + ("" if used == total else f". THE OTHER {total - used} FELL BACK TO EXTRACTIVE QUOTING, so the figures below are NOT model figures for them"))
            for k, m in model_stats.items():
                made, acc = m["claims_made"], m["claims_accepted"]
                rate = f"{acc / made:.0%}" if made else "n/a"
                print(f"  {k:<10} composed by model {m['model_composed']}/{m['questions']}, plan by model {m['planner_model']}/{m['questions']}, "
                      f"fell back to extractive {m['fell_back']}, produced no claim {m['no_claims']}; "
                      f"claims written {made}, passed citation checking {acc} (citation pass rate {rate})")
            print("  A claim 'passes' only if its quote is a verbatim span of the cited evidence, its identifiers appear in it, and the "
                  "lexical check plus two entailment prompts to the SAME small model all accept it. A pass is still not proof the "
                  "claim is right: see 'a different company's answer offered' above for wrong answers. Which check rejected each "
                  "dropped claim is not broken out here.")
        else:
            print("\nNOT MEASURED: any language model (set ASTRAL_EVAL_LLM to a local llama.cpp server): planning, composing, "
                  "model verifiers, citation pass rate of model-written claims.")
    finally:
        db.close()
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


if __name__ == "__main__":
    main()
