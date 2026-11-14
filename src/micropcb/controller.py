"""Machine controller: position, work zero, soft limits, jog, go-to-zero.

No Qt here so it can be unit-tested with the simulator. The GUI only calls
these methods and listens to events.

Coordinates
- machine position: steps counted by the link since "machine home".
  Home = X min (bed right), Y min, Z top. Then X 0..140, Y 0..200, Z -40..0.
- work position = machine position - work offset.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

import numpy as np

from .gcode import Program, parse
from .job import PAUSED_STATE, Job
from .autolevel import HeightMap, probe_order
from .jog import jog_ticks, pack
from .protocol import HEADER_JOG, Tick, encode_packet
from .planner import Planner
from .serial_link import (EV_CONNECTED, EV_DISCONNECTED, EV_IDLE, EV_LOST,
                          EV_OVERLOAD, EV_PROBE, EV_RELEASED, SerialLink,
                          find_machine)
from .settings import MachineState, Settings

AXES = "xyz"
EV_STATE = "state"  # controller-level: something about position validity changed
EV_JOB = "job"      # payload: Job (state changed)


class MachineController:
    def __init__(self, settings: Settings | None = None, state: MachineState | None = None,
                 listener: Callable[[str, object], None] | None = None,
                 persist: bool = True) -> None:
        self.settings = settings or Settings()
        self.state = state or MachineState()
        self.listener = listener or (lambda e, p: None)
        self.persist = persist
        self.link = self._new_link()
        self.port: str | None = None
        self.was_unclean = not self.state.clean_exit
        self.job: Job | None = None
        # probing (Stage 2.4)
        self._probe_wait: threading.Event | None = None
        self._touch_test_valid: bool | None = None   # set while a touch-test wiggle is out
        self._touch_latched = False   # machine refuses Z- after a touch until Z moves up
        self._probe_value: int | None = None
        self.last_probe_value: int | None = None
        self.fw_z_offset: int | None = None   # PC Z steps - machine Z counter
        self.probe_log: list[str] = []
        self.program_transform: Callable[[Program], Program] | None = None

    # ------------------------------------------------------------ helpers
    def _new_link(self) -> SerialLink:
        return SerialLink(self._on_link_event, performance=self.settings.performance)

    def _emit(self, ev: str, payload: object = None) -> None:
        try:
            self.listener(ev, payload)
        except Exception:
            pass

    def _on_link_event(self, ev: str, payload: object) -> None:
        if ev == EV_PROBE:
            self._touch_latched = True
        if ev == EV_PROBE and self._touch_test_valid is not None:
            # touch-test wiggle (Z +14, Z -14): the up tick ran, the down tick
            # was cut short somewhere - assume half of it (+-0.028 mm)
            x, y, z = self.link.position
            self.link.set_position(x, y, z + 7)
            self.link.position_valid = self._touch_test_valid
            self._touch_test_valid = None
            self.last_probe_value = payload
            self._emit(ev, payload)
            return
        if ev == EV_PROBE:
            self.last_probe_value = payload
            if self._probe_wait is not None:
                # our own probe cycle: it restores the position itself
                self._probe_value = payload
                self._probe_wait.set()
                self._emit(ev, payload)
                return
            if not self.link.last_probe_in_motion:
                # contact while idle (e.g. clip touched to the tool): harmless
                self._emit(ev, payload)
                return
        if ev in (EV_RELEASED, EV_PROBE, EV_LOST) and (ev != EV_RELEASED or payload):
            # step counting can no longer be trusted
            self.state.home_set = False
            self._fail_job({EV_RELEASED: "Motors released - job stopped",
                            EV_PROBE: "Probe touch - job stopped",
                            EV_LOST: "Connection lost - job stopped"}[ev])
        if ev == EV_OVERLOAD:
            self._fail_job("Spindle overload - job stopped")
        if ev in (EV_IDLE, EV_DISCONNECTED, EV_LOST):
            self.save_state()
        self._emit(ev, payload)
        if ev in (EV_RELEASED, EV_PROBE, EV_LOST):
            self._emit(EV_STATE)

    def save_state(self) -> None:
        self.state.machine_pos = list(self.link.position)
        if self.persist:
            try:
                self.state.save()
            except OSError:
                pass

    def mmps(self, i: int) -> float:
        return self.settings.mm_per_step[i]

    # ------------------------------------------------------------ connection
    @property
    def connected(self) -> bool:
        return self.link.connected

    def connect(self, port: str | None = None, transport=None) -> str:
        if self.connected:
            return self.port or ""
        self.link = self._new_link()
        self.fw_z_offset = None  # machine may have been power-cycled
        if transport is not None:
            self.link.open(transport)
            self.port = getattr(transport, "port", "SIM")
        else:
            port = port or find_machine()
            if not port:
                raise ConnectionError("Wegstr machine not found on any COM port")
            self.link.open_port(port)
            self.port = port
            self.settings.last_port = port
        # Restore last known position (machine has no memory).
        x, y, z = self.state.machine_pos
        self.link.set_position(x, y, z)
        if self.was_unclean:
            self.link.position_valid = False
            self.state.home_set = False
        self.state.clean_exit = False
        self.save_state()
        self._emit(EV_STATE)
        return self.port

    def disconnect(self) -> None:
        if self.job_active:
            self.job.stop("Disconnected")  # type: ignore[union-attr]
            self.job.join(2)  # type: ignore[union-attr]
        if self.connected:
            self.link.close()
        self.state.clean_exit = self.link.position_valid
        self.save_state()

    # ------------------------------------------------------------ positions
    @property
    def position_valid(self) -> bool:
        return self.link.position_valid

    def machine_steps(self) -> tuple[int, int, int]:
        return self.link.position

    def machine_mm(self) -> tuple[float, float, float]:
        p = self.link.position
        return tuple(p[i] * self.mmps(i) for i in range(3))  # type: ignore[return-value]

    def work_mm(self) -> tuple[float, float, float]:
        p = self.link.position
        o = self.state.work_offset
        return tuple((p[i] - o[i]) * self.mmps(i) for i in range(3))  # type: ignore[return-value]

    def zero_axes(self, axes: str = AXES) -> None:
        p = self.link.position
        for a in axes:
            i = AXES.index(a)
            self.state.work_offset[i] = p[i]
        if axes == AXES or set(axes) == set(AXES):
            # user has re-referenced the work -> trust step counting again
            self.link.position_valid = True
            self.was_unclean = False
        self.save_state()
        self._emit(EV_STATE)

    def set_home(self) -> None:
        """Current position becomes machine home (X min, Y min, Z top)."""
        p = self.link.position
        self.state.work_offset = [self.state.work_offset[i] - p[i] for i in range(3)]
        self.link.set_position(0, 0, 0)
        self.state.home_set = True
        self.link.position_valid = True
        self.was_unclean = False
        self.save_state()
        self._emit(EV_STATE)

    # ------------------------------------------------------------ limits
    def limits_active(self) -> bool:
        return self.settings.soft_limits and self.state.home_set and self.link.position_valid

    def limit_range(self, i: int) -> tuple[int, int]:
        n = round(self.settings.work_area_mm[i] / self.mmps(i))
        return (-n, 0) if i == 2 else (0, n)

    def clamp_steps(self, i: int, delta: int) -> int:
        if not self.limits_active():
            return delta
        lo, hi = self.limit_range(i)
        cur = self.link.position[i]
        return max(lo, min(hi, cur + delta)) - cur

    # ------------------------------------------------------------ motion
    def _require_ready(self, allow_job: bool = False) -> None:
        if not self.connected:
            raise RuntimeError("not connected")
        if self.link.released:
            raise RuntimeError("motors released - press the release button again")
        if not allow_job and self.job_active and not self.job_paused:
            raise RuntimeError("a job is running - pause or stop it first")

    def jog(self, axis: str, mm: float, steps_per_tick: int | None = None) -> float:
        """Relative jog; returns the distance actually commanded (after limits)."""
        self._require_ready()
        i = AXES.index(axis)
        delta = self.clamp_steps(i, round(mm / self.mmps(i)))
        if delta:
            spt = min(steps_per_tick or self.settings.jog_steps_per_tick,
                      self.settings.max_steps_per_tick)
            sp = self.link.spindle
            self.link.send(pack(jog_ticks(axis, delta, spt, sp), spindle=sp))
            if axis == "z" and delta > 0:
                self._touch_latched = False
        return delta * self.mmps(i)

    def jog_continuous(self, axis: str, direction: int, steps_per_tick: int | None = None) -> None:
        """Move until stop() (or the soft limit). Used while a key is held."""
        i = AXES.index(axis)
        far = self.settings.work_area_mm[i] * (1 if direction > 0 else -1)
        self.jog(axis, far, steps_per_tick)

    def go_to_work_zero(self) -> None:
        """Raise Z to safe height, move X/Y to 0, then lower Z to 0."""
        self._require_ready()
        x, y, z = self.work_mm()
        safe = self.settings.safe_height_mm
        if z < safe:
            self.jog("z", safe - z)
            z = safe
        if abs(x) > 1e-9:
            self.jog("x", -x)
        if abs(y) > 1e-9:
            self.jog("y", -y)
        self.jog("z", -z)

    def set_spindle(self, on: bool) -> None:
        self._require_ready()
        self.link.set_spindle(on)

    def stop(self) -> None:
        """Soft stop: drop queued motion (machine halts within ~60 ms)."""
        if self.job_active:
            self.job.stop()  # type: ignore[union-attr]
        self.link.stop()

    def emergency_stop(self) -> None:
        """Stop everything and turn the spindle off."""
        if self.job_active:
            self.job.stop("E-STOP")  # type: ignore[union-attr]
        if self.connected:
            self.link.stop()
            if not self.link.released:
                self.link.set_spindle(False)

    def busy(self) -> bool:
        return self.connected and not self.link.wait_idle(0)

    def wait_idle(self, timeout: float | None = None) -> bool:
        return self.link.wait_idle(timeout)


    # ------------------------------------------------------------ probing
    def touch_check(self) -> bool:
        """One touch-test wiggle: Z up 14 steps then down 14 (net 0, 0.056 mm).

        The machine reports contact only while Z moves down fast enough (it
        ignores a zero-motion packet), so a tiny real move is needed.
        Returns False when it could not be sent (busy / not ready).
        """
        if not self.connected or self.link.released or self.job_active or self.busy():
            return False
        if self.link.spindle:
            raise RuntimeError("switch the spindle off for the touch test")
        self._touch_test_valid = self.link.position_valid
        self.link.send([encode_packet([Tick(z=14), Tick(z=-14), Tick()], HEADER_JOG)])
        return True

    def _unlatch_touch(self) -> None:
        """After a touch the machine refuses Z- until Z moves up: lift 14 steps
        (0.056 mm) and come back. A touch reported here means the clip is still
        touching the tool."""
        self._probe_value = None
        self._probe_wait = threading.Event()
        try:
            self.link.send([encode_packet([Tick(z=14), Tick(z=-14), Tick()], HEADER_JOG)])
            if not self.link.wait_idle(2):
                raise RuntimeError("machine did not answer")
            touched = self._probe_wait.wait(0.05)
        finally:
            self._probe_wait = None
        if touched:
            raise ProbeError("The probe still reports contact - is the clip touching the "
                             "tool or is the tool resting on the copper? Lift Z and try again.")
        self._touch_latched = False

    def end_touch_test(self) -> None:
        self._touch_test_valid = None

    def probe(self, max_travel_mm: float, steps_per_tick: int = 14,
              stop: threading.Event | None = None) -> float:
        """Move Z down until the tool touches the copper (clip on the board).

        Returns the work Z (mm) of the touch and leaves the tool there; the
        position is exact afterwards (from the machine's Z counter).
        Raises ProbeError if nothing is touched within max_travel_mm.

        Hardware (capture 2026-11-14 17:01): the machine does NOT report a touch
        at 3 steps/tick (it keeps answering 01); the original software probes at
        14 and every touch is reported. Each packet carries ONE moving tick and
        two empty ones, so the step count of the packet that hit is uncertain by
        at most one tick (14 steps = 0.056 mm), not three.
        """
        self._touch_test_valid = None
        self._require_ready()
        if self.busy():
            raise RuntimeError("machine is moving")
        if self.link.spindle:
            raise RuntimeError("switch the spindle off before probing")
        if self._touch_latched:
            self._unlatch_touch()
        was_valid = self.link.position_valid
        mz = self.mmps(2)
        spt = max(1, min(int(steps_per_tick), self.settings.max_steps_per_tick))
        remaining = round(max_travel_mm / mz)
        self._probe_value = None
        self._probe_wait = threading.Event()
        try:
            while remaining > 0 and not self._probe_wait.is_set():
                if stop is not None and stop.is_set():
                    self.link.stop()
                    raise ProbeCancelled("probing cancelled")
                if self.link.queue_size() >= 2:
                    time.sleep(0.003)
                    continue
                ticks = [Tick(z=-min(spt, remaining)), Tick(), Tick()]
                remaining -= -ticks[0].z
                self.link.send([encode_packet(ticks, HEADER_JOG)])
            while not self._probe_wait.is_set() and not self.link.wait_idle(0.05):
                if not self.connected:
                    raise RuntimeError("connection lost while probing")
            touched = self._probe_wait.wait(0.3)
            if not touched:
                raise ProbeError(f"No touch within {max_travel_mm:.2f} mm - is the clip on the "
                                 "copper and the tool above the board?")
            self.link.wait_idle(2)
            value = int(self._probe_value)  # type: ignore[arg-type]
        finally:
            self._probe_wait = None
        before = self.link.position  # counted steps exclude the packet that hit
        x, y, zb = before
        if self.fw_z_offset is None:
            # first touch: the packet in flight did 1..3*spt steps; take the middle
            guess = zb - max(1, round(0.5 * spt))
            self.fw_z_offset = guess - value
            self.probe_log.append(f"probe reference: machine counter {value}, PC Z {guess}")
            z_touch = guess
        else:
            z_touch = value + self.fw_z_offset
            z_touch += round((zb - z_touch) / 65536) * 65536  # 16-bit wrap
            if not (zb - spt - 2 <= z_touch <= zb + 2):
                # The machine's counter is exact; our sent-step count is not
                # (steps refused around a touch are still acknowledged). Trust
                # the counter - unless it jumped so far that it was reset.
                diff_mm = (z_touch - zb) * mz
                if abs(diff_mm) <= 2.0:
                    self.probe_log.append(
                        f"Z corrected {diff_mm:+.3f} mm from the machine's counter "
                        f"(counter {value}, counted Z {zb}, now {z_touch})")
                else:
                    guess = zb - max(1, round(0.5 * spt))
                    self.fw_z_offset = guess - value
                    self.probe_log.append(
                        f"⚠ machine counter jumped {diff_mm:+.2f} mm (counter {value}, counted "
                        f"Z {zb}) - new probe reference taken. Set Z zero again before cutting.")
                    z_touch = guess
        self.link.set_position(x, y, z_touch)
        self.link.position_valid = was_valid
        self.save_state()
        self._emit(EV_STATE)
        return (z_touch - self.state.work_offset[2]) * mz

    def run_autolevel(self, hm: HeightMap, *, clearance_mm: float = 1.0,
                      first_travel_mm: float = 5.0, extra_travel_mm: float = 1.0,
                      probe_spt: int = 14, stop: threading.Event | None = None,
                      on_point: Callable[[int, int, float], None] | None = None) -> HeightMap:
        """Probe every grid point (work X/Y) and fill hm.z (mm, first point = 0).

        Start with the tool at most first_travel_mm above the copper. Z is lifted
        by clearance_mm before EVERY sideways move, also the first one (the tool
        may be resting just above the copper after Probe + set Z 0).
        Work Z 0 is set to the copper at the first point.
        """
        if self.job_active:
            raise RuntimeError("stop the job before measuring a height map")
        self._require_ready()
        if not self.position_valid:
            raise RuntimeError("position is not reliable - set work zero first")
        order = probe_order(len(hm.xs), len(hm.ys))
        hm.z[:] = float("nan")
        top = 0.0  # highest surface measured so far (work mm)
        first_lift = 0.0
        for n, (i, j) in enumerate(order):
            if stop is not None and stop.is_set():
                raise ProbeCancelled("auto-level cancelled")
            x, y = float(hm.xs[i]), float(hm.ys[j])
            wx, wy, wz = self.work_mm()
            if n:
                travel_z = top + clearance_mm
                if wz < travel_z:
                    self.jog("z", travel_z - wz)
            elif abs(x - wx) > 0.01 or abs(y - wy) > 0.01:
                # copper height unknown yet: lift relative to where the tool is
                first_lift = self.jog("z", clearance_mm)
            self.jog("x", x - wx)
            self.jog("y", y - wy)
            if not self.wait_idle(120):
                raise RuntimeError("move did not finish")
            px, py, _ = self.work_mm()
            if abs(px - x) > 0.01 or abs(py - y) > 0.01:
                raise ProbeError(f"Probe point X {x:.2f} Y {y:.2f} is outside the work area "
                                 "(soft limits)")
            if n == 0:
                z = self.probe(first_travel_mm + first_lift, max(1, probe_spt - 1), stop)
                self.zero_axes("z")  # copper at the first point = work Z 0
                z = 0.0
            else:
                travel = self.work_mm()[2] - (np.nanmin(hm.z) - extra_travel_mm)
                z = self.probe(travel, probe_spt, stop)
            hm.z[j, i] = z
            top = max(top, z)
            self.jog("z", clearance_mm)
            self.wait_idle(30)
            if on_point is not None:
                on_point(i, j, z)
        hm.meta.update({"points": hm.probed, "clearance_mm": clearance_mm,
                        "time": time.strftime("%Y-%m-%d %H:%M:%S")})
        return hm

    # ------------------------------------------------------------ jobs
    @property
    def job_active(self) -> bool:
        return self.job is not None and self.job.active

    def _fail_job(self, reason: str) -> None:
        if self.job_active:
            self.job.fail(reason)  # type: ignore[union-attr]

    @property
    def job_paused(self) -> bool:
        return self.job is not None and self.job.state == PAUSED_STATE

    def _on_job(self, job: Job) -> None:
        if job.state == PAUSED_STATE:
            # remember where we left the path (tool change: user may jog / re-zero Z)
            self._pause_offset = list(self.state.work_offset)
        if not job.active:
            self.save_state()
        self._emit(EV_JOB, job)

    def program_for_run(self, text: str) -> Program:
        """Parse with the current work position as the start point."""
        prog = parse(text, self.work_mm() if self.connected else (0.0, 0.0, 0.0))
        if self.program_transform is not None:  # e.g. auto-level height map
            prog = self.program_transform(prog)
        return prog

    def check_program(self, prog: Program) -> list[str]:
        """Problems that block running (empty list = OK)."""
        problems = []
        b = prog.bounds()
        if b is None and not prog.moves:
            problems.append("The file has no moves.")
        if b is not None and self.limits_active():
            off = self.state.work_offset
            for i, a in enumerate("XYZ"):
                lo, hi = self.limit_range(i)
                mn = b[0][i] / self.mmps(i) + off[i]
                mx = b[1][i] / self.mmps(i) + off[i]
                if mn < lo - 0.5 or mx > hi + 0.5:
                    problems.append(
                        f"{a} goes outside the work area ({lo * self.mmps(i):.1f} … "
                        f"{hi * self.mmps(i):.1f} mm machine; job needs "
                        f"{mn * self.mmps(i):.2f} … {mx * self.mmps(i):.2f}).")
        return problems

    def make_planner(self, prog: Program) -> Planner:
        return Planner(prog, self.settings.mm_per_step, self.link.position,
                       self.state.work_offset, self.settings.max_steps_per_tick)

    def start_job(self, text: str, name: str = "job") -> Job:
        """Parse, check and start a G-code job from the current position."""
        if self.job_active:
            raise RuntimeError("a job is already active")
        self._require_ready()
        if self.busy():
            raise RuntimeError("machine is moving - wait until it is idle")
        if not self.position_valid:
            raise RuntimeError("position is not reliable - set work zero first")
        prog = self.program_for_run(text)
        problems = self.check_program(prog)
        if problems:
            raise RuntimeError(" ".join(problems))
        planner = self.make_planner(prog)
        total = planner.estimate_ticks()
        self.job = Job(name, prog, planner, self.link, self._on_job)
        self.job.total_ticks = total
        self.job.start()
        self._emit(EV_JOB, self.job)
        return self.job

    def pause_job(self) -> None:
        if self.job_active:
            self.job.pause()  # type: ignore[union-attr]

    def resume_job(self) -> None:
        """Resume. If the tool was moved or Z re-zeroed during the pause (tool
        change), first return to the path: Z up, X/Y back, Z down."""
        if not self.job_active:
            return
        self._require_ready(allow_job=True)
        job = self.job
        if self.job_paused:
            if self.busy() and not self.wait_idle(10):
                raise RuntimeError("machine is still moving")
            pl = job.planner  # type: ignore[union-attr]
            old = getattr(self, "_pause_offset", None) or list(self.state.work_offset)
            now = list(self.state.work_offset)
            dz = now[2] - old[2]
            if now[:2] != old[:2]:
                self.state.work_offset[:2] = old[:2]   # X/Y zero must not change mid-job
                self.probe_log.append("X/Y zero was changed during the pause - restored")
            if dz:
                pl.shift(0, 0, dz)                       # remaining path follows new Z zero
                pl.cur[2] += dz
                self._pause_offset = list(self.state.work_offset)
            target = list(pl.cur)
            pos = list(self.link.position)
            if pos != target:
                mm = self.mmps
                safe = max(pos[2], target[2]) + round(self.settings.safe_height_mm / mm(2))
                self._move_steps("z", safe - pos[2])
                self._move_steps("x", target[0] - pos[0])
                self._move_steps("y", target[1] - pos[1])
                self._move_steps("z", target[2] - safe)
                if not self.wait_idle(120):
                    raise RuntimeError("return to the path did not finish")
                pl.cur = list(self.link.position)
        job.resume()  # type: ignore[union-attr]

    def _move_steps(self, axis: str, steps: int) -> None:
        if steps:
            sp = self.link.spindle
            self.link.send(pack(jog_ticks(axis, steps, self.settings.jog_steps_per_tick, sp),
                                spindle=sp))

    def stop_job(self) -> None:
        if self.job_active:
            self.job.stop()  # type: ignore[union-attr]


class ProbeError(RuntimeError):
    pass


class ProbeCancelled(RuntimeError):
    pass


__all__ = ["MachineController", "EV_STATE", "EV_CONNECTED", "EV_JOB"]
