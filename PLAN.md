# malakaRouter3 — build plan

Third, **clean-room** attempt at a state-of-the-art PCB autorouter for this
project. The architecture — **topology first, negotiated congestion,
push-and-shove realization** — stands on its own merits from the IC/VLSI and
topological-routing literature (below), not because a prior attempt endorsed it.

## Status log

### Strategy: escape-gen first, KiCadRoutingTools as commodity harness
The engines don't overlap: `projects/KiCadRoutingTools` (drandyhaas) is grid-A*
+ rip-up (geometry-first); router3's core is topological + negotiated congestion
+ native push-and-shove. But the *plumbing* overlaps heavily (KiCad I/O, plane
tapping, cleanup, DRC). Decision: use KiCadRoutingTools as the commodity harness
for the easy 90% (bulk signal routing, plane taps, file I/O, DRC), and build
router3 as only the differentiated core **plus the fine-pitch escape generator**
— the one thing that resolves the actual wall. This is NOT a clean-room breach:
that rule is about not inheriting *our* router1/2 debt, not about refusing to
shell out to a third-party tool for commodity work.

### Baseline to beat — KiCadRoutingTools on boardB_ripped (2026-07-24)
Full workflow (bulk signals + fine-pitch rip/reroute + plane taps + +3.3v tree +
missing-net sweep + pcbnew refill), judged by kicad-cli DRC with the **original**
`.kicad_pro` (0.125 clearance / standard vias):
- baseline (ripped): **210 unconnected / 95 nets**, 74 violations (dangling).
- KiCadRoutingTools maxed: **36 unconnected / 17 nets**, **~98 violations**.
- prior router2 best (`milestones/boardB/boardB_best_0err_43`): **0 violations / 43
  unconnected** — cleaner but less complete.
Verdict: NOT adoptable — connects most nets fast but (a) can't close the CM5
fine-pitch escape, (b) buys fine-pitch power-tap connectivity with **sub-spec
0.25/0.15 vias** (illegal), leaving ~68 of the 98 violations. It is the baseline
to beat: router3's bar is 0 violations / 0 unconnected / standard vias only.

### Escape generator v0/v1 (`projects/malakaRouter3/escape_gen.py`, 2026-07-24)
First router3 component (S4). Python, borrows only KiCad I/O from the harness;
escape algorithm is ours (portable to Rust later). Synthesizes DRC-clean escapes
for a connector's SIGNAL pins: front row straight out on top; deeper rows via to
bottom with **obstacle-aware, standard-via-only** placement. Validated on J_FP
(0.4 mm staggered dual-row micro-connector — 3 high-speed pairs + 2-wire control + 3.3V).
- Naive placement (vias at pad centers): 66 new DRC violations.
- Obstacle-aware placement (existing vias/pads as rect/circle obstacles, cleared
  by fab clearance) + correct net ids + waypoint B.Cu routing + refill:
  **6 real geometric defects** at the connector (from 66 naive), standard
  0.45/0.2 vias only, all 5 back-row signal vias placed. Zone-fill "violations"
  (28) proved to be refill artifacts (vanish once zones refill).
- Waypoint routing (chosen over thinning GND vias — no board change): each
  back-row escape jogs laterally to clear the front-row GND stitching vias, with
  diff-pair halves diverging. Cleared the P-polarity traces fully.
- SOLVED (segment-aware placement): the search now validates the ENTIRE escape
  geometry — via point + F.Cu stub + B.Cu waypoint legs — against layer-aware
  obstacles (pads as rects on their own layers, vias as all-layer circles) and
  previously-placed escape copper, with a 0.02 mm search margin over the raw fab
  clearance (KiCad's exact geometry finds a hair less than a sampled gap).
  Result: **0 real geometric defects** at J_FP, all 5 back-row signal vias +
  front-row stubs placed, **standard 0.45/0.2 vias only**, ~1.5 s. The remaining
  near-connector DRC items are dangling/unconnected stub-ends by design (the
  escape ends in open space for the bulk router to pick up).
- KEY FINDING: the connector's own redundant GND stitching vias crowd the
  corridor; the N-side pins are laterally boxed between two GND vias. Solved on
  2 layers with NO board change via a small per-trace lateral jog past the
  front-row GND vias (pairs diverge).

### END-TO-END PROOF (2026-07-24): escape-gen -> KiCadRoutingTools
Ran the generator on J_FP, then handed the escaped board to KiCadRoutingTools
to connect the escape stubs to MOD_A (the far end). Head-to-head at J_FP,
refilled + kicad-cli DRC with the original `.pro`:
- **KiCadRoutingTools alone**: 2 illegal sub-spec vias (annular_width/via_diameter)
  + **2 fine-pitch signals unconnected** (PAIR_A_D0_P, PAIR_A_C_P).
- **escape-gen -> KiCadRoutingTools**: **8/8 single-ended + 10/10 multipoint
  connected**, **0 illegal vias, 0 real clearance defects** (only leftover
  cleanup stubs on 1 net). The previously-impossible D0_P/C_P now route.
This is the router3 thesis proven on real copper: the escape generator owns the
hard fine-pitch field with legal geometry; the commodity harness does the easy
open-space bulk. Composed, they beat either alone.
NEXT: generalize escape-gen to the fine-pitch POWER pads (the other ~68 sub-spec
vias in the baseline), then wire the full-board pipeline (escape all fine-pitch
-> harness bulk+planes -> refill -> DRC) and compare to the 98-violation baseline.

### FULL-BOARD PIPELINE DRIVE (2026-07-24): escape-gen + harness + cleanup
Wired the whole pipeline on boardB_ripped: escape J_FP -> harness bulk
signals -> route_planes power/GND with **standard vias PINNED** (--fab-overrides,
no 0.25/0.15 escalation) -> +3.3v tree + stragglers -> escape-gen power-taps for
the fine-pitch pads route_planes couldn't tap -> DRC-driven cleanup (peel dead-
ends + drop redundant GND stitching vias flagged for hole_clearance) -> refill.
Added to escape_gen.py: `power_taps()` (single-pad obstacle+hole-aware standard-
via plane taps, `--power-taps REF.PAD,...`) and hole-to-hole (drill) awareness in
the obstacle model. Cleanup harness: `complete/cleanup.py` (DRC-driven, iterative).

