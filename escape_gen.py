#!/usr/bin/env python3
"""
Fine-pitch escape / fanout generator  (router3 component, v0)

Synthesizes DRC-clean escape copper for a dense connector's SIGNAL pins:
  - front row (nearest the board interior): straight stub out on the top layer
  - deeper rows: dive to the bottom layer through a STANDARD via (never sub-spec),
    with alternating depth-stagger so adjacent same-pitch vias never collide,
    then a bottom-layer stub out to a clean fan boundary
Diff pairs are kept together and fanned symmetrically. GND ties and NC pins are
left alone in v0 (GND fine-pitch tie is a separate sub-problem).

The escape ALGORITHM here is ours (clean-room). Only KiCad file I/O is borrowed
from the KiCadRoutingTools harness (kicad_parser / kicad_writer) so v0 moves fast;
it is portable to the Rust router3 core later.

Acceptance = kicad-cli DRC (with the sibling .kicad_pro, zones refilled).
"""
from __future__ import annotations
import argparse
import math
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# --- borrow ONLY file I/O from the harness -----------------------------------
HARNESS = os.path.join(os.path.dirname(__file__), "..", "KiCadRoutingTools")
sys.path.insert(0, os.path.abspath(HARNESS))
from kicad_parser import parse_kicad_pcb          # noqa: E402
from kicad_writer import add_tracks_and_vias_to_pcb  # noqa: E402


# --- hard fab rules (standard tier — NO escalation to sub-spec geometry) ------
@dataclass(frozen=True)
class Fab:
    track: float = 0.13
    clearance: float = 0.125
    via_size: float = 0.45
    via_drill: float = 0.20
    hole2hole: float = 0.20     # edge-to-edge drill spacing floor


POWER_NETS = {"GND", "GNDPWR"}  # v0: not escaped (plane-tied elsewhere)

# search safety margin over the raw fab clearance: KiCad's exact geometry can
# find a hair less than a sampled/analytic gap, so leave headroom.
MARGIN = 0.02


@dataclass
class EscapePin:
    net: str
    x: float
    y: float
    is_front: bool           # front row escapes on top layer without a via
    pair_key: Optional[str]  # diff-pair stem, e.g. ".../DPHY1_D0"
    polarity: Optional[str]  # 'P' / 'N' / None


