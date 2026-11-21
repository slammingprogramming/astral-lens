"""Graph projection on Apache AGE (Apache-2.0, a PostgreSQL extension): a REBUILDABLE copy of a case's graph that
analysts can query in Cypher (docs/03 §8). It is never the system of record.

Rules this module keeps (the same ones as every dedicated service, invariant 16):
  * Postgres is the truth. The projection is dropped and rebuilt whenever the case graph's fingerprint changes, so
    a query never runs against a graph older than the data it describes; if the rebuild fails the query fails
    with the reason, it does not fall through to a stale graph.
  * The graph holds IDS ONLY (an entity's cluster id, its schema as the vertex label, an edge's id, kind, grades and
    independent-source count). Names, values and every other word shown to the analyst are re-read from Postgres
    after the query, and a vertex Postgres no longer holds is marked stale, never shown as current. Because no
    free text is ever written into a Cypher string, building the graph has no injection surface.
  * Analyst queries are READ-ONLY by two independent means: a keyword check, and a READ ONLY transaction that the
    database itself enforces. They cannot contain `$` (the quoting that would let a query escape into SQL), `;`, or
    anything that writes, loads or calls. A timeout and a row cap bound the cost.
  * One graph per case, in its own database (ASTRAL_AGE_DSN), erased with the case.

Not provided: Cypher `shortestPath` (AGE does not implement it; the built-in path search stays relational, which
was measured faster), and any claim that a Cypher answer is more correct than the relational one: both read the same
recorded links.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

import psycopg

from astral import views
from astral.services import ServiceError

MAX_ROWS = 500
TIMEOUT_MS = 15_000
BATCH = 200
MAX_QUERY = 2000
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
LABEL = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,40}$")
EDGE_ID = re.compile(r"^[A-Za-z0-9:_\-]{1,200}$")
FORBIDDEN = re.compile(r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD|COPY|CALL|FOREACH|UNION|EXPLAIN|PROFILE)\b", re.I)


class GraphQueryError(ValueError):
    """The analyst's query was refused; the message says why."""


def graph_name(case_id: uuid.UUID) -> str:
    return "case_" + case_id.hex


def fingerprint(graph: dict[str, Any]) -> str:
    h = hashlib.sha256()
    for n in sorted(graph["nodes"], key=lambda n: n["id"]):
        h.update(f"n|{n['id']}|{n['schema']}\n".encode())
    for e in sorted(graph["edges"], key=lambda e: e["id"]):
        h.update(f"e|{e['id']}|{e['source']}|{e['target']}|{e['label']}|{e['source_grade']}|{e['info_grade']}|{e['independent_sources']}\n".encode())
    return h.hexdigest()


def _label(raw: Any, fallback: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]", "_", str(raw or ""))
    return s if LABEL.match(s) else fallback


# ------------------------------------------------------------------ validating an analyst's Cypher
def _scan(q: str) -> tuple[str, list[tuple[int, str, int]]]:
    """(text with string literals blanked, [(position, UPPER keyword, depth)] of RETURN/ORDER/SKIP/LIMIT tokens)."""
    out, depth, i, toks = [], 0, 0, []
    while i < len(q):
        ch = q[i]
        if ch in "'\"":
            j = i + 1
            while j < len(q) and q[j] != ch:
                j += 2 if q[j] == "\\" else 1
            if j >= len(q):
                raise GraphQueryError("unterminated string in the query")
            out.append(" " * (j - i + 1))
            i = j + 1
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        m = re.match(r"(RETURN|ORDER\s+BY|SKIP|LIMIT)\b", q[i:], re.I) if (i == 0 or not (q[i - 1].isalnum() or q[i - 1] == "_")) else None
        if m:
            toks.append((i, re.sub(r"\s+", " ", m.group(1).upper()), depth))
        out.append(ch)
        i += 1
    if depth != 0:
        raise GraphQueryError("unbalanced brackets in the query")
    return "".join(out), toks