Headline (kicad-cli DRC, original `.pro`, best artifact `complete/FINAL2f`):
| stage | violations | unconnected items |
|---|---:|---:|
| ripped baseline | 74 (all dangling) | 210 (95 nets) |
| KiCadRoutingTools alone (maxed) | ~98 | 36 |
| **escape-gen + harness + cleanup** | **17** | **32 (13 nets)** |

Wins: sub-spec vias 68->~1 (the 1 left is +3.3v from the ONE route.py pass not
fab-pinned — fix: pin it too); `hole_clearance` 47->0 (redundant-GND-via cleanup);
dangling stubs ->0. NOT yet 0/0. Remaining 17 = 8 pour hygiene (slivers/islands),
7 local clearance/crossing/short conflicts on PAIR_A_D1 / BUS_A_EN / +5v, 1 sub-spec
+ annular. Remaining 13 unconnected nets = a few GND pads the via-cleanup dropped,
crystals (U_OSC XI/XO), AVDD_x analog rails, CTRL_SDA, bus internal, U_PM KEY/BAT.
LESSON: greedy single-pad power-taps whack-a-mole (re-tapping traded 5 opens for 7
new violations) — the last-mile local conflicts are exactly what router3's
negotiation + shove resolves and a greedy pipeline cannot. The pipeline massively
improves the board AND re-confirms why the router3 core is the real prize.

### NEGOTIATION CORE — increment 1: corridor-graph substrate (2026-07-24)
`projects/malakaRouter3/r3/corridor.py` — builds the free-space CORRIDOR GRAPH
of a layer: Delaunay triangulation of obstacle-boundary sites (pad corners, via
centers, board-outline samples), each triangulation edge labeled with its exact
usable width (gap = dist - radii, NO rasterization per ROUTING-SKILL) and lane
CAPACITY at the fab pitch. Dual graph (triangles=nodes, shared passable edges=
arcs) is the graph the router searches. Verified + rendered on f02_ladder /
f04_crossings / f06_obstacle: captures corridors correctly. Prototype in Python
(scipy Delaunay + harness parser); hot loop ports to Rust once proven.
Applied ROUTING-SKILL lessons: exact-geometry clearance, effective clearance =
board min_clearance (passed in), pad interior edges excluded from corridors.
OPEN: board-outline sites absent when a fixture has no parsed outline (hull =
pad convex hull); constrained-Delaunay (force obstacle/outline edges) deferred;
lane formula is approximate (refine when wiring capacity to negotiation).
NEXT increments: (2) PathFinder negotiated-congestion Dijkstra over the dual
graph; (3) taut-string realization + push-and-shove to copper; validate fixtures
(f04 crossings, f07 deadlock, f11 finepitch) at 0 DRC, then the boardB region.

### v2 pipeline result: fixed a real short + illegal via (2026-07-24)
User pushed to actually fix the v1 pipeline's remaining issues rather than just
report them. Found and fixed the ROOT CAUSE of the worst violation: `/+5v`
crossed and shorted `/BUS_A/EN` + `/BUS_B/EN` because `escape_gen.py`'s
`power_taps()` obstacle model only saw pads/vias, never existing TRACK
SEGMENTS — a stub could drive straight through already-routed foreign copper.
Fixed properly (not patched around): added `collect_board_segments()` and
threaded it through `clears()`/`seg_clear()` in both `power_taps()` and the
connector `plan()` path, so all new copper now checks against existing segments
too. Also added a 1-bend waypoint fallback (`find_path()`) to `power_taps()` for
sites boxed from straight-line reach, and scoped its obstacle collection per-pad
(was collecting the WHOLE board at radius=1e9 per candidate — correct but
pathologically slow; fixed to a local radius).

Removed the illegal stubs/via, re-tapped standard-only, then routed the
remaining easy nets. Result (`best/boardB_pipeline_v2_13viol_31unconn`):
**13 violations (from 17), 31 unconnected / 11 nets (from 32/13)** — and
critically, **zero shorting_items, zero tracks_crossing, zero sub-spec vias**.
Both v1 and v2 are frozen in `projects/malakaRouter3/best/` (v1 kept for history).

What's LEFT is genuinely hard, not quick fixes: e.g. `U_XA.3`/`U_XB.3` (bus
transceiver VCC pins) are boxed by their OWN chip's EN escape stub 0.17mm
away — legally connecting them requires moving the sibling net too. This is
exactly the two-net local-conflict class the negotiation core exists to solve;
further hand-fixing here is the whack-a-mole the earlier lesson already flagged.

