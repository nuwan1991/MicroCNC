import math
import threading

import numpy as np
import pytest

from micropcb.autolevel import HeightMap, apply_heightmap, make_grid, probe_order
from micropcb.controller import MachineController, ProbeCancelled, ProbeError
from micropcb.gcode import parse
from micropcb.settings import MachineState, Settings
from micropcb.sim_machine import SimMachine

S = 250  # steps per mm


# ------------------------------------------------------------------ pure parts
def test_grid_and_order():
    xs, ys = make_grid(0, 0, 30, 20, spacing=10, margin=1)
    assert xs[0] == -1 and xs[-1] == 31 and len(xs) == 5
    assert ys[0] == -1 and ys[-1] == 21 and len(ys) == 4
    assert probe_order(3, 2) == [(0, 0), (1, 0), (2, 0), (2, 1), (1, 1), (0, 1)]
    xs, ys = make_grid(0, 0, 1, 1, spacing=10)
    assert len(xs) == 2 and len(ys) == 2


def test_bilinear_interpolation_and_stats():
    hm = HeightMap(np.array([0.0, 10.0]), np.array([0.0, 10.0]),
                   np.array([[0.0, 0.1], [0.2, 0.3]]))
    assert hm.at(0, 0) == 0 and hm.at(10, 10) == pytest.approx(0.3)
    assert hm.at(5, 5) == pytest.approx(0.15)
    assert hm.at(-5, 0) == 0          # clamped outside
    st = hm.stats()
    assert st["range"] == pytest.approx(0.3)
    assert st["tilt_x_per_100"] == pytest.approx(1.0)
    assert st["tilt_y_per_100"] == pytest.approx(2.0)


def test_json_roundtrip():
    hm = HeightMap.empty(np.array([0.0, 5.0]), np.array([0.0, 5.0]))
    hm.z[0, 0] = 0.0
    hm2 = HeightMap.from_json(hm.to_json())
    assert hm2.probed == 1 and not hm2.complete
    with pytest.raises(ValueError):
        HeightMap.from_json('{"format": "x"}')


def test_apply_splits_and_follows_surface():
    hm = HeightMap(np.array([0.0, 10.0]), np.array([0.0, 10.0]),
                   np.array([[0.0, 0.2], [0.0, 0.2]]))   # rises 0.2 mm along X
    prog = parse("G0 Z1\nG1 Z-0.1 F50\nG1 X10 F100\nG0 Z1")
    out = apply_heightmap(prog, hm, max_seg_mm=1.0)
    cut = [m for m in out.lines() if m.line == 3]
    assert len(cut) == 10
    for m in cut:
        assert m.end[2] == pytest.approx(-0.1 + 0.02 * m.end[0])
    with pytest.raises(ValueError):
        apply_heightmap(prog, HeightMap.empty(hm.xs, hm.ys))


# ------------------------------------------------------------------ with simulator
def _ctl(surface, fw_offset=-12345):
    c = MachineController(Settings(), MachineState(), persist=False)
    sim = SimMachine(realtime=False, surface=surface, fw_z_offset=fw_offset)
    c.connect(transport=sim)
    c.sim = sim
    c.zero_axes()
    return c


def test_probe_finds_surface_and_keeps_position_exact():
    c = _ctl(lambda x, y: -500)          # copper 2 mm below start
    try:
        z = c.probe(5)
        assert z == pytest.approx(-2.0, abs=3 * 3 * 0.004)
        assert c.position_valid
        c.jog("z", 1)
        c.wait_idle(5)
        # second probe comes from the machine counter: identical, no new guess
        err = c.link.position[2] - c.sim.pos[2]
        z2 = c.probe(3)
        assert z2 == pytest.approx(z)
        assert c.link.position[2] - c.sim.pos[2] == err
    finally:
        c.disconnect()


def test_probe_no_touch_raises():
    c = _ctl(lambda x, y: -5000)
    try:
        with pytest.raises(ProbeError, match="No touch"):
            c.probe(1)
        assert c.sim.pos[2] == -250
    finally:
        c.disconnect()


