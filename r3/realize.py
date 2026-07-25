#!/usr/bin/env python3
"""
router3 core — increment 3: realize negotiated corridor paths to copper.

Turns a RoutedNet (a sequence of triangle-edge crossings, from negotiate.py)
into an exact-geometry polyline: start at the source pad, pass through the
midpoint of each crossed corridor edge (the safest point along that gap —
equidistant from the two obstacles that bound it), end at the destination pad.
Then TAUT-STRING the polyline: greedily shortcut to the farthest waypoint
reachable by a straight, DRC-clear segment (ROUTING-SKILL.md's "safe string
pulling"), re-verified against exact geometry at every step — never a raster
grid, which over-blocks by up to half a cell.

One model of truth: clearance checking here reuses escape_gen.py's
`seg_clear`/`collect_obstacles`/`collect_board_segments` directly rather than
re-deriving a second clearance implementation. Realized copper accumulates
into the same "placed" list other nets' realization checks against, so a
later net's straight line cannot cut through an earlier net's already-realized
copper (nets realize in negotiated order; each net's own crossed-corridor path
already reserved its lanes, so this should rarely reject — it exists as a
correctness backstop, matching the design's "One model of truth" principle).
"""
from __future__ import annotations
import math
import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(__file__))
from corridor import CorridorGraph, Fab, build_graph  # noqa: E402
from negotiate import Net, RoutedNet, negotiate, nets_from_pcb  # noqa: E402

HARNESS = os.path.join(os.path.dirname(__file__), "..", "..", "KiCadRoutingTools")
sys.path.insert(0, os.path.abspath(HARNESS))
from kicad_parser import parse_kicad_pcb  # noqa: E402
from kicad_writer import add_tracks_and_vias_to_pcb  # noqa: E402

ESCAPE_GEN = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(ESCAPE_GEN))
from escape_gen import (collect_obstacles, collect_board_segments,  # noqa: E402
                        seg_clear, Fab as EscapeFab)


Point = Tuple[float, float]


def edge_midpoint(g: CorridorGraph, key: Tuple[int, int]) -> Point:
    a, b = g.vertices[key[0]], g.vertices[key[1]]
    return (float(a[0] + b[0]) / 2.0, float(a[1] + b[1]) / 2.0)


def waypoints_for(g: CorridorGraph, rn: RoutedNet) -> List[Point]:
    pts = [rn.net.a_center]
    for key in rn.edge_path:
        pts.append(edge_midpoint(g, key))
    pts.append(rn.net.b_center)
    # dedupe consecutive near-identical points (can happen at short hops)
    out = [pts[0]]
    for p in pts[1:]:
        if math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > 1e-6:
            out.append(p)
    return out


def taut_string(pts: List[Point], net: str, layer: str, fab, obs, placed, board_segs) -> List[Point]:
    """Greedy string-pulling: from each kept point, jump to the FARTHEST point
    still reachable by a clear straight segment. Every candidate segment is
    checked with exact-geometry seg_clear (escape_gen's proven kernel) — never
    a raster grid."""
    if len(pts) <= 2:
        return pts
    result = [pts[0]]
    i = 0
    while i < len(pts) - 1:
        j = len(pts) - 1
        while j > i + 1:
            if seg_clear(pts[i], pts[j], fab.track, net, layer, obs, placed,
                        board_segs, fab):
                break
            j -= 1
        result.append(pts[j])
        i = j
    return result


def realize_all(g: CorridorGraph, pcb, layer: str, routed: Dict[str, RoutedNet],
                fab: EscapeFab, board_segs):
    """Realize every routed net to copper, in the order given (caller should
    order by e.g. negotiated length or net priority).

    Every RAW hop (pad->edge-midpoint->edge-midpoint->...->pad, BEFORE
    taut-string shortcutting) is validated against exact geometry first. A
    raw hop crossing corridor edge `key` that fails clearance is real physical
    information the topology model got wrong (lane capacity there only checked
    the edge's own 2 endpoint obstacles, not third-party obstacles/copper
    nearby) — per the design ("realization reports back to the negotiator as
    a capacity correction, never a silent drop"), this returns those edge keys
    so the caller can zero their lane capacity and re-negotiate, instead of
    silently dropping copper or emitting an illegal segment.

    Returns (tracks, notes, bad_edges) — bad_edges is a set of corridor edge
    keys that failed raw-hop validation this round."""
    obs = collect_obstacles(pcb, (0, 0), radius=1e18, fab=fab)
    placed: List[tuple] = []       # (x,y,net,r) — realization doesn't add vias
    placed_segs: List[tuple] = list(board_segs)
    tracks: List[dict] = []
    notes: List[str] = []
    bad_edges = set()

    for name, rn in routed.items():
        raw = waypoints_for(g, rn)
        # validate each RAW hop first; a failing hop implicates the corridor
        # edge(s) whose midpoint(s) bound it (waypoints are
        # [pad_a, mid(edge0), mid(edge1), ..., mid(edgeN-1), pad_b])
        ok = True
        for i, (a, b) in enumerate(zip(raw, raw[1:])):
            if math.hypot(b[0] - a[0], b[1] - a[1]) <= 1e-6:
                continue
            if seg_clear(a, b, fab.track, name, layer, obs, placed, placed_segs, fab):
                continue
            ok = False
            for edge_idx in (i - 1, i):
                if 0 <= edge_idx < len(rn.edge_path):
                    bad_edges.add(rn.edge_path[edge_idx])
            notes.append(f"{name}: raw hop {i} ({a}->{b}) fails clearance — "
                        f"corridor edge over-promised capacity there, flagged "
                        f"for correction, net deferred this round")
        if not ok:
            continue    # deferred to next round after the correction

        path = taut_string(raw, name, layer, fab, obs, placed, placed_segs)
        emitted = 0
        for a, b in zip(path, path[1:]):
            if math.hypot(b[0] - a[0], b[1] - a[1]) <= 1e-6:
                continue
            if not seg_clear(a, b, fab.track, name, layer, obs, placed,
                             placed_segs, fab):
                # should not happen (raw hops already validated, and shortcuts
                # are only taken when seg_clear passes) — treat as a bug signal
                notes.append(f"{name}: UNEXPECTED taut-string leg failure "
                            f"({a}->{b}) after raw-hop validation passed")
                ok = False
                continue
            tracks.append(dict(start=a, end=b, width=fab.track, layer=layer,
                               net_id=None, net=name))
            placed_segs.append((a, b, fab.track, name, layer))
            emitted += 1
        notes.append(f"{name}: {len(raw)} waypoints -> {len(path)} after taut-string "
                    f"-> {emitted} segments")
    return tracks, notes, bad_edges