### NEGOTIATION CORE — increment 1 v2: real CDT (2026-07-24)
Rebuilt `r3/corridor.py` on a proper constrained-Delaunay triangulation
(Shewchuk's Triangle via the `triangle` pip package) instead of plain
scipy Delaunay + same-obstacle edge-blocking. Every pad/via is now a literal
HOLE punched in the mesh (forced boundary segments + interior hole point), so
a corridor edge can never cut through solid copper by construction — closes
the OPEN item from increment 1. Verified visually on f01/f02/f04/f06/f11:
clean gaps at every pad, corridors correctly route around obstacles, fine-pitch
sub-lane gaps show red (blocked) as expected. Cross-validates malakaRouter2's
Rust core, which independently arrived at the same fix (`spade`'s
`ConstrainedDelaunayTriangulation`) — two implementations converging on "plain
Delaunay isn't enough" is a good sign the design is right.
Fixtures with no parsed Edge.Cuts polygon (the synthetic ones) fall back to
the board bbox (+1mm) as the outer boundary.

### NEGOTIATION CORE — increment 2: PathFinder negotiated congestion (2026-07-24)
`r3/negotiate.py` — routes ALL nets as triangle-sequences over the corridor
graph's dual graph (multi-source/sink Dijkstra: a pad is reachable from any
triangle touching its boundary). Cost = centroid-to-centroid length x
congestion, congestion = (1+history)*(1+pres_fac*overuse) — classic
McMurchie & Ebeling PathFinder. Iterate rip-up-and-reroute-ALL with rising
pres_fac/history until 0 edges are over lane capacity, or max_iterations.

Two-pin nets only this increment (`nets_from_pcb`: groups pads by net, takes
the farthest pair — matches the simple fixtures). Multi-pin/Steiner nets are
a later increment.

VALIDATION: the simple fixtures (f01/f02/f04/f06/f07) all converge at
iteration 0 — they're too open to actually contend a corridor, so that alone
doesn't prove the congestion mechanism works. Wrote
`r3/test_negotiate_synthetic.py`: hand-built graphs (bypassing CDT/KiCad
entirely) that isolate `negotiate_core` (the graph-agnostic loop the real
KiCad-facing `negotiate()` also calls — refactored so the test exercises the
ACTUAL implementation, not a re-description of it) —
  1. two nets forced through a single-lane bottleneck with NO alternate path:
     correctly reports NOT converged (no false-success on genuine
     infeasibility — the "certificate of infeasibility" the plan calls for).
  2. same bottleneck WITH a higher-lane alternate route: converges in 2
     iterations, confirmed one net was actually pushed onto the alternate.
Both pass. This is the proof the core differentiator works: a greedy router
commits the first net to the short path and fails the second (exactly what
happened with escape_gen's power-taps and the BUS_A/EN short); negotiation
discovers the reroute is globally better instead.

NEXT: increment 3 (taut-string realization + push-and-shove to copper),
validated against kicad-cli DRC on the fixtures, then a real multi-net
congestion scenario pulled from boardB (e.g. the bus transceiver VCC/EN
conflict) to prove the core actually resolves what the greedy pipeline couldn't.

### NEGOTIATION CORE — increment 3: realization + topology/geometry feedback (2026-07-24)
`r3/realize.py` — turns a RoutedNet's triangle-edge crossings into exact-
geometry copper: waypoints = [pad_A, midpoint of each crossed corridor edge,
pad_B], then taut-string shortcutting (greedy string-pulling per
ROUTING-SKILL.md — jump to the farthest waypoint reachable by a straight,
DRC-clear segment, re-verified every step). Clearance checking reuses
escape_gen.py's `seg_clear`/`collect_obstacles`/`collect_board_segments`
directly — one model of truth, not a second implementation.

REAL BUG FOUND AND FIXED (topology/geometry inconsistency): corridor.py's lane
formula didn't include the same safety `MARGIN` escape_gen's `seg_clear`
enforces, so topology could claim "1 lane fits" at a boundary case realization
then rejected. Fixed by importing escape_gen.MARGIN into the lane formula
directly (`r3/corridor.py`) so both layers agree on what's passable.

DEEPER GAP FOUND, FIXED PROPERLY (not patched around): lane capacity only
checks an edge's own 2 endpoint obstacles — a THIRD obstacle can sit close to
an edge's midpoint without being one of its defining vertices (a valid CDT
edge can still pass near unrelated copper). This is a necessary-but-not-
sufficient capacity model, same class of approximation real topological
routers make. Per the design's own stated principle ("realization reports
back to the negotiator as a capacity correction, never a silent drop" —
TOPO_ROUTER.md's own realization section), built the correct feedback loop:
`route_and_realize()` validates every RAW (pre-taut-string) hop with exact
geometry; a hop that fails implicates the corridor edge(s) it crosses, whose
lane capacity is zeroed, and the affected nets are re-negotiated — up to
`max_correction_rounds`. Also fixed a real inconsistency bug in that loop: the
raw-hop pre-check was validating against the static original board copper
while the later taut-string/emission step validated against the ACCUMULATING
set of copper already realized earlier in the same round — the two checks
disagreed, producing "raw hop passed, but somehow the same segment fails
later" false alarms. Fixed by making both checks use the same accumulating list.

VALIDATED on the two-pin fixtures (this increment's documented scope; multi-pin
nets are a later increment): f01, f02, f06, f07 all realize to **0 real DRC
violations, 0 unconnected** (the only report lines are `lib_footprint_issues`,
an environment library-path artifact, not a routing defect). f06_obstacle is
the clean proof of the correction loop itself: round 0 finds 3 over-promised
edges near the obstacle, zeros them, re-negotiates, and N1 then realizes
perfectly. f04_crossings: 3 of 4 nets realize cleanly; net X2 (genuinely
2-pin) does not converge within 15 correction rounds — an honest, documented
scope boundary of the current coarse correction strategy (blame both edges
adjacent to a failing hop, zero them, re-negotiate globally), not a silent
failure. f13_two_ic's stuck nets (GND=10 pads, VCC=9 pads, etc.) are genuinely
multi-pin — out of scope as documented, not a bug.

