#!/usr/bin/env python3
"""
router3 core — the topological substrate (increment 1, v2: real CDT).

Builds the free-space CORRIDOR GRAPH of a board layer via a proper
CONSTRAINED Delaunay triangulation (Shewchuk's Triangle, via the `triangle`
package): every pad and via is a literal HOLE in the mesh (its boundary is a
forced constraint, its interior is punched out), so a corridor edge can never
cut through solid copper — no same-obstacle-blocking hack needed. This is the
graph the negotiated-congestion router (next increment) searches: nets are
sequences of triangles; crossing an edge consumes a lane; congestion is a
number on an edge.

Design notes (from hardware/carrier/ROUTING-SKILL.md):
  - Exact geometry only. Every site sits exactly ON a copper boundary (pad
    corner, via-circle sample, outline sample) — never a rasterized cell.
  - Effective clearance is the board's min_clearance, measured — not a
    netclass guess. The caller passes it in.

Only KiCad file I/O is borrowed from the KiCadRoutingTools harness; the
algorithm is ours. Prototype in Python; the hot search loop ports to Rust
once proven (malakaRouter2's Rust core already uses a real CDT via `spade`
for the same reason — cross-validates this approach).
"""
from __future__ import annotations
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import triangle as tr

HARNESS = os.path.join(os.path.dirname(__file__), "..", "..", "KiCadRoutingTools")
sys.path.insert(0, os.path.abspath(HARNESS))
from kicad_parser import parse_kicad_pcb  # noqa: E402

ESCAPE_GEN = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(ESCAPE_GEN))
from escape_gen import MARGIN  # noqa: E402
# Lane capacity must use the SAME safety margin the exact-geometry realizer
# (escape_gen.seg_clear) enforces — otherwise topology can claim "1 lane fits"
# at a boundary case realization then rejects, a real topology/geometry
# mismatch this project's "one model of truth" principle exists to prevent.


@dataclass(frozen=True)
class Fab:
    track: float = 0.13
    clearance: float = 0.125
    via_size: float = 0.45
    via_drill: float = 0.20

    @property
    def pitch(self) -> float:
        """Center-to-center spacing of two adjacent routed tracks."""
        return self.track + self.clearance


@dataclass
class Site:
    """A point exactly ON a copper (or board-edge) boundary."""
    x: float
    y: float
    obstacle_id: int      # which pad/via/outline this boundary belongs to
    net: str              # net of the owning copper ("" = none / outline)


@dataclass
class CorridorEdge:
    a: int
    b: int
    width: float          # usable gap = raw distance between two boundary sites
    lanes: int            # capacity for this fab pitch


@dataclass
class CorridorGraph:
    sites: List[Site]
    vertices: np.ndarray             # (N,2) — from `triangle`'s output (incl. any Steiner pts)
    triangles: np.ndarray            # (M,3) vertex indices
    neighbors: np.ndarray            # (M,3) neighbor triangle per opposite-edge, -1 = none
    edges: Dict[Tuple[int, int], CorridorEdge]
    tri_adj: List[List[Tuple[int, int, int]]]   # per triangle: (neighbor_tri, edge_a, edge_b)
    vertex_tris: List[List[int]]     # per ORIGINAL site index: incident triangle ids
    fab: Fab

    def centroid(self, ti: int) -> Tuple[float, float]:
        vs = self.vertices[self.triangles[ti]]
        return float(vs[:, 0].mean()), float(vs[:, 1].mean())

    def stats(self) -> dict:
        lanes = [e.lanes for e in self.edges.values()]
        return {
            "sites": len(self.sites),
            "mesh_vertices": len(self.vertices),
            "triangles": len(self.triangles),
            "corridor_edges": len(self.edges),
            "passable_edges": sum(1 for l in lanes if l >= 1),
        }


def _rect_loop(cx, cy, hw, hh):
    """Closed 4-point loop for a pad rectangle boundary, in order."""
    return [(cx - hw, cy - hh), (cx + hw, cy - hh),
            (cx + hw, cy + hh), (cx - hw, cy + hh)]


def _circle_loop(cx, cy, r, n=16):
    return [(cx + r * math.cos(2 * math.pi * k / n),
             cy + r * math.sin(2 * math.pi * k / n)) for k in range(n)]


