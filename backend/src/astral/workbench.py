"""The analyst workbench: the Maltego-style loop of select an entity, run a connector on it, watch the
graph grow, annotate, analyse, export.

Nothing here is a new source of truth. A "transform" is an ordinary plugin invocation: it writes
assertions with evidence through the same writer, so everything it adds is graded, attributed and
retractable like any other result. Annotations, saved views and machine runs are analyst workspace
data in their own tables; the projection never reads them and they are never evidence.

Three rules carry over from the rest of the system:
  * an input derived from an entity is a GUESS that the entity's value is what the connector should be
    asked about (a company's name can match the wrong registry record), so results arrive unmerged and
    the resolver, not this module, proposes identity;
  * a connector that cannot be asked, or is missing configuration, is "unknown", never "no results";
  * a target_contact connector never runs without approval at the moment of the call.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any
from xml.sax.saxutils import escape as xml_escape, quoteattr

import psycopg

from astral import audit, investigate, projector, views
from astral.assertions import AssertionDraft, AssertionWriter, DerivedBy
from astral.db import Actor
from astral.evidence import EvidenceStore
from astral.plugins.host import PluginHost, RateLimitExceeded, entity_namespace
from astral.plugins import registry
from astral.plugins.manifest import PluginManifest
from astral.policy import ApprovalRequired

# Predicates that supply each connector input. The first value (best grade first) is offered; the
# others are returned as alternatives so the analyst can choose.
FIELD_PREDICATES: dict[str, tuple[str, ...]] = {
    "country": ("jurisdiction", "country", "nationality"),
    "url": ("sourceUrl", "website", "url", "archiveUrl"),
    "username": ("username",),
    "address": ("publicKey",),
    "lat": ("latitude",), "lon": ("longitude",),
}
# Entities for which "the caption is a name to search registries for" would be nonsense.
NOT_A_NAME = {"CryptoWallet", "UserAccount", "Url", "WebPage", "WebSnapshot", "Payment", "Address", "Document",
              "EmailAddress", "PhoneNumber", "BankAccount", "Image", "Video", "Audio", "Event", "Directorship",
              "Ownership", "Sanction", "Face"}
ANNOTATION_KINDS = ("note", "pin", "colour", "label", "group")
MAX_IMPORT_ROWS = 5000
MAX_CELL = 500


class WorkbenchError(ValueError):
    pass


class NotFound(LookupError):
    """The thing asked about does not exist in this case (reported as 404, never as a server fault)."""


# ------------------------------------------------------------------ transforms
def entity_inputs(conn: psycopg.Connection, case_id: uuid.UUID, cluster_id: uuid.UUID) -> dict[str, Any]:
    ent = conn.execute("SELECT schema_name, caption FROM proj_entity WHERE case_id=%s AND cluster_id=%s",
                       (case_id, cluster_id)).fetchone()
    if not ent:
        raise NotFound("no such entity")
    props: dict[str, list[Any]] = {}
    for r in conn.execute("SELECT predicate, value FROM proj_property WHERE case_id=%s AND cluster_id=%s"
                          " ORDER BY info_grade, source_grade, assertion_id", (case_id, cluster_id)):
        props.setdefault(r["predicate"], []).append(r["value"])
    values: dict[str, list[Any]] = {}
    if ent["schema_name"] not in NOT_A_NAME:
        names = [v for v in props.get("name", []) if isinstance(v, str)]
        if ent["caption"] and ent["caption"] not in names:
            names.insert(0, ent["caption"])
        if names:
            values["name"] = names
    for field, preds in FIELD_PREDICATES.items():
        vs = [v for p in preds for v in props.get(p, []) if isinstance(v, (str, int, float)) and str(v).strip()]
        if vs:
            values[field] = vs
    inputs = {k: v[0] for k, v in values.items()}
    alternatives = {k: [x for x in dict.fromkeys(v) if x != inputs[k]] for k, v in values.items() if len(set(v)) > 1}
    return {"schema": ent["schema_name"], "caption": ent["caption"], "inputs": inputs, "alternatives": alternatives}


def _query_fields(m: PluginManifest) -> list[str]:
    return [f for c in m.consumes if c.type == "Query" for f in c.required]


def transforms_for(conn: psycopg.Connection, case_id: uuid.UUID, cluster_id: uuid.UUID) -> dict[str, Any]:
    ent = entity_inputs(conn, case_id, cluster_id)
    out = []
    for m, version in investigate.query_plugins(conn):
        need = _query_fields(m)
        missing = [f for f in need if f not in ent["inputs"]]
        out.append({
            "plugin": m.id, "version": version, "title": m.title, "footprint": m.footprint,
            "produces": list(m.produces), "requires": need, "ready": not missing, "missing": missing,
            "needs_approval": m.footprint == "target_contact",
            "inputs": {f: ent["inputs"][f] for f in need if f in ent["inputs"]},
        })
    out.sort(key=lambda t: (not t["ready"], t["footprint"] == "target_contact", t["plugin"]))
    return {"entity": {"id": str(cluster_id), "schema": ent["schema"], "caption": ent["caption"]},
            "available_inputs": ent["inputs"], "alternatives": ent["alternatives"], "transforms": out}


def _snapshot(conn: psycopg.Connection, case_id: uuid.UUID) -> tuple[dict[uuid.UUID, str | None], set[int]]:
    ents = {r["cluster_id"]: r["caption"] for r in conn.execute(
        "SELECT cluster_id, caption FROM proj_entity WHERE case_id=%s", (case_id,))}
    edges = {r["assertion_id"] for r in conn.execute("SELECT assertion_id FROM proj_edge WHERE case_id=%s", (case_id,))}
    return ents, edges


def run_transform(host: PluginHost, conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID, cluster_id: uuid.UUID,
                  plugin_id: str, *, config: dict[str, Any] | None = None, approved: bool = False,
                  overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one connector against one entity and report what appeared. Raises ApprovalRequired for a
    target_contact connector that was not approved; every other failure becomes an 'unknown' result."""
    found = {t["plugin"]: t for t in transforms_for(conn, case_id, cluster_id)["transforms"]}
    if plugin_id not in found:
        raise NotFound(f"{plugin_id} is not an enabled connector")
    t = found[plugin_id]
    inp = {**{f: v for f, v in t["inputs"].items()}, **(overrides or {})}
    missing = [f for f in t["requires"] if f not in inp]
    if missing:
        raise WorkbenchError(f"this entity has no value for: {', '.join(missing)}")
    before_ents, before_edges = _snapshot(conn, case_id)
    note: dict[str, Any]
    try:
        with conn.transaction():
            res = host.invoke(conn, actor, case_id, plugin_id, {k: inp[k] for k in t["requires"]}, version=t["version"],
                              config=config, approved=approved)
        note = {"status": res.status, "coverage": res.coverage, "gaps": res.gaps, "assertions": len(res.assertion_ids),
                "stats": res.stats}
    except ApprovalRequired:
        raise
    except (registry.PluginError, RateLimitExceeded, ValueError) as e:
        note = {"status": "error", "coverage": [{"source": t["title"], "outcome": "unknown", "note": f"{type(e).__name__}: {e}"}],
                "gaps": [str(e)], "assertions": 0, "stats": {}}
    proj = projector.rebuild_case(conn, case_id)
    after_ents, after_edges = _snapshot(conn, case_id)
    new = [{"id": str(c), "caption": after_ents[c]} for c in after_ents if c not in before_ents]
    audit.record(conn, actor, "workbench.transform", case_id=case_id, target_kind="entity", target_id=str(cluster_id),
                 detail={"plugin": plugin_id, "status": note["status"], "new_entities": len(new)})
    return {**note, "plugin": plugin_id, "input": {k: inp[k] for k in t["requires"]}, "new_entities": new,
            "new_edges": len(after_edges - before_edges), "projection": proj,
            "note": "results arrive unmerged: a name match may be a different party until a person decides"}


