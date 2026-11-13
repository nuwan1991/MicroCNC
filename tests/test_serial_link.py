import time

import pytest

from micropcb.jog import jog_ticks, pack
from micropcb.protocol import HANDSHAKE, encode_settings
from micropcb.serial_link import (EV_LOST, EV_PROBE, EV_RELEASED, SerialLink)
from micropcb.sim_machine import SimMachine


class Events:
    def __init__(self):
        self.items = []

    def __call__(self, ev, payload):
        self.items.append((ev, payload))

    def names(self):
        return [e for e, _ in self.items]


@pytest.fixture
def setup():
    ev = Events()
    sim = SimMachine(realtime=False)
    link = SerialLink(ev, keepalive_interval=0.05)
    link.open(sim)
    yield link, sim, ev
    link.close()


def test_handshake_first(setup):
    link, sim, ev = setup
    assert sim.written[0] == HANDSHAKE
    assert "connected" in ev.names()


def test_keepalive_sent_when_idle(setup):
    link, sim, ev = setup
    time.sleep(0.25)
    assert sim.written.count(encode_settings(4)) >= 2


def test_jog_updates_position(setup):
    link, sim, ev = setup
    link.send(pack(jog_ticks("x", 2500)))
    link.send(pack(jog_ticks("z", -333)))
    assert link.wait_idle(5)
    assert link.position == (2500, 0, -333)
    assert sim.pos == [2500, 0, -333]


def test_release_button_blocks_and_invalidates(setup):
    link, sim, ev = setup
    sim.press_release_button()
    time.sleep(0.2)  # keep-alive will see 03
    assert link.released and not link.position_valid
    assert ("released", True) in ev.items
    with pytest.raises(RuntimeError):
        link.send(pack(jog_ticks("x", 10)))
    sim.press_release_button()
    time.sleep(0.2)
    assert not link.released
    assert ("released", False) in ev.items
    link.set_position(0, 0, 0)
    assert link.position_valid


def test_probe_stops_motion():
    ev = Events()
    sim = SimMachine(realtime=False, probe_z=-500)
    link = SerialLink(ev, keepalive_interval=10)
    link.open(sim)
    link.send(pack(jog_ticks("z", -5000)))
    assert link.wait_idle(5)
    probes = [p for e, p in ev.items if e == EV_PROBE]
    assert probes == [-500]
    assert not link.position_valid
    assert sim.pos[2] == -500
    link.close()


def test_lost_connection_detected():
    ev = Events()
    sim = SimMachine(realtime=False)
    link = SerialLink(ev, keepalive_interval=0.02, reply_timeout=0.1)
    link.open(sim)
    sim._handle = lambda data: (b"", 0.0)  # machine goes silent (power cut)
    time.sleep(0.4)
    assert EV_LOST in ev.names()
    assert not link.connected


def test_spindle_bit_and_stop(setup):
    link, sim, ev = setup
    link.set_spindle(True)
    assert link.wait_idle(2)
    assert sim.spindle and link.spindle
    link.stop()  # sends spindle-off packet
    assert link.wait_idle(2)
    time.sleep(0.05)
    assert not sim.spindle


def test_realtime_timing_matches_capture():
    sim = SimMachine(realtime=True)
    link = SerialLink(keepalive_interval=10)
    link.open(sim)
    pkts = pack(jog_ticks("x", 14 * 3 * 10, 14))
    t0 = time.monotonic()
    link.send(pkts)
    link.wait_idle(10)
    dt = time.monotonic() - t0
    link.close()
    # ~59 ms per full packet in the capture
    assert 0.03 * len(pkts) < dt < 0.12 * len(pkts)
