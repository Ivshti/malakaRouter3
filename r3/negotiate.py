#!/usr/bin/env python3
"""
router3 core — increment 2: PathFinder negotiated-congestion routing.

Routes ALL nets as sequences of triangle-edge crossings over the corridor
graph (corridor.py). Nets are allowed to overlap during search; a rising
congestion cost (McMurchie & Ebeling 1995 "PathFinder") forces them apart over
iterations until no corridor edge is used beyond its lane capacity. This is
the actual negotiation core — the mechanism that resolves the two-net local
conflicts a greedy router (KiCadRoutingTools, and our own escape_gen power-taps)
cannot: it can discover "net A should yield this lane to net B" globally,
instead of committing A first and reporting B failed.

Terminal binding: a net's pad is reachable from ANY triangle touching one of
its boundary vertices (multi-source/multi-sink — a pad has many legal exit
directions). Cost of a corridor-graph edge = centroid-to-centroid Euclidean
distance (a proxy for physical path length; exact geometry is the realizer's
job in increment 3) times a congestion multiplier from present + historical
overuse. Two-terminal nets only in this increment (matches the simple
fixtures); multi-pin/Steiner nets are a later increment.

Only KiCad file I/O is borrowed from the KiCadRoutingTools harness. The
negotiation algorithm is ours.
"""
from __future__ import annotations
import heapq
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(__file__))
from corridor import CorridorGraph, Fab, build_graph  # noqa: E402

HARNESS = os.path.join(os.path.dirname(__file__), "..", "..", "KiCadRoutingTools")
sys.path.insert(0, os.path.abspath(HARNESS))
from kicad_parser import parse_kicad_pcb  # noqa: E402


@dataclass
class Net:
    name: str
    a_sites: List[int]     # boundary vertex indices of terminal A (a pad)
    b_sites: List[int]     # boundary vertex indices of terminal B
    a_center: Tuple[float, float] = (0.0, 0.0)   # real pad center (for realization)
    b_center: Tuple[float, float] = (0.0, 0.0)


@dataclass
class RoutedNet:
    net: Net
    tri_path: List[int]                 # triangle sequence
    edge_path: List[Tuple[int, int]]    # corridor edge keys crossed, in order
    length: float


@dataclass
class NegotiateResult:
    routed: Dict[str, RoutedNet]
    failed: List[str]
    iterations: int
    converged: bool
    final_overuse: int


def nets_from_pcb(pcb, g: CorridorGraph, layer: str) -> List[Net]:
    """Two-pin nets on this layer: group pads by net, take the two farthest-
    apart footprint instances as terminals A/B (matches the simple fixtures,
    which are all point-to-point)."""
    site_by_pos: Dict[Tuple[float, float], int] = {}
    for i, s in enumerate(g.sites):
        site_by_pos.setdefault((round(s.x, 3), round(s.y, 3)), i)

    by_net: Dict[str, List[Tuple[str, float, float]]] = {}
    for fp in pcb.footprints.values():
        for pd in fp.pads:
            on = ("*.Cu" in pd.layers) or (layer in pd.layers)
            if not on:
                continue
            net = (pd.net_name or "").strip()
            if not net:
                continue
            by_net.setdefault(net, []).append((fp.reference, pd.global_x, pd.global_y))

    def pad_sites(x, y, hw=0.5):
        """Boundary-vertex indices whose obstacle is centred near (x,y)."""
        out = []
        for i, s in enumerate(g.sites):
            if abs(s.x - x) < 3 * hw or abs(s.y - y) < 3 * hw:
                pass  # cheap prefilter only; real match below
        # sites don't carry the pad center, so match by nearest obstacle_id:
        # find the obstacle_id of any site within hw of (x,y), then take all
        # sites sharing that obstacle_id.
        best_oid, best_d = None, 1e9
        for i, s in enumerate(g.sites):
            d = math.hypot(s.x - x, s.y - y)
            if d < best_d:
                best_d, best_oid = d, s.obstacle_id
        return [i for i, s in enumerate(g.sites) if s.obstacle_id == best_oid]

    nets: List[Net] = []
    for name, pads in by_net.items():
        if len(pads) < 2:
            continue
        # farthest pair (matches fixture semantics: one net = one connection)
        best = None
        for i in range(len(pads)):
            for j in range(i + 1, len(pads)):
                d = math.hypot(pads[i][1] - pads[j][1], pads[i][2] - pads[j][2])
                if best is None or d > best[0]:
                    best = (d, pads[i], pads[j])
        _, pa, pb = best
        a_sites = pad_sites(pa[1], pa[2])
        b_sites = pad_sites(pb[1], pb[2])
        if a_sites and b_sites and set(a_sites) != set(b_sites):
            nets.append(Net(name, a_sites, b_sites, (pa[1], pa[2]), (pb[1], pb[2])))
    return nets