def check_query(q: str) -> int:
    """Validate a read-only Cypher query and return how many columns its final RETURN produces."""
    if not isinstance(q, str) or not q.strip():
        raise GraphQueryError("empty query")
    if len(q) > MAX_QUERY:
        raise GraphQueryError(f"query longer than {MAX_QUERY} characters")
    if any(c in q for c in ("$", ";", "\x00")):
        raise GraphQueryError("'$', ';' and NUL are not allowed in a query")
    blanked, toks = _scan(q)
    bad = FORBIDDEN.search(blanked)
    if bad:
        raise GraphQueryError(f"{bad.group(1).upper()} is not allowed: queries are read-only")
    rets = [t for t in toks if t[1] == "RETURN" and t[2] == 0]
    if not rets:
        raise GraphQueryError("a query must end in RETURN")
    start = rets[-1][0] + len("RETURN")
    ends = [t[0] for t in toks if t[2] == 0 and t[0] > start and t[1] in ("ORDER BY", "SKIP", "LIMIT")]
    body = blanked[start:min(ends) if ends else len(blanked)]
    body = re.sub(r"^\s*DISTINCT\b", "", body, flags=re.I)
    if body.strip() in ("", "*"):
        raise GraphQueryError("name the values to RETURN (RETURN * is not supported)")
    cols, depth, cur = 1, 0, ""
    for ch in body:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            cols += 1
    if cols > 12:
        raise GraphQueryError("at most 12 returned columns")
    return cols


_TAIL = re.compile(r"(?<=[}\]])::(vertex|edge|path)|(?<=\d)::numeric")


def parse_agtype(text: str | None) -> Any:
    if text is None:
        return None
    return json.loads(_TAIL.sub("", text))