NEXT: push-and-shove realization (displace already-placed copper within slack
instead of only re-negotiating topology — should resolve cases like f04's X2
without needing many correction rounds); multi-pin/Steiner nets; then apply
the full pipeline to a real congestion scenario pulled from boardB (the bus
transceiver VCC/EN conflict) to prove the core resolves what the greedy
pipeline could not.

### ARCHITECTURE DECISION (2026-07-27): global relaxation realizer is the differentiator; boardA needs F/B only

Measured boardA's real scale and re-derived what S6 actually requires instead of
assuming it. Two findings change the build order.

**1. boardA scale (`boardA_unrouted.kicad_pcb`, measured).**
- 233 non-GND nets with ≥2 pads → **504 pad-pairs to connect**
- 1245 netted pads, of which 346 are GND/GNDPWR (plane-tied, not routed)
- 124 × 68 mm pad field, 4 copper layers

This quantifies the topological bet. A 0.05 mm grid over that area is ~3.4M
cells/layer → **~13.5M search nodes**; a CDT corridor graph over ~1300
obstacles is **~15k nodes across all layers**. Three orders of magnitude. The
state-space collapse *is* the acceleration — nothing bolted onto a maze router
closes that gap, which is consistent with the Performance section below (the
negotiation search was never the bottleneck).

**2. RESOLVED — the inner-layer open question: F/B is enough for boardA.**
Measured the human-routed boardA answer key per layer:
- F.Cu **2128** segments, B.Cu **1140** segments
- In1.Cu **0** segments, In2.Cu **1** segment
- **zero** non-power nets on either inner layer

The human routed the entire board on two layers; In1/In2 really are pure
planes. router2's F/B assumption was correct for boardA, and **S6 therefore does
not need a layer-stacked (3D) corridor graph** — vias are F↔B swaps only. That
removes a large piece of scope from the critical path.

Caveat, not a contradiction: denser later revisions *do* spill onto inner
layers — on boardC, `PWR_A` routes across In1.Cu **and** In2.Cu. A
layer-stacked graph is required eventually for those boards; it simply is not
what blocks the boardA proof.

**3. The differentiator to build: a GLOBAL RELAXATION REALIZER (revises S3).**
S3 currently specifies "PNS-style shove of provisional neighbours" — sequential,
one trace at a time. That ordering is exactly what makes "route A first, B
fails" inevitable, and it is where `realize.py` stands today: independent
taut-string per net, no shove at all, so f04's X2 cannot converge in 15
correction rounds because the only available lever is re-negotiating topology.

Replace it with a *simultaneous* formulation. Once negotiation has fixed the
topology — which side of every obstacle each trace passes — the remainder is a
smooth constrained optimization: place polyline vertices to minimise length
subject to clearance. Solve it as a physical system (traces = elastic strings,
obstacles and other traces = repulsive potentials), stepping **every vertex of
every net at once** via gradient / position-based dynamics until the bundle
settles.

Why this is the right shape:
- **No ordering.** Every trace yields to every other simultaneously; there is
  no "committed first" net. This is the native push-and-shove the four laws
  require, not a call out to an external PNS.
- **It is precisely the workload the Metal stance already predicted pays.**
  Thousands of vertices, purely local interactions, fixed iteration count — the
  same embarrassingly-parallel batch shape as `seg_ok_batch`. GPU stays out of
  the negotiation loop (correct — that search is ms-scale) and goes where the
  cost actually is.
- **The geometry comes out human.** Relaxed elastic bundles are smooth,
  parallel and diagonal. Per-layer directional preference then drops in as an
  *anisotropic term in the energy* rather than an A* tiebreaker — cf. the
  README's "Layer direction discipline": route.py's H/V preference is
  deliberately low-weight (cost 50 vs turn/crossing 1000), the same
  tiebreaker-not-bias choice atopile makes.

Prior-art check, i.e. why this is worth claiming: TopoR is the only mainstream
truly-topological PCB router and is proprietary; Freerouting is a shove-capable
geometric/maze router, not rubber-band topological; KiCad PNS is interactive and
sequential; rubber-band routing's academic roots are the MCM-era SURF work (Dai
et al.). An open-source **global-simultaneous** relaxation realizer sitting on
exact-geometry topological negotiation is a real gap, not a crowded field.

**4. Make exact lane capacity an explicit invariant, not a comment.** In a
topological router congestion is *exact* — "do N traces fit this gap?" is
`gap ≥ N·track + (N+1)·clearance` — where a grid router can only estimate it. A
truthful cost signal is much of why negotiation converges at all. This is why
the topology-formula-vs-realizer-margin mismatch (increment 3 above) was
load-bearing rather than cosmetic: enforce the shared margin by construction,
per the project rule that a threshold encoding a physical assumption gets
enforced or tested, never left as prose.

**Deprioritised.** Parallel-batched negotiation rounds — routing all 504
connections concurrently against a frozen cost snapshot — is legitimate and
standard in FPGA CAD, but the Performance section's own analysis puts the
search at milliseconds, so it optimises what is not the bottleneck. Revisit
only if a profile says otherwise. Neural/RL routing stays rejected: it trades
the DRC-oracle determinism this project is built on for a training-data problem.

### Clean-room stance (read this first)

router3 does **not** build on router1/router2 code, does not import their
modules, and does not inherit their architecture by default. We are not
"continuing" — we are rebuilding from the algorithm up, so we do not re-inherit
the debt that made prior attempts plateau.