# ------------------------------------------------------------------ annotations and views
def annotate(conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID, kind: str, body: str,
             cluster_id: uuid.UUID | None = None) -> uuid.UUID:
    if kind not in ANNOTATION_KINDS:
        raise WorkbenchError(f"kind must be one of {ANNOTATION_KINDS}")
    body = body.strip()
    if not body or len(body) > 4000:
        raise WorkbenchError("an annotation is 1-4000 characters")
    if kind == "colour" and not re.fullmatch(r"#[0-9a-fA-F]{6}", body):
        raise WorkbenchError("a colour is #rrggbb")
    if cluster_id and not conn.execute("SELECT 1 FROM proj_entity WHERE case_id=%s AND cluster_id=%s", (case_id, cluster_id)).fetchone():
        raise NotFound("no such entity")
    if kind in ("pin", "colour", "label") and cluster_id:                       # one of each per entity: replace
        conn.execute("DELETE FROM graph_annotation WHERE case_id=%s AND cluster_id=%s AND kind=%s", (case_id, cluster_id, kind))
    aid = uuid.uuid4()
    conn.execute("INSERT INTO graph_annotation (id, workspace_id, case_id, cluster_id, kind, body, created_by)"
                 " VALUES (%s,%s,%s,%s,%s,%s,%s)", (aid, actor.workspace_id, case_id, cluster_id, kind, body, actor.principal_id))
    audit.record(conn, actor, "workbench.annotate", case_id=case_id, target_kind="annotation", target_id=str(aid),
                 detail={"kind": kind})                                           # the text stays out of the audit log
    return aid


