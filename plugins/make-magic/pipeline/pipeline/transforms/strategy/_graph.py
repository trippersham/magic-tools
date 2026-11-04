"""Tiny, dependency-free graph ops for the STRUCTURE-ONLY engine facts.

The validated prototype used ``networkx`` for two things — weakly-connected
components (the convergent event engines) and greedy-modularity communities
(the structure communities). Both are reported as *facts* under
``facts["engines"]`` and, per the engine's own contract, **never decide the
label**. networkx is not a pipeline dependency, so this module re-implements
exactly those two operations in pure Python with deterministic ordering.

``greedy_modularity_communities`` is the standard Clauset-Newman-Moore greedy
agglomeration (merge the community pair with the largest modularity gain until
no positive gain remains), with ties broken on sorted node labels so the output
is byte-stable across runs and hash seeds.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Hashable, Iterable


def weakly_connected_components[T: Hashable](
    edges: Iterable[tuple[T, T]],
) -> list[set[T]]:
    """Weakly-connected components of a directed edge list (direction ignored).

    Returns the components as a list of node sets. Only nodes that appear in an
    edge are included (matching ``networkx.weakly_connected_components`` over a
    ``DiGraph`` built solely from ``add_edge``).
    """
    adj: dict[T, set[T]] = defaultdict(set)
    nodes: set[T] = set()
    for u, v in edges:
        adj[u].add(v)
        adj[v].add(u)
        nodes.add(u)
        nodes.add(v)
    seen: set[T] = set()
    comps: list[set[T]] = []
    for start in nodes:
        if start in seen:
            continue
        comp: set[T] = set()
        stack = [start]
        seen.add(start)
        while stack:
            n = stack.pop()
            comp.add(n)
            for m in adj[n]:
                if m not in seen:
                    seen.add(m)
                    stack.append(m)
        comps.append(comp)
    return comps


def greedy_modularity_communities[T: Hashable](
    nodes: Iterable[T],
    weighted_edges: Iterable[tuple[T, T, float]],
) -> list[list[T]]:
    """Clauset-Newman-Moore greedy modularity communities (deterministic).

    ``weighted_edges`` are undirected ``(u, v, weight)`` triples. Isolated nodes
    (no edges) each form their own singleton community, matching networkx. The
    result is a list of communities, each a sorted node list; communities are
    ordered largest-weight-first then by their sorted members.
    """
    node_list = sorted(nodes, key=_keyfn)
    if not node_list:
        return []
    # Collapse parallel edges by summing weights; drop self loops.
    w: dict[frozenset[T], float] = defaultdict(float)
    deg: dict[T, float] = defaultdict(float)
    m2 = 0.0  # 2m = total weight * 2 (each edge counted once here -> m)
    present = set(node_list)
    for u, v, wt in weighted_edges:
        if u == v or u not in present or v not in present:
            continue
        w[frozenset((u, v))] += wt
        deg[u] += wt
        deg[v] += wt
        m2 += wt
    m = m2
    if m <= 0:
        # no edges -> every node is its own community
        return [[n] for n in node_list]

    # community -> set of member nodes; start singletons
    comm: dict[T, set[T]] = {n: {n} for n in node_list}
    comm_deg: dict[T, float] = {n: deg[n] for n in node_list}
    # adjacency weight between communities
    adjw: dict[T, dict[T, float]] = defaultdict(dict)
    for pair, wt in w.items():
        u, v = tuple(pair)  # self-loops already dropped, so every pair has 2 members
        adjw[u][v] = adjw[u].get(v, 0.0) + wt
        adjw[v][u] = adjw[v].get(u, 0.0) + wt

    def gain(a: T, b: T) -> float:
        # ΔQ for merging communities a,b = eab/m - (deg_a*deg_b)/(2 m^2)
        eab = adjw.get(a, {}).get(b, 0.0)
        return eab / m - (comm_deg[a] * comm_deg[b]) / (2.0 * m * m)

    alive = set(node_list)
    while True:
        best: tuple[float, T, T] | None = None
        for a in sorted(alive, key=_keyfn):
            for b in sorted(adjw.get(a, {}), key=_keyfn):
                if b not in alive or _keyfn(b) <= _keyfn(a):
                    continue  # each unordered pair considered once
                g = gain(a, b)
                if g <= 0:
                    continue
                cand = (g, a, b)
                if best is None or _better(cand, best):
                    best = cand
        if best is None:
            break
        _g, a, b = best
        # merge b into a (keep the lexicographically smaller label as the rep)
        keep, drop = (a, b) if _keyfn(a) <= _keyfn(b) else (b, a)
        comm[keep] |= comm[drop]
        comm_deg[keep] += comm_deg[drop]
        # merge adjacency
        for nb, wt in list(adjw.get(drop, {}).items()):
            if nb == keep:
                continue
            adjw[keep][nb] = adjw[keep].get(nb, 0.0) + wt
            adjw[nb][keep] = adjw[nb].get(keep, 0.0) + wt
            adjw[nb].pop(drop, None)
        adjw[keep].pop(drop, None)
        adjw.pop(drop, None)
        alive.discard(drop)
        comm.pop(drop, None)
        comm_deg.pop(drop, None)

    communities = [sorted(members, key=_keyfn) for members in comm.values()]
    communities.sort(key=lambda members: (-len(members), [_keyfn(x) for x in members]))
    return communities


def _keyfn(x: Hashable) -> str:
    return str(x)


def _better(cand: tuple[float, Hashable, Hashable], best: tuple[float, Hashable, Hashable]) -> bool:
    # higher gain wins; ties broken deterministically on the (a,b) labels
    if cand[0] != best[0]:
        return cand[0] > best[0]
    return (_keyfn(cand[1]), _keyfn(cand[2])) < (_keyfn(best[1]), _keyfn(best[2]))
