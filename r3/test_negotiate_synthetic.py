#!/usr/bin/env python3
"""
Synthetic, geometry-free unit tests for the negotiation core (negotiate.py).

Fixture triangulations don't happen to have multiple nets sharing one scarce
corridor, so this constructs tiny hand-built graphs directly (no CDT involved)
to isolate and prove `negotiate_core` itself — the SAME function the real
KiCad-facing `negotiate()` calls, not a re-implementation of its logic:

  1. Two nets forced through a single-lane bottleneck with NO alternate path
     -> must NOT silently claim success; must report genuine infeasibility.
  2. Same bottleneck, but WITH a higher-cost alternate path of its own capacity
     -> negotiation must reroute one net onto the alternate so BOTH succeed
        and 0 edges end up overused. This is the actual differentiator: a
        greedy router commits the first net to the short path and fails the
        second; negotiation discovers the reroute is better globally.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from corridor import CorridorEdge, Fab  # noqa: E402
from negotiate import negotiate_core  # noqa: E402
import numpy as np


def make_bottleneck_graph(alt_lanes=0):
    """
    Triangles 0..5: two sources (0,1) must each reach sink 4, squeezed through
    a shared single-lane bottleneck edge (2,3), plus an optional alternate
    route via triangle 5 (only wired if alt_lanes > 0).

        0 --(lanes=5)-- 2 --(lanes=1)-- 3 --(lanes=5)-- 4
        1 --(lanes=5)--/
        [alternate, if alt_lanes>0]:  0 --(lanes=alt)-- 5 --(lanes=alt)-- 3
    """
    coords = {0: (0, 0), 1: (0, 2), 2: (2, 1), 3: (4, 1), 4: (6, 1), 5: (2, 4)}
    n_tris = 6

    class FakeGraph:
        def __init__(self):
            self.tri_adj = [[] for _ in range(n_tris)]
            self.edges = {}
            self._coords = coords

        def centroid(self, ti):
            return self._coords[ti]

    g = FakeGraph()

    def link(a, b, lanes):
        key = (a, b) if a < b else (b, a)
        g.edges[key] = CorridorEdge(key[0], key[1], width=10.0, lanes=lanes)
        g.tri_adj[a].append((b, key[0], key[1]))
        g.tri_adj[b].append((a, key[0], key[1]))

    link(0, 2, 5)
    link(1, 2, 5)
    link(2, 3, 1)     # the bottleneck: only 1 lane
    link(3, 4, 5)
    if alt_lanes > 0:
        link(0, 5, alt_lanes)
        link(5, 3, alt_lanes)
    return g


def test_infeasible_bottleneck():
    g = make_bottleneck_graph(alt_lanes=0)
    sources = {"A": [0], "B": [1]}
    sinks = {"A": {4}, "B": {4}}
    routed, failed, iterations, converged = negotiate_core(
        g, sources, sinks, max_iterations=20, log=lambda s: None)

    assert not converged, (
        "FAIL: negotiate_core claimed success on a provably infeasible single-"
        "lane bottleneck with 2 competing nets and no alternate route — this "
        "would be a silent correctness bug (false convergence)")
    print(f"PASS: infeasible bottleneck correctly reported as NOT converged "
          f"after {iterations} iterations (routed={list(routed)}, failed={failed})")


def test_negotiation_finds_alternate():
    g = make_bottleneck_graph(alt_lanes=5)
    sources = {"A": [0], "B": [1]}
    sinks = {"A": {4}, "B": {4}}
    routed, failed, iterations, converged = negotiate_core(
        g, sources, sinks, max_iterations=20, log=lambda s: None)

    assert converged, ("FAIL: negotiate_core failed to converge even though a "
                       "legal alternate route exists")
    assert not failed, f"FAIL: converged but reported failed nets: {failed}"
    used_alt = any(5 in path for (path, _edges, _len) in routed.values())
    assert used_alt, ("FAIL: converged, but neither net was pushed onto the "
                      "alternate route — the bottleneck may not have been "
                      "genuinely contended")
    print(f"PASS: negotiate_core converged in {iterations} iterations and "
          "rerouted one net onto the alternate path to relieve the bottleneck")


if __name__ == "__main__":
    test_infeasible_bottleneck()
    test_negotiation_finds_alternate()
    print("\nALL SYNTHETIC NEGOTIATION TESTS PASSED")
