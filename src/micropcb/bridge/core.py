"""Bridge engine (no Qt).

front = the virtual COM port the original Wegstr software talks to
back  = the real machine (serial port) or a SimMachine
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from ..protocol import (HEADER_JOG, HEADER_MOTION, HEADER_SETTINGS, Reply,
                        ReplyCode, ReplyParser)

PARTIAL_REPLY_TIMEOUT = 0.3  # give up waiting for the rest of a probe reply


class PacketFramer:
    """Splits the PC->machine byte stream into whole packets.

    7F xx (2 bytes), 3F/FF + 6 bytes (7 bytes). Unknown bytes pass alone.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buf.extend(data)
        out = []
        while self._buf:
            h = self._buf[0]
            need = 2 if h == HEADER_SETTINGS else 7 if h in (HEADER_JOG, HEADER_MOTION) else 1
            if len(self._buf) < need:
                break
            out.append(bytes(self._buf[:need]))
            del self._buf[:need]
        return out

    def take_partial(self) -> bytes:
        b = bytes(self._buf)
        self._buf.clear()
        return b


def encode_reply(r: Reply) -> bytes:
    if r.code == ReplyCode.PROBE and r.value is not None:
        v = r.value & 0xFFFF
        return bytes((r.code, v >> 8, v & 0xFF))
    return bytes((r.code,))


# listener events
EV_LOG = "log"          # str
EV_TX = "tx"            # bytes  PC -> machine
EV_RX = "rx"            # bytes  machine -> PC (as delivered to PC)
EV_REASSEMBLED = "fix"  # int    probe reply that arrived split and was joined
EV_STOPPED = "stopped"  # str reason


class Bridge:
    def __init__(self, front, back, listener: Callable[[str, object], None] | None = None) -> None:
        self.front = front
        self.back = back
        self.listener = listener or (lambda e, p: None)
        self._run = threading.Event()
        self._threads: list[threading.Thread] = []
        self.stats = {"packets": 0, "replies": 0, "probes": 0, "reassembled": 0}
        self.recorder = None     # bridge.recorder.Recorder while recording

    def _rec(self, direction: str, data: bytes, note: str = "", t: float | None = None) -> None:
        r = self.recorder
        if r is not None:
            try:
                r.record(direction, data, note, t)
            except Exception:
                pass

    def _emit(self, ev: str, payload: object) -> None:
        try:
            self.listener(ev, payload)
        except Exception:
            pass

    @property
    def running(self) -> bool:
        return self._run.is_set()

    def start(self) -> None:
        self.front.timeout = 0.02
        self.back.timeout = 0.02
        self._run.set()
        self._threads = [
            threading.Thread(target=self._pc_to_machine, name="bridge-up", daemon=True),
            threading.Thread(target=self._machine_to_pc, name="bridge-down", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self, reason: str = "stopped") -> None:
        if not self._run.is_set():
            return
        self._run.clear()
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=1)
        for p in (self.front, self.back):
            try:
                p.close()
            except Exception:
                pass
        self._emit(EV_STOPPED, reason)

    def _fail(self, where: str, e: Exception) -> None:
        if self._run.is_set():
            threading.Thread(target=self.stop, args=(f"{where}: {e}",), daemon=True).start()

    # PC -> machine: forward whole packets
    def _pc_to_machine(self) -> None:
        framer = PacketFramer()
        while self._run.is_set():
            try:
                data = self.front.read(64)
            except Exception as e:
                self._fail("Wegstr side port", e)
                return
            if not data:
                continue
            t_in = time.perf_counter()
            for pkt in framer.feed(data):
                self._rec("TX", pkt, t=t_in)
                try:
                    self.back.write(pkt)
                except Exception as e:
                    self._fail("machine port", e)
                    return
                self.stats["packets"] += 1
                self._emit(EV_TX, pkt)

    # machine -> PC: forward whole replies in ONE write each
    def _machine_to_pc(self) -> None:
        parser = ReplyParser()
        partial_since: float | None = None
        reads_in_reply = 0
        while self._run.is_set():
            try:
                data = self.back.read(1)
            except Exception as e:
                self._fail("machine port", e)
                return
            now = time.perf_counter()
            if data:
                reads_in_reply += 1
                replies = parser.feed(data)
                if parser.pending():
                    partial_since = partial_since or now
                for r in replies:
                    out = encode_reply(r)
                    note = ""
                    if r.code == ReplyCode.PROBE:
                        self.stats["probes"] += 1
                        if reads_in_reply > 1:
                            note = f"re-assembled from {reads_in_reply} reads"
                            self.stats["reassembled"] += 1
                            self._emit(EV_REASSEMBLED, r.value)
                    try:
                        self.front.write(out)
                    except Exception as e:
                        self._fail("Wegstr side port", e)
                        return
                    self.stats["replies"] += 1
                    self._rec("RX", out, note, t=now)
                    self._emit(EV_RX, out)
                if not parser.pending():
                    partial_since = None
                    reads_in_reply = 0
            elif partial_since and now - partial_since > PARTIAL_REPLY_TIMEOUT:
                # incomplete reply: pass through raw rather than block forever
                raw = bytes(parser._buf)
                parser._buf.clear()
                partial_since = None
                reads_in_reply = 0
                self._emit(EV_LOG, f"incomplete reply passed through: {raw.hex(' ')}")
                self._rec("EV", raw, "incomplete reply passed through")
                try:
                    self.front.write(raw)
                except Exception as e:
                    self._fail("Wegstr side port", e)
                    return
