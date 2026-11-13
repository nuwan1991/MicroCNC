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
