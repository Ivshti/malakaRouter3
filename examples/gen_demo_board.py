#!/usr/bin/env python3
"""Build a small, fully synthetic demo board for escape_gen.py's public README:
one fine-pitch connector (staggered 2-row, 0.4mm pitch within each row, ~1.2mm
between rows -- the same proportions as the real dual-row micro-connector the
tool was proven on) plus two target components for its signal nets to reach,
plus a GND row beyond the back row so the back-row escape has to route past
real obstacles, not open space. No real project data -- generic refs/nets only.
"""
import os
import pcbnew

HERE = os.path.dirname(os.path.abspath(__file__))

board = pcbnew.BOARD()

outline = pcbnew.PCB_SHAPE(board)
outline.SetShape(pcbnew.SHAPE_T_RECT)
outline.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(0), pcbnew.FromMM(0)))
outline.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(40), pcbnew.FromMM(30)))
outline.SetLayer(pcbnew.Edge_Cuts)
outline.SetWidth(pcbnew.FromMM(0.1))
board.Add(outline)


def add_pad(fp, num, x_mm, y_mm, size_mm, net, anchor=(0.0, 0.0)):
    """x_mm/y_mm are ABSOLUTE board coordinates; converted to the footprint-
    relative frame internally (KiCad pads are stored relative to their
    footprint's own anchor)."""
    pad = pcbnew.PAD(fp)
    pad.SetNumber(str(num))
    pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(size_mm), pcbnew.FromMM(size_mm)))
    pad.SetFPRelativePosition(pcbnew.VECTOR2I(pcbnew.FromMM(x_mm - anchor[0]),
                                              pcbnew.FromMM(y_mm - anchor[1])))
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    lset = pcbnew.LSET()
    lset.addLayer(pcbnew.F_Cu)
    lset.addLayer(pcbnew.F_Mask)
    lset.addLayer(pcbnew.F_Paste)
    pad.SetLayerSet(lset)
    if net is not None:
        pad.SetNet(net)
    fp.Add(pad)
    return pad


def make_net(name):
    n = pcbnew.NETINFO_ITEM(board, name)
    board.Add(n)
    return n

net_pwr = make_net("PWR")
net_gnd = make_net("GND")
net_a = make_net("SIG_A")
net_b = make_net("SIG_B")

# --- J1: fine-pitch connector -----------------------------------------------
# Row A (front, y=10.0, closest to board interior): PWR, SIG_A, PWR -- escapes
#   straight out on the top layer, no via needed.
# Row B (back, y=11.2, 1.2mm further from interior, staggered by 0.2mm):
#   PWR, SIG_B -- boxed in by row A ahead of it, must dive to the bottom layer.
# Row C (y=12.4, GND): real obstacles the back row's via/route must clear,
#   not skipped as free space (GND is not escaped by the tool, but its copper
#   still counts as an obstacle for everyone else).
J1_ANCHOR = (20.0, 13.0)   # beyond the GND row, like a real connector's shell
fp1 = pcbnew.FOOTPRINT(board)
fp1.SetReference("J1")
fp1.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(J1_ANCHOR[0]), pcbnew.FromMM(J1_ANCHOR[1])))
add_pad(fp1, "A1", 19.6, 10.0, 0.2, net_pwr, J1_ANCHOR)
add_pad(fp1, "A2", 20.0, 10.0, 0.2, net_a, J1_ANCHOR)
add_pad(fp1, "A3", 20.4, 10.0, 0.2, net_pwr, J1_ANCHOR)
add_pad(fp1, "B1", 19.8, 11.2, 0.2, net_pwr, J1_ANCHOR)
add_pad(fp1, "B2", 20.2, 11.2, 0.2, net_b, J1_ANCHOR)
add_pad(fp1, "C1", 19.6, 12.4, 0.2, net_gnd, J1_ANCHOR)
add_pad(fp1, "C2", 20.0, 12.4, 0.2, net_gnd, J1_ANCHOR)
add_pad(fp1, "C3", 20.4, 12.4, 0.2, net_gnd, J1_ANCHOR)
board.Add(fp1)

# --- U1: target for SIG_A + PWR, elsewhere on the board ---------------------
fp2 = pcbnew.FOOTPRINT(board)
fp2.SetReference("U1")
fp2.SetPosition(pcbnew.VECTOR2I(0, 0))
add_pad(fp2, "1", 7.0, 20.0, 1.0, net_a)
add_pad(fp2, "2", 9.0, 20.0, 1.0, net_pwr)
board.Add(fp2)

# --- U2: target for SIG_B + GND, elsewhere on the board ---------------------
fp3 = pcbnew.FOOTPRINT(board)
fp3.SetReference("U2")
fp3.SetPosition(pcbnew.VECTOR2I(0, 0))
add_pad(fp3, "1", 31.0, 22.0, 1.0, net_b)
add_pad(fp3, "2", 33.0, 22.0, 1.0, net_gnd)
board.Add(fp3)

# --- design settings: standard tier (matches escape_gen.py's default Fab) ---
# Both the board-wide floors (m_TrackMinWidth etc, the absolute minimum any
# net may use) AND the "Default" netclass itself (what DRC actually checks a
# plain net against) need setting -- pcbnew doesn't derive one from the other.
ds = board.GetDesignSettings()
ds.m_TrackMinWidth = pcbnew.FromMM(0.08)
ds.m_MinClearance = pcbnew.FromMM(0.125)
ds.m_ViasMinSize = pcbnew.FromMM(0.3)
ds.m_ViasMinDrill = pcbnew.FromMM(0.15)
ds.m_MinThroughDrill = pcbnew.FromMM(0.15)

default_nc = ds.m_NetSettings.GetDefaultNetclass()
default_nc.SetTrackWidth(pcbnew.FromMM(0.13))
default_nc.SetClearance(pcbnew.FromMM(0.125))
default_nc.SetViaDiameter(pcbnew.FromMM(0.45))
default_nc.SetViaDrill(pcbnew.FromMM(0.2))

OUT = os.path.join(HERE, "demo_board.kicad_pcb")
pcbnew.SaveBoard(OUT, board)
print("wrote", OUT)
