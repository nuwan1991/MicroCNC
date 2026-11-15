import math
import time

import pytest
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

from micropcb.controller import MachineController
from micropcb.gcode import PAUSE, parse
from micropcb.job import DONE, PAUSED_STATE
from micropcb.pcb import cam
from micropcb.pcb.board import Placement, PlacedBoard
from micropcb.pcb.excellon import Hole
from micropcb.pcb.tools import DRILL, ENDMILL, VBIT, Tool, ToolLibrary
from micropcb.settings import MachineState, Settings
from micropcb.sim_machine import SimMachine

VB = Tool("V30", VBIT, 3.175, angle=30, tip=0.1, feed=100, plunge=30)
EM = Tool("EM1", ENDMILL, 1.0, feed=120, plunge=30, stepdown=0.6)
DR = Tool("D0.8", DRILL, 0.8, feed=60, plunge=40, stepdown=0.5)


def board(gap=1.0):
    """20 x 10 board, two pads + a track, 2 holes."""
    pads = [Point(5, 5).buffer(1.0), Point(5 + 2 + gap, 5).buffer(1.0)]
    track = LineString([(12, 5), (18, 5)]).buffer(0.25)
    pb = PlacedBoard(size=(20, 10), placement=Placement())
    pb.outline = box(0, 0, 20, 10)
    pb.copper = {"top": unary_union(pads + [track]), "bottom": Polygon()}
    pb.holes = [Hole(5, 5, 0.8), Hole(5 + 2 + gap, 5, 0.8), Hole(15, 8, 3.0)]
    return pb


def test_tool_widths_and_library(tmp_path):
    assert VB.cut_width(0.1) == pytest.approx(0.1 + 2 * 0.1 * math.tan(math.radians(15)))
    assert EM.cut_width(1) == 1.0
    lib = ToolLibrary()
    lib.save(tmp_path / "t.json")
    assert ToolLibrary.load(tmp_path / "t.json").tools == lib.tools
    assert 0.8 in lib.drill_sizes()


def test_isolation_surrounds_copper_at_tool_radius():
    pb = board()
    r = cam.isolation(pb, "top", VB, depth=0.1, passes=1)
    w = VB.cut_width(0.1)
    assert r.paths and r.problem_area is None
    for p in r.paths:
        d = LineString(p).distance(pb.copper["top"])
        assert d == pytest.approx(w / 2, abs=0.01)
    prog = parse(cam.combine([r], "t"))
    assert prog.bounds()[0][2] == pytest.approx(-0.1)
    # two passes go further out
    r2 = cam.isolation(pb, "top", VB, depth=0.1, passes=2, overlap=0.5)
    assert len(r2.paths) > len(r.paths)


def test_reach_check_flags_narrow_gap_only():
    narrow = board(gap=0.1)
    r = cam.isolation(narrow, "top", VB, depth=0.1)     # width ~0.15 > gap 0.1
    assert r.problem_area is not None and "too narrow" in " ".join(r.notes)
    assert r.problem_area.centroid.x == pytest.approx(6.05, abs=0.05)   # between the pads
    assert cam.isolation(board(gap=1.0), "top", VB, depth=0.1).problem_area is None


def test_climb_direction():
    pb = board()
    outer = cam.isolation(pb, "top", VB, climb=True).paths
    from shapely.geometry import LinearRing
    rings = [LinearRing(p) for p in outer if p[0] == p[-1]]
    assert rings and all(not r.is_ccw for r in rings)   # outside contours CW


def test_drill_assignment_peck_and_mill():
    pb = board()
    groups, mill = cam.assign_drills(pb.holes, [0.8, 1.0], tolerance=0.15, mill_above=2.0)
    assert list(groups) == [0.8] and len(groups[0.8]) == 2 and len(mill) == 1
    d = cam.drilling(groups[0.8], DR, depth=1.6, peck=0.5)
    zs = [float(l.split("Z")[1].split()[0]) for l in d.lines if l.startswith("G1 Z")]
    assert zs == [-0.5, -1.0, -1.5, -1.6] * 2
    m = cam.mill_holes(mill, EM, depth=1.6)
    prog = parse(cam.combine([m], "m"))
    xs = [p[0] for mv in prog.lines() for p in (mv.end,)]
    assert max(xs) == pytest.approx(15 + (3.0 - 1.0) / 2, abs=1e-3)
    assert prog.bounds()[0][2] == pytest.approx(-1.6)


