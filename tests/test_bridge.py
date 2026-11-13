"""Bridge tests with in-memory ports (stand-in for com0com + CH340)."""
import threading
import time

from micropcb.bridge.core import Bridge, PacketFramer
from micropcb.jog import jog_ticks, pack
from micropcb.protocol import HANDSHAKE, encode_settings
from micropcb.serial_link import EV_PROBE, SerialLink
from micropcb.sim_machine import SimMachine


class Pipe:
    """One end of an in-memory null-modem pair."""

    def __init__(self):
        self.rx = bytearray()
        self.cv = threading.Condition()
        self.peer: "Pipe" = None  # type: ignore[assignment]
        self.timeout = 0.05
        self.writes: list[bytes] = []
        self.read_sizes: list[int] = []

    @staticmethod
    def pair():
        a, b = Pipe(), Pipe()
        a.peer, b.peer = b, a
        return a, b

    def write(self, data):
        self.writes.append(bytes(data))
        with self.peer.cv:
            self.peer.rx.extend(data)
            self.peer.cv.notify_all()
        return len(data)

    def read(self, size=1):
        end = time.monotonic() + (self.timeout or 0)
        with self.cv:
            while not self.rx and time.monotonic() < end:
                self.cv.wait(end - time.monotonic())
            out = bytes(self.rx[:size])
            del self.rx[:size]
        if out:
            self.read_sizes.append(len(out))
        return out

    def close(self):
        pass


class ByteByByteMachine:
    """Machine end that dribbles each reply out one byte at a time (HW-597B)."""

    def __init__(self, sim):
        self.sim = sim
        self.timeout = 0.05
        self._pending = bytearray()

    def write(self, d):
        return self.sim.write(d)

    def read(self, size=1):
        if not self._pending:
            self._pending += self.sim.read(16)
        out = bytes(self._pending[:1])
        del self._pending[:1]
        return out

    def close(self):
        pass


def test_framer_reassembles_split_packets():
    f = PacketFramer()
    stream = HANDSHAKE + encode_settings(4) + HANDSHAKE
    out = []
    for b in stream:
        out += f.feed(bytes([b]))
    assert out == [HANDSHAKE, encode_settings(4), HANDSHAKE]


def _wegstr_side_reader(pc_port, stop):
    """Records how replies arrive at the Wegstr software (read chunk sizes)."""
    chunks = []
    while not stop.is_set():
        d = pc_port.read(64)
        if d:
            chunks.append(d)
    return chunks


def test_probe_reply_delivered_in_one_piece():
    sim = SimMachine(realtime=False, probe_z=-100)
    machine = ByteByByteMachine(sim)
    pc, bridge_front = Pipe.pair()
    br = Bridge(bridge_front, machine)
    br.start()
    try:
        # behave like the original software: write packet, then read reply
        pc.write(HANDSHAKE)
        time.sleep(0.1)
        assert pc.read(64) == b"\x01"
        for p in pack(jog_ticks("z", -400)):
            pc.write(p)
            time.sleep(0.03)
            reply = pc.read(64)
            if reply[:1] == b"\x04":
                break
        assert reply == b"\x04\xff\x9c"  # -100 in one chunk, not 04 / ff / 9c
        assert br.stats["reassembled"] == 1
    finally:
        br.stop()


def test_simulator_mode_end_to_end_with_serial_link():
    """MicroPCB's own link talks through the bridge to the simulator."""
    sim = SimMachine(realtime=False, surface=lambda x, y: -50 - x // 100)
    pc, bridge_front = Pipe.pair()
    br = Bridge(bridge_front, sim)
    br.start()
    events = []
    link = SerialLink(lambda e, p: events.append((e, p)), keepalive_interval=0.05)
    try:
        link.open(pc)
        link.send(pack(jog_ticks("x", 500)))
        assert link.wait_idle(5)
        assert sim.pos[0] == 500
        link.send(pack(jog_ticks("z", -1000)))
        assert link.wait_idle(5)
        assert [p for e, p in events if e == EV_PROBE] == [-55]
        time.sleep(0.2)
        assert sim.performance == 4  # keep-alive passed through
    finally:
        link.close()
        br.stop()


def test_power_cut_simulation_detected_by_link():
    sim = SimMachine(realtime=False)
    pc, bridge_front = Pipe.pair()
    br = Bridge(bridge_front, sim)
    br.start()
    events = []
    link = SerialLink(lambda e, p: events.append(e), keepalive_interval=0.03,
                      reply_timeout=0.15)
    link.open(pc)
    sim.silent = True
    time.sleep(0.5)
    assert "lost" in events
    br.stop()