def annotations(conn: psycopg.Connection, case_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT id, cluster_id, kind, body, created_by, created_at FROM graph_annotation WHERE case_id=%s"
                        " ORDER BY created_at", (case_id,)).fetchall()
    return [{"id": str(r["id"]), "cluster_id": str(r["cluster_id"]) if r["cluster_id"] else None, "kind": r["kind"],
             "body": r["body"], "created_by": str(r["created_by"]), "created_at": r["created_at"].isoformat(),
             "is_evidence": False} for r in rows]


def remove_annotation(conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID, annotation_id: uuid.UUID) -> None:
    n = conn.execute("DELETE FROM graph_annotation WHERE id=%s AND case_id=%s", (annotation_id, case_id)).rowcount
    if not n:
        raise NotFound("no such annotation")
    audit.record(conn, actor, "workbench.annotation_remove", case_id=case_id, target_kind="annotation", target_id=str(annotation_id))


def save_view(conn: psycopg.Connection, actor: Actor, case_id: uuid.UUID, name: str, layout: str, state: dict[str, Any]) -> uuid.UUID:
    if not 1 <= len(name.strip()) <= 100:
        raise WorkbenchError("a view name is 1-100 characters")
    if len(json.dumps(state)) > 500_000:
        raise WorkbenchError("view state is too large")
    row = conn.execute(
        "INSERT INTO graph_view (id, workspace_id, case_id, name, layout, state, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (case_id, name) DO UPDATE SET layout=EXCLUDED.layout, state=EXCLUDED.state RETURNING id",
        (uuid.uuid4(), actor.workspace_id, case_id, name.strip(), layout, json.dumps(state), actor.principal_id)).fetchone()
    audit.record(conn, actor, "workbench.view_save", case_id=case_id, target_kind="graph_view", target_id=str(row["id"]))
    return row["id"]


def list_views(conn: psycopg.Connection, case_id: uuid.UUID) -> list[dict[str, Any]]:
    return [{"id": str(r["id"]), "name": r["name"], "layout": r["layout"], "state": r["state"], "created_at": r["created_at"].isoformat()}
            for r in conn.execute("SELECT * FROM graph_view WHERE case_id=%s ORDER BY name", (case_id,))]


# ------------------------------------------------------------------ table import
_SCHEMA = re.compile(r"^[A-Z][A-Za-z]{2,40}$")
_PRED = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,40}$")


def _check_mapping(mapping: dict[str, Any], header: list[str]) -> None:
    if not _SCHEMA.match(str(mapping.get("schema", ""))):
        raise WorkbenchError("mapping.schema must be a schema name such as Person or Company")
    cols = set(header)
    key = mapping.get("key")
    if key not in cols:
        raise WorkbenchError(f"mapping.key {key!r} is not a column of the file")
    for col, pred in (mapping.get("columns") or {}).items():
        if col not in cols or not _PRED.match(str(pred)) or pred in ("schema",):
            raise WorkbenchError(f"mapping.columns: {col!r} -> {pred!r} is not usable")
    for ln in mapping.get("links") or []:
        if ln.get("column") not in cols or not _PRED.match(str(ln.get("predicate", ""))) or not _SCHEMA.match(str(ln.get("schema", ""))):
            raise WorkbenchError(f"mapping.links entry is not usable: {ln!r}")
    if not (mapping.get("columns") or mapping.get("links")) and not mapping.get("key"):
        raise WorkbenchError("the mapping produces nothing")


