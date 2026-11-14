"""Simulated Wegstr machine with a pyserial-like interface.

Lets the app and tests run without hardware. Timing follows the capture:
~19.6 ms per tick when moving, ~4 ms reply to keep-alive.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

from .protocol import (HEADER_JOG, HEADER_MOTION, HEADER_SETTINGS, TICK_SECONDS,
                       ReplyCode, decode_packet)


class SimMachine:
    port = "SIM"

    def __init__(self, realtime: bool = True, probe_z: int | None = None,
                 surface: "Callable[[int, int], int] | None" = None,
                 fw_z_offset: int = 0) -> None:
        self.realtime = realtime
        self.timeout = 0.5
        self.pos = [0, 0, 0]
        self.spindle = False
        self.released = False
        self.overload = False
        self.silent = False  # simulate power cut: no replies at all
        self.contact = False    # touch test: clip touching the tool
        self.latched = False    # touch reported; Z- refused until a Z+ move
        self.probe_z = probe_z  # Z step position where the probe touches
        # surface(x_steps, y_steps) -> z_steps of the board surface; overrides probe_z
        self.surface = surface
        self.probe_count = 0
        # The real firmware reports its own absolute Z step counter, whose origin
        # differs from the PC's count (it runs since power-on).
        self.fw_z_offset = fw_z_offset
        self.performance = None
        self.written: list[bytes] = []
        self._rx: deque[tuple[float, bytes]] = deque()
        self._lock = threading.Condition()
        self._busy_until = 0.0
        self.is_open = True

    # ---- pyserial-like API
    def write(self, data: bytes) -> int:
        with self._lock:
            self.written.append(bytes(data))
            now = time.monotonic()
            reply, duration = self._handle(bytes(data))
            ready = max(now, self._busy_until) + duration
            self._busy_until = ready
            if not self.realtime:
                ready = now
            self._rx.append((ready, reply))
            self._lock.notify_all()
        return len(data)

    def read(self, size: int = 1) -> bytes:
        deadline = time.monotonic() + (self.timeout or 0)
        out = bytearray()
        with self._lock:
            while len(out) < size:
                now = time.monotonic()
                if self._rx and self._rx[0][0] <= now:
                    t, chunk = self._rx.popleft()
                    take = chunk[: size - len(out)]
                    out += take
                    if len(take) < len(chunk):
                        self._rx.appendleft((t, chunk[len(take):]))
                    continue
                if out or now >= deadline:
                    break
                wait = deadline - now
                if self._rx:
                    wait = min(wait, self._rx[0][0] - now)
                self._lock.wait(max(wait, 0.0005))
        return bytes(out)

    @property
    def in_waiting(self) -> int:
        now = time.monotonic()
        with self._lock:
            return sum(len(c) for t, c in self._rx if t <= now)

    def reset_input_buffer(self) -> None:
        with self._lock:
            self._rx.clear()

    def close(self) -> None:
        self.is_open = False

    # ---- test helpers
    def press_release_button(self) -> None:
        self.released = not self.released

    # ---- machine logic
    def _status(self) -> bytes:
        if self.released:
            return bytes((ReplyCode.RELEASED,))
        if self.overload:
            return bytes((ReplyCode.OVERLOAD,))
        return bytes((ReplyCode.READY,))

    def _surface_z(self) -> int | None:
        if self.surface is not None:
            return self.surface(self.pos[0], self.pos[1])
        return self.probe_z

    def _handle(self, data: bytes) -> tuple[bytes, float]:
        if self.silent:
            return b"", 0.0
        if len(data) == 2 and data[0] == HEADER_SETTINGS:
            self.performance = data[1]
            return self._status(), 0.004
        if len(data) == 7 and data[0] in (HEADER_MOTION, HEADER_JOG):
            if self.released or self.overload:
                return self._status(), 0.009
            header, ticks = decode_packet(data)
            surf = self._surface_z() if header == HEADER_JOG else None
            for t in ticks:
                self.spindle = t.spindle
                if t.z > 0:
                    self.latched = False      # any Z up clears the touch latch
                elif t.z < 0 and self.latched:
                    # after a touch the firmware refuses Z- until Z moves up
                    # (hardware 2026-11-14: Test touch, then Probe here failed)
                    t = type(t)(t.x, t.y, 0, t.spindle)
                elif t.z < 0 and header == HEADER_JOG and self.contact:
                    # clip held against the tool by hand: reported on a Z- move
                    self.latched = True
                    v = (self.pos[2] + self.fw_z_offset) & 0xFFFF
                    return bytes((ReplyCode.PROBE, v >> 8, v & 0xFF)), 0.009
                if surf is not None and t.z < 0 and self.pos[2] <= surf:
                    # already touching: firmware does not move further down (capture 1)
                    t = type(t)(t.x, t.y, 0, t.spindle)
                for _ in range(abs(t.z)):
                    self.pos[2] += 1 if t.z > 0 else -1
                    if surf is not None and t.z < 0 and self.pos[2] <= surf:
                        self.probe_count += 1
                        self.latched = True
                        v = (self.pos[2] + self.fw_z_offset) & 0xFFFF
                        return bytes((ReplyCode.PROBE, v >> 8, v & 0xFF)), 0.02
                self.pos[0] += t.x
                self.pos[1] += t.y
            moving = any(not t.is_zero() for t in ticks)
            return self._status(), (3 * TICK_SECONDS if moving else 0.009)
        return b"", 0.0  # unknown -> no reply
