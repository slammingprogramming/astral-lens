"""Read models for the workspace UI: graph, timeline, documents, coverage.

All of these read the PROJECTIONS (layers 2-3) and the assertion log; none
writes anything. Every graph edge carries the ids of the assertions behind it,
so "click an edge, reach its source record" is a query, not a feature.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg

from astral.evidence import EvidenceStore
from astral.db import Actor

# relationship-as-entity schemata (FollowTheMoney): (from-property, to-property)
RELATIONSHIPS: dict[str, tuple[str, str]] = {
    "Ownership": ("owner", "asset"), "Directorship": ("director", "organization"),
    "Membership": ("member", "organization"), "Family": ("person", "relative"),
    "Associate": ("person", "associate"), "Representation": ("agent", "client"),
    "Employment": ("employee", "employer"), "Payment": ("payer", "beneficiary"),
}
EDGE_LABEL = {"Ownership": "owns", "Directorship": "director of", "Membership": "member of",
              "Family": "family of", "Associate": "associated with",
              "Representation": "represents", "Employment": "employed by", "Payment": "paid"}
REL_PROPS = ("role", "startDate", "endDate", "percentage", "status", "amount", "currency")
DATE_PREDICATES = {
    "birthDate": "born", "incorporationDate": "incorporated", "dissolutionDate": "dissolved",
    "startDate": "starts", "endDate": "ends", "capturedAt": "captured", "snapshotTimestamp": "archived",
    "deathDate": "died", "registrationDate": "registered",
    # what a media file CLAIMS about when it was made (graded as a claim, not as the event's date)
    "dateCreated": "recorded (file claims)",
}
_GRADES = "ABCDEF"


def _weakest(rows: list[dict[str, Any]]) -> tuple[str, int]:
    return max((r["source_grade"] for r in rows), default="F"), max((r["info_grade"] for r in rows), default=6)


def build_geo(conn: psycopg.Connection, case_id: uuid.UUID) -> dict[str, Any]:
    """Entities that carry a latitude and longitude, for the map. Positions are only what sources asserted:
    a coordinate that is out of range, or that arrives without its partner, is listed as unplaced rather
    than drawn somewhere wrong."""
    caps = {r["cluster_id"]: (r["schema_name"], r["caption"]) for r in conn.execute(
        "SELECT cluster_id, schema_name, caption FROM proj_entity WHERE case_id=%s", (case_id,))}
    pos: dict[uuid.UUID, dict[str, Any]] = {}
    for r in conn.execute("SELECT cluster_id, predicate, value, source_grade, info_grade FROM proj_property WHERE case_id=%s"
                          " AND predicate IN ('latitude','longitude','date') ORDER BY info_grade, source_grade, assertion_id", (case_id,)):
        slot = pos.setdefault(r["cluster_id"], {})
        if r["predicate"] not in slot:
            slot[r["predicate"]] = r["value"]
            slot["grade"] = slot.get("grade") or f"{r['source_grade']}{r['info_grade']}"
    points, unplaced = [], []
    for cid, p in pos.items():
        if "latitude" not in p and "longitude" not in p:
            continue
        try:
            la, lo = float(p["latitude"]), float(p["longitude"])
        except (KeyError, TypeError, ValueError):
            unplaced.append(str(cid))
            continue
        if not (-90 <= la <= 90 and -180 <= lo <= 180):
            unplaced.append(str(cid))
            continue
        schema, caption = caps.get(cid, (None, None))
        points.append({"id": str(cid), "schema": schema, "caption": caption, "lat": la, "lon": lo,
                       "date": p.get("date"), "grade": p.get("grade")})
    return {"points": sorted(points, key=lambda x: x["id"]), "unplaced": sorted(unplaced),
            "note": "positions are what sources reported; an entity with no coordinates is not shown, which says nothing about where it is"}


def build_graph(conn: psycopg.Connection, case_id: uuid.UUID, *, include_documents: bool = True) -> dict[str, Any]:
    ents = {r["cluster_id"]: r for r in conn.execute(
        "SELECT cluster_id, schema_name, caption, member_count FROM proj_entity WHERE case_id=%s", (case_id,))}
    edges = conn.execute(
        "SELECT subject_cluster, predicate, object_cluster, assertion_id, source_grade, info_grade,"
        " independent_sources FROM proj_edge WHERE case_id=%s", (case_id,)).fetchall()
    props = conn.execute(
        "SELECT cluster_id, predicate, value, assertion_id, source_grade, info_grade FROM proj_property"
        " WHERE case_id=%s", (case_id,)).fetchall()
    by_ent: dict[uuid.UUID, list[dict]] = {}
    for p in props:
        by_ent.setdefault(p["cluster_id"], []).append(p)

    rel_ids = {c for c, e in ents.items() if e["schema_name"] in RELATIONSHIPS}
    out_edges: list[dict[str, Any]] = []
    out_of: dict[uuid.UUID, list[dict]] = {}
    for e in edges:
        out_of.setdefault(e["subject_cluster"], []).append(e)

    for rid in sorted(rel_ids, key=str):
        schema = ents[rid]["schema_name"]
        a_prop, b_prop = RELATIONSHIPS[schema]
        a = [e for e in out_of.get(rid, []) if e["predicate"] == a_prop]
        b = [e for e in out_of.get(rid, []) if e["predicate"] == b_prop]
        if not a or not b:
            continue                                         # half a relationship is not drawn as one
        rel_props = [p for p in by_ent.get(rid, []) if p["predicate"] in REL_PROPS or p["predicate"] == "schema"]
        for ea in a:
            for eb in b:
                backing = [ea, eb] + rel_props
                sg, ig = _weakest(backing)
                out_edges.append({
                    "id": f"{rid}:{ea['assertion_id']}:{eb['assertion_id']}",
                    "source": str(ea["object_cluster"]), "target": str(eb["object_cluster"]),
                    "kind": schema, "label": EDGE_LABEL[schema], "via": str(rid),
                    "assertions": sorted({x["assertion_id"] for x in backing}),
                    "source_grade": sg, "info_grade": ig,
                    "independent_sources": min(ea["independent_sources"], eb["independent_sources"]),
                    "properties": {p["predicate"]: p["value"] for p in rel_props if p["predicate"] != "schema"},
                })
    for e in edges:
        if e["subject_cluster"] in rel_ids:
            continue
        if not include_documents and ents.get(e["subject_cluster"], {}).get("schema_name") == "Document":
            continue
        out_edges.append({
            "id": f"e{e['assertion_id']}", "source": str(e["subject_cluster"]),
            "target": str(e["object_cluster"]), "kind": "link", "label": e["predicate"], "via": None,
            "assertions": [e["assertion_id"]], "source_grade": e["source_grade"],
            "info_grade": e["info_grade"], "independent_sources": e["independent_sources"],
            "properties": {},
        })
    degree: dict[str, int] = {}
    for e in out_edges:
        for end in (e["source"], e["target"]):
            degree[end] = degree.get(end, 0) + 1
    nodes = []
    for cid, e in sorted(ents.items(), key=lambda kv: str(kv[0])):
        if cid in rel_ids:
            continue
        if not include_documents and e["schema_name"] == "Document":
            continue
        listed = sorted({str(p["value"]) for p in by_ent.get(cid, []) if p["predicate"] == "listedIn"})
        nodes.append({"id": str(cid), "schema": e["schema_name"], "caption": e["caption"],
                      "members": e["member_count"], "degree": degree.get(str(cid), 0),
                      "listed_in": listed})
    present = {n["id"] for n in nodes}
    out_edges = [e for e in out_edges if e["source"] in present and e["target"] in present]
    return {"nodes": nodes, "edges": out_edges}


def build_timeline(conn: psycopg.Connection, case_id: uuid.UUID) -> list[dict[str, Any]]:
    """Dated facts across the case, one event per assertion. Partial dates keep
    their precision ('1970' is a year, not January 1st)."""
    ents = {r["cluster_id"]: r for r in conn.execute(
        "SELECT cluster_id, schema_name, caption FROM proj_entity WHERE case_id=%s", (case_id,))}
    edges = {}
    for e in conn.execute("SELECT subject_cluster, predicate, object_cluster FROM proj_edge WHERE case_id=%s",
                          (case_id,)):
        edges.setdefault(e["subject_cluster"], []).append(e)
    events = []
    for p in conn.execute(
            "SELECT cluster_id, predicate, value, assertion_id, source_grade, info_grade FROM proj_property"
            " WHERE case_id=%s AND predicate = ANY(%s)", (case_id, list(DATE_PREDICATES))):
        v = str(p["value"])
        if not v[:4].isdigit():
            continue
        subj = ents.get(p["cluster_id"], {})
        label, about = subj.get("caption"), [str(p["cluster_id"])]
        if subj.get("schema_name") in RELATIONSHIPS:
            a_prop, b_prop = RELATIONSHIPS[subj["schema_name"]]
            ends = [e for e in edges.get(p["cluster_id"], []) if e["predicate"] in (a_prop, b_prop)]
            names = [ents.get(e["object_cluster"], {}).get("caption") or "?" for e in ends]
            label = f"{' → '.join(names)} ({EDGE_LABEL[subj['schema_name']]})"
            about = [str(e["object_cluster"]) for e in ends]
        events.append({"date": v, "precision": {4: "year", 7: "month", 10: "day"}.get(len(v), "time"),
                       "what": DATE_PREDICATES[p["predicate"]], "label": label, "entities": about,
                       "assertion_id": p["assertion_id"], "source_grade": p["source_grade"],
                       "info_grade": p["info_grade"]})
    events.sort(key=lambda e: (e["date"], e["assertion_id"]))
    return events


def list_documents(conn: psycopg.Connection, case_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT e.cluster_id, e.caption, "
        " (SELECT value #>> '{}' FROM proj_property p WHERE p.case_id=e.case_id AND p.cluster_id=e.cluster_id"
        "  AND p.predicate='hasText' ORDER BY assertion_id LIMIT 1) AS text_sha,"
        " (SELECT value #>> '{}' FROM proj_property p WHERE p.case_id=e.case_id AND p.cluster_id=e.cluster_id"
        "  AND p.predicate='title' ORDER BY assertion_id LIMIT 1) AS title,"
        " (SELECT value #>> '{}' FROM proj_property p WHERE p.case_id=e.case_id AND p.cluster_id=e.cluster_id"
        "  AND p.predicate='mimeType' ORDER BY assertion_id LIMIT 1) AS mime,"
        " (SELECT value #>> '{}' FROM proj_property p WHERE p.case_id=e.case_id AND p.cluster_id=e.cluster_id"
        "  AND p.predicate='contentStatus' ORDER BY assertion_id LIMIT 1) AS status,"
        " (SELECT count(*) FROM proj_edge x WHERE x.case_id=e.case_id AND x.subject_cluster=e.cluster_id"
        "  AND x.predicate='mentions') AS mentions"
        " FROM proj_entity e WHERE e.case_id=%s AND e.schema_name='Document' ORDER BY e.caption NULLS LAST,"
        " e.cluster_id", (case_id,)).fetchall()
    return [{**r, "cluster_id": str(r["cluster_id"])} for r in rows]


def document_view(conn: psycopg.Connection, store: EvidenceStore, actor: Actor, case_id: uuid.UUID,
                  text_sha: str) -> dict[str, Any]:
    """The extracted text plus every mention with its exact span and entity."""
    text = store.read(conn, actor, case_id, bytes.fromhex(text_sha)).decode("utf-8", "replace")
    segments = _segments_for_text(conn, store, actor, case_id, text_sha)
    member = {r["entity_id"]: r["cluster_id"] for r in conn.execute(
        "SELECT entity_id, cluster_id FROM proj_entity_member WHERE case_id=%s", (case_id,))}
    caption = {r["cluster_id"]: (r["caption"], r["schema_name"]) for r in conn.execute(
        "SELECT cluster_id, caption, schema_name FROM proj_entity WHERE case_id=%s", (case_id,))}
    rows = conn.execute(
        "SELECT a.id, a.object_entity, a.source_grade, a.info_grade, a.derived_by FROM assertion a"
        " LEFT JOIN assertion_retraction r ON r.assertion_id = a.id"
        " WHERE a.case_id=%s AND a.predicate='mentions' AND r.id IS NULL"
        " AND a.derived_by -> 'params' ->> 'text_artifact' = %s ORDER BY a.id", (case_id, text_sha)).fetchall()
    mentions = []
    for r in rows:
        p = r["derived_by"]["params"]
        c = member.get(r["object_entity"])
        cap, schema = caption.get(c, (None, None))
        seg = _time_at(segments, p["span"][0])
        mentions.append({"t_ms": seg["start_ms"] if seg else None,
            "assertion_id": r["id"], "start": p["span"][0], "end": p["span"][1], "text": p["text"],
            "label": p.get("label"), "score": p.get("score"), "validated": p.get("validated"),
            "entity": str(c) if c else None, "entity_caption": cap, "schema": schema,
            "source_grade": r["source_grade"], "info_grade": r["info_grade"],
            "backend": r["derived_by"].get("backend"), "degraded": r["derived_by"].get("degraded")})
    return {"text": text, "mentions": mentions}


MEDIA_SCHEMAS = ("Video", "Audio", "Image")


def _segments_for_text(conn: psycopg.Connection, store: EvidenceStore, actor: Actor, case_id: uuid.UUID,
                       text_sha: str) -> dict[str, Any] | None:
    """The time index of a transcript, if this text came from speech."""
    row = conn.execute(
        "SELECT s.value #>> '{}' AS sha FROM proj_property s JOIN proj_property t"
        " ON t.case_id=s.case_id AND t.cluster_id=s.cluster_id AND t.predicate='hasText'"
        " WHERE s.case_id=%s AND s.predicate='hasSegments' AND t.value #>> '{}' = %s LIMIT 1",
        (case_id, text_sha)).fetchone()
    if not row:
        return None
    return json.loads(store.read(conn, actor, case_id, bytes.fromhex(row["sha"])))


def _time_at(segments: dict[str, Any] | None, offset: int) -> dict[str, Any] | None:
    for s in (segments or {}).get("segments", []):
        if s["char_start"] <= offset < s["char_end"]:
            return s
    return None


def list_media(conn: psycopg.Connection, case_id: uuid.UUID) -> list[dict[str, Any]]:
    ents = conn.execute("SELECT cluster_id, schema_name, caption FROM proj_entity WHERE case_id=%s"
                        " AND schema_name = ANY(%s) ORDER BY caption", (case_id, list(MEDIA_SCHEMAS))).fetchall()
    props: dict[Any, dict[str, Any]] = {}
    for p in conn.execute(
            "SELECT cluster_id, predicate, value FROM proj_property WHERE case_id=%s AND predicate = ANY(%s)"
            " ORDER BY assertion_id", (case_id, ["contentHash", "durationSeconds", "dateCreated", "deviceMake",
                                                  "deviceModel", "contentCredentials", "hasAudio"])):
        props.setdefault(p["cluster_id"], {}).setdefault(p["predicate"], p["value"])
    return [{"cluster_id": str(e["cluster_id"]), "kind": e["schema_name"].lower(), "name": e["caption"],
             "sha256": props.get(e["cluster_id"], {}).get("contentHash"),
             "duration_s": props.get(e["cluster_id"], {}).get("durationSeconds"),
             "date_created": props.get(e["cluster_id"], {}).get("dateCreated"),
             "device": " ".join(x for x in (props.get(e["cluster_id"], {}).get("deviceMake"),
                                            props.get(e["cluster_id"], {}).get("deviceModel")) if x) or None,
             "content_credentials": props.get(e["cluster_id"], {}).get("contentCredentials"),
             "has_audio": props.get(e["cluster_id"], {}).get("hasAudio")} for e in ents]


def media_view(conn: psycopg.Connection, store: EvidenceStore, actor: Actor, case_id: uuid.UUID,
               sha: str) -> dict[str, Any]:
    """One media file: what its metadata claims, its transcript and scenes, and a single
    time-ordered list in which every item carries a citable anchor (file, start_ms, end_ms)."""
    row = conn.execute("SELECT cluster_id FROM proj_property WHERE case_id=%s AND predicate='contentHash'"
                       " AND value #>> '{}' = %s LIMIT 1", (case_id, sha)).fetchone()
    if not row:
        raise LookupError("no media entity for this artifact; ingest it first")
    mc = row["cluster_id"]
    ent = conn.execute("SELECT schema_name, caption FROM proj_entity WHERE case_id=%s AND cluster_id=%s",
                       (case_id, mc)).fetchone()
    props = conn.execute(
        "SELECT p.predicate, p.value, p.assertion_id, p.source_grade, p.info_grade,"
        " (a.derived_by -> 'params' ->> 'claimed_by_file') AS claimed FROM proj_property p"
        " JOIN assertion a ON a.id = p.assertion_id WHERE p.case_id=%s AND p.cluster_id=%s ORDER BY p.assertion_id",
        (case_id, mc)).fetchall()

    def linked(pred: str) -> list[Any]:
        return [r["subject_cluster"] for r in conn.execute(
            "SELECT subject_cluster FROM proj_edge WHERE case_id=%s AND object_cluster=%s AND predicate=%s",
            (case_id, mc, pred))]

    def value(cluster: Any, pred: str) -> Any:
        r = conn.execute("SELECT value FROM proj_property WHERE case_id=%s AND cluster_id=%s AND predicate=%s"
                         " ORDER BY assertion_id LIMIT 1", (case_id, cluster, pred)).fetchone()
        return r["value"] if r else None

    transcript, timeline, mentions = None, [], []
    for d in linked("textOf"):
        text_sha, seg_sha = value(d, "hasText"), value(d, "hasSegments")
        if not text_sha:
            continue
        doc = document_view(conn, store, actor, case_id, text_sha)
        segs = json.loads(store.read(conn, actor, case_id, bytes.fromhex(seg_sha))) if seg_sha else None
        translations = [{"text_sha": value(t, "hasText"), "language": value(t, "language")}
                        for t in [r["subject_cluster"] for r in conn.execute(
                            "SELECT subject_cluster FROM proj_edge WHERE case_id=%s AND object_cluster=%s"
                            " AND predicate='translationOf'", (case_id, d))]]
        transcript = {"cluster_id": str(d), "text_sha": text_sha, "language": value(d, "language"),
                      "segments": (segs or {}).get("segments", []), "translations": translations}
        for s in transcript["segments"]:
            timeline.append({"t_ms": s["start_ms"], "end_ms": s["end_ms"], "kind": "speech",
                             "speaker": s.get("speaker"), "text": s["text"],
                             "anchor": {"artifact": sha, "start_ms": s["start_ms"], "end_ms": s["end_ms"]}})
        for m in doc["mentions"]:
            s = _time_at(segs, m["start"])
            mentions.append({**m, "t_ms": s["start_ms"] if s else None})
            if s:
                timeline.append({"t_ms": s["start_ms"], "end_ms": s["end_ms"], "kind": "mention", "text": m["text"],
                                 "label": m["label"], "entity": m["entity"], "entity_caption": m["entity_caption"],
                                 "anchor": {"artifact": sha, "start_ms": s["start_ms"], "end_ms": s["end_ms"],
                                            "text_artifact": text_sha, "span": [m["start"], m["end"]]}})
    scenes = []
    for sc in linked("sceneOf"):
        scenes.append({"cluster_id": str(sc), "name": conn.execute(
            "SELECT caption FROM proj_entity WHERE case_id=%s AND cluster_id=%s", (case_id, sc)).fetchone()["caption"],
            "start_ms": value(sc, "startMs"), "end_ms": value(sc, "endMs"),
            "keyframe": value(sc, "keyframe"), "ocr_text": value(sc, "ocrText")})
    scenes.sort(key=lambda s: s["start_ms"] or 0)
    for s in scenes:
        timeline.append({"t_ms": s["start_ms"], "end_ms": s["end_ms"], "kind": "scene", "text": s["name"],
                         "keyframe": s["keyframe"], "ocr_text": s["ocr_text"],
                         "anchor": {"artifact": sha, "start_ms": s["start_ms"], "end_ms": s["end_ms"]}})
    order = {"scene": 0, "speech": 1, "mention": 2}
    timeline.sort(key=lambda e: (e["t_ms"] or 0, order[e["kind"]]))
    return {"sha256": sha, "cluster_id": str(mc), "kind": ent["schema_name"].lower(), "name": ent["caption"],
            "properties": [{**p, "claimed_by_file": p["claimed"] == "true"} for p in props],
            "transcript": transcript, "scenes": scenes, "mentions": mentions, "timeline": timeline}


def coverage_ledger(conn: psycopg.Connection, case_id: uuid.UUID, run_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT run_id, ran_at, plugin_id, plugin_version, query, status, coverage, gaps, assertion_count"
        " FROM search_run WHERE case_id=%s AND (%s::uuid IS NULL OR run_id=%s) ORDER BY id DESC LIMIT 500",
        (case_id, run_id, run_id)).fetchall()
    return [{**r, "run_id": str(r["run_id"])} for r in rows]