def import_table(conn: psycopg.Connection, store: EvidenceStore, writer: AssertionWriter, actor: Actor, case_id: uuid.UUID,
                 data: bytes, mapping: dict[str, Any], *, filename: str = "table.csv", source_grade: str = "F",
                 info_grade: int = 6) -> dict[str, Any]:
    """Bulk-create entities (and links) from a spreadsheet. The file itself is stored as evidence and every
    assertion cites it. The default grades say what is true of an arbitrary spreadsheet: reliability and
    credibility cannot be judged (F, 6) until the analyst says otherwise."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise WorkbenchError("the file is not UTF-8 text") from e
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    if not header:
        raise WorkbenchError("the file has no header row")
    rows = []
    for i, row in enumerate(reader):
        if i >= MAX_IMPORT_ROWS:
            raise WorkbenchError(f"more than {MAX_IMPORT_ROWS} rows: split the file")
        rows.append({k: (v or "").strip()[:MAX_CELL] for k, v in row.items() if k})
    _check_mapping(mapping, header)

    ing = store.ingest(conn, actor, case_id, data, acquired_by="workbench.table-import", media_type="text/csv",
                       acquired_from=f"upload:{filename}", acquisition_ctx={"rows": len(rows), "columns": header})
    ev = [ing.hex]
    sha = ing.hex
    from astral import cases
    source = cases.get_or_create_source(conn, actor, kind="import", name=f"table:{sha[:16]}", locator=filename,
                                        origin_key=f"import:{sha[:16]}", default_source_grade=source_grade)
    by = DerivedBy(plugin="workbench.table-import", version="1", backend="csv", kind="import",
                   params={"file": filename, "schema": mapping["schema"]})
    ns = entity_namespace(case_id)
    made: set[uuid.UUID] = set()
    written = 0

    def put(subject: uuid.UUID, pred: str, *, value: Any = None, obj: uuid.UUID | None = None) -> None:
        nonlocal written
        writer.write(conn, actor, case_id, AssertionDraft(subject=subject, predicate=pred, object_value=value, object_entity=obj,
                                                         source_id=source, evidence=ev, derived_by=by, info_grade=info_grade,
                                                         source_grade=source_grade))
        written += 1

    def entity(schema: str, key: str, name: str | None) -> uuid.UUID:
        eid = uuid.uuid5(ns, f"import:{schema}:{key.lower()}")
        if eid not in made:
            made.add(eid)
            put(eid, "schema", value=schema)
            if name:
                put(eid, "name", value=name)
        return eid

    skipped = 0
    for row in rows:
        key = row.get(mapping["key"], "")
        if not key:
            skipped += 1
            continue
        subject = entity(mapping["schema"], key, mapping.get("name_column") and row.get(mapping["name_column"]) or key)
        for col, pred in (mapping.get("columns") or {}).items():
            if row.get(col):
                put(subject, pred, value=row[col])
        for ln in mapping.get("links") or []:
            cell = row.get(ln["column"], "")
            if cell:
                put(subject, ln["predicate"], obj=entity(ln["schema"], cell, cell))
    audit.record(conn, actor, "workbench.import", case_id=case_id, target_kind="artifact", target_id=sha,
                 detail={"rows": len(rows), "assertions": written, "skipped": skipped})
    return {"artifact": sha, "rows": len(rows), "skipped_rows_without_key": skipped, "entities": len(made), "assertions": written,
            "source_grade": source_grade, "info_grade": info_grade,
            "note": "grades default to F/6 (cannot be judged); raise them only if you can say why"}


# ------------------------------------------------------------------ export and report
def _csv_safe(v: Any) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s       # spreadsheet formula injection


def to_csv(graph: dict[str, Any]) -> dict[str, str]:
    nodes, edges = io.StringIO(), io.StringIO()
    w = csv.writer(nodes)
    w.writerow(["id", "schema", "caption", "degree"])
    for n in graph["nodes"]:
        w.writerow([_csv_safe(n["id"]), _csv_safe(n["schema"]), _csv_safe(n.get("caption")), n.get("degree", 0)])
    w = csv.writer(edges)
    w.writerow(["id", "source", "target", "label", "source_grade", "info_grade", "independent_sources", "assertions"])
    for e in graph["edges"]:
        w.writerow([_csv_safe(e["id"]), e["source"], e["target"], _csv_safe(e["label"]), e["source_grade"], e["info_grade"],
                    e["independent_sources"], " ".join(str(a) for a in e["assertions"])])
    return {"nodes.csv": nodes.getvalue(), "edges.csv": edges.getvalue()}


def to_graphml(graph: dict[str, Any]) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">']
    keys = [("d0", "node", "caption"), ("d1", "node", "schema"), ("d2", "edge", "label"), ("d3", "edge", "source_grade"),
            ("d4", "edge", "info_grade"), ("d5", "edge", "independent_sources")]
    for kid, domain, name in keys:
        out.append(f'  <key id="{kid}" for="{domain}" attr.name="{name}" attr.type="string"/>')
    out.append('  <graph id="case" edgedefault="directed">')
    for n in graph["nodes"]:
        out.append(f'    <node id={quoteattr(n["id"])}><data key="d0">{xml_escape(str(n.get("caption") or ""))}</data>'
                   f'<data key="d1">{xml_escape(str(n["schema"]))}</data></node>')
    for e in graph["edges"]:
        out.append(f'    <edge id={quoteattr(e["id"])} source={quoteattr(e["source"])} target={quoteattr(e["target"])}>'
                   f'<data key="d2">{xml_escape(str(e["label"]))}</data><data key="d3">{e["source_grade"]}</data>'
                   f'<data key="d4">{e["info_grade"]}</data><data key="d5">{e["independent_sources"]}</data></edge>')
    out += ["  </graph>", "</graphml>"]
    return "\n".join(out)


READ_FIRST = ("This is a summary of what the sources reported, with each relationship's weakest-link grade and number of "
              "independent sources. Entities were not merged unless a person decided so. It is not a finding about any person, it does "
              "not show what was not collected, and it is not evidence on its own: the underlying assertions and artifacts are in the case.")


def _report_data(conn: psycopg.Connection, case_id: uuid.UUID) -> tuple[dict[str, Any], dict[str, str], list[dict[str, Any]], dict[str, list[str]]]:
    g = views.build_graph(conn, case_id, include_documents=False)
    names = {n["id"]: n.get("caption") or n["schema"] for n in g["nodes"]}
    notes = [a for a in annotations(conn, case_id) if a["kind"] == "note"]
    runs = conn.execute("SELECT plugin_id, status, coverage FROM search_run WHERE case_id=%s ORDER BY ran_at DESC LIMIT 200", (case_id,)).fetchall()
    gaps: dict[str, list[str]] = {}
    for r in runs:
        for c in r["coverage"] or []:
            if c.get("outcome") == "unknown":
                gaps.setdefault(r["plugin_id"], []).append(str(c.get("note") or ""))
    return g, names, notes, gaps


def report_pdf(conn: psycopg.Connection, case_id: uuid.UUID, case_name: str) -> bytes:
    """The same summary as report_html, as a PDF. Needs reportlab (pip install reportlab). Long tables are
    capped per page by the layout, not truncated: every entity and relationship is listed."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError as e:
        raise WorkbenchError("PDF output needs reportlab (pip install reportlab); the HTML report works without it") from e
    import io
    from xml.sax.saxutils import escape

    g, names, notes, gaps = _report_data(conn, case_id)
    st = getSampleStyleSheet()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
                            title=f"{case_name} - case summary", author="Astral Lens")
    cell = st["BodyText"].clone("cell", fontSize=8, leading=10)

    def table(head: list[str], rows: list[list[str]], widths: list[float]) -> Table:
        data = [[Paragraph(f"<b>{escape(h)}</b>", cell) for h in head]] + [[Paragraph(escape(str(c)), cell) for c in r] for r in rows]
        t = Table(data, colWidths=[w * mm for w in widths], repeatRows=1)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.grey), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                               ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        return t

    flow: list[Any] = [Paragraph(escape(case_name), st["Title"]),
                       Paragraph(f"Generated {stamp}. {len(g['nodes'])} entities, {len(g['edges'])} relationships.", st["BodyText"]),
                       Spacer(1, 4 * mm), Paragraph(f"<b>Read this first.</b> {escape(READ_FIRST)}", st["BodyText"]), Spacer(1, 4 * mm),
                       Paragraph("Entities", st["Heading2"]),
                       table(["Name", "Type", "Links"], [[n.get("caption") or "", n["schema"], n.get("degree", 0)] for n in g["nodes"]], [100, 50, 30]),
                       Paragraph("Relationships", st["Heading2"]),
                       table(["From", "Relationship", "To", "Grade", "Sources"],
                             [[names.get(x["source"], "?"), x["label"], names.get(x["target"], "?"), f"{x['source_grade']}{x['info_grade']}",
                               x["independent_sources"]] for x in g["edges"]], [50, 40, 50, 20, 20]),
                       Paragraph("Sources that could not be asked", st["Heading2"])]
    flow += [Paragraph(f"<b>{escape(p)}</b>: {escape('; '.join(sorted(set(v))[:3]))}", st["BodyText"]) for p, v in sorted(gaps.items())] or [Paragraph("none recorded", st["BodyText"])]
    flow.append(Paragraph("Analyst notes (not evidence)", st["Heading2"]))
    flow += [Paragraph(f"{escape(names.get(a['cluster_id'] or '', 'whole graph'))}: {escape(a['body'])}", st["BodyText"]) for a in notes] or [Paragraph("none", st["BodyText"])]
    doc.build(flow)
    return buf.getvalue()


