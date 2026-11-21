"""Link analysis over the case graph (the analyst's "which nodes matter, how are these two connected").

Pure functions over the dict that `views.build_graph` returns ({"nodes": [...], "edges": [...]}); no
database, no side effects, deterministic (ties are broken by node id) so a result can be reproduced.

What these numbers are. They describe the shape of the graph AS ASSERTED. A node with high
betweenness sits on many shortest paths between other recorded nodes; that is a statement about the
recorded links, not about the real network. A sparse graph usually means sparse collection, so a
low score is not evidence of low importance, and communities are a convenience for reading the
picture, not a finding. Edges are treated as undirected and unweighted unless a function says so.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

MAX_PATHS = 20


def adjacency(graph: dict[str, Any]) -> dict[str, list[str]]:
    adj: dict[str, set[str]] = {n["id"]: set() for n in graph["nodes"]}
    for e in graph["edges"]:
        a, b = e["source"], e["target"]
        if a == b or a not in adj or b not in adj:
            continue
        adj[a].add(b)
        adj[b].add(a)
    return {k: sorted(v) for k, v in adj.items()}


def components(adj: dict[str, list[str]]) -> list[list[str]]:
    seen: set[str] = set()
    out: list[list[str]] = []
    for start in sorted(adj):
        if start in seen:
            continue
        comp, queue = [], deque([start])
        seen.add(start)
        while queue:
            v = queue.popleft()
            comp.append(v)
            for w in adj[v]:
                if w not in seen:
                    seen.add(w)
                    queue.append(w)
        out.append(sorted(comp))
    return sorted(out, key=lambda c: (-len(c), c[0]))


EXACT_LIMIT = 1500      # above this many nodes betweenness and closeness are SAMPLED: exact is O(nodes x edges) and hangs a request
SAMPLE_MAX, SAMPLE_MIN = 300, 20
EDGE_BUDGET = 1_500_000  # sources x edges the sampled estimates may visit (about a second or two of Python)


def sample_size(adj: dict[str, list[str]]) -> int:
    """How many source nodes the sampled estimates use: fewer as the graph grows, so the cost stays about constant."""
    edges = max(1, sum(len(v) for v in adj.values()) // 2)
    return max(SAMPLE_MIN, min(SAMPLE_MAX, EDGE_BUDGET // edges))


def _spread(items: list[str], k: int) -> list[str]:
    return items if len(items) <= k else [items[int(i * len(items) / k)] for i in range(k)]


def betweenness(adj: dict[str, list[str]], sources: list[str] | None = None) -> dict[str, float]:
    """Brandes' algorithm, unweighted, undirected, normalised to 0..1. Exact when `sources` is None; with a sample of
    source nodes it is the standard pivot estimate (each source's contribution scaled by nodes / sources)."""
    bc: dict[str, float] = {v: 0.0 for v in adj}
    for s in (sorted(adj) if sources is None else sources):
        stack: list[str] = []
        pred: dict[str, list[str]] = defaultdict(list)
        sigma: dict[str, float] = defaultdict(float)
        dist: dict[str, int] = {s: 0}
        sigma[s] = 1.0
        queue = deque([s])
        while queue:
            v = queue.popleft()
            stack.append(v)
            for w in adj[v]:
                if w not in dist:
                    dist[w] = dist[v] + 1
                    queue.append(w)
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    pred[w].append(v)
        delta: dict[str, float] = defaultdict(float)
        while stack:
            w = stack.pop()
            for v in pred[w]:
                delta[v] += sigma[v] / sigma[w] * (1 + delta[w])
            if w != s:
                bc[w] += delta[w]
    n = len(adj)
    extrapolate = n / len(sources) if sources else 1.0
    scale = extrapolate / ((n - 1) * (n - 2)) if n > 2 else 0.0   # each pair counted twice (undirected)
    return {v: round(x * scale, 6) for v, x in bc.items()}


def closeness(adj: dict[str, list[str]], nodes: list[str] | None = None) -> dict[str, float]:
    """Wasserman-Faust closeness: scaled by the size of the reachable component, so isolated nodes score 0.
    With `nodes`, only those nodes are scored (the rest are absent, not zero)."""
    n = len(adj)
    out: dict[str, float] = {}
    for s in (adj if nodes is None else nodes):
        dist = {s: 0}
        queue = deque([s])
        while queue:
            v = queue.popleft()
            for w in adj[v]:
                if w not in dist:
                    dist[w] = dist[v] + 1
                    queue.append(w)
        reach, total = len(dist) - 1, sum(dist.values())
        out[s] = round((reach / total) * (reach / (n - 1)), 6) if total and n > 1 else 0.0
    return out


def pagerank(adj: dict[str, list[str]], damping: float = 0.85, iterations: int = 60) -> dict[str, float]:
    n = len(adj)
    if not n:
        return {}
    rank = {v: 1.0 / n for v in adj}
    for _ in range(iterations):
        sink = sum(rank[v] for v in adj if not adj[v])
        nxt = {v: (1 - damping) / n + damping * sink / n for v in adj}
        for v, nbrs in adj.items():
            if nbrs:
                share = damping * rank[v] / len(nbrs)
                for w in nbrs:
                    nxt[w] += share
        rank = nxt
    return {v: round(x, 6) for v, x in rank.items()}


def communities(adj: dict[str, list[str]], max_nodes: int = 3000) -> list[list[str]]:
    """Greedy modularity maximisation (Clauset-Newman-Moore), deterministic: repeatedly merge the pair of
    communities whose union raises modularity most (ties -> smallest ids) until no merge helps. Beyond
    `max_nodes` it declines to guess and returns the connected components instead."""
    if len(adj) > max_nodes:
        return components(adj)
    m2 = sum(len(v) for v in adj.values())                      # twice the number of edges
    if not m2:
        return [[v] for v in sorted(adj)]
    members = {v: [v] for v in adj}
    a = {v: len(adj[v]) / m2 for v in adj}                      # share of edge ends in each community
    e: dict[tuple[str, str], float] = {}                        # share of edge ends between two communities
    for v, nbrs in adj.items():
        for w in nbrs:
            if v < w:
                e[(v, w)] = e.get((v, w), 0.0) + 1.0 / m2
    while e:
        pair, gain = None, 1e-12
        for k, x in sorted(e.items()):                          # sorted: ties go to the smallest ids
            dq = 2 * (x - a[k[0]] * a[k[1]])
            if dq > gain:
                pair, gain = k, dq
        if pair is None:
            break
        i, j = pair
        members[i] += members.pop(j)
        a[i] += a.pop(j)
        merged: dict[tuple[str, str], float] = {}
        for (p, q), x in e.items():
            if (p, q) == (i, j):
                continue
            p2, q2 = (i if p == j else p), (i if q == j else q)
            key = (p2, q2) if p2 < q2 else (q2, p2)
            merged[key] = merged.get(key, 0.0) + x
        e = merged
    return sorted((sorted(g) for g in members.values()), key=lambda g: (-len(g), g[0]))


def shortest_paths(adj: dict[str, list[str]], a: str, b: str, limit: int = 5) -> dict[str, Any]:
    """Up to `limit` shortest paths between two nodes. No path means none is RECORDED, not that none exists."""
    if a not in adj or b not in adj:
        raise KeyError("unknown node")
    limit = max(1, min(limit, MAX_PATHS))
    if a == b:
        return {"length": 0, "paths": [[a]], "truncated": False}
    dist, parents = {a: 0}, defaultdict(list)
    queue = deque([a])
    while queue:
        v = queue.popleft()
        if b in dist and dist[v] >= dist[b]:
            break
        for w in adj[v]:
            if w not in dist:
                dist[w] = dist[v] + 1
                queue.append(w)
            if dist[w] == dist[v] + 1:
                parents[w].append(v)
    if b not in dist:
        return {"length": None, "paths": [], "truncated": False,
                "note": "no connection is recorded between these two; that is not evidence that none exists"}
    paths: list[list[str]] = []
    stack = [(b, [b])]
    while stack and len(paths) <= limit:
        v, tail = stack.pop()
        if v == a:
            paths.append(list(reversed(tail)))
            continue
        for p in sorted(parents[v], reverse=True):
            stack.append((p, tail + [p]))
    paths.sort()
    return {"length": dist[b], "paths": paths[:limit], "truncated": len(paths) > limit}


def neighbourhood(adj: dict[str, list[str]], start: str, hops: int = 1) -> dict[str, int]:
    """node -> distance, for everything within `hops` of start (hops capped at 6)."""
    if start not in adj:
        raise KeyError("unknown node")
    dist, queue = {start: 0}, deque([start])
    while queue:
        v = queue.popleft()
        if dist[v] >= min(hops, 6):
            continue
        for w in adj[v]:
            if w not in dist:
                dist[w] = dist[v] + 1
                queue.append(w)
    return dist


def common_neighbours(adj: dict[str, list[str]], nodes: list[str]) -> list[str]:
    if len(nodes) < 2 or any(n not in adj for n in nodes):
        return []
    shared = set(adj[nodes[0]])
    for n in nodes[1:]:
        shared &= set(adj[n])
    return sorted(shared - set(nodes))


def analyse(graph: dict[str, Any], *, top: int = 10) -> dict[str, Any]:
    adj = adjacency(graph)
    caption = {n["id"]: n.get("caption") or n.get("schema") for n in graph["nodes"]}
    exact = len(adj) <= EXACT_LIMIT
    if exact:
        bc, cc = betweenness(adj), closeness(adj)
    else:
        k = sample_size(adj)
        bc = betweenness(adj, _spread(sorted(adj), k))
        by_degree = sorted(adj, key=lambda v: (-len(adj[v]), v))
        cc = closeness(adj, by_degree[:k])                            # a ranking of the best-connected nodes only
    pr = pagerank(adj)
    comps, comms = components(adj), communities(adj)

    def rank(scores: dict[str, float]) -> list[dict[str, Any]]:
        return [{"id": v, "caption": caption[v], "score": s} for v, s in
                sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:top] if s > 0]

    return {
        "nodes": len(adj), "edges": sum(len(v) for v in adj.values()) // 2,
        "components": [{"size": len(c), "members": c[:50]} for c in comps],
        "communities": [{"size": len(c), "members": c[:50]} for c in comms],
        "degree": rank({v: float(len(n)) for v, n in adj.items()}),
        "betweenness": rank(bc), "closeness": rank(cc), "pagerank": rank(pr),
        "approximate": None if exact else {
            "reason": f"{len(adj)} nodes is above {EXACT_LIMIT}: exact centrality would take too long",
            "betweenness": f"estimated from {k} evenly spaced source nodes (deterministic)",
            "closeness": f"scored for the {k} best-connected nodes only"},
        "note": ("These describe the recorded links only. Sparse collection makes a graph look more fragmented than "
                 "the real network; communities and rankings are aids to reading, not findings."),
    }