def test_cutout_offset_passes_and_tabs():
    pb = board()
    r = cam.cutout(pb, EM, thickness=1.6, tabs=4, tab_width=3, tab_height=0.6)
    prog = parse(cam.combine([r], "c"))
    lo, hi = prog.bounds()
    assert lo[0] == pytest.approx(-0.5, abs=0.01) and hi[0] == pytest.approx(20.5, abs=0.01)
    assert lo[2] == pytest.approx(-1.7)
    zs = sorted({round(m.end[2], 3) for m in prog.lines() if not m.rapid})
    assert -1.0 in zs           # tab top = -(1.6 - 0.6)
    assert -1.7 in zs


def test_clearing_with_rest_tool():
    pb = board()
    ops = cam.clearing(pb, "top", EM, depth=0.1, keep=0.2, rest_tool=VB)
    assert ops and ops[0].paths
    for p in ops[0].paths:   # never touches kept copper
        assert LineString(p).distance(pb.copper["top"]) >= 0.2 + 0.5 - 0.02


def test_pins_on_flip_axis():
    pb = board()
    pb.placement = Placement(flip_axis="x", margin=10)
    pb.size = (20, 10)
    assert cam.pin_positions(pb, 5) == [(20.0, 5.0), (20.0, 25.0)]
    pb.placement = Placement(flip_axis="y", margin=10)
    assert cam.pin_positions(pb, 5) == [(5.0, 15.0), (35.0, 15.0)]
    pb.placement = Placement(margin=0)
    assert "below work 0" in " ".join(cam.pins(pb, DR, 3.0).notes)


def test_combine_tool_changes_and_run_on_simulator():
    pb = board()
    groups, _ = cam.assign_drills(pb.holes, [0.8], mill_above=2.0)
    ops = [cam.isolation(pb, "top", VB, depth=0.05),
           cam.drilling(groups[0.8], DR, depth=0.5)]
    text = cam.combine(ops, "test")
    prog = parse(text)
    pauses = [m for m in prog.moves if m.kind == PAUSE]
    assert len(pauses) == 1 and "D0.8" in pauses[0].value
    c = MachineController(Settings(), MachineState(), persist=False)
    sim = SimMachine(realtime=False)
    c.connect(transport=sim)
    try:
        c.zero_axes()
        job = c.start_job(text, "t")
        end = time.time() + 30
        while job.state != PAUSED_STATE and time.time() < end:
            time.sleep(0.01)
        assert job.state == PAUSED_STATE
        c.resume_job()
        while job.state != DONE and time.time() < end:
            time.sleep(0.01)
        assert job.state == DONE
        assert sim.pos[2] == 500        # ends at safe Z 2 mm
    finally:
        c.disconnect()


def test_copper_gaps_and_recommendation():
    pb = board(gap=0.2)
    gaps = cam.copper_gaps(pb.copper["top"])
    assert gaps[0] == pytest.approx(0.2, abs=0.01)
    wide = Tool("V60", VBIT, 3.175, angle=60, tip=0.3)
    adv, summary = cam.recommend_isolation(pb.copper["top"], [wide, VB, EM])
    assert "0.200" in summary
    best = adv[0]
    assert best.tool is VB and best.narrow == 0
    assert 0.04 <= best.depth <= 0.08 and best.width <= 0.9 * 0.2 + 1e-6
    assert adv[-1].narrow > 0          # 0.3 tip / end mill cannot fit


def test_problem_spots_listed():
    pb = board(gap=0.1)
    op = cam.isolation(pb, "top", VB, 0.1)
    spots = cam.problem_spots(op.problem_area)
    assert len(spots) == 1
    x, y, _ = spots[0]
    assert x == pytest.approx(6.05, abs=0.1) and y == pytest.approx(5, abs=0.1)