def _pair_key(net: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (stem, polarity) for a diff-pair net name, else (None, None)."""
    for suf, pol in (("_P", "P"), ("_N", "N")):
        if net.endswith(suf):
            return net[: -len(suf)], pol
    return None, None


@dataclass
class Obstacle:
    x: float
    y: float
    hw: float            # half-width  of copper rect (0 for a via)
    hh: float            # half-height of copper rect (0 for a via)
    r: float             # extra radius (via copper radius; 0 for a pad rect)
    net: str             # net name ("" = no net / treat as foreign)
    layers: frozenset    # copper layers this copper is on; {"*"} = all layers
    drill: float = 0.0   # drill diameter (for hole-to-hole checks; 0 = no hole)

    def surface_gap(self, px: float, py: float) -> float:
        """Signed distance from point (px,py) to this obstacle's copper edge."""
        dx = max(abs(px - self.x) - self.hw, 0.0)
        dy = max(abs(py - self.y) - self.hh, 0.0)
        return math.hypot(dx, dy) - self.r

    def on(self, layer: str) -> bool:
        return "*" in self.layers or layer in self.layers


def _copper_layers(layers) -> frozenset:
    out = set()
    for l in layers:
        if l == "*.Cu":
            return frozenset({"*"})
        if l.endswith(".Cu"):
            out.add(l)
    return frozenset(out) if out else frozenset({"*"})


def collect_board_segments(pcb, center, radius) -> List[tuple]:
    """Existing track segments near `center`, in the (a,b,width,net,layer) shape
    `seg_clear` expects for `placed_segs` — so new copper is checked against
    copper THIS RUN didn't place, not just pads/vias. Missing this was a real
    bug: a power-tap stub drove straight through an already-routed CAN/STBY
    track because collect_obstacles only modeled pads and vias."""
    cx, cy = center
    out = []
    for s in pcb.segments:
        if abs(s.start_x - cx) > radius and abs(s.end_x - cx) > radius:
            continue
        if abs(s.start_y - cy) > radius and abs(s.end_y - cy) > radius:
            continue
        net = pcb.net_id_to_name.get(s.net_id, "")
        out.append(((s.start_x, s.start_y), (s.end_x, s.end_y), s.width, net, s.layer))
    return out


def collect_obstacles(pcb, center, radius, fab: Fab) -> List[Obstacle]:
    """Existing vias (all-layer circles) + all pads (axis-aligned rects, on their
    own copper layers) near the connector."""
    cx, cy = center
    obs: List[Obstacle] = []
    for v in pcb.vias:
        if abs(v.x - cx) < radius and abs(v.y - cy) < radius:
            obs.append(Obstacle(v.x, v.y, 0.0, 0.0, v.size / 2.0,
                                pcb.net_id_to_name.get(v.net_id, ""), frozenset({"*"}),
                                drill=v.drill))
    for fp in pcb.footprints.values():
        for pd in fp.pads:
            if abs(pd.global_x - cx) < radius and abs(pd.global_y - cy) < radius:
                obs.append(Obstacle(pd.global_x, pd.global_y,
                                    pd.size_x / 2.0, pd.size_y / 2.0, 0.0,
                                    (pd.net_name or "").strip(), _copper_layers(pd.layers),
                                    drill=pd.drill))
    return obs


# --- segment geometry -------------------------------------------------------
def _pt_seg_dist(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _ccw(ax, ay, bx, by, cx, cy) -> float:
    return (cy - ay) * (bx - ax) - (by - ay) * (cx - ax)


def _seg_seg_dist(a, b, c, d) -> float:
    d1 = _ccw(c[0], c[1], d[0], d[1], a[0], a[1])
    d2 = _ccw(c[0], c[1], d[0], d[1], b[0], b[1])
    d3 = _ccw(a[0], a[1], b[0], b[1], c[0], c[1])
    d4 = _ccw(a[0], a[1], b[0], b[1], d[0], d[1])
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return 0.0  # cross
    return min(_pt_seg_dist(a[0], a[1], c[0], c[1], d[0], d[1]),
               _pt_seg_dist(b[0], b[1], c[0], c[1], d[0], d[1]),
               _pt_seg_dist(c[0], c[1], a[0], a[1], b[0], b[1]),
               _pt_seg_dist(d[0], d[1], a[0], a[1], b[0], b[1]))


def seg_clear(a, b, width, net, layer, obs, placed_vias, placed_segs, fab, step=0.04) -> bool:
    """True if a track a->b of `width` on `net`/`layer` clears every foreign
    obstacle on that layer, every placed escape via, and every placed escape
    segment on that layer, by the fab clearance."""
    hw = width / 2.0
    L = math.hypot(b[0] - a[0], b[1] - a[1])
    n = max(1, int(L / step))
    lobs = [o for o in obs if o.on(layer)]
    for i in range(n + 1):
        t = i / n
        px, py = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
        for o in lobs:
            if o.net and o.net == net:
                continue
            if o.surface_gap(px, py) - hw < fab.clearance + MARGIN:
                return False
    for (vx, vy, vnet, vr) in placed_vias:          # vias span all layers
        if vnet == net:
            continue
        if _pt_seg_dist(vx, vy, a[0], a[1], b[0], b[1]) - hw - vr < fab.clearance + MARGIN:
            return False
    for (c, d, cw, cnet, clayer) in placed_segs:
        if cnet == net or clayer != layer:
            continue
        if _seg_seg_dist(a, b, c, d) - hw - cw / 2.0 < fab.clearance + MARGIN:
            return False
    return True


def clears(x, y, via_r, net, obs: List[Obstacle], placed, fab: Fab, segs=()) -> bool:
    """True if a via of radius via_r on `net` at (x,y) clears all obstacles,
    previously-placed escape vias, and existing board segments (any layer the
    via spans) by the fab clearance. Same-net copper may touch (via-in-pad is
    legal)."""
    for (sa, sb, sw, snet, _slayer) in segs:
        if snet == net:
            continue
        if _pt_seg_dist(x, y, sa[0], sa[1], sb[0], sb[1]) - via_r - sw / 2.0 \
                < fab.clearance + MARGIN:
            return False
    for o in obs:
        # hole-to-hole applies regardless of net (drill spacing is physical)
        if o.drill > 0.0:
            if math.hypot(x - o.x, y - o.y) - fab.via_drill / 2.0 - o.drill / 2.0 \
                    < fab.hole2hole + MARGIN:
                return False
        if o.net and o.net == net:
            continue
        if o.surface_gap(x, y) - via_r < fab.clearance + MARGIN:
            return False
    for (px, py, pnet, pr) in placed:
        need = via_r + pr + (0.0 if pnet == net else fab.clearance)
        if math.hypot(x - px, y - py) < need + MARGIN:
            return False
    return True


def analyze(pcb, ref: str) -> Tuple[List[EscapePin], Tuple[float, float], Tuple[float, float]]:
    """Return (signal pins, escape unit direction, connector centroid)."""
    fp = pcb.footprints.get(ref)
    if fp is None:
        sys.exit(f"footprint {ref} not found")
    pads = [pd for pd in fp.pads if pd.drill == 0.0]  # SMD signal pads only
    if not pads:
        sys.exit(f"{ref}: no SMD pads")

    cx = sum(pd.global_x for pd in pads) / len(pads)
    cy = sum(pd.global_y for pd in pads) / len(pads)
    # escape direction = from footprint origin toward the pad field, normalized
    dx, dy = cx - fp.x, cy - fp.y
    n = math.hypot(dx, dy) or 1.0
    edir = (dx / n, dy / n)

    # project each pad onto the escape axis; group into rows by projection.
    def proj(pd):
        return (pd.global_x - cx) * edir[0] + (pd.global_y - cy) * edir[1]

    projs = sorted(proj(pd) for pd in pads)
    # LARGEST projection along the escape direction = interior-most row = the
    # "front" row that can escape straight out on the top layer without a via.
    front_proj = projs[-1]
    row_tol = 0.35

    pins: List[EscapePin] = []
    for pd in pads:
        net = (pd.net_name or "").strip()
        if not net or net in POWER_NETS:
            continue  # NC or GND: skipped in v0
        stem, pol = _pair_key(net)
        pins.append(
            EscapePin(
                net=net,
                x=pd.global_x,
                y=pd.global_y,
                is_front=abs(proj(pd) - front_proj) <= row_tol,
                pair_key=stem,
                polarity=pol,
            )
        )
    return pins, edir, (cx, cy)


def plan(
    pins: List[EscapePin],
    edir: Tuple[float, float],
    obs: List[Obstacle],
    fab: Fab,
    fan_len: float,
    fan_spread: float,
    board_segs=(),
) -> Tuple[List[dict], List[dict], List[str]]:
    """Emit tracks + vias. edir points OUTWARD = toward the board interior."""
    ex, ey = edir
    px, py = -ey, ex          # perpendicular (lateral) axis
    tracks: List[dict] = []
    vias: List[dict] = []
    notes: List[str] = []
    placed: List[tuple] = []  # (x,y,net,r) escape vias placed so far
    via_r = fab.via_size / 2.0

    def out(x, y, d):            # move along escape axis (+d = toward interior)
        return (x + ex * d, y + ey * d)

    def lat(x, y, off):          # move along lateral axis
        return (x + px * off, y + py * off)

    # lateral (perpendicular) coordinate of a point, connector-relative
    cx = sum(p.x for p in pins) / len(pins)
    cy = sum(p.y for p in pins) / len(pins)

    def along(x, y):
        return (x - cx) * ex + (y - cy) * ey

    def latc(x, y):
        return (x - cx) * px + (y - cy) * py

    # GND stitching vias that sit AHEAD of the back row (the front-row ones that
    # block the escape lanes): collect their lateral positions.
    back_along = min(along(p.x, p.y) for p in pins if not p.is_front) if any(
        not p.is_front for p in pins) else 0.0
    front_gnd_lats = sorted(
        latc(o.x, o.y) for o in obs
        if o.net in POWER_NETS and o.r > 0 and along(o.x, o.y) > back_along + 0.4
    )

    gnd_lats = [latc(o.x, o.y) for o in obs if o.net in POWER_NETS and o.r > 0]

    def away_sign(pin: EscapePin):
        """+1 / -1: the lateral direction AWAY from the nearest GND obstacle."""
        base = latc(pin.x, pin.y)
        near = min(gnd_lats, key=lambda g: abs(g - base), default=base)
        return 1.0 if base >= near else -1.0

    placed_segs: List[tuple] = list(board_segs)   # (a, b, width, net, layer)

    def commit_seg(a, b, layer, net):
        if math.hypot(b[0] - a[0], b[1] - a[1]) <= 1e-6:
            return
        tracks.append(dict(start=a, end=b, width=fab.track, layer=layer,
                           net_id=None, net=net))
        placed_segs.append((a, b, fab.track, net, layer))

    def try_back(pin: EscapePin):
        """Search (via depth, lateral nudge) x (B.Cu jog) for a placement whose
        ENTIRE geometry — via, F.Cu stub, and B.Cu legs — is DRC-clear. Returns
        (via_xy, stub, legs) or None."""
        s = away_sign(pin)
        nudges = (0.0, s * 0.12, s * 0.24, -s * 0.12, -s * 0.24)
        jogs = (0.0, s * 0.14, s * 0.2, s * 0.28, s * 0.34,
                -s * 0.14, -s * 0.2, -s * 0.28, -s * 0.34)
        for depth in [d * 0.1 for d in range(0, 22)]:
            for nud in nudges:
                vxy = lat(*out(pin.x, pin.y, -depth), nud)
                if not clears(vxy[0], vxy[1], via_r, pin.net, obs, placed, fab,
                             segs=placed_segs):
                    continue
                stub = ((pin.x, pin.y), vxy)
                if not seg_clear(*stub, fab.track, pin.net, "F.Cu",
                                 obs, placed, placed_segs, fab):
                    continue
                for jog in jogs:
                    w1 = lat(*out(pin.x, pin.y, 0.4), 0.0)
                    w2 = lat(*out(pin.x, pin.y, 0.9), jog)
                    ex_ = lat(*out(pin.x, pin.y, fan_len), jog)
                    legs = [(vxy, w1), (w1, w2), (w2, ex_)]
                    if all(seg_clear(a, b, fab.track, pin.net, "B.Cu",
                                     obs, placed, placed_segs, fab)
                           for a, b in legs if math.hypot(b[0]-a[0], b[1]-a[1]) > 1e-6):
                        return vxy, stub, legs
        return None

    failed = []
    # front rows first (their stubs become obstacles the back-row legs respect)
    for p in sorted(pins, key=lambda q: (not q.is_front,)):
        net = p.net
        if p.is_front:
            end = out(p.x, p.y, fan_len)
            if seg_clear((p.x, p.y), end, fab.track, net, "F.Cu",
                         obs, placed, placed_segs, fab):
                commit_seg((p.x, p.y), end, "F.Cu", net)
            else:
                failed.append(net)
            continue
        res = try_back(p)
        if res is None:
            failed.append(net)
            continue
        vxy, stub, legs = res
        placed.append((vxy[0], vxy[1], net, via_r))
        vias.append(dict(x=vxy[0], y=vxy[1], size=fab.via_size, drill=fab.via_drill,
                         layers=["F.Cu", "B.Cu"], net_id=None, net=net))
        commit_seg(*stub, "F.Cu", net)
        for a, b in legs:
            commit_seg(a, b, "B.Cu", net)

    back = [p for p in pins if not p.is_front]
    notes.append(f"{len(pins)} signal pins ({len(back)} via-diving); "
                 f"placed {len(placed)} vias; failed={failed or 'none'}")
    return tracks, vias, notes


def power_taps(pcb, specs, fab: Fab, search_radius: float = 6.0):
    """Standard-via plane taps for fine-pitch power pads (REF.PAD list). For each
    pad, try a via-in-pad, else spiral outward to the first DRC-clear standard-via
    site, connecting pad->via with a short (possibly 1-bend) F.Cu path. The
    through via crosses the inner planes, tying the pad to its plane on refill.
    NEVER sub-spec. Obstacles/segments are collected PER PAD within
    `search_radius` — power taps are local, so this keeps the search fast
    (collecting the whole board's copper for every candidate site does not scale)."""
    tracks: List[dict] = []
    vias: List[dict] = []
    placed: List[tuple] = []
    via_r = fab.via_size / 2.0
    done, failed = [], []

    def find_path(px, py, vx, vy, net, all_segs):
        """pad -> via path: straight stub, else a 1-bend waypoint route (bend
        near the pad, perpendicular to the pad->via direction) so a via that is
        legal but not in straight line-of-sight (boxed by a sibling pin's own
        escape) is still reachable. Returns a list of (a,b) legs, or None."""
        if seg_clear((px, py), (vx, vy), fab.track, net, "F.Cu",
                     obs, placed, all_segs, fab):
            return [((px, py), (vx, vy))]
        dx, dy = vx - px, vy - py
        L = math.hypot(dx, dy) or 1.0
        ux, uy = dx / L, dy / L          # unit along pad->via
        nx, ny = -uy, ux                 # unit perpendicular
        for bend_frac in (0.3, 0.5, 0.7):
            bx, by = px + dx * bend_frac, py + dy * bend_frac
            for off in (0.3, -0.3, 0.6, -0.6, 0.9, -0.9, 1.2, -1.2):
                wx, wy = bx + nx * off, by + ny * off
                leg1, leg2 = (px, py), (wx, wy)
                leg3 = (vx, vy)
                if seg_clear(leg1, leg2, fab.track, net, "F.Cu", obs, placed, all_segs, fab) and \
                   seg_clear(leg2, leg3, fab.track, net, "F.Cu", obs, placed, all_segs, fab):
                    return [(leg1, leg2), (leg2, leg3)]
        return None

    for ref, padnum in specs:
        fp = pcb.footprints.get(ref)
        pd = next((p for p in fp.pads if p.pad_number == padnum), None) if fp else None
        if pd is None:
            failed.append(f"{ref}.{padnum} (pad not found)")
            continue
        net = (pd.net_name or "").strip()
        px, py = pd.global_x, pd.global_y
        obs = collect_obstacles(pcb, (px, py), radius=search_radius, fab=fab)
        board_segs = collect_board_segments(pcb, (px, py), radius=search_radius)
        site = None
        path = None
        for r in [0.0] + [0.1 * k for k in range(3, 45)]:   # via-in-pad, then rings
            steps = 1 if r == 0 else max(8, int(2 * math.pi * r / 0.1))
            for i in range(steps):
                ang = 2 * math.pi * i / steps
                x, y = px + r * math.cos(ang), py + r * math.sin(ang)
                if not clears(x, y, via_r, net, obs, placed, fab, segs=board_segs):
                    continue
                if r == 0.0:
                    site, path = (x, y), [((px, py), (x, y))]
                    break
                p = find_path(px, py, x, y, net, list(board_segs))
                if p is not None:
                    site, path = (x, y), p
                    break
            if site:
                break
        if site is None:
            failed.append(f"{ref}.{padnum}")
            continue
        vx, vy = site
        placed.append((vx, vy, net, via_r))
        vias.append(dict(x=vx, y=vy, size=fab.via_size, drill=fab.via_drill,
                         layers=["F.Cu", "B.Cu"], net_id=None, net=net))
        for a, b in path:
            if math.hypot(b[0] - a[0], b[1] - a[1]) > 1e-6:
                tracks.append(dict(start=a, end=b, width=fab.track,
                                   layer="F.Cu", net_id=None, net=net))
        done.append(f"{ref}.{padnum}")
    return tracks, vias, done, failed


def write_board(inp: str, outp: str, tracks, vias, net_id_to_name):
    add_tracks_and_vias_to_pcb(inp, outp, tracks=tracks, vias=vias,
                               net_id_to_name=net_id_to_name)


def refill_and_drc(pcb_path: str, pro_src: str, kicad_py: str, refill_py: str,
                   center: Tuple[float, float], box: float) -> Tuple[int, dict]:
    """Refill zones, run kicad-cli DRC, return (violations_in_box, breakdown)."""
    base = pcb_path[:-10]
    filled = base + "_filled.kicad_pcb"
    # sibling .pro is the oracle
    import shutil
    shutil.copy(pro_src, filled[:-10] + ".kicad_pro")
    cx, cy = center
    subprocess.run([kicad_py, refill_py, pcb_path, filled,
                    str(cx), str(cy), str(box * 2)],
                   check=True, capture_output=True)
    rpt = base + ".rpt"
    subprocess.run(["kicad-cli", "pcb", "drc", filled, "-o", rpt],
                   capture_output=True)
    import re
    txt = open(rpt).read()
    inbox = 0
    breakdown: dict = {}
    for blk in re.split(r"\n(?=\[)", txt):
        m = re.match(r"\[([a-z_]+)\]", blk)
        if not m:
            continue
        cat = m.group(1)
        coords = re.findall(r"@\(([\d.]+) mm, ([\d.]+) mm\)", blk)
        near = any(abs(float(x) - cx) < box and abs(float(y) - cy) < box
                   for x, y in coords)
        if near:
            inbox += 1
            breakdown[cat] = breakdown.get(cat, 0) + 1
    return inbox, breakdown


def main():
    ap = argparse.ArgumentParser(description="Fine-pitch escape generator (v0)")
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--ref", help="connector footprint ref, e.g. J1701")
    ap.add_argument("--power-taps", default=None,
                    help="comma list of REF.PAD power pads to tap to plane with "
                         "standard vias (mode: single-pad plane taps, not a connector escape)")
    ap.add_argument("--fan-len", type=float, default=3.0)
    ap.add_argument("--fan-spread", type=float, default=0.25)
    ap.add_argument("--pro", default=None, help="oracle .kicad_pro (default: sibling of input)")
    ap.add_argument("--validate", action="store_true")
    args = ap.parse_args()

    pcb = parse_kicad_pcb(args.input)
    fab = Fab()

    if args.power_taps:
        specs = []
        for tok in args.power_taps.split(","):
            tok = tok.strip()
            if not tok:
                continue
            ref, _, pad = tok.partition(".")
            specs.append((ref, pad))
        tracks, vias, done, failed = power_taps(pcb, specs, fab)
        print(f"  power taps: {len(done)} placed, failed={failed or 'none'}")
        name2id = {v: k for k, v in pcb.net_id_to_name.items()}
        for d in tracks + vias:
            d["net_id"] = name2id[d["net"]]
        write_board(args.input, args.output, tracks, vias, pcb.net_id_to_name)
        print(f"wrote {args.output}: +{len(tracks)} tracks +{len(vias)} vias")
        return

    if not args.ref:
        sys.exit("need --ref (connector escape) or --power-taps")
    pins, edir, center = analyze(pcb, args.ref)
    obs = collect_obstacles(pcb, center, radius=6.0, fab=fab)
    board_segs = collect_board_segments(pcb, center, radius=6.0)
    tracks, vias, notes = plan(pins, edir, obs, fab, args.fan_len, args.fan_spread,
                              board_segs=board_segs)
    for n in notes:
        print("  " + n)
    # resolve net NAME -> the board's net_id (writer keys on net_id; a missing
    # id would silently emit net 0 and short across everything)
    name2id = {v: k for k, v in pcb.net_id_to_name.items()}
    for d in tracks + vias:
        nid = name2id.get(d["net"])
        if nid is None:
            sys.exit(f"net '{d['net']}' not found in board net table")
        d["net_id"] = nid
    write_board(args.input, args.output, tracks, vias, pcb.net_id_to_name)
    print(f"wrote {args.output}: +{len(tracks)} tracks +{len(vias)} vias")

    if args.validate:
        pro = args.pro or (args.input[:-10] + ".kicad_pro")
        here = os.path.dirname(os.path.abspath(__file__))
        refill_py = os.path.join(here, "refill_zones.py")
        kicad_py = os.environ.get("KICAD_PYTHON", "")
        if not kicad_py:
            print("  (set KICAD_PYTHON to KiCad's own python3 — the one with "
                 "the pcbnew module — to validate, e.g. on macOS:\n"
                 "  /Applications/KiCad/KiCad.app/Contents/Frameworks/"
                 "Python.framework/Versions/Current/bin/python3)")
            return
        inbox, bd = refill_and_drc(args.output, pro, kicad_py, refill_py, center, box=8.0)
        print(f"  DRC violations within 8mm of {args.ref}: {inbox}  {bd}")


if __name__ == "__main__":
    main()