def report_html(conn: psycopg.Connection, case_id: uuid.UUID, case_name: str) -> str:
    """A self-contained, read-only summary of the graph. It states what it cannot show."""
    g, names, notes, gaps = _report_data(conn, case_id)
    e = html.escape
    rows_n = "".join(f"<tr><td>{e(str(n.get('caption') or ''))}</td><td>{e(n['schema'])}</td><td>{n.get('degree', 0)}</td></tr>" for n in g["nodes"])
    rows_e = "".join(f"<tr><td>{e(names.get(x['source'], '?'))}</td><td>{e(x['label'])}</td><td>{e(names.get(x['target'], '?'))}</td>"
                     f"<td>{e(x['source_grade'])}{x['info_grade']}</td><td>{x['independent_sources']}</td></tr>" for x in g["edges"])
    rows_g = "".join(f"<li><b>{e(p)}</b>: {e('; '.join(sorted(set(v))[:3]))}</li>" for p, v in sorted(gaps.items()))
    rows_a = "".join(f"<li>{e(names.get(a['cluster_id'] or '', 'whole graph'))}: {e(a['body'])}</li>" for a in notes)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = (f"<h1>{e(case_name)}</h1><p>Generated {stamp}. {len(g['nodes'])} entities, {len(g['edges'])} relationships.</p>"
            f"<p class=w><b>Read this first.</b> {e(READ_FIRST)}</p>"
            f"<h2>Entities</h2><table><tr><th>Name</th><th>Type</th><th>Links</th></tr>{rows_n}</table>"
            f"<h2>Relationships</h2><table><tr><th>From</th><th>Relationship</th><th>To</th><th>Grade</th><th>Sources</th></tr>{rows_e}</table>"
            f"<h2>Sources that could not be asked</h2><ul>{rows_g or '<li>none recorded</li>'}</ul>"
            f"<h2>Analyst notes (not evidence)</h2><ul>{rows_a or '<li>none</li>'}</ul>")
    return ("<!doctype html><html><head><meta charset=utf-8><title>Case summary</title><style>"
            "body{font:14px system-ui;max-width:960px;margin:2em auto;padding:0 1em}table{border-collapse:collapse;width:100%}"
            "td,th{border:1px solid #ccc;padding:3px 6px;text-align:left}.w{background:#fff6d6;padding:8px;border:1px solid #e0c560}"
            f"</style></head><body>{body}</body></html>")