What prior work *is* allowed to be:
- **A read-only reference** to consult when stuck, then close.
- **A regression oracle**: their artifacts and the human boardA board are things
  our output is *checked against*, not built from.
- **A source of already-paid-for facts** (below) that we re-derive and re-verify
  independently before trusting — never copy-paste on faith.

If a router3 decision can only be justified by "router2 did it this way," that is
not a justification. Every choice must trace to the literature, the DRC oracle,
or a fixture that proves it. Prior code is guilty until independently re-proven.

## Why a third attempt

- **router1** (`projects/malakaRouter`): GPU/Metal raster A* + rip-up-reroute.
  Fast per-trace pathfinding, but greedy and geometry-first. Connects nets and
  then fights shorts. Same algorithmic class as freerouting.
- **router2** (`projects/malakaRouter2`): pass-ordered, human-strategy pipeline.
  Genuinely good bundle/pair proofs (both high-speed pair buses, all PCIe pairs, 5G USB3
  channel routed atomically, SI-clean). But whole-board completion stalled: the
  best full-board artifact is `milestones/best_reap2_0err_134` — **0 DRC errors
  but 134 of 235 nets still unconnected**. It got there by *committing* copper
  early (plant-and-reap, freeze-existing, locked escapes) and then could not
  route the rest through the mortgage it had taken on.

The lesson is exactly the one in TOPO_ROUTER law #2: **nothing is committed
until everything is routed.** router2 violated its own design. router3 obeys it.

### The failure mode we are killing

router1/router2 both search *geometry* directly (raster cells or per-net A*).
A geometry-first router cannot see that a "no path" net becomes routable if
three neighbours shift half a millimetre — it just reports failure and rips up.
A topological router represents that same situation as *a number on an edge*
(this corridor is over capacity by one lane) and resolves it by negotiation.
That is the whole reason the CM5 escape stalls at ~86% for everyone: it is a
lane-packing problem wearing a pathfinding costume.

## The design (four laws + one pipeline)

Carried verbatim from `malakaRouter2/docs/TOPO_ROUTER.md` because it is correct.
Repeated here so router3 is self-contained.

1. **Topology first, geometry second.** Route which side of each obstacle and
   which layer each net takes, as symbolic corridor sequences. Exact coordinates
   are a consequence produced once, at the end.
2. **Nothing *we route* is committed until everything is routed.** Every net in
   the *active routing set* stays provisional (flexible topological path)
   through global negotiation; realization to copper happens once, when overuse
   is zero. No locked escapes, no freeze-existing, no plant-and-reap among our
   own nets. (This is the law router2 broke.) This is distinct from *exogenous*
   copper we did not route — see the copper mutability model: that is an input
   constraint, not a commitment we made mid-solve.
3. **One model of truth.** A single exact-geometry kernel (`Legal`: copper,
   holes, per-pad clearance overrides, net-0 obstacles, circumscribed pads,
   materialized zone fills) feeds capacity, realization, and self-checks.
   `kicad-cli pcb drc` (with the sibling `.kicad_pro` present, pcbnew-prefilled
   zones) is the **only** acceptance authority — a per-stage regression gate,
   never an inner-loop search primitive.
4. **Route classes in forced order, as units.** Fine-pitch escapes first
   (pattern forced by the pinout), diff pairs and buses as bundles that reserve
   multi-lane capacity, power as pours + stitch vias, singles last into leftover
   capacity.

### Pipeline

```
extract   obstacles (pads, holes, keepouts, existing copper) + materialized
          zone fills                                        [exact Legal kernel]
escapes   emit measured human escape patterns for fine-pitch connectors
          (stub + staggered via pairs); their exits become net terminals
tri       constrained Delaunay triangulation (CDT) of free space, per signal
          layer (F/B; inner layers only if they carry signals)
capacity  each CDT edge: usable width = clear span between endpoint obstacles;
          lanes(edge, class) = floor(width / (trace + clearance))
terminals map each net's pads/escape-exits to triangulation vertices
search    route ALL nets as edge-crossing sequences in the triangle dual graph;
          cost = length + via + congestion (PathFinder: present_overuse *
          pres_fac + history); bundles consume n lanes atomically
negotiate iterate search with rising pres_fac until overuse == 0  ← the router
order     within each crossed edge, order the nets passing through it
          (planarity inside a corridor is a sorting problem)
realize   rubber-band each net through its corridor sequence, pull taut,
          assign lane offsets, place vias at Legal-verified sites; shove
          provisionally-placed neighbours within corridor slack (PNS-style)
power     pours + plane stitch vias (implement fresh; pours via pcbnew)
gate      kicad-cli DRC after realize; any error is a kernel bug to fix,
          not a repair loop to run
```

## Copper mutability model (the core of the real goal)

The primary target is **incremental routing into a board that already has
committed copper** (the boardB ripped region), not a greenfield board. That makes
"what am I allowed to touch, and how" the central design question — router2/1
never modelled this cleanly. Every piece of copper falls into exactly one class,
and the router treats each differently:

| Class | Examples | May move? | May reroute/break? | Role in planning |
|-------|----------|-----------|--------------------|------------------|
| **Frozen** | pads, holes, keepouts, board outline, explicitly locked traces | no | no | hard obstacle; defines capacity |
| **Exogenous traces** | existing routed traces we did *not* plan (human/earlier work) | yes — shove within slack | no (topology-locked) | pre-occupied corridor lanes; shovable neighbours |
| **Pours / zones** | GND plane, power islands, copper fill | n/a — refill | n/a | *not* obstacles; they reflow around whatever we place |
| **Active nets** | the unrouted nets router3 is placing | fully provisional | yes (until final realize) | Law 2 applies |

