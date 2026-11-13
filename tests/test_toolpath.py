import numpy as np
import pytest

from micropcb.gcode import parse
from micropcb.planner import Planner
from micropcb.toolpath import ERROR, WARNING, Timeline, Toolpath, check_program

MMPS = [0.004] * 3


def tl_for(code, start=(0, 0, 0), offset=(0, 0, 0)):
    return Timeline.build(Planner(parse(code), MMPS, start, list(offset)))


def test_toolpath_arrays_and_line_lookup():
    tp = Toolpath(parse("G0 X1\nG1 F100\nG2 X3 Y0 I1\nG1 Y5"))
    assert len(tp) > 3
    arc = tp.segments_for_line(3)
    assert arc.stop - arc.start > 10
    assert (tp.line[arc] == 3).all()
    assert tp.segments_for_line(99) == slice(len(tp), len(tp))
    lo, hi = tp.bounds()
    assert hi[1] == pytest.approx(5)


def test_nearest_segment():
    tp = Toolpath(parse("G0 X10\nG1 Y10 F100"))
    assert tp.nearest(10.05, 5, tol=0.1) == 1
    assert tp.nearest(5, 0.02, tol=0.1) == 0
    assert tp.nearest(5, 0.02, tol=0.1, cuts_only=True) is None
    assert tp.nearest(50, 50, tol=1) is None


def test_timeline_matches_planner_and_ends_at_target():
    tl = tl_for("G0 X10 Y5\nG1 Z-1 F60\nM0\nG0 Z2")
    assert tl.pos[0].tolist() == [0, 0, 0]
    assert tl.pos[-1] == pytest.approx([10, 5, 2])
    assert tl.ticks == Planner(parse("G0 X10 Y5\nG1 Z-1 F60\nM0\nG0 Z2"), MMPS,
                               (0, 0, 0), [0, 0, 0]).estimate_ticks()
    assert len(tl.pauses) == 1
    # line numbers never go backwards
    assert (np.diff(tl.lines[1:]) >= 0).all()
    i = tl.index_for_line(4)
    assert tl.lines[i] == 4 and tl.pos[i][2] == pytest.approx(-1, abs=0.06)


def test_timeline_work_coordinates_with_offset():
    # machine at step 1000 (=4 mm) which is work 0 -> positions are work mm
    tl = tl_for("G0 X1", start=(1000, 0, 0), offset=(1000, 0, 0))
    assert tl.pos[0][0] == 0 and tl.pos[-1][0] == pytest.approx(1)


def _sev(issues):
    return {(i.line, i.severity) for i in issues}


def test_checks_find_dangerous_moves():
    code = "\n".join([
        "G0 Z1",            # 1 ok
        "G1 Z-0.1 F50",     # 2 cut with spindle off
        "M3",               # 3
        "G0 X5",            # 4 rapid sideways below surface
        "G0 Z1",            # 5 retract ok
        "G0 Z-0.5",         # 6 rapid plunge
        "G1 Z-3 F50",       # 7 too deep
        "G1 X6 F9000",      # 8 too fast
    ])
    issues = check_program(Toolpath(parse(code)), max_depth_mm=2, max_feed_mm_min=170)
    s = _sev(issues)
    assert (2, ERROR) in s
    assert (4, ERROR) in s
    assert (6, ERROR) in s
    assert (7, WARNING) in s
    assert (8, WARNING) in s
    assert not any(i.line in (1, 5) for i in issues)


def test_checks_clean_file_and_area():
    code = "G0 Z1\nM3\nG0 X1 Y1\nG1 Z-0.1 F50\nG1 X20 F150\nG0 Z1\nM5"
    tp = Toolpath(parse(code))
    assert check_program(tp, max_feed_mm_min=170) == []
    area = (np.array([0, 0, -40.0]), np.array([10, 200, 5.0]))
    out = check_program(tp, max_feed_mm_min=170, area=area)
    assert out and all(i.text.startswith("Outside") for i in out)
