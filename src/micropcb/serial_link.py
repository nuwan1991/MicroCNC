"""Serial link to the Wegstr machine.

A worker thread owns the port and does strict stop-and-wait:
send one packet -> wait for one reply -> next packet. When idle it sends the
keep-alive/settings packet once per second (like the original software).

The link also counts executed steps, so it is the source of truth for the
machine position (the machine itself has no position memory).
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Protocol

from .protocol import (BAUDRATE, HANDSHAKE, HEADER_JOG, HEADER_MOTION, ZERO_TICK,
                       Reply, ReplyCode, ReplyParser, Tick, decode_packet,
                       encode_packet, encode_settings)

CH340_VID = 0x1A86
REPLY_TIMEOUT = 0.5
KEEPALIVE_INTERVAL = 1.0


class Transport(Protocol):
    timeout: float | None

    def write(self, data: bytes) -> int: ...
    def read(self, size: int = 1) -> bytes: ...
    def close(self) -> None: ...


# Event names sent to the listener
EV_CONNECTED = "connected"
EV_DISCONNECTED = "disconnected"
EV_POSITION = "position"      # payload: (x, y, z) steps
EV_RELEASED = "released"      # payload: bool (motors released by button)
EV_OVERLOAD = "overload"      # payload: None
EV_PROBE = "probe"            # payload: int value from machine
EV_LOST = "lost"              # payload: str reason
EV_IDLE = "idle"              # payload: None (queue drained)
EV_TRAFFIC = "traffic"        # payload: (direction 'tx'/'rx', bytes)

Listener = Callable[[str, object], None]


def open_serial(port: str) -> Transport:
    import serial  # local import: tests / sim mode do not need pyserial

    return serial.Serial(port, BAUDRATE, bytesize=8, parity="N", stopbits=1,
                         timeout=REPLY_TIMEOUT, write_timeout=1.0)


def list_ports() -> list[tuple[str, str]]:
    """(device, description) for all COM ports, CH340 ports first."""
    from serial.tools import list_ports as lp

    ports = list(lp.comports())
    ports.sort(key=lambda p: (p.vid != CH340_VID, p.device))
    return [(p.device, p.description) for p in ports]


def probe_port(port: str) -> bool:
    """True if a Wegstr answers the handshake on this port."""
    try:
        t = open_serial(port)
    except Exception:
        return False
    try:
        t.write(HANDSHAKE)
        r = t.read(1)
        return len(r) == 1 and r[0] in (ReplyCode.READY, ReplyCode.OVERLOAD,
                                        ReplyCode.RELEASED)
    except Exception:
        return False
    finally:
        t.close()


def find_machine() -> str | None:
    for dev, _ in list_ports():
        if probe_port(dev):
            return dev
    return None


class SerialLink:
    def __init__(self, listener: Listener | None = None, performance: int = 4,
                 keepalive_interval: float = KEEPALIVE_INTERVAL,
                 reply_timeout: float = REPLY_TIMEOUT) -> None:
        self.listener = listener or (lambda ev, payload: None)
        self.performance = performance
        self.keepalive_interval = keepalive_interval
        self.reply_timeout = reply_timeout
        self._t: Transport | None = None
        self._q: queue.Queue[bytes] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._lock = threading.Lock()
        self._parser = ReplyParser()
        self._pos = [0, 0, 0]
        self.position_valid = True
        self.released = False
        self.spindle = False
        self.last_probe_in_motion = False

    # ------------------------------------------------------------ lifecycle
    @property
    def connected(self) -> bool:
        return self._running.is_set()

    def open(self, transport: Transport) -> None:
        if self.connected:
            raise RuntimeError("already connected")
        self._t = transport
        self._t.timeout = self.reply_timeout
        self._parser = ReplyParser()
        # Handshake: same as original port detection.
        reply = self._transact(HANDSHAKE)
        if reply is None:
            transport.close()
            self._t = None
            raise ConnectionError("no reply to handshake - is this a Wegstr?")
        self._handle_status(reply, None)
        self._running.set()
        self._thread = threading.Thread(target=self._run, name="wegstr-link", daemon=True)
        self._thread.start()
        self._emit(EV_CONNECTED, None)

    def open_port(self, port: str) -> None:
        self.open(open_serial(port))

    def close(self) -> None:
        if not self.connected:
            return
        self.stop()
        self._running.clear()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._shutdown(EV_DISCONNECTED, None)

    def _shutdown(self, event: str, payload: object) -> None:
        self._running.clear()
        t, self._t = self._t, None
        if t is not None:
            try:
                t.close()
            except Exception:
                pass
        self._drain()
        self._emit(event, payload)

    # ------------------------------------------------------------ commands
    def send(self, packets: list[bytes]) -> None:
        if not self.connected:
            raise RuntimeError("not connected")
        if self.released:
            raise RuntimeError("motors released - press the release button again")
        for p in packets:
            self._idle.clear()
            self._q.put(p)

    def stop(self) -> None:
        """Drop all queued motion. Machine halts after the packet in flight."""
        self._drain()
        if self.connected and self.spindle:
            self._q.put(encode_packet([ZERO_TICK] * 3, HEADER_JOG))

    def set_spindle(self, on: bool) -> None:
        if not self.connected:
            raise RuntimeError("not connected")
        t = Tick(spindle=on)
        self._idle.clear()
        self._q.put(encode_packet([t, t, t], HEADER_JOG))

    def wait_idle(self, timeout: float | None = None) -> bool:
        return self._idle.wait(timeout)

    def queue_size(self) -> int:
        """Packets waiting to be sent (not counting the one in flight)."""
        return self._q.qsize()

    @property
    def position(self) -> tuple[int, int, int]:
        with self._lock:
            return tuple(self._pos)  # type: ignore[return-value]

    def set_position(self, x: int, y: int, z: int) -> None:
        with self._lock:
            self._pos = [x, y, z]
        self.position_valid = True
        self._emit(EV_POSITION, self.position)

    # ------------------------------------------------------------ internals
    def _emit(self, ev: str, payload: object) -> None:
        try:
            self.listener(ev, payload)
        except Exception:
            pass  # a broken listener must never kill the link

    def _drain(self) -> None:
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    def _transact(self, data: bytes) -> Reply | None:
        """Write one packet and wait for exactly one reply."""
        assert self._t is not None
        self._emit(EV_TRAFFIC, ("tx", data))
        self._t.write(data)
        deadline = time.monotonic() + self.reply_timeout
        while time.monotonic() < deadline:
            chunk = self._t.read(1)
            if not chunk:
                continue
            self._emit(EV_TRAFFIC, ("rx", chunk))
            replies = self._parser.feed(chunk)
            if replies:
                for extra in replies[1:]:  # unexpected extra replies
                    self._handle_status(extra, None)
                return replies[0]
        return None

    def _run(self) -> None:
        last_tx = time.monotonic()
        while self._running.is_set():
            try:
                pkt = self._q.get(timeout=0.02)
            except queue.Empty:
                if not self._idle.is_set():
                    self._idle.set()
                    self._emit(EV_IDLE, None)
                if time.monotonic() - last_tx < self.keepalive_interval:
                    continue
                pkt = encode_settings(self.performance)
            try:
                reply = self._transact(pkt)
            except Exception as e:  # port vanished (E-stop power cut, USB unplugged)
                self._shutdown(EV_LOST, f"serial error: {e}")
                return
            last_tx = time.monotonic()
            if reply is None:
                self._shutdown(EV_LOST, "machine not responding (power off / E-stop?)")
                return
            self._handle_status(reply, pkt)

    def _handle_status(self, reply: Reply, pkt: bytes | None) -> None:
        code = reply.code
        if code == ReplyCode.RELEASED:
            self._drain()
            if not self.released:
                self.released = True
                self.position_valid = False
                self._emit(EV_RELEASED, True)
            return
        if self.released and code in (ReplyCode.READY, ReplyCode.OVERLOAD):
            self.released = False
            self._emit(EV_RELEASED, False)
        if code == ReplyCode.READY:
            if pkt is not None and len(pkt) == 7 and pkt[0] in (HEADER_MOTION, HEADER_JOG):
                _, ticks = decode_packet(pkt)
                with self._lock:
                    for t in ticks:
                        self._pos[0] += t.x
                        self._pos[1] += t.y
                        self._pos[2] += t.z
                self.spindle = ticks[-1].spindle
                if any(not t.is_zero() for t in ticks):
                    self._emit(EV_POSITION, self.position)
        elif code == ReplyCode.OVERLOAD:
            self._drain()
            self.spindle = False
            self._emit(EV_OVERLOAD, None)
        elif code == ReplyCode.PROBE:
            # value = the machine's own absolute Z step counter at contact.
            moving = (pkt is not None and len(pkt) == 7 and pkt[0] in (HEADER_MOTION, HEADER_JOG)
                      and any(not t.is_zero() for t in decode_packet(pkt)[1]))
            self.last_probe_in_motion = moving
            if moving:
                # Motion stopped part-way through this packet; how many of its
                # steps were done is unknown, so our count is no longer exact.
                self._drain()
                self.position_valid = False
            # else: contact seen while idle (keep-alive) - nothing moved.
            self._emit(EV_PROBE, reply.value)
        # unknown codes are ignored (visible via traffic log)