Consequences that drive the implementation:

- **Pours are never obstacles during planning.** They are materialized only for
  the DRC gate and for stitch-via legality; the router plans as if they will
  reflow (because they will). Treating a pour as a fixed obstacle is a router2
  bug we must not repeat — it strangles the free space that actually reopens.
- **Exogenous traces are shovable but topology-locked.** They occupy corridor
  lanes (they consume capacity like our own provisional routes), and the shove
  engine may displace them *within their slack* to make room — but it may never
  reroute them to a different corridor, change which side of an obstacle they
  pass, or break/delete them. Their homotopy class is a hard input constraint.
  This is the same shove primitive as for our own provisional copper, with one
  flag flipped: `topology_locked = true`.
- **The active set is only the ripped nets.** Capacity accounting must subtract
  the lanes consumed by exogenous traces up front, so negotiation packs the new
  nets into *leftover* capacity — exactly the situation that is "achievable by
  eye" and that greedy per-net search fumbles.

### Deferred axis: component placement mutability (future, not now)

There is a further degree of freedom beyond copper: **inboard (non-I/O)
components can be moved or rotated** to relieve routing, and prior notes
(`prompt-malakaRouter.md`) already flagged this. It is deliberately **out of
scope for the initial design space** — opening it turns routing into
simultaneous place-and-route, a much larger search we should not take on before
the fixed-placement router works end to end.

Keep it in mind so nothing forecloses it later:

- **I/O and edge components are hard-frozen forever** — connectors, mounting
  holes, board-edge parts define the external interface and mechanical fit;
  never candidates for movement.
- **Inboard components are frozen *for now*** — treated as Frozen copper anchors
  in S0–S7, but conceptually a separate "placement-mutable" class that a future
  stage could unlock.
- When we do add it, the project rule from the original prompt stands: placement
  feedback must live **inside the routing loop** (immediate congestion/again
  feedback), not a separate place-then-route handoff. That means the
  negotiator's congestion field should be designed so it *could* later drive a
  component nudge — keep the cost field inspectable per region.

For every current milestone, assume fixed placement.

### Last-resort context-aware rip-up (bounded, explicit, never silent)

If — and only if — negotiation proves a region cannot be completed without
touching an exogenous trace's topology, the router may escalate to **rerouting a
specific, named exogenous trace**, under strict conditions:

- Triggered only when the negotiator returns a **hard infeasibility certificate**
  for a corridor (over capacity even after all legal shoving), not on the first
  hard hop.
- Scoped to the **minimum set** of exogenous traces whose rerouting relieves the
  certified pinch — chosen by cost, not "rip up everything nearby."
- The ripped trace re-enters the **active set** and is re-routed by the same
  topological negotiation as any other net — never patched with a local A*.
- **Logged with the reason** (which certificate, which pinch edge, why this
  trace). No silent rip-up, ever (project rule: no swallowed decisions).

This replaces router2's plant-and-reap (which ripped up *our own* committed
copper as a routine strategy) with a rare, certified, reversible escalation that
only ever touches *exogenous* copper.

## Facts to re-derive independently (knowledge, not code)

These are hard-won *facts* about this board and KiCad's behaviour that prior
attempts paid for in debugging time. router3 does not inherit the code that
encodes them — but it would be foolish to relearn them by hitting the same wall.
Treat each as a hypothesis to **re-verify with a fresh test**, then trust:

- **Geometry transform is a trap.** A prior parser double-mirrored
  bottom-footprint pad centres → 131 phantom clearance violations. Build the
  geometry kernel fresh, but its first test is: our computed pad centres for a
  few known footprints (esp. J_BS / bottom-side) match `pad.GetPosition()` from
  KiCad exactly. Do not proceed past S0 until that passes.
- **Effective clearance ≠ default netclass.** The binding constraint on boardA
  came from board `min_clearance` (0.125 mm) and the `100R` class, not the
  0.2 mm Default. Measure the effective rule from what DRC actually passes.
- **NPTH pad clearance overrides.** MOD_A's NPTH pads carry a `(clearance 1.7)`
  override that drives `hole_clearance` — the default 0.19 is a red herring.
- **uuid uniqueness by construction.** A fixed-seed uuid generator once made
  KiCad DRC blame the wrong copper for days (router-oracle memory). Guarantee
  uniqueness and assert it; never debug attribution before checking this.
- **Inner-layer policy**, confirmed against a commercial router: one inner layer
  = full GND plane, the other = power islands + GND pour. Re-confirm on S6.

## Reference material (read-only, consult then close)

Not dependencies. Read for ideas or to avoid a known pitfall, then implement
fresh:
- `malakaRouter2/docs/TOPO_ROUTER.md` — the architecture write-up this plan
  formalizes.
- Measured human escape geometry (`engine/docs/ESCAPE_PATTERNS.md`,
  `CONNECTOR_FIELD_TEMPLATE.md`, `MODULE_FIELD_PLAYBOOK.md`). Useful as *data*
  about what a legal 0.4 mm escape looks like — re-measure against the actual
  board before encoding.
- `hardware/carrier/ROUTING-SKILL.md` — the manual escape playbook.
- KiCad PNS source — reference shove engine (see below).

## Explicitly rejected (the plateau causes)

