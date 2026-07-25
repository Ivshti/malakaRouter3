#!/usr/bin/env python3
"""
DRC-driven cleanup: iteratively removes dangling track/via stubs and
redundant GND/GNDPWR stitching vias that DRC flags for hole_clearance, until
stable.

Why this exists: automated routing passes (route.py's rescue/rip-up logic,
plane-repair tools, etc.) sometimes leave behind orphaned copper -- a stub
that stopped a fraction short, a stitching via that duplicates a nearby one
closely enough to violate hole-to-hole. This removes exactly the items
kicad-cli DRC names (`track_dangling`, `via_dangling`, `hole_clearance` on a
GND/GNDPWR via), by coordinate match against the raw .kicad_pcb text -- robust
to KiCad's own number formatting. kicad-cli DRC is the oracle; each round
re-runs it, so a chain of dead-ends peels back one leaf at a time rather than
needing to be found all at once.

Usage: cleanup_dangling.py IN.kicad_pcb OUT.kicad_pcb PROJECT.kicad_pro [max_rounds]
Requires only `kicad-cli` on PATH (no pcbnew/KiCad-Python needed).
"""
import re
import shutil
import subprocess
import sys

REDUNDANT_NETS = ("GND", "GNDPWR")   # a stitching via here is redundant to remove


def find_blocks(content, tag):
    """Yield (start, end, text) for every top-level-ish `(tag ...)` s-expr."""
    out = []
    i = 0
    needle = "(" + tag
    while True:
        j = content.find(needle, i)
        if j < 0:
            break
        depth = 0
        k = j
        while k < len(content):
            if content[k] == "(":
                depth += 1
            elif content[k] == ")":
                depth -= 1
                if depth == 0:
                    out.append((j, k + 1, content[j:k + 1]))
                    break
            k += 1
        i = k + 1
    return out


def near(a, b, tol=2e-3):
    return abs(a - b) <= tol


def run_drc(pcb, rpt):
    subprocess.run(["kicad-cli", "pcb", "drc", pcb, "-o", rpt], capture_output=True)
    return open(rpt).read()


def targets_from_drc(txt):
    """Return (via_pts, seg_pts): via centers to delete, segment-endpoint
    coords whose segment to delete."""
    via_pts, seg_pts = set(), set()
    for blk in re.split(r"\n(?=\[)", txt):
        m = re.match(r"\[([a-z_]+)\]", blk)
        if not m:
            continue
        cat = m.group(1)
        lines = blk.splitlines()
        if cat == "via_dangling":
            for ln in lines:
                mm = re.search(r"@\(([\d.]+) mm, ([\d.]+) mm\): Via", ln)
                if mm:
                    via_pts.add((round(float(mm.group(1)), 3), round(float(mm.group(2)), 3)))
        elif cat == "track_dangling":
            for ln in lines:
                mm = re.search(r"@\(([\d.]+) mm, ([\d.]+) mm\): Track", ln)
                if mm:
                    seg_pts.add((round(float(mm.group(1)), 3), round(float(mm.group(2)), 3)))
        elif cat == "hole_clearance":
            for ln in lines:
                mm = re.search(r"@\(([\d.]+) mm, ([\d.]+) mm\): Via \[([^\]]+)\]", ln)
                if mm and mm.group(3).strip() in REDUNDANT_NETS:
                    via_pts.add((round(float(mm.group(1)), 3), round(float(mm.group(2)), 3)))
    return via_pts, seg_pts


def remove(content, via_pts, seg_pts):
    drop = []
    for s, e, t in find_blocks(content, "via"):
        m = re.search(r"\(at ([-\d.]+) ([-\d.]+)\)", t)
        if not m:
            continue
        x, y = float(m.group(1)), float(m.group(2))
        if any(near(x, vx) and near(y, vy) for vx, vy in via_pts):
            drop.append((s, e))
    for s, e, t in find_blocks(content, "segment"):
        m = re.search(r"\(start ([-\d.]+) ([-\d.]+)\).*?\(end ([-\d.]+) ([-\d.]+)\)", t, re.S)
        if not m:
            continue
        sx, sy, ex, ey = map(float, m.groups())
        if any((near(sx, px) and near(sy, py)) or (near(ex, px) and near(ey, py))
               for px, py in seg_pts):
            drop.append((s, e))
    drop.sort(reverse=True)   # remove from the back so earlier offsets stay valid
    for s, e in drop:
        content = content[:s] + content[e:]
    return content, len(drop)


def main():
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    inp, outp, pro = sys.argv[1], sys.argv[2], sys.argv[3]
    rounds = int(sys.argv[4]) if len(sys.argv) > 4 else 6
    shutil.copy(inp, outp)
    shutil.copy(pro, outp[:-len(".kicad_pcb")] + ".kicad_pro")
    rpt = outp[:-len(".kicad_pcb")] + ".cleanup.rpt"
    for r in range(rounds):
        txt = run_drc(outp, rpt)
        via_pts, seg_pts = targets_from_drc(txt)
        if not via_pts and not seg_pts:
            print(f"round {r}: nothing to remove — stable")
            break
        content = open(outp).read()
        content, n = remove(content, via_pts, seg_pts)
        open(outp, "w").write(content)
        print(f"round {r}: removed {n} block(s) "
              f"({len(via_pts)} via targets, {len(seg_pts)} track targets)")
        if n == 0:
            print("  WARNING: DRC named targets but none matched by coordinate — stopping")
            break


if __name__ == "__main__":
    main()
