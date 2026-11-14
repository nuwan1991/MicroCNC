"""Timed serial capture for the bridge (version 0.2).

Every packet / reply is written as one CSV row the moment it passes the
bridge (timestamps from the bridge threads, perf_counter, µs resolution):

    t_s, dir, hex, kind, ticks, spindle, x, y, z, gap_ms, reply_ms, note

dir   TX = Wegstr software -> machine, RX = machine -> software, EV = event
kind  keepalive / job / jog / handshake / ready / overload / released / probe / raw
x/y/z machine step counter after the packet (sum of all motion ticks sent)
gap_ms   time since the previous row in the same direction
reply_ms RX only: time since the last TX packet

On stop a report (`*-report.txt`) and the reconstructed motion
(`*-motion.nc`) are written next to the CSV. `python -m micropcb.bridge.analyze
capture.csv` makes them again from any capture file.
"""
from __future__ import annotations

import csv
import os
import threading
import time
from pathlib import Path

from ..protocol import HEADER_JOG, HEADER_MOTION, HEADER_SETTINGS, ReplyCode, decode_packet

FIELDS = ["t_s", "dir", "hex", "kind", "ticks", "spindle", "x", "y", "z", "gap_ms",
          "reply_ms", "note"]
TX, RX, EV = "TX", "RX", "EV"
REPLY_NAMES = {ReplyCode.READY: "ready", ReplyCode.OVERLOAD: "overload",
               ReplyCode.RELEASED: "released", ReplyCode.PROBE: "probe"}


def default_folder() -> Path:
    return Path.home() / "Documents" / "MicroPCB captures"


class Recorder:
    """Thread-safe CSV writer; call record() from any thread."""

    def __init__(self, folder: str | os.PathLike | None = None, name: str = "capture",
                 meta: dict | None = None) -> None:
        folder = Path(folder) if folder else default_folder()
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = folder / f"{name}-{stamp}.csv"
        self._f = open(self.path, "w", newline="", encoding="utf-8")
        self._w = csv.writer(self._f)
        self._f.write(f"# MicroPCB bridge capture, started {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        for k, v in (meta or {}).items():
            self._f.write(f"# {k}: {v}\n")
        self._w.writerow(FIELDS)
        self._lock = threading.Lock()
        self._t0 = time.perf_counter()
        self._last = {TX: None, RX: None}
        self._last_tx = None
        self.pos = [0, 0, 0]
        self.rows = 0
        self.closed = False
        self._last_flush = 0.0

    def now(self) -> float:
        return time.perf_counter() - self._t0

    def record(self, direction: str, data: bytes = b"", note: str = "",
               t: float | None = None) -> None:
        with self._lock:
            if self.closed:
                return
            t = self.now() if t is None else t - self._t0
            kind, ticks, spindle = describe(direction, data)
            if direction == TX and kind in ("job", "jog"):
                for tk in decode_packet(data)[1]:
                    self.pos[0] += tk.x
                    self.pos[1] += tk.y
                    self.pos[2] += tk.z
            gap = reply = ""
            if direction in self._last:
                if self._last[direction] is not None:
                    gap = f"{(t - self._last[direction]) * 1000:.3f}"
                self._last[direction] = t
            if direction == RX and self._last_tx is not None:
                reply = f"{(t - self._last_tx) * 1000:.3f}"
            if direction == TX:
                self._last_tx = t
            if direction == RX and kind == "probe" and len(data) == 3:
                v = (data[1] << 8) | data[2]
                v = v - 0x10000 if v & 0x8000 else v
                note = (note + " " if note else "") + f"probe Z counter {v}"
            self._w.writerow([f"{t:.6f}", direction, data.hex(" "), kind, ticks, spindle,
                              *self.pos, gap, reply, note])
            self.rows += 1
            if t - self._last_flush > 1.0:
                self._f.flush()
                self._last_flush = t

    def close(self) -> Path:
        with self._lock:
            if not self.closed:
                self.closed = True
                self._f.close()
        return self.path


def describe(direction: str, data: bytes) -> tuple[str, str, str]:
    """(kind, ticks text, spindle) of a packet / reply."""
    if direction == TX:
        if len(data) == 2 and data[0] == HEADER_SETTINGS:
            return "keepalive", f"perf={data[1]}", ""
        if len(data) == 7 and data[0] in (HEADER_MOTION, HEADER_JOG):
            _, ticks = decode_packet(data)
            if data[0] == HEADER_JOG and all(t.is_zero() for t in ticks) and \
                    not any(t.spindle for t in ticks):
                kind = "handshake"
            else:
                kind = "job" if data[0] == HEADER_MOTION else "jog"
            txt = " ".join(f"{t.x},{t.y},{t.z}" for t in ticks)
            sp = "".join("1" if t.spindle else "0" for t in ticks)
            return kind, txt, sp
        return "raw", "", ""
    if direction == RX and data:
        return REPLY_NAMES.get(data[0], "raw"), "", ""
    return "event", "", ""