Do not reintroduce these even if a prior attempt used them:
- Raster A* / per-net geometry search as the *primary* router (router1's class).
- plant-and-reap, freeze-existing, locked escapes, any early copper commit
  (the law-2 violations that stalled router2 at 0 err / 134 unconn).
- DRC-feedback repair loops as a routing *strategy* (they plateau; DRC is a gate,
  not a search primitive).

## Prior art from IC/VLSI to lift

PCB autorouting research stagnated ~20 years ago; the transferable muscle is in
chip routing. The adaptation is always the same: **IC is gridded / Manhattan /
millions of cells; PCB is gridless / 45° / thousands of corridors.** We keep the
*algorithms* and drop the grid.

- **Global → detailed decomposition** (every modern IC flow). Our CDT + corridor
  search *is* the global route; realization is detailed routing. Keeps search on
  a ~10⁴-node graph instead of 10⁶ raster cells.
- **PathFinder negotiated congestion** (McMurchie & Ebeling 1995). The core of
  the negotiate loop: nets share resources, a rising `present` cost plus
  `history` cost forces them apart over iterations. This is the single most
  important import and the thing router2 never fully committed to.
- **FastRoute** (global router). Two ideas worth stealing: build a
  **rectilinear Steiner minimum tree** (FLUTE) per multi-pin net as the initial
  topology before negotiation, and maintain a **congestion map** to steer
  detours. FLUTE gives near-optimal multi-pin trees cheaply — directly useful
  for our multi-pad power/GND and fan-out nets.
- **TritonRoute / detailed routing with access points** (OpenROAD). Each pad
  gets a set of legal *access points* (where a track can legally reach it);
  routing connects access points, not pad centres. Maps cleanly onto our
  escape-exit terminals. Also its notion of **track assignment** = our "order
  the nets within a crossed edge" step.
- **Track assignment / layer assignment as an ordering problem.** Assigning nets
  to tracks in a channel is a sorting/interval problem; corridor lane ordering
  is the same. Left-edge / interval-graph colouring applies.
- **Monotonic and bounded-box routing** (Dr.CU and others): restrict a net's
  search to a bounded region first, escalate only on failure. Cheap, and keeps
  the negotiation iterations fast.
- **Min-cost multi-commodity flow** as the theory behind negotiated congestion —
  useful framing if we ever want an LP relaxation to check whether a corridor is
  provably over capacity (a certificate that "add a layer / move a part" is
  required, not a router failure).
- **OpenROAD** (open source) is the reference implementation to read for
  FastRoute + TritonRoute if we want concrete code, even though it is Manhattan.

## Other approaches to incorporate

- **Rubber-band sketch / topological routing** (Dayan's thesis; Dai's SURF for
  multi-layer, octilinear, any-angle). This is the theoretical backbone of the
  symbolic-path-then-relax model. Realization = pulling the rubber band taut
  through its corridor sequence (homotopy class fixed).
- **Constrained Delaunay triangulation of free space** as the corridor graph.
  This is the gridless equivalent of gcells and the practical heart of the
  router — it makes "usable width between two obstacles" a first-class number.
- **Push-and-shove realization — implemented natively, in our engine.** KiCad's
  PNS (Tomasz Włostowski) is **reference reading only**; we never call it, link
  it, or shell out to it at runtime — it is old C++ inside KiCad and would be a
  slow, awkward external dependency. We build our own shove in the router's own
  data model. The rule it embodies: a failed hop is an instruction, not a dead
  end — walk around the blocker → shove a shovable neighbour within its slack
  (homotopy preserved, geometry only) → if neither works, report a capacity
  correction to the negotiator and re-plan. See the copper mutability model for
  *what* is allowed to be shoved.
- **Sketch / river routing for buses** (Xpedition-style). A bundle crossing a
  corridor flows as one unit with preserved order — already partly proven in
  router2's atomic bundle router; keep it.
- **BGA/fine-pitch escape & fanout literature.** Even without BGAs on this board,
  the 0.4 mm CM5 connector is the same escape problem: assign escape direction +
  layer per pin up front so the corridor search starts from a legal fan-out.
  Hungarian/bipartite matching for pad↔access-point assignment (a standard
  technique both router2 and KiCadRoutingTools use) belongs here.
- **Length matching** (trombone/accordion meanders) as a bounded post-realize
  pass on SI nets.
- **RL for the combinatorial layer only (optional, later).** The honest reading
  of DeepPCB/Quilter/XRoute: end-to-end RL does not beat rule-based geometry, but
  RL *net ordering, layer assignment, and escape-direction choice* — the
  discrete planning decisions feeding a deterministic geometric core — is where
  it plausibly helps. Do not build this until the deterministic router completes
  boards. It is a ranker on top, not the engine.

## Goals: primary vs stretch

- **Primary target: route the unrouted nets of the boardB ripped region**
  (`milestones/boardB/boardB_ripped_refilled`) into the existing board —
  full copper mutability model in play (frozen / exogenous-shovable / pours /
  active). This is *the* deliverable that makes the router useful day-to-day,
  and it is harder along the dimension that matters: reasoning about mixed
  mutability, not raw scale.
- **Stretch / proof-of-power: route boardA from fully unrouted** to 0 err /
  0 unconn. Greenfield (no exogenous copper, so simpler mutability, harder
  scale). Make no mistake — the router *must* be able to do this: full CM5 0.4 mm
  escape, topological packing, bundles. It is the proof the architecture is
  complete, not the primary use case.

## Staged milestones (each gated by kicad-cli DRC: 0 errors, 0 unconnected)

Fixtures first. A stage is not "done" until its DRC gate passes with the
`.kicad_pro` present and zones prefilled.

- **S0 — geometry kernel, from scratch.** Build the exact-geometry legality
  kernel (copper, holes, per-pad clearance overrides, net-0 obstacles,
  circumscribed pads, materialized zone fills) and the KiCad parser/writer
  fresh, with the J_BS pad-position regression test and uuid-uniqueness check
  from day one. No routing yet. Gate: differential geometry tests vs KiCad pass;
  DRC on the untouched boardA board reproduces the known baseline (0 err /
  499 unconn).
- **S1 — CDT + capacity on a trivial fixture.** `f01_two_pads`, `f02_ladder`:
  build the triangulation, compute per-edge lane capacity, route by pure
  corridor search (no congestion yet). Gate: 100% routed, 0 errors.
- **S2 — negotiated congestion.** `f03_bus`, `f04_crossings`, `f11_finepitch`:
  add the PathFinder loop (present + history cost, rising pres_fac to zero
  overuse) and corridor ordering. Gate: fixtures that a greedy router deadlocks
  on route 100% clean.
- **S3 — realization + GLOBAL RELAXATION shove.** Rubber-band taut-pull and lane
  offsets, then the differentiator: relax **all** nets' vertices simultaneously
  (elastic strings vs. repulsive obstacles/neighbours, gradient / position-based
  dynamics) instead of shoving one trace at a time PNS-style — see the
  2026-07-27 architecture decision above for why ordering is the thing to
  eliminate. Batch-shaped from day one so Metal drops in. Gate: S1–S2 fixtures
  produce human-tidy 45° copper, 0 DRC errors, and f04's X2 (which the
  topology-only correction loop cannot converge) resolves without extra
  negotiation rounds.
