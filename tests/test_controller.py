import time

import pytest

from micropcb.controller import MachineController
from micropcb.settings import MachineState, Settings
from micropcb.sim_machine import SimMachine


@pytest.fixture
def ctl():
    c = MachineController(Settings(), MachineState(), persist=False)
    sim = SimMachine(realtime=False)
    c.connect(transport=sim)
    c.sim = sim
    yield c
    c.disconnect()


def test_jog_and_work_zero(ctl):
    ctl.jog("x", 10)
    ctl.jog("y", 5)
    ctl.wait_idle(5)
    assert ctl.machine_mm() == pytest.approx((10, 5, 0))
    ctl.zero_axes()
    assert ctl.work_mm() == pytest.approx((0, 0, 0))
    ctl.jog("x", -2.5)
    ctl.wait_idle(5)
    assert ctl.work_mm()[0] == pytest.approx(-2.5)


def test_go_to_work_zero_lifts_z_first(ctl):
    ctl.zero_axes()
    ctl.jog("x", 3)
    ctl.jog("z", -1)
    ctl.wait_idle(5)
    ctl.go_to_work_zero()
    ctl.wait_idle(5)
    assert ctl.work_mm() == pytest.approx((0, 0, 0))
    assert ctl.sim.pos == [0, 0, 0]


def test_soft_limits_clamp_after_home(ctl):
    ctl.set_home()
    assert ctl.limits_active()
    got = ctl.jog("x", -5)  # below X min
    assert got == 0
    got = ctl.jog("z", 3)  # above Z top
    assert got == 0
    got = ctl.jog("x", 500)
    ctl.wait_idle(10)
    assert got == pytest.approx(140)
    assert ctl.machine_mm()[0] == pytest.approx(140)


def test_no_limits_without_home(ctl):
    assert not ctl.limits_active()
    assert ctl.jog("x", -1) == pytest.approx(-1)


def test_release_invalidates_home_and_position(ctl):
    ctl.set_home()
    ctl.sim.press_release_button()
    ctl.link.keepalive_interval = 0.02
    time.sleep(0.2)
    assert not ctl.position_valid and not ctl.limits_active()
    with pytest.raises(RuntimeError):
        ctl.jog("x", 1)
    ctl.sim.press_release_button()
    time.sleep(0.2)
    ctl.zero_axes()
    assert ctl.position_valid


def test_position_restored_on_reconnect():
    st = MachineState(machine_pos=[100, 200, -300])
    c = MachineController(Settings(), st, persist=False)
    c.connect(transport=SimMachine(realtime=False))
    assert c.machine_steps() == (100, 200, -300)
    assert c.position_valid
    c.disconnect()
    assert st.clean_exit


def test_unclean_exit_marks_position_invalid():
    st = MachineState(machine_pos=[1, 2, 3], clean_exit=False, home_set=True)
    c = MachineController(Settings(), st, persist=False)
    c.connect(transport=SimMachine(realtime=False))
    assert not c.position_valid and not c.limits_active()
    c.disconnect()


def test_touch_check_wiggle_reports_contact():
    """Touch test: a tiny Z up/down wiggle is answered with 04 while touching."""
    import time as _t
    from micropcb.controller import MachineController
    from micropcb.protocol import HANDSHAKE
    from micropcb.serial_link import EV_PROBE
    from micropcb.settings import MachineState, Settings
    from micropcb.sim_machine import SimMachine
    events = []
    c = MachineController(Settings(), MachineState(), persist=False)
    c.listener = lambda e, p: events.append(e)
    sim = SimMachine(realtime=False)
    c.connect(transport=sim)
    try:
        c.zero_axes()
        assert c.touch_check() and c.link.wait_idle(2)
        _t.sleep(0.05)
        assert EV_PROBE not in events and sim.pos == [0, 0, 0]   # net zero move
        sim.contact = True
        assert c.touch_check() and c.link.wait_idle(2)
        _t.sleep(0.05)
        assert EV_PROBE in events and c.link.position_valid
        assert abs(c.link.position[2]) <= 14 and c.state.home_set == c.state.home_set
        c.end_touch_test()
    finally:
        c.disconnect()


def test_probe_after_touch_test_unlatches_first():
    """Hardware 2026-11-14: after Test touch the machine refuses Z- until Z moves
    up, so Probe here failed with 'no touch'. The probe now lifts 14 steps first."""
    import time as _t
    from micropcb.controller import MachineController, ProbeError
    from micropcb.settings import MachineState, Settings
    from micropcb.sim_machine import SimMachine
    import pytest
    c = MachineController(Settings(), MachineState(), persist=False)
    sim = SimMachine(realtime=False, surface=lambda x, y: -300)   # copper 1.2 mm below
    c.connect(transport=sim)
    try:
        c.zero_axes()
        sim.contact = True
        assert c.touch_check() and c.link.wait_idle(2)
        _t.sleep(0.05)
        sim.contact = False
        assert sim.latched                      # machine now refuses Z-
        z = c.probe(5.0)                        # must still find the copper
        assert sim.probe_count == 1 and z == pytest.approx(-1.2, abs=0.06)
        # clip still touching the tool -> clear message instead of a silent miss
        c.jog("z", 1.0)
        assert c.link.wait_idle(2)
        c.touch_check() and c.link.wait_idle(2)
        sim.contact = True
        assert c.touch_check() and c.link.wait_idle(2)
        _t.sleep(0.05)
        with pytest.raises(ProbeError, match="still reports contact"):
            c.probe(5.0)
    finally:
        c.disconnect()


def test_zeroing_axes_one_by_one_restores_trust():
    from micropcb.controller import MachineController
    from micropcb.settings import MachineState, Settings
    from micropcb.sim_machine import SimMachine
    c = MachineController(Settings(), MachineState(), persist=False)
    c.connect(transport=SimMachine(realtime=False))
    try:
        c.link.position_valid = False
        c.zero_axes("x")
        c.zero_axes("y")
        assert not c.position_valid
        c.zero_axes("z")
        assert c.position_valid
    finally:
        c.disconnect()


def test_micropcb_records_its_own_traffic(tmp_path):
    """MicroPCB can record like the bridge: TX/RX with times, probe replies whole."""
    import time as _t
    from micropcb.bridge.analyze import load
    from micropcb.bridge.recorder import Recorder
    from micropcb.controller import MachineController
    from micropcb.settings import MachineState, Settings
    from micropcb.sim_machine import SimMachine
    c = MachineController(Settings(), MachineState(), persist=False)
    rec = Recorder(tmp_path, "t")
    c.recorder = rec
    c.connect(transport=SimMachine(realtime=False, surface=lambda x, y: -100))
    try:
        c.zero_axes()
        c.probe(2.0)
        rec.record("EV", b"", "note from the UI")
        _t.sleep(0.05)
    finally:
        c.disconnect()
    meta, rows = load(rec.close())
    kinds = {r.kind for r in rows}
    assert {"jog", "ready", "probe"} <= kinds and any(r.dir == "EV" for r in rows)
    assert sum(1 for r in rows if r.dir == "TX") == sum(1 for r in rows if r.dir == "RX")
