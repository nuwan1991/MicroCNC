import math
import time

import pytest

from micropcb.controller import MachineController
from micropcb.gcode import DWELL, END, LINE, PAUSE, SPINDLE, GCodeError, parse
from micropcb.job import DONE, PAUSED_STATE, RUNNING, STOPPED, FAILED
from micropcb.planner import FINISHED, PAUSED, TICK, Planner
from micropcb.settings import MachineState, Settings
from micropcb.sim_machine import SimMachine

MMPS = [0.004] * 3


# ------------------------------------------------------------------ parser
def test_parse_basic_modal_and_units():
    p = parse("G21 G90\nG0 X1 Y2\nG1 Z-0.1 F100\nX3\nG91 Y1\nG20 G90 X1")
    lines = p.lines()
    assert [m.end for m in lines] == [(1, 2, 0), (1, 2, -0.1), (3, 2, -0.1),
                                      (3, 3, -0.1), (25.4, 3, -0.1)]
    assert lines[0].rapid and not lines[1].rapid
    assert lines[2].feed == 100


def test_parse_comments_n_words_and_warnings():
    p = parse("%\nN10 G0 X1 (move) ; trailing\nG1 X2 F50 S10000\nM8\nG43 H1\n%")
    assert len(p.lines()) == 2
    assert any("S (spindle speed)" in w for w in p.warnings)
    assert any("G43" in w for w in p.warnings)


def test_parse_errors():
    with pytest.raises(GCodeError):
        parse("G1 X1")           # no feed
    with pytest.raises(GCodeError):
        parse("G18\nG2 X1 I1")   # XZ plane
    with pytest.raises(GCodeError):
        parse("G0 X1 hello")


def test_spindle_pause_dwell_end():
    p = parse("M3\nG4 P0.5\nM6 T2\nM0\nM5\nM30\nG0 X99")
    kinds = [m.kind for m in p.moves]
    assert kinds == [SPINDLE, DWELL, PAUSE, PAUSE, SPINDLE, END]
    assert p.moves[2].value == "Change tool to T2"
    assert not p.lines()  # nothing after M30


def test_spindle_switched_off_at_end_if_left_on():
    p = parse("M3\nG1 X1 F60")
    assert p.moves[-1].kind == SPINDLE and p.moves[-1].value is False


def test_g92_shifts_coordinates():
    p = parse("G0 X5\nG92 X0\nG0 X1", start=(0, 0, 0))
    assert p.lines()[-1].end == (6, 0, 0)


def test_relative_start_position():
    p = parse("G91 G0 X1", start=(10, 20, 3))
    assert p.lines()[0].end == (11, 20, 3)


@pytest.mark.parametrize("code", ["G2 X10 Y0 I5 J0", "G2 X10 Y0 R5"])
def test_arc_cw_half_circle_goes_through_top(code):
    p = parse(f"G1 F100\n{code}")
    pts = [m.end for m in p.lines()]
    assert pts[-1] == (10, 0, 0)
    assert max(pt[1] for pt in pts) == pytest.approx(5, abs=0.01)
    for x, y, _ in pts:
        assert math.hypot(x - 5, y) == pytest.approx(5, abs=1e-6)
    b = p.bounds()
    assert b[1][1] == pytest.approx(5, abs=0.01)


def test_r_arc_sign_picks_short_or_long_arc():
    def length(code):
        return sum(math.dist(m.start, m.end) for m in parse("G1 F100\n" + code).lines())
    quarter = math.pi * 5 / 2
    assert length("G2 X5 Y5 R5") == pytest.approx(quarter, rel=1e-3)
    assert length("G2 X5 Y5 R-5") == pytest.approx(3 * quarter, rel=1e-3)
    # short CW arc from (0,0) to (5,5) has its centre at (5,0)
    for m in parse("G1 F100\nG2 X5 Y5 R5").lines():
        assert math.hypot(m.end[0] - 5, m.end[1]) == pytest.approx(5, abs=1e-6)


def test_arc_ccw_full_circle_and_chord_error():
    p = parse("G1 F100\nG3 X0 Y0 I5 J0")
    segs = p.lines()
    assert len(segs) > 50
    assert min(m.end[1] for m in segs) == pytest.approx(-5, abs=0.01)
    for m in segs:  # chord midpoint within tolerance of circle
        mx, my = (m.start[0] + m.end[0]) / 2, (m.start[1] + m.end[1]) / 2
        assert 5 - math.hypot(mx - 5, my) <= 0.0051


# ------------------------------------------------------------------ planner
def _run_planner(pl):
    pos = list(pl.cur)
    ticks = []
    for kind, t, _ in pl.items():
        if kind == TICK:
            ticks.append(t)
            pos = [pos[0] + t.x, pos[1] + t.y, pos[2] + t.z]
    return pos, ticks