def build_sites_and_pslg(pcb, layer: str, fab: Fab):
    """Return (sites, vertices_array, segments_array, holes_array) — the PSLG
    (planar straight-line graph) `triangle` needs: every pad/via on this layer
    becomes a closed boundary loop (forced segments) + one interior hole point;
    the board outline becomes the outer boundary loop (no hole — it IS the
    domain)."""
    sites: List[Site] = []
    segs: List[Tuple[int, int]] = []
    holes: List[Tuple[float, float]] = []
    oid = 0

    def add_loop(pts, obstacle_id, net, is_hole):
        start = len(sites)
        for (x, y) in pts:
            sites.append(Site(x, y, obstacle_id, net))
        n = len(pts)
        for k in range(n):
            segs.append((start + k, start + (k + 1) % n))
        if is_hole:
            # any interior point works; centroid of a convex loop is safe here
            cx = sum(p[0] for p in pts) / n
            cy = sum(p[1] for p in pts) / n
            holes.append((cx, cy))

    for fp in pcb.footprints.values():
        for pd in fp.pads:
            on = ("*.Cu" in pd.layers) or (layer in pd.layers)
            if not on:
                continue
            net = (pd.net_name or "").strip()
            hw, hh = max(pd.size_x / 2.0, 0.01), max(pd.size_y / 2.0, 0.01)
            add_loop(_rect_loop(pd.global_x, pd.global_y, hw, hh), oid, net, is_hole=True)
            oid += 1
    for v in pcb.vias:
        net = pcb.net_id_to_name.get(v.net_id, "")
        add_loop(_circle_loop(v.x, v.y, v.size / 2.0), oid, net, is_hole=True)
        oid += 1

    outline = pcb.board_info.board_outline or []
    if not outline:
        # fixtures with no parsed Edge.Cuts polygon: fall back to the bbox,
        # inflated slightly so pads flush with the nominal edge stay enclosed.
        x0, y0, x1, y1 = pcb.board_info.board_bounds
        m = 1.0
        outline = [(x0 - m, y0 - m), (x1 + m, y0 - m), (x1 + m, y1 + m), (x0 - m, y1 + m)]
    pts = []
    for i in range(len(outline)):
        x0, y0 = outline[i]
        x1, y1 = outline[(i + 1) % len(outline)]
        seglen = math.hypot(x1 - x0, y1 - y0)
        n = max(1, int(seglen / 2.0))
        for k in range(n):
            t = k / n
            pts.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    add_loop(pts, oid, "", is_hole=False)  # outer boundary: no hole, it's the domain edge
    oid += 1

    verts = np.array([(s.x, s.y) for s in sites])
    segs_arr = np.array(segs, dtype=np.int32)
    holes_arr = np.array(holes) if holes else np.zeros((0, 2))
    return sites, verts, segs_arr, holes_arr


def build_graph(pcb, layer: str, fab: Fab) -> CorridorGraph:
    sites, verts, segs, holes = build_sites_and_pslg(pcb, layer, fab)
    pslg = {"vertices": verts, "segments": segs}
    if len(holes):
        pslg["holes"] = holes
    out = tr.triangulate(pslg, "pn")   # p=PSLG (respects segments), n=neighbor list

    mesh_v = out["vertices"]
    tris = out["triangles"]
    neigh = out.get("neighbors")
    if neigh is None:
        raise RuntimeError("triangle did not return a neighbor list ('n' flag)")

    # map an ORIGINAL input vertex index -> its (possibly renumbered) mesh index.
    # `triangle` may append Steiner points but keeps input vertices at the front
    # in the same order when no refinement flags are used ('p' alone doesn't
    # add points), so identity mapping holds for our flags. Verify defensively.
    n_in = len(sites)
    if len(mesh_v) < n_in or not np.allclose(mesh_v[:n_in], verts, atol=1e-6):
        raise RuntimeError("triangle renumbered/added input vertices unexpectedly; "
                           "site<->vertex mapping would be wrong")

    vertex_tris: List[List[int]] = [[] for _ in range(n_in)]
    for ti, tri_v in enumerate(tris):
        for vi in tri_v:
            if vi < n_in:
                vertex_tris[vi].append(ti)

    edges: Dict[Tuple[int, int], CorridorEdge] = {}
    tri_adj: List[List[Tuple[int, int, int]]] = [[] for _ in range(len(tris))]
    for ti, tri_v in enumerate(tris):
        for k in range(3):
            nb = int(neigh[ti][k])
            if nb <= ti:      # -1 (no neighbor) or already visited from the other side
                continue
            # edge opposite vertex k = the other two vertices
            a, b = int(tri_v[(k + 1) % 3]), int(tri_v[(k + 2) % 3])
            key = (a, b) if a < b else (b, a)
            if key not in edges:
                width = math.hypot(mesh_v[a][0] - mesh_v[b][0], mesh_v[a][1] - mesh_v[b][1])
                eff_clearance = fab.clearance + MARGIN
                usable = max(0.0, width - eff_clearance)
                lanes = int(usable // (fab.track + eff_clearance))
                edges[key] = CorridorEdge(key[0], key[1], width, lanes)
            tri_adj[ti].append((nb, key[0], key[1]))
            tri_adj[nb].append((ti, key[0], key[1]))

    return CorridorGraph(sites, mesh_v, tris, neigh, edges, tri_adj, vertex_tris, fab)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Build + inspect the corridor graph (real CDT)")
    ap.add_argument("board")
    ap.add_argument("--layer", default="F.Cu")
    ap.add_argument("--png", default=None)
    ap.add_argument("--min-clearance", type=float, default=0.125)
    args = ap.parse_args()

    pcb = parse_kicad_pcb(args.board)
    fab = Fab(clearance=args.min_clearance)
    g = build_graph(pcb, args.layer, fab)
    print(f"layer {args.layer}:", g.stats())

    if args.png:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(13, 10))
        # draw the mesh triangles lightly
        for tri_v in g.triangles:
            pts = g.vertices[list(tri_v) + [tri_v[0]]]
            ax.plot(pts[:, 0], pts[:, 1], color="#ccc", lw=0.3, zorder=1)
        for key, e in g.edges.items():
            va, vb = g.vertices[e.a], g.vertices[e.b]
            col = "#2a6" if e.lanes >= 1 else "#c33"
            lw = 0.4 + 0.25 * min(e.lanes, 6)
            ax.plot([va[0], vb[0]], [va[1], vb[1]], color=col, lw=lw, alpha=0.6, zorder=2)
        ax.set_aspect("equal")
        ax.invert_yaxis()
        ax.set_title(f"corridor graph (real CDT, holes) {args.layer}: green=passable "
                     f"red=blocked — {g.stats()['passable_edges']} corridors")
        plt.tight_layout()
        plt.savefig(args.png, dpi=120)
        print("wrote", args.png)
