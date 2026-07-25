# malakaRouter3

**A fine-pitch escape/fanout generator for KiCad, built to close the exact gap
that stalls automated routing on very dense connectors and packages: getting
every pin out to open copper using *only standard-spec vias* — no via-in-pad,
no sub-spec drill/annular escalation — even when a straight-line escape is
blocked by neighboring copper.**

It is not a full autorouter. It solves one specific, hard sub-problem — the
fine-pitch escape — and is designed to be the first stage of a pipeline in
front of a general-purpose router. This repo pairs with
[KiCadRoutingTools](https://github.com/drandyhaas/KiCadRoutingTools), which
handles everything *around* that sub-problem: bulk point-to-point routing,
BGA/QFN fanout, plane/zone repair, and DRC-oracle validation. Together they
route boards that neither tool closes alone.

## Why this exists

Modern boards increasingly carry WLCSP, micro-BGA, and dense micro-connector
packages at 0.4 mm pitch or tighter, with 0.2 mm pads. At that scale, a
standard 0.45 mm via often cannot physically fit under or between the pads —
so most automated fanout tools (including KiCadRoutingTools' own
`bga_fanout.py`) fall back to a smaller, sub-spec via (e.g. 0.25 mm/0.15 mm
drill) with a warning, because *some* legal via site genuinely doesn't exist
right at the pad.

But "no legal via site at the pad" and "no legal via site nearby" are
different claims. Very often a pin that's boxed in at zero offset has a
completely legal standard-via site 0.3–1.5 mm away, reachable by a short jog
around whatever's in the way (a neighboring pad, another pin's own escape
stub, a stitching via). `escape_gen.py` searches for exactly that: it walks
outward from each pin, in rings and increasing radii, checking every
candidate site against every real obstacle nearby (not just the pin's own
footprint — existing traces, vias, and other pads within range), and only
emits a via where the standard geometry actually verifies clean. When it
can't find one, it says so explicitly rather than silently degrading to a
smaller via or leaving the pin abandoned.

This was built and proven closing out real, otherwise-stuck escapes on a
complex CM5-based carrier board carrying multiple 0.4 mm-pitch WLCSP sensor
packages and micro-connectors — cases where the fine-pitch escape was the last
blocker standing between a fully-populated board and full autorouting.

## What's in this repo

| File | Purpose |
|---|---|
| `escape_gen.py` | The escape generator. Two modes: connector escape (`--ref`) and single-pad power/plane taps (`--power-taps`). |
| `refill_zones.py` | Scoped zone refill (only zones near a point) — avoids the whack-a-mole a full-board refill can trigger on a large, densely-zoned board. |
| `cleanup_dangling.py` | DRC-driven removal of dangling stubs/vias and redundant stitching vias, iterated against `kicad-cli` until stable. |
| `examples/gen_demo_board.py` | Generates a small synthetic demo board (below) — no real project data. |
| `examples/demo_board.kicad_pcb` | The pre-generated demo board, ready to route. |

`r3/` contains an in-progress, experimental topological-routing research
prototype (constrained-Delaunay corridor graph + negotiated congestion). It is
**not** part of the proven workflow this README documents and is validated
only on tiny synthetic fixtures so far — see its own docstrings if you're
curious, but don't expect it to route anything real yet.

## Requirements

- **KiCad 9 or 10** with `kicad-cli` on `PATH` (the DRC oracle — every step
  below is validated against it, never a heuristic).
- **KiCad's own Python** (has the `pcbnew` module) for `refill_zones.py` and
  `examples/gen_demo_board.py`. On macOS this is typically
  `/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3`.
  `escape_gen.py` and `cleanup_dangling.py` need only a normal Python 3 (no
  `pcbnew` import) plus `kicad-cli` on `PATH`.