def _dijkstra_multi(g: CorridorGraph, sources: List[int], sinks: set,
                    edge_cost) -> Optional[List[int]]:
    """Shortest triangle-path from ANY source triangle to ANY sink triangle.
    edge_cost(tri_a, tri_b, key) -> float. Returns triangle id sequence."""
    dist = {t: 0.0 for t in sources}
    prev: Dict[int, int] = {}
    pq = [(0.0, t) for t in sources]
    heapq.heapify(pq)
    visited = set()
    goal = None
    while pq:
        d, u = heapq.heappop(pq)
        if u in visited:
            continue
        visited.add(u)
        if u in sinks:
            goal = u
            break
        for (v, ka, kb) in g.tri_adj[u]:
            if v in visited:
                continue
            w = edge_cost(u, v, (ka, kb))
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    if goal is None:
        return None
    path = [goal]
    while path[-1] in prev:
        path.append(prev[path[-1]])
    path.reverse()
    return path


def negotiate_core(g, tri_sources: Dict[str, List[int]], tri_sinks: Dict[str, set],
                   max_iterations: int = 60, pres_fac_init: float = 1.0,
                   pres_fac_mult: float = 1.5, hist_factor: float = 0.5,
                   log=lambda s: None):
    """The actual PathFinder loop, decoupled from KiCad/CDT terminal binding:
    takes `g` (anything exposing .edges / .tri_adj / .centroid(ti), i.e. a
    CorridorGraph or a hand-built graph for testing) plus explicit per-net
    source/sink TRIANGLE sets. Iterate ripping up and rerouting ALL nets with
    a rising congestion penalty until no corridor edge is used beyond its lane
    capacity, or max_iterations is hit. Returns (routed, failed, iterations,
    converged) where routed[name] = (tri_path, edge_path, length)."""
    history: Dict[Tuple[int, int], float] = {k: 0.0 for k in g.edges}
    pres_fac = pres_fac_init
    routed: Dict[str, tuple] = {}
    last_overuse = -1
    names = list(tri_sources.keys())

    for it in range(max_iterations):
        usage: Dict[Tuple[int, int], int] = {k: 0 for k in g.edges}

        def edge_cost(u, v, key, _usage=usage):
            e = g.edges[key]
            base = math.hypot(*(a - b for a, b in
                                zip(g.centroid(u), g.centroid(v))))
            over = max(0, _usage[key] + 1 - e.lanes) if e.lanes > 0 else 999
            congestion = (1.0 + history[key]) * (1.0 + pres_fac * over)
            return base * congestion

        routed = {}
        failed = []
        for name in names:
            srcs, snks = tri_sources[name], tri_sinks[name]
            if not srcs or not snks:
                failed.append(name)
                continue
            path = _dijkstra_multi(g, srcs, snks, edge_cost)
            if path is None:
                failed.append(name)
                continue
            edge_keys = []
            length = 0.0
            for a, b in zip(path, path[1:]):
                key = next(k for (v, *k) in
                          [(v, ka, kb) for (v, ka, kb) in g.tri_adj[a] if v == b])
                key = tuple(key)
                edge_keys.append(key)
                usage[key] += 1
                length += math.hypot(*(x - y for x, y in
                                       zip(g.centroid(a), g.centroid(b))))
            routed[name] = (path, edge_keys, length)

        overuse_edges = {k: max(0, usage[k] - g.edges[k].lanes)
                         for k in g.edges if usage[k] > g.edges[k].lanes}
        total_overuse = sum(overuse_edges.values())
        log(f"iter {it}: routed={len(routed)}/{len(names)} "
            f"overused_edges={len(overuse_edges)} total_overuse={total_overuse} "
            f"pres_fac={pres_fac:.2f}")

        if total_overuse == 0 and not failed:
            return routed, failed, it, True

        for k, over in overuse_edges.items():
            history[k] += over * hist_factor
        pres_fac *= pres_fac_mult
        last_overuse = total_overuse

    return routed, failed, max_iterations, False


