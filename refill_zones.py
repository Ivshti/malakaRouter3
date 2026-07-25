#!/usr/bin/env python3
"""
Scoped zone refill -- refills only the zones whose bounding box is near a given
point, leaving the rest of the board's existing zone fill untouched.

Why this exists: `kicad-cli pcb drc --refill-zones` (KiCad's own built-in
refill) is the correct tool for most cases -- it matches what "Refill All
Zones" does in the GUI. But on a large board with many zones, a *full-board*
refill can expose an unrelated, pre-existing marginal zone-boundary case
somewhere else on the board (a thin sliver a fresh fill algorithm resolves
slightly differently than whatever produced the file you started from) even
though nothing there was touched. Refilling ONLY the zones near your actual
change avoids that whack-a-mole: it recomputes just what could plausibly be
affected by the new copper you added, and leaves everything else exactly as
it was.

Use kicad-cli's --refill-zones first. Reach for this only if a full refill
introduces a violation far from anything you routed.

Usage: refill_zones.py IN.kicad_pcb OUT.kicad_pcb CENTER_X CENTER_Y RADIUS_MM
Requires KiCad's own Python (has the `pcbnew` module), e.g. on macOS:
  /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3
"""
import sys
import pcbnew


def main():
    if len(sys.argv) != 6:
        sys.exit(__doc__)
    src, dst, cx, cy, radius = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5])
    board = pcbnew.LoadBoard(src)
    cx_iu, cy_iu = pcbnew.VECTOR2I(int(cx * 1e6), int(cy * 1e6))
    near = []
    for z in board.Zones():
        bbox = z.GetBoundingBox()
        zx, zy = bbox.GetCenter().x, bbox.GetCenter().y
        d = ((zx - cx_iu) ** 2 + (zy - cy_iu) ** 2) ** 0.5 / 1e6
        if d < radius or bbox.Contains(pcbnew.VECTOR2I(int(cx * 1e6), int(cy * 1e6))):
            near.append(z)
    print(f"Refilling {len(near)} of {len(board.Zones())} zones near ({cx},{cy}) r={radius}mm")
    filler = pcbnew.ZONE_FILLER(board)
    ok = filler.Fill(near)
    print(f"filler.Fill returned {ok}")
    board.BuildConnectivity()
    pcbnew.SaveBoard(dst, board)
    print(f"Saved {dst}")


if __name__ == "__main__":
    main()
