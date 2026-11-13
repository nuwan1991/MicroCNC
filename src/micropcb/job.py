"""Job runner: streams planner ticks to the serial link with pause/resume/stop.

Runs in its own thread. Keeps only a few packets queued in the link so that
pause and stop react quickly (the machine itself buffers about one packet).
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from .gcode import Program
from .planner import FINISHED, PAUSED, TICK, Planner
from .protocol import HEADER_MOTION, TICKS_PER_PACKET, Tick, encode_packet

QUEUE_DEPTH = 3

# job states
RUNNING = "running"
PAUSING = "pausing"
PAUSED_STATE = "paused"
STOPPING = "stopping"
DONE = "done"
STOPPED = "stopped"
FAILED = "failed"

ACTIVE = (RUNNING, PAUSING, PAUSED_STATE, STOPPING)


class Job:
    def __init__(self, name: str, program: Program, planner: Planner, link,
                 on_change: Callable[["Job"], None]) -> None:
        self.name = name
        self.program = program
        self.planner = planner
        self.link = link
        self.on_change = on_change
        self.state = RUNNING
        self.message = ""          # pause reason / error
        self.line = 0              # source line of the last packet sent
        self.ticks_sent = 0
        self.total_ticks = 0       # filled by caller (estimate)
        self.started = time.monotonic()
        self.paused_time = 0.0
        self._pause_t0 = 0.0
        self._abort = threading.Event()
        self._resume = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="micropcb-job", daemon=True)

    # ------------------------------------------------------------ control
    def start(self) -> None:
        self._thread.start()

    @property
    def active(self) -> bool:
        return self.state in ACTIVE

    def pause(self) -> None:
        if self.state == RUNNING:
            self._set(PAUSING)
            self.planner.request_pause()

    def resume(self) -> None:
        if self.state == PAUSED_STATE:
            self._resume.set()

    def stop(self, reason: str = "") -> None:
        """Stop now. The machine halts within about one packet."""
        if not self.active:
            return
        with self._lock:
            self._abort.set()
            self.message = reason
            self.state = STOPPING
            try:
                self.link.stop()
            except Exception:
                pass
        self._resume.set()
        self._notify()

    def fail(self, reason: str) -> None:
        """Called by the controller when the machine reports a problem."""
        if self.active:
            self.stop(reason)

    def join(self, timeout: float | None = None) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def elapsed(self) -> float:
        t = time.monotonic() - self.started - self.paused_time
        if self.state == PAUSED_STATE:
            t -= time.monotonic() - self._pause_t0
        return max(t, 0.0)

    def progress(self) -> float:
        if self.state == DONE:
            return 1.0
        return min(self.ticks_sent / self.total_ticks, 1.0) if self.total_ticks else 0.0

    # ------------------------------------------------------------ internals
    def _set(self, state: str, message: str | None = None) -> None:
        self.state = state
        if message is not None:
            self.message = message
        self._notify()

    def _notify(self) -> None:
        try:
            self.on_change(self)
        except Exception:
            pass

    def _send(self, ticks: list[Tick], line: int) -> bool:
        while not self._abort.is_set():
            if self.link.queue_size() < QUEUE_DEPTH:
                break
            time.sleep(0.004)
        pad = Tick(spindle=ticks[-1].spindle)
        ticks = ticks + [pad] * (TICKS_PER_PACKET - len(ticks))
        with self._lock:
            if self._abort.is_set():
                return False
            self.link.send([encode_packet(ticks, HEADER_MOTION)])
        self.line = line
        return True

    def _flush(self, buf: list[Tick], line: int) -> bool:
        if not buf:
            return True
        ok = self._send(buf, line)
        self.ticks_sent += len(buf)
        buf.clear()
        return ok

    def _run(self) -> None:
        buf: list[Tick] = []
        try:
            for kind, payload, line in self.planner.items():
                if self._abort.is_set():
                    break
                if kind == TICK:
                    buf.append(payload)  # type: ignore[arg-type]
                    if len(buf) == TICKS_PER_PACKET:
                        if not self._flush(buf, line):
                            break
                elif kind == PAUSED:
                    if not self._flush(buf, line):
                        break
                    self.link.wait_idle(5)
                    if self._abort.is_set():
                        break
                    self._resume.clear()
                    self.planner._pause.clear()  # a program pause also ends a user pause
                    self._pause_t0 = time.monotonic()
                    self.line = line
                    self._set(PAUSED_STATE, str(payload or "Paused"))
                    self._resume.wait()
                    self.paused_time += time.monotonic() - self._pause_t0
                    if self._abort.is_set():
                        break
                    self._set(RUNNING, "")
                elif kind == FINISHED:
                    if not self._flush(buf, line):
                        break
                    while not self._abort.is_set() and not self.link.wait_idle(0.1):
                        pass
                    if not self._abort.is_set():
                        self.line = line
                        self._set(DONE, "Job finished")
                    break
        except Exception as e:  # link closed under us etc.
            with self._lock:
                self._abort.set()
            self.message = f"Job error: {e}"
        if self._abort.is_set():
            # make sure the spindle ends up off
            try:
                if self.link.connected and not self.link.released:
                    self.link.wait_idle(1)
                    if self.link.spindle:
                        self.link.set_spindle(False)
            except Exception:
                pass
            self._set(FAILED if self.message else STOPPED, self.message or "Stopped by user")