class GraphService:
    name = "age"

    def __init__(self, dsn: str, *, timeout_ms: int = TIMEOUT_MS, max_rows: int = MAX_ROWS) -> None:
        self.dsn, self.timeout_ms, self.max_rows = dsn, timeout_ms, max_rows

    # -- connection
    def _connect(self) -> psycopg.Connection:
        try:
            c = psycopg.connect(self.dsn, autocommit=True, connect_timeout=5)
            c.execute("LOAD 'age'")
            c.execute('SET search_path = ag_catalog, "$user", public')
            return c
        except psycopg.Error as e:
            raise ServiceError(f"apache age: {type(e).__name__}: {str(e).splitlines()[0][:160] if str(e) else ''}") from e

    def ping(self) -> dict[str, Any]:
        with self._connect() as c:
            v = c.execute("SELECT extversion FROM pg_extension WHERE extname='age'").fetchone()
            if not v:
                raise ServiceError("the age extension is not installed in this database (CREATE EXTENSION age)")
            n = c.execute("SELECT count(*) FROM ag_catalog.ag_graph").fetchone()[0]
            return {"version": v[0], "graphs": n}

    def has(self, case_id: uuid.UUID) -> bool:
        with self._connect() as c:
            return bool(c.execute("SELECT 1 FROM ag_catalog.ag_graph WHERE name=%s", (graph_name(case_id),)).fetchone())

    def delete_case(self, case_id: uuid.UUID) -> int:
        with self._connect() as c:
            gone = c.execute("SELECT 1 FROM ag_catalog.ag_graph WHERE name=%s", (graph_name(case_id),)).fetchone()
            if gone:
                c.execute("SELECT drop_graph(%s, true)", (graph_name(case_id),))
            return 1 if gone else 0

    # -- building the projection
    def project(self, graph: dict[str, Any], case_id: uuid.UUID) -> dict[str, Any]:
        """Replace the case's graph with `graph` (the dict views.build_graph returns), in one transaction."""
        name = graph_name(case_id)
        nodes, label_of, skipped = [], {}, 0
        for n in graph["nodes"]:
            if not UUID.match(str(n["id"])):
                skipped += 1
                continue
            lab = _label(n.get("schema"), "Entity")
            nodes.append((n["id"], lab))
            label_of[n["id"]] = lab
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for e in graph["edges"]:
            s, t = label_of.get(e["source"]), label_of.get(e["target"])
            sg = e.get("source_grade") if re.fullmatch(r"[A-F]", str(e.get("source_grade") or "")) else None
            ig = e.get("info_grade") if isinstance(e.get("info_grade"), int) and 1 <= e["info_grade"] <= 6 else None
            if not s or not t or not EDGE_ID.match(str(e["id"])) or not re.fullmatch(r"[a-z]{1,20}", str(e.get("kind") or "")):
                skipped += 1
                continue
            groups.setdefault((s, t, _label(e.get("label"), "LINK")), []).append(
                {"s": e["source"], "t": e["target"], "id": e["id"], "k": e["kind"], "sg": sg, "ig": ig,
                 "n": int(e["independent_sources"]) if isinstance(e.get("independent_sources"), int) else None})
        with self._connect() as c, c.transaction():
            c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (name,))
            if c.execute("SELECT 1 FROM ag_catalog.ag_graph WHERE name=%s", (name,)).fetchone():
                c.execute("SELECT drop_graph(%s, true)", (name,))
            c.execute("SELECT create_graph(%s)", (name,))
            by_label: dict[str, list[str]] = {}
            for nid, lab in nodes:
                by_label.setdefault(lab, []).append(nid)
            for lab, ids in by_label.items():
                for i in range(0, len(ids), BATCH):
                    rows = ",".join("{id:'%s'}" % x for x in ids[i:i + BATCH])
                    self._cypher(c, name, f"UNWIND [{rows}] AS r CREATE (:{lab} {{id: r.id}})")
                ix = "ix_" + hashlib.md5(f"{name}{lab}".encode()).hexdigest()[:20]
                c.execute(f'CREATE INDEX "{ix}" ON "{name}"."{lab}" USING gin (properties)')
            made = 0
            for (s, t, lab), es in groups.items():
                for i in range(0, len(es), BATCH):
                    rows = ",".join("{s:'%s',t:'%s',id:'%s',k:'%s',sg:%s,ig:%s,n:%s}" % (
                        r["s"], r["t"], r["id"], r["k"], "'%s'" % r["sg"] if r["sg"] else "null",
                        r["ig"] if r["ig"] is not None else "null", r["n"] if r["n"] is not None else "null") for r in es[i:i + BATCH])
                    self._cypher(c, name, f"UNWIND [{rows}] AS r MATCH (a:{s} {{id: r.s}}), (b:{t} {{id: r.t}}) "
                                          f"CREATE (a)-[:{lab} {{id: r.id, kind: r.k, sg: r.sg, ig: r.ig, n: r.n}}]->(b)")
                    made += len(es[i:i + BATCH])
        return {"vertices": len(nodes), "edges": made, "skipped": skipped, "graph": name}

    @staticmethod
    def _cypher(c: psycopg.Connection, name: str, body: str) -> list[tuple]:
        # `name` is graph_name(): "case_" + 32 hex characters. `body` was assembled only from validated identifiers.
        return c.execute(f"SELECT * FROM ag_catalog.cypher('{name}', $astral$ {body} $astral$) AS (v ag_catalog.agtype)").fetchall()

    # -- an analyst's read-only query
    def query(self, case_id: uuid.UUID, q: str) -> dict[str, Any]:
        cols = check_query(q)
        name = graph_name(case_id)
        aliases = ", ".join(f"c{i} ag_catalog.agtype" for i in range(cols))
        with self._connect() as c:
            c.autocommit = False
            try:
                c.execute("SET TRANSACTION READ ONLY")
                c.execute(f"SET LOCAL statement_timeout = {int(self.timeout_ms)}")
                try:
                    cur = c.execute(f"SELECT * FROM ag_catalog.cypher('{name}', $astral$ {q} $astral$) AS ({aliases})")
                    rows = cur.fetchmany(self.max_rows + 1)
                except psycopg.errors.QueryCanceled as e:
                    raise GraphQueryError(f"the query ran longer than {self.timeout_ms // 1000}s and was stopped") from e
                except psycopg.errors.InvalidSchemaName as e:
                    raise ServiceError("this case has no graph projection yet") from e
                except psycopg.Error as e:
                    raise GraphQueryError(str(e).splitlines()[0][:200]) from e
            finally:
                c.rollback()
        truncated = len(rows) > self.max_rows
        return {"columns": cols, "rows": [[parse_agtype(v) for v in r] for r in rows[:self.max_rows]], "truncated": truncated}


# ------------------------------------------------------------------ keeping the projection in step, and hydrating answers
def sync(conn: psycopg.Connection, svc: GraphService, case_id: uuid.UUID, *, force: bool = False) -> dict[str, Any]:
    graph = views.build_graph(conn, case_id, include_documents=True)
    fp = fingerprint(graph)
    have = conn.execute("SELECT fingerprint FROM service_sync WHERE case_id=%s AND service=%s AND kind='graph'", (case_id, svc.name)).fetchone()
    if not force and have and have["fingerprint"] == fp and svc.has(case_id):
        return {"rebuilt": False, "vertices": len(graph["nodes"]), "edges": len(graph["edges"])}
    out = svc.project(graph, case_id)
    conn.execute("INSERT INTO service_sync (case_id, service, kind, cursor, fingerprint) VALUES (%s,%s,'graph',%s,%s)"
                 " ON CONFLICT (case_id, service, kind) DO UPDATE SET cursor=EXCLUDED.cursor, fingerprint=EXCLUDED.fingerprint, synced_at=now()",
                 (case_id, svc.name, out["vertices"], fp))
    return {"rebuilt": True, **out}