def test_planner_ends_exactly_and_respects_step_limit():
    p = parse("G0 Z1\nG0 X10.0021 Y3.3333\nG1 X0 Y0 Z-0.1 F100\nG2 X10 Y0 I5 F150\nG0 Z2")
    pl = Planner(p, MMPS, (0, 0, 0), [0, 0, 0], 14)
    pos, ticks = _run_planner(pl)
    assert pos == [2500, 0, 500]
    assert max(max(abs(t.x), abs(t.y), abs(t.z)) for t in ticks) <= 14


def test_planner_offset_and_start():
    p = parse("G0 X1")
    pl = Planner(p, MMPS, (1000, 7, 0), [1000, 0, 0])
    pos, _ = _run_planner(pl)
    assert pos == [1250, 0, 0]


def test_feed_rate_sets_speed():
    p = parse("G1 X20 F60")  # 1 mm/s -> 0.0196 mm/tick = 4.9 steps/tick
    pl = Planner(p, MMPS, (0, 0, 0), [0, 0, 0])
    _, ticks = _run_planner(pl)
    secs = len(ticks) * 0.0196
    assert secs == pytest.approx(20, rel=0.03)
    assert max(t.x for t in ticks) <= 5


def test_rapid_uses_max_and_ramps():
    p = parse("G0 X40")
    pl = Planner(p, MMPS, (0, 0, 0), [0, 0, 0])
    _, ticks = _run_planner(pl)
    xs = [t.x for t in ticks]
    assert xs[0] <= 4 and xs[-1] <= 4       # starts and ends slow
    assert xs.count(14) > len(xs) * 0.8      # cruises at max
    for a, b in zip(xs, xs[1:-1]):
        assert abs(b - a) <= 3               # no sudden jumps


def test_sharp_corner_slows_down():
    p = parse("G1 X10 F300\nG1 Y10")
    pl = Planner(p, MMPS, (0, 0, 0), [0, 0, 0])
    _, ticks = _run_planner(pl)
    corner = next(i for i, t in enumerate(ticks) if t.y != 0)
    near = ticks[corner - 1:corner + 1]
    assert all(abs(t.x) + abs(t.y) <= 8 for t in near)


def test_spindle_bit_and_spinup():
    p = parse("M3\nG1 X1 F100\nM5")
    pl = Planner(p, MMPS, (0, 0, 0), [0, 0, 0])
    _, ticks = _run_planner(pl)
    assert ticks[0].spindle and ticks[0].is_zero()
    moving = [t for t in ticks if not t.is_zero()]
    assert all(t.spindle for t in moving)
    assert not ticks[-1].spindle
    spinup = 0
    for t in ticks:
        if not t.is_zero():
            break
        spinup += 1
    assert spinup * 0.0196 == pytest.approx(1.0, abs=0.05)


def test_pause_mid_move_and_resume_is_exact():
    p = parse("G0 X40")
    pl = Planner(p, MMPS, (0, 0, 0), [0, 0, 0])
    pos = [0, 0, 0]
    events = []
    for n, (kind, t, _) in enumerate(pl.items()):
        if n == 100:
            pl.request_pause()
        if kind == TICK:
            pos[0] += t.x
        else:
            events.append((kind, pos[0]))
    assert events[0][0] == PAUSED and 0 < events[0][1] < 10000
    assert events[-1][0] == FINISHED
    assert pos[0] == 10000


def test_program_pause_item():
    pl = Planner(parse("G0 X1\nM0\nG0 X2"), MMPS, (0, 0, 0), [0, 0, 0])
    kinds = [k for k, _, _ in pl.items() if k != TICK]
    assert kinds == [PAUSED, FINISHED]


def test_estimate_time():
    pl = Planner(parse("G1 X10 F60"), MMPS, (0, 0, 0), [0, 0, 0])
    assert pl.estimate_seconds() == pytest.approx(10, rel=0.03)
    assert pl.estimate_ticks() == pl.estimate_ticks()  # does not consume the planner


# ------------------------------------------------------------------ job runner
@pytest.fixture
def ctl():
    c = MachineController(Settings(), MachineState(), persist=False)
    sim = SimMachine(realtime=False)
    c.connect(transport=sim)
    c.sim = sim
    yield c
    c.disconnect()


def _wait(pred, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.005)
    return False