- **[KiCadRoutingTools](https://github.com/drandyhaas/KiCadRoutingTools)**
  cloned as a sibling directory (`../KiCadRoutingTools` relative to this repo)
  — `escape_gen.py` imports its `kicad_parser`/`kicad_writer` modules for file
  I/O, and the workflow below calls its `bga_fanout.py`, `route.py`, and
  `route_disconnected_planes.py` directly. Follow that repo's own setup
  instructions (`python build_router.py` to fetch/build its Rust core) before
  using the combo workflow.

## Quick start: reproduce the demo end to end

The demo board (`examples/demo_board.kicad_pcb`) is a small synthetic fixture:
one fine-pitch connector `J1` (staggered 2-row, 0.4 mm pitch within each row,
0.2 mm pads — the same proportions as the real hardware this was proven on)
carrying a power net and two signal nets, plus two target components (`U1`,
`U2`) elsewhere on the board that those nets need to reach. Regenerate it
anytime with `python3 examples/gen_demo_board.py` (KiCad's Python).

```sh
cd malakaRouter3
KICAD_PY=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3

# 1. Escape the fine-pitch connector: standard vias only, verified against
#    every real obstacle nearby (not just J1's own footprint).
python3 escape_gen.py examples/demo_board.kicad_pcb /tmp/1_escaped.kicad_pcb --ref J1
cp examples/demo_board.kicad_pro /tmp/1_escaped.kicad_pro

# 2. Hand off to KiCadRoutingTools' route.py to finish the point-to-point
#    connections from the escape stubs to their targets. escape_gen's output
#    is ordinary KiCad copper, so any KiCad-aware router can pick it up from
#    here -- this is the actual combo: escape_gen owns the hard fine-pitch
#    part, route.py owns the easy open-space bulk routing.
cd ../KiCadRoutingTools
python3 route.py /tmp/1_escaped.kicad_pcb /tmp/2_routed.kicad_pcb \
  PWR GND SIG_A SIG_B \
  --track-width 0.13 --clearance 0.125 --via-size 0.45 --via-drill 0.2 \
  --hole-to-hole-clearance 0.2 --layers F.Cu B.Cu
cd -
cp examples/demo_board.kicad_pro /tmp/2_routed.kicad_pro

# 3. Refill zones and check with the DRC oracle -- the only authority that
#    matters. kicad-cli's own --refill-zones is correct for almost every case.
kicad-cli pcb drc --exit-code-violations --refill-zones --save-board \
  /tmp/2_routed.kicad_pcb -o /tmp/final.rpt

# 4. If DRC flags dangling stubs/vias (routers occasionally leave a few after
#    a rescue/rip-up pass), sweep them and re-check.
python3 cleanup_dangling.py /tmp/2_routed.kicad_pcb /tmp/3_final.kicad_pcb \
  examples/demo_board.kicad_pro
kicad-cli pcb drc --exit-code-violations --refill-zones \
  /tmp/3_final.kicad_pcb -o /tmp/final.rpt
```

Running this exactly reproduces: 0 unconnected items, no sub-spec vias, only
cosmetic silkscreen warnings left (default reference-designator placement —
harmless, unrelated to routing).

## The full workflow (what to reach for, and when)

This is the methodology, generalized from what actually closed the real
board. Not every step is needed every time — use judgment per board.

1. **Survey first.** Copy the board (never touch the original — every step
   below writes a new file). Run `kicad-cli pcb drc --exit-code-violations
   --refill-zones` for the true baseline: unconnected count, violation
   breakdown, and which are pre-existing vs. introduced by later steps (diff
   later reports against this baseline by matching `[category]` + coordinates
   — an unrelated violation identical to baseline is not yours to fix).

2. **Fine-pitch/BGA-style packages: try `bga_fanout.py` first** (from
   KiCadRoutingTools) for genuinely 2D/radial pad grids (WLCSP, BGA, QFN). Its
   escape-direction handling is built for that topology.
   `escape_gen.py --ref` is built for *directional* connectors instead —
   footprints whose own anchor point sits offset from the pad field in a
   consistent direction (true of most real connector footprints, since the
   anchor is usually at the part's body/shell). It computes each pin's escape
   direction from that anchor→pad-field vector, so it degenerates on a
   perfectly radially-symmetric footprint anchored at its own pad centroid
   (see **Limitations**). When a directional connector's escape needs
   standard-vias-only guarantees `bga_fanout.py` doesn't give you, or when
   `bga_fanout.py`'s own escape leaves specific pins isolated, run
   `escape_gen.py --ref CONNECTOR_REF` on it directly.

3. **Single fine-pitch power/ground pads that need a plane tap** (not a full
   connector escape — one pad at a time, e.g. a BGA's own VCC ball): use
   `escape_gen.py --power-taps "REF.PAD,REF.PAD,..."`. It tries via-in-pad
   first, then spirals outward for a standard-via site with a clear stub,
   including a 1-bend waypoint fallback for pads boxed in by a sibling pin's
   own escape copper. If no standard-via site exists at all within its search
   radius, it fails that pad honestly rather than emitting sub-spec geometry —
   that pad may need a genuinely different fix (nudging a neighboring net's
   trace, widening local pitch), which is a design decision, not something to
   route around silently.

4. **Bulk / point-to-point completion: `route.py`** (KiCadRoutingTools). One
   thing worth knowing before you hit it: if a net needs a very long
   point-to-point run (tens of mm across a busy board) *and* you route it
   together with several other nets in one call, it can exhaust its search
   budget fighting for the same iterations as the others and fail — while the
   exact same net, routed **alone** first with the same budget, succeeds
   easily. If a net fails only when bundled with others, try it solo first,
   then layer the remaining nets onto that result in a second `route.py` call
   (`filter_already_routed` skips what's already connected, so this composes
   cleanly). This one change turns "can't find a route" into "found it in
   under 200k iterations" with no parameter changes at all.

5. **Zone/plane connectivity gaps** (a GND/power pad or a whole isolated
   copper region not reaching its pour, usually surfaced by DRC as
   `unconnected_items` between two zone fragments or a pad and a zone): use
   KiCadRoutingTools' `route_disconnected_planes.py --nets NET --plane-layers
   LAYER`. It finds disconnected regions of a net's plane and bridges them
   with tracks/vias, with its own DRC-oracle recheck loop.

6. **Refill zones.** `kicad-cli pcb drc --refill-zones --save-board` is the
   right tool for almost every case — it matches KiCad's own "Refill All
   Zones." Reach for `refill_zones.py`'s scoped refill only if a full-board
   refill introduces a new violation somewhere unrelated to what you actually
   changed (happens occasionally on boards with many zones — refilling
   *everything* can nondeterministically resolve one marginal pre-existing
   zone-boundary case while exposing a different one elsewhere; refilling only
   the zones near your change avoids disturbing the rest of the board).

7. **Sweep dangling copper.** `cleanup_dangling.py` — iterates
   `track_dangling`/`via_dangling`/GND-`hole_clearance` removal against the
   DRC oracle until stable.

8. **The DRC oracle is the only authority, always.** Every step above is
   judged by `kicad-cli pcb drc`, with the project's own `.kicad_pro` sibling
   present (board-specific rules — netclasses, min clearance/track/via floors
   — live there; a bare `.kicad_pcb` DRC'd without it will report the wrong
   thing). Never accept "looks routed" — check.

## How `escape_gen.py` works, briefly

- **Exact geometry, not a grid.** Every clearance check is analytic distance
  math (point-to-segment, segment-to-segment) against the real shapes
  involved — pad rectangles, via/track circles, existing copper — with a small
  safety margin over the board's stated clearance rule. No rasterization, so
  no half-cell over-blocking.
- **Obstacle-aware, including copper the tool didn't place.** Candidate via
  and stub sites are checked against every nearby pad, via, *and existing
  track segment* — not just the target footprint's own pads. (An earlier
  version only modeled pads/vias, which let a stub drive straight through an
  already-routed trace; fixed by making every check segment-aware.)
- **Never sub-spec.** All geometry uses one fixed, standard fab tier (0.13 mm
  track / 0.125 mm clearance / 0.45 mm via / 0.2 mm drill by default,
  configurable). If no legal site exists at that tier, the pin is reported as
  failed — never silently downgraded.
- **Search strategy:** ring search outward from each pin/pad (increasing
  radius, all angles) for a legal via site with a clear straight stub; if the
  straight stub is blocked, a 1-bend waypoint search (bend near the pin,
  perpendicular offset) finds a path around whatever's in the way. For a
  multi-pin connector, front-row pins (closest to the board interior, no via
  needed) are committed first so their stubs become obstacles the back row's
  search correctly respects.

## Layer direction discipline

Manual routers commonly bias each copper layer toward a consistent direction —
often diagonal, with adjacent layers running opposite diagonals — rather than
letting each net find whatever angle is locally shortest. The reason isn't
cosmetic: it keeps any two adjacent layers from running long parallel stretches
in the same direction (which is what couples them), and it makes via
transitions and DRC review predictable to eyeball, since a trace's layer is
recognizable from its angle alone.

KiCadRoutingTools' `route.py` already does an orthogonal version of this by
default, whether or not you ask for it: `direction_preference_cost` defaults to
`50` (nonzero = enabled), and `get_layer_direction_preferences()` assigns each
layer an alternating horizontal/vertical preference by layer index (F.Cu
horizontal, In1.Cu vertical, In2.Cu horizontal, B.Cu vertical, ...), penalizing
routes that go against their layer's preferred axis. Every `route.py` call in
the workflow above benefits from this without any extra flags — it's why
routed boards already show a visible per-layer directional tendency.

That cost is deliberately small relative to the router's other costs —
`TURN_COST` and `CROSSING_PENALTY` are both `1000`, twenty times
`DIRECTION_PREFERENCE_COST`'s `50` (the same order as `VIA_COST`). So it acts
as a **tiebreaker between otherwise-equal paths**, not a dominant steering
force: a genuinely shorter or lower-via route still wins even against its
layer's grain. This mirrors the same design choice other PCB tooling (e.g.
atopile) makes for the identical reason — a hard directional bias would fight
the router's actual job (shortest legal path) on every board where the
"wrong-grain" route is meaningfully better; a tiebreaker only kicks in when the
router would otherwise be indifferent.

What isn't modeled yet is true diagonal preference (opposite 45°/135° per
layer, rather than orthogonal H/V). The router's pathfinding is octolinear (45°
jogs are already part of how it connects two points), but the preference-cost
mechanism only recognizes horizontal, vertical, or no preference — not a
specific diagonal orientation. Formalizing that as the same kind of low-weight
tiebreaker (alternating +45°/-45° instead of H/V) is a reasonable next step,
given how directly it matches established manual-routing practice.

## Limitations

- **Directional connectors only for `--ref`.** The escape-direction heuristic
  is `normalize(pad_field_centroid - footprint_anchor)`. This matches how most
  real connector footprints are authored (anchor near the body/shell, offset
  from the pads) but fails on a footprint anchored exactly at its own pad
  centroid, or on genuinely radial 2D grids (BGA/WLCSP) — use
  `bga_fanout.py` for those instead (step 2 above).
- **No multi-pin/Steiner routing.** `--power-taps` handles one pad at a time;
  connector escape handles one connector's pins independently. Tying multiple
  same-net pads *within* the escaped footprint together (e.g. bridging a
  package's own split power pins) is a separate step — route it with
  `route.py` afterward, same as any other net.
- **Single-layer-pair (F.Cu/B.Cu) by default.** Inner-layer escapes aren't
  modeled.
- **GND/GNDPWR pins are skipped** in connector-escape mode (assumed
  plane-tied elsewhere) — use `--power-taps` if a specific ground pad genuinely
  needs an explicit tap.