def schema(conn: psycopg.Connection, case_id: uuid.UUID) -> dict[str, Any]:
    """What a Cypher query over this case can mention: the vertex labels, and which edge label links which pair of
    vertex labels, with counts. Read from Postgres with the same naming and skipping rules as `GraphService.project`,
    so it needs no graph server and cannot disagree with what a projection would hold. Feeds the query builder."""
    graph = views.build_graph(conn, case_id, include_documents=True)
    label_of: dict[str, str] = {}
    labels: dict[str, int] = {}
    for n in graph["nodes"]:
        if not UUID.match(str(n["id"])):
            continue
        lab = _label(n.get("schema"), "Entity")
        label_of[n["id"]] = lab
        labels[lab] = labels.get(lab, 0) + 1
    rels: dict[tuple[str, str, str], int] = {}
    for e in graph["edges"]:
        s, t = label_of.get(e["source"]), label_of.get(e["target"])
        if not s or not t or not EDGE_ID.match(str(e["id"])) or not re.fullmatch(r"[a-z]{1,20}", str(e.get("kind") or "")):
            continue
        key = (s, _label(e.get("label"), "LINK"), t)
        rels[key] = rels.get(key, 0) + 1
    return {"labels": [{"name": k, "count": v} for k, v in sorted(labels.items())],
            "relations": [{"from": s, "predicate": p, "to": t, "count": n} for (s, p, t), n in sorted(rels.items())],
            "vertices": sum(labels.values()), "edges": sum(rels.values())}


def hydrate(conn: psycopg.Connection, case_id: uuid.UUID, result: dict[str, Any]) -> dict[str, Any]:
    """Replace graph vertices/edges/paths in a result with what Postgres holds now; mark any it no longer holds."""
    ids: set[str] = set()

    def walk(v: Any) -> None:
        if isinstance(v, dict) and "properties" in v and "label" in v and "start_id" not in v and isinstance(v["properties"].get("id"), str):
            ids.add(v["properties"]["id"])
        elif isinstance(v, list):
            for x in v:
                walk(x)
    for r in result["rows"]:
        for cell in r:
            walk(cell)
    known = {str(r["cluster_id"]): r for r in conn.execute(
        "SELECT cluster_id, schema_name, caption FROM proj_entity WHERE case_id=%s AND cluster_id = ANY(%s::uuid[])",
        (case_id, [i for i in ids if UUID.match(i)])).fetchall()} if ids else {}
    stale = 0

    def conv(v: Any) -> Any:
        nonlocal stale
        if isinstance(v, dict) and "label" in v and "properties" in v:
            if "start_id" in v:
                p = v["properties"]
                return {"kind": "edge", "label": v["label"], "id": p.get("id"), "link_kind": p.get("kind"), "source_grade": p.get("sg"),
                        "info_grade": p.get("ig"), "independent_sources": p.get("n")}
            eid = v["properties"].get("id")
            row = known.get(eid)
            if row is None:
                stale += 1
                return {"kind": "node", "id": eid, "schema": v["label"], "caption": None, "stale": True}
            return {"kind": "node", "id": eid, "schema": row["schema_name"], "caption": row["caption"]}
        if isinstance(v, list):
            items = [conv(x) for x in v]
            kinds = [x.get("kind") if isinstance(x, dict) else None for x in items]
            return {"kind": "path", "elements": items} if len(items) > 1 and kinds == ["node", "edge"] * (len(items) // 2) + ["node"] else items
        return v
    out = {**result, "rows": [[conv(c) for c in r] for r in result["rows"]]}
    if stale:
        out["stale_vertices"] = stale
        out["note"] = f"{stale} vertex/vertices are no longer in Postgres and are marked stale"
    return out


def run_query(conn: psycopg.Connection, svc: GraphService, case_id: uuid.UUID, q: str) -> dict[str, Any]:
    check_query(q)                                   # refuse before touching the projection
    s = sync(conn, svc, case_id)
    res = hydrate(conn, case_id, svc.query(case_id, q))
    res["projection"] = {"rebuilt_for_this_query": s["rebuilt"], "vertices": s["vertices"], "edges": s["edges"]}
    res["note"] = (res.get("note", "") + " Cypher reads a rebuildable copy of the recorded links; it shows what was recorded, not what exists.").strip()
    return res