def test_job_runs_to_end_on_simulator(ctl):
    ctl.zero_axes()
    job = ctl.start_job("G21\nM3\nG1 Z-0.2 F60\nG1 X5 Y2 F150\nG2 X5 Y2 I1\nG0 Z1\nM5\nM2")
    assert _wait(lambda: job.state == DONE)
    assert ctl.sim.pos == [1250, 500, 250]
    assert ctl.work_mm() == pytest.approx((5, 2, 1))
    assert not ctl.sim.spindle
    assert job.progress() == 1.0


def test_job_requires_valid_position(ctl):
    ctl.link.position_valid = False
    with pytest.raises(RuntimeError, match="zero"):
        ctl.start_job("G0 X1")


def test_job_blocks_jog(ctl):
    ctl.zero_axes()
    job = ctl.start_job("G0 X50")
    with pytest.raises(RuntimeError, match="job"):
        ctl.jog("x", 1)
    ctl.stop_job()
    assert _wait(lambda: not job.active)


def test_job_pause_resume(ctl):
    ctl.zero_axes()
    job = ctl.start_job("G0 X20")
    assert _wait(lambda: ctl.sim.pos[0] > 500)
    ctl.pause_job()
    assert _wait(lambda: job.state == PAUSED_STATE)
    p = ctl.sim.pos[0]
    time.sleep(0.1)
    assert ctl.sim.pos[0] == p < 5000
    ctl.resume_job()
    assert _wait(lambda: job.state == DONE)
    assert ctl.sim.pos[0] == 5000


def test_program_pause_m0(ctl):
    ctl.zero_axes()
    job = ctl.start_job("G0 X1\nM0\nG0 X2")
    assert _wait(lambda: job.state == PAUSED_STATE)
    assert "M0" in job.message and ctl.sim.pos[0] == 250
    ctl.resume_job()
    assert _wait(lambda: job.state == DONE)
    assert ctl.sim.pos[0] == 500


def test_stop_turns_spindle_off(ctl):
    ctl.zero_axes()
    job = ctl.start_job("M3\nG1 X30 F150")
    assert _wait(lambda: ctl.sim.pos[0] > 100)
    ctl.stop_job()
    assert _wait(lambda: not job.active)
    assert job.state == STOPPED
    ctl.wait_idle(2)
    assert not ctl.sim.spindle
    assert ctl.sim.pos[0] < 7500


def test_release_fails_job(ctl):
    ctl.zero_axes()
    job = ctl.start_job("G0 X30")
    assert _wait(lambda: ctl.sim.pos[0] > 100)
    ctl.sim.press_release_button()
    assert _wait(lambda: not job.active)
    assert job.state == FAILED and "released" in job.message.lower()


def test_soft_limit_check_blocks_job(ctl):
    ctl.set_home()
    ctl.zero_axes()
    with pytest.raises(RuntimeError, match="outside"):
        ctl.start_job("G0 X-5")
    with pytest.raises(RuntimeError, match="outside"):
        ctl.start_job("G0 Z1")   # Z top is 0 at home
    ctl.start_job("G0 X5 Z-1")


def test_tool_change_jog_rezero_and_return(ctl):
    """M6 pause -> jog away, re-zero Z 0.5 mm higher -> resume returns to the
    path and the rest of the job runs 0.5 mm (125 steps) higher."""
    ctl.zero_axes()
    job = ctl.start_job("G0 X5\nG1 Z-1 F60\nM6 (Change tool)\nG1 X10 F150\nG0 Z2")
    assert _wait(lambda: job.state == PAUSED_STATE)
    assert job.message == "Change tool"
    assert ctl.sim.pos == [1250, 0, -250]
    ctl.jog("z", 10)                 # lift to change the tool
    ctl.jog("x", -3)
    ctl.wait_idle(10)
    ctl.jog("z", -9.5)               # new tool touches 0.5 mm higher
    ctl.wait_idle(10)
    ctl.zero_axes("z")               # new tool on the copper -> work Z 0 here
    before = list(ctl.state.work_offset)
    ctl.resume_job()
    assert _wait(lambda: job.state == DONE)
    dz = before[2]                   # new Z zero in machine steps (old one was 0)
    assert ctl.sim.pos[0] == 2500
    # final G0 Z2 lands 2 mm above the NEW zero
    assert ctl.sim.pos[2] == dz + 500


def test_jog_allowed_only_when_paused(ctl):
    ctl.zero_axes()
    job = ctl.start_job("G0 X30\nM0\nG0 X0")
    with pytest.raises(RuntimeError):
        ctl.jog("y", 1)
    assert _wait(lambda: job.state == PAUSED_STATE)
    ctl.jog("y", 1)
    ctl.wait_idle(5)
    ctl.resume_job()                 # returns Y to the path first
    assert _wait(lambda: job.state == DONE)
    assert ctl.sim.pos[:2] == [0, 0]