- **S4 — copper mutability model.** Implement the four-class model: exogenous
  traces as shovable-but-topology-locked corridor occupants, pours as
  reflow-not-obstacle, capacity accounting that subtracts exogenous lanes.
  Test on a synthetic fixture with pre-placed traces + a pour that the active
  nets must route *around and through*. Gate: active nets route 100% clean
  while every exogenous trace keeps its topology (verified by union-find, not
  eyeball) and the pour reflows legally.
- **S5 — PRIMARY TARGET: boardB ripped region.** Route the unrouted nets of
  `milestones/boardB/boardB_ripped_refilled` into the existing board.
  Escape emission (patterns re-verified against the board), atomic bundles, SI
  gate, pours + stitch vias, and the mutability model all in play. Gate: fully
  routed, 0 errors, 0 unconnected, exogenous topology intact, bundles visually
  hold. Last-resort rip-up, if it fires at all, is logged and touches only
  exogenous copper.
- **S6 — STRETCH / PROOF: boardA full board from unrouted.** From
  `boardA_unrouted.kicad_pcb` to 0 errors / 0 unconnected,
  greenfield: 504 pad-pairs across 233 non-GND nets, **F.Cu/B.Cu only** (proven
  sufficient — the human answer key puts 0 segments on In1.Cu and 1 on In2.Cu,
  so no 3D corridor graph is needed here). This is where router2 stalled at 134
  unconnected; S6 passing proves the architecture is complete. Compare corridor agreement against the
  human answer key (do not copy coordinates — the human board is an oracle, not
  a template).
- **S7 — generalization.** Route a second, structurally different board with no
  board-specific code. Confirms we built a router, not a boardA fitter.

## Performance / Metal (honest, from TOPO_ROUTER)

The topological graph is small (~10⁴ triangles), so the negotiation loop is
CPU-bound on ~10⁴–10⁵ cheap Dijkstra runs — milliseconds each, seconds total on
CPU. **GPU does not pay in the core search.** Where Metal pays: batch legality
(`seg_ok_batch` / `disc_ok_batch` — millions of independent segment-vs-obstacle
tests during realization and capacity refinement, embarrassingly parallel,
matches router1's existing Metal infra) and — added 2026-07-27 — the **global
relaxation realizer** (S3), which is the same shape: thousands of vertices,
purely local interactions, fixed iteration count, every vertex stepped per
iteration. Decision: **CPU-first, batch-shaped kernel APIs from day one, drop in
Metal for batch legality and relaxation when a profile demands it.** No GPU in
the negotiation loop.

## Non-negotiables (project rules that bind this build)

- kicad-cli DRC with the sibling `.kicad_pro` is the sole acceptance oracle;
  prefill zones via pcbnew first. Internal checks only reject bad variants fast.
- No WIP project name in code identifiers (module/type/fn/const/file). Neutral,
  domain names only.
- No silent catch/error swallowing anywhere, including throwaway harness scripts.
- The boardA human board is a *regression oracle and training reference*, never a
  coordinate template — learn topology/order/constraints, not positions.

## Open questions to resolve early

- ~~Inner-layer policy: do signals ever need an inner layer, or is F/B enough?~~
  **RESOLVED 2026-07-27 — F/B is enough for boardA.** The human answer key routes
  F.Cu 2128 / B.Cu 1140 segments with In1.Cu 0 and In2.Cu 1, and zero non-power
  nets on either inner layer; the inner pair really is one full GND plane + one
  power-islands-plus-GND plane. router2's F/B assumption was right. Still open
  for *denser* revisions (boardC does route `PWR_A` across both inner
  layers), which is what a layer-stacked corridor graph is for — later, not S6.
- Escape emission vs CDT: escapes are emitted *before* triangulation so their
  exits are terminals. Confirm the exit points sit on CDT vertices cleanly.
- Certificate of infeasibility: when a corridor is provably over capacity, emit
  "needs a layer / part move" (min-cost-flow bound) rather than looping — turns
  the CM5 "achievable by eye" intuition into a checkable claim.
```
