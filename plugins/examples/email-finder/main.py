"""Reference plugin: find e-mail addresses in text.

Demonstrates the whole contract in ~40 lines: it receives one JSON request,
emits assertions with evidence, and never imports core objects or model
libraries. Entity ids are minted deterministically from the case-scoped
namespace the host provides, so a rebuild or re-run yields the same ids.
"""

import re
import uuid

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _eid(namespace, key):
    return str(uuid.uuid5(uuid.UUID(namespace), key))


def invoke(request):
    call_id = request["call_id"]
    inp, cfg = request["input"], request["config"]
    ns = request["context"]["entity_namespace"]
    artifact, text = inp["artifact"], inp["text"]

    doc = _eid(ns, "document:" + artifact)
    found = []
    for m in EMAIL.finditer(text):
        addr = m.group(0).lower() if cfg["lowercase"] else m.group(0)
        if addr not in found:
            found.append(addr)
        if len(found) >= cfg["max_results"]:
            break

    yield {"op": "log", "level": "info", "msg": f"found {len(found)} address(es)"}
    assertions = [{
        "subject": doc, "predicate": "schema", "object_value": "Document",
        "evidence": [artifact], "info_grade": 2,
    }]
    for addr in found:
        mail = _eid(ns, "email:" + addr)
        assertions.append({
            "subject": mail, "predicate": "schema", "object_value": "EmailAddress",
            "evidence": [artifact], "info_grade": 2,
        })
        assertions.append({
            "subject": mail, "predicate": "name", "object_value": addr,
            "evidence": [artifact], "info_grade": 2,
        })
        assertions.append({
            "subject": doc, "predicate": "mentions", "object_entity": mail,
            "evidence": [artifact], "info_grade": 2,
        })
    yield {"op": "emit", "call_id": call_id, "assertions": assertions}
    yield {"op": "result", "call_id": call_id, "status": "ok", "stats": {"addresses": len(found)}}


def selfcheck():
    """Known-present and known-absent fixture, Maigret-style (04 §5)."""
    present = EMAIL.search("write to a.b@example.org") is not None
    absent = EMAIL.search("no address here") is None
    return {"ok": present and absent}