def negotiate(g: CorridorGraph, nets: List[Net], max_iterations: int = 60,
             pres_fac_init: float = 1.0, pres_fac_mult: float = 1.5,
             hist_factor: float = 0.5, log=lambda s: None) -> NegotiateResult:
    """KiCad-facing wrapper: binds each Net's a_sites/b_sites to their incident
    triangles (multi-source/sink), then calls the graph-agnostic negotiate_core."""
    tri_sources: Dict[str, List[int]] = {}
    tri_sinks: Dict[str, set] = {}
    for n in nets:
        tri_sources[n.name] = sorted({t for si in n.a_sites for t in g.vertex_tris[si]})
        tri_sinks[n.name] = {t for si in n.b_sites for t in g.vertex_tris[si]}

    routed_raw, failed, iterations, converged = negotiate_core(
        g, tri_sources, tri_sinks, max_iterations, pres_fac_init,
        pres_fac_mult, hist_factor, log)

    by_name = {n.name: n for n in nets}
    routed = {name: RoutedNet(by_name[name], path, edge_keys, length)
             for name, (path, edge_keys, length) in routed_raw.items()}
    return NegotiateResult(routed, failed, iterations, converged,
                           0 if converged else 1)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Negotiated-congestion router (increment 2)")
    ap.add_argument("board")
    ap.add_argument("--layer", default="F.Cu")
    ap.add_argument("--min-clearance", type=float, default=0.125)
    ap.add_argument("--max-iterations", type=int, default=60)
    ap.add_argument("--png", default=None)
    args = ap.parse_args()

    pcb = parse_kicad_pcb(args.board)
    fab = Fab(clearance=args.min_clearance)
    g = build_graph(pcb, args.layer, fab)
    nets = nets_from_pcb(pcb, g, args.layer)
    print(f"{len(nets)} two-pin nets on {args.layer}")

    result = negotiate(g, nets, max_iterations=args.max_iterations, log=print)
    print(f"\nconverged={result.converged} iterations={result.iterations} "
          f"routed={len(result.routed)}/{len(nets)} failed={result.failed}")

    if args.png:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(13, 10))
        for tri_v in g.triangles:
            pts = g.vertices[list(tri_v) + [tri_v[0]]]
            ax.plot(pts[:, 0], pts[:, 1], color="#eee", lw=0.3, zorder=1)
        colors = plt.cm.tab20.colors
        for i, (name, rn) in enumerate(result.routed.items()):
            pts = [g.centroid(t) for t in rn.tri_path]
            xs, ys = zip(*pts)
            ax.plot(xs, ys, color=colors[i % len(colors)], lw=1.8, alpha=0.85,
                   marker="o", markersize=2, label=name, zorder=3)
        ax.set_aspect("equal")
        ax.invert_yaxis()
        ax.set_title(f"negotiated routes ({len(result.routed)}/{len(nets)}, "
                     f"converged={result.converged}, {result.iterations} iters)")
        if len(nets) <= 20:
            ax.legend(fontsize=6, loc="upper right")
        plt.tight_layout()
        plt.savefig(args.png, dpi=120)
        print("wrote", args.png)
