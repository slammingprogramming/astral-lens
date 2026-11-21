// Builds a read-only Cypher query from the choices in the query builder. The labels and predicates come from the server's
// schema for the case (graph/schema) and are checked again here, so nothing but a plain identifier can reach the text.
// The server still refuses anything that writes, whatever this produces.

export type BuildOpts = {
  from: string;        // vertex label, "" = any
  predicate: string;   // edge label, "" = any
  to: string;          // vertex label, "" = any
  worstGrade: string;  // "" = any source grade, else the lowest source grade allowed (A..F)
  minSources: number;  // minimum independent sources, 0 = no condition
  limit: number;
};

const IDENT = /^[A-Za-z][A-Za-z0-9_]{0,40}$/;
const GRADES = "ABCDEF";

function ident(v: string, what: string): string {
  if (v !== "" && !IDENT.test(v)) throw new Error(`${what} is not a plain label: ${v}`);
  return v;
}

export function buildCypher(o: BuildOpts): string {
  const a = ident(o.from, "From"), r = ident(o.predicate, "Relationship"), b = ident(o.to, "To");
  const where: string[] = [];
  if (o.worstGrade) {
    const i = GRADES.indexOf(o.worstGrade);
    if (i < 0) throw new Error(`Unknown grade: ${o.worstGrade}`);
    where.push(`r.sg IN [${GRADES.slice(0, i + 1).split("").map((g) => `'${g}'`).join(", ")}]`);
  }
  if (Number.isInteger(o.minSources) && o.minSources > 0) where.push(`r.n >= ${Math.min(o.minSources, 1000)}`);
  const limit = Math.max(1, Math.min(Math.trunc(o.limit) || 25, 500));
  return [
    `MATCH (a${a ? ":" + a : ""})-[r${r ? ":" + r : ""}]->(b${b ? ":" + b : ""})`,
    where.length ? `WHERE ${where.join(" AND ")}` : "",
    `RETURN a, r, b`,
    `LIMIT ${limit}`,
  ].filter(Boolean).join("\n");
}