def route_and_realize(g: CorridorGraph, pcb, layer: str, nets: List[Net],
                      fab: EscapeFab, board_segs, max_iterations: int = 60,
                      max_correction_rounds: int = 5, log=lambda s: None):
    """Top-level driver: negotiate, realize, and when realization finds a
    corridor edge the topology model over-promised, zero its lane capacity and
    re-negotiate — up to `max_correction_rounds`. Returns (tracks, notes,
    unrealizable_nets) where unrealizable_nets lists nets that could not be
    realized after all correction rounds (an honest infeasibility report, not
    a silent drop)."""
    remaining = list(nets)
    all_tracks: List[dict] = []
    all_notes: List[str] = []
    board_segs = list(board_segs)

    for round_i in range(max_correction_rounds):
        result = negotiate(g, remaining, max_iterations=max_iterations, log=log)
        if result.failed:
            log(f"correction round {round_i}: negotiate itself could not route "
                f"{result.failed} (no legal topology at all, not a realization issue)")
        ordered = dict(sorted(result.routed.items(), key=lambda kv: kv[1].length))
        tracks, notes, bad_edges = realize_all(g, pcb, layer, ordered, fab, board_segs)
        all_tracks.extend(tracks)
        all_notes.extend(notes)
        realized_names = {t["net"] for t in tracks}
        # accumulate this round's realized copper as an obstacle for the next
        for t in tracks:
            board_segs.append(((t["start"][0], t["start"][1]),
                              (t["end"][0], t["end"][1]), t["width"], t["net"], t["layer"]))
        still_pending = [n for n in remaining if n.name not in realized_names]

        if not bad_edges:
            unrealizable = [n.name for n in still_pending] + list(result.failed)
            return all_tracks, all_notes, unrealizable

        log(f"correction round {round_i}: zeroing lane capacity on "
            f"{len(bad_edges)} over-promised corridor edge(s), re-negotiating "
            f"{len(still_pending)} remaining net(s)")
        for key in bad_edges:
            g.edges[key].lanes = 0
        remaining = still_pending
        if not remaining:
            return all_tracks, all_notes, []

    return all_tracks, all_notes, [n.name for n in remaining]


def write_board(inp: str, outp: str, tracks: List[dict], pcb):
    name2id = {v: k for k, v in pcb.net_id_to_name.items()}
    for d in tracks:
        d["net_id"] = name2id[d["net"]]
    add_tracks_and_vias_to_pcb(inp, outp, tracks=tracks, vias=[],
                              net_id_to_name=pcb.net_id_to_name)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Realize negotiated routes to copper "
                                 "(increment 3)")
    ap.add_argument("board")
    ap.add_argument("output")
    ap.add_argument("--layer", default="F.Cu")
    ap.add_argument("--min-clearance", type=float, default=0.125)
    ap.add_argument("--track-width", type=float, default=0.13)
    ap.add_argument("--max-iterations", type=int, default=60)
    args = ap.parse_args()

    pcb = parse_kicad_pcb(args.board)
    cfab = Fab(clearance=args.min_clearance, track=args.track_width)
    g = build_graph(pcb, args.layer, cfab)
    nets = nets_from_pcb(pcb, g, args.layer)
    print(f"{len(nets)} two-pin nets on {args.layer}")

    efab = EscapeFab(track=args.track_width, clearance=args.min_clearance)
    board_segs = collect_board_segments(pcb, (0, 0), radius=1e18)
    tracks, notes, unrealizable = route_and_realize(
        g, pcb, args.layer, nets, efab, board_segs,
        max_iterations=args.max_iterations, log=print)
    for n in notes:
        print("  " + n)
    print(f"unrealizable nets (honest failure report): {unrealizable or 'none'}")

    write_board(args.board, args.output, tracks, pcb)
    print(f"wrote {args.output}: +{len(tracks)} segments")