def test_idle_touch_does_not_lose_position():
    c = _ctl(lambda x, y: -500)
    try:
        events = []
        c.listener = lambda e, p: events.append(e)
        c.link._handle_status(type("R", (), {"code": 4, "value": 7})(), b"\x7f\x04")
        assert c.position_valid and c.state.home_set is False or c.position_valid
        assert "probe" in events
    finally:
        c.disconnect()


def test_autolevel_measures_tilted_warped_board():
    # surface in sim steps: 1 mm below start, tilt 0.1 mm per 10 mm in X, bump in Y
    def surface(x, y):
        xmm, ymm = x / S, y / S
        return round((-1.0 + 0.01 * xmm + 0.05 * math.sin(ymm / 20 * math.pi)) * S)

    c = _ctl(surface)
    try:
        xs, ys = make_grid(0, 0, 20, 20, spacing=10)
        hm = HeightMap.empty(xs, ys)
        seen = []
        c.run_autolevel(hm, on_point=lambda i, j, z: seen.append((i, j)))
        assert hm.complete and len(seen) == 9
        assert hm.z[0, 0] == 0
        for j, y in enumerate(ys):
            for i, x in enumerate(xs):
                expect = surface(x * S, y * S) - surface(0, 0)
                assert hm.z[j, i] == pytest.approx(expect / S, abs=0.0041)
        # tool ends above the last point, position still exact
        assert c.link.position[:2] == tuple(c.sim.pos[:2])
        assert abs(c.link.position[2] - c.sim.pos[2]) <= 6  # first-touch guess only
    finally:
        c.disconnect()


def test_autolevel_cancel():
    c = _ctl(lambda x, y: -250)
    try:
        stop = threading.Event()
        stop.set()
        with pytest.raises(ProbeCancelled):
            c.run_autolevel(HeightMap.empty(*make_grid(0, 0, 10, 10, 10)), stop=stop)
    finally:
        c.disconnect()


def test_autolevel_lifts_before_moving_to_first_point():
    """Hardware 2026-11-14: the tool moved to the first grid point without lifting.
    Every sideways move must happen with Z raised by the clearance."""
    from micropcb.protocol import decode_packet
    c = _ctl(lambda x, y: -250)            # copper 1 mm below the start
    try:
        c.zero_axes()
        xy_at_z = []                       # machine Z when each XY packet arrives
        orig = c.sim.write

        def spy(data):
            if len(data) == 7 and any(t.x or t.y for t in decode_packet(data)[1]):
                xy_at_z.append(c.sim.pos[2])
            return orig(data)

        c.sim.write = spy
        hm = HeightMap.empty(*make_grid(5, 5, 15, 15, spacing=10))
        c.run_autolevel(hm, clearance_mm=1.0)
        assert hm.complete and xy_at_z
        assert xy_at_z[0] >= 250            # lifted 1 mm before the first XY move
        assert min(xy_at_z) >= -250 + 250   # never sideways below copper + clearance
    finally:
        c.disconnect()


def test_autolevel_closing_reprobe_reports_drift():
    """After the grid the first point is probed again: 0 when nothing drifted,
    the drift when Z moved (hardware 2026-11-14: ~2 mm error went unnoticed)."""
    level = {"z": -250}
    c = _ctl(lambda x, y: level["z"])
    try:
        c.zero_axes()
        hm = HeightMap.empty(*make_grid(0, 0, 10, 10, spacing=10))
        c.run_autolevel(hm)
        assert hm.meta["drift_mm"] == pytest.approx(0, abs=0.0045)

        hm2 = HeightMap.empty(*make_grid(0, 0, 10, 10, spacing=10))
        n = {"k": 0}

        def raise_board(i, j, z):           # board 0.1 mm higher after the last point
            n["k"] += 1
            if n["k"] == 4:
                level["z"] = -225
        c.run_autolevel(hm2, on_point=raise_board)
        assert hm2.meta["drift_mm"] == pytest.approx(0.1, abs=0.06)
    finally:
        c.disconnect()
