"""Machine controller: position, work zero, soft limits, jog, go-to-zero.

No Qt here so it can be unit-tested with the simulator. The GUI only calls
these methods and listens to events.

Coordinates
- machine position: steps counted by the link since "machine home".
  Home = X min (bed right), Y min, Z top. Then X 0..140, Y 0..200, Z -40..0.
- work position = machine position - work offset.
"""
from __future__ import annotations

from typing import Callable

from .gcode import Program, parse
from .job import Job
from .jog import jog_ticks, pack
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

    # ------------------------------------------------------------ helpers
    def _new_link(self) -> SerialLink:
        return SerialLink(self._on_link_event, performance=self.settings.performance)

    def _emit(self, ev: str, payload: object = None) -> None:
        try:
            self.listener(ev, payload)
        except Exception:
            pass

    def _on_link_event(self, ev: str, payload: object) -> None:
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
        if not allow_job and self.job_active:
            raise RuntimeError("a job is running - stop it first")

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


    # ------------------------------------------------------------ jobs
    @property
    def job_active(self) -> bool:
        return self.job is not None and self.job.active

    def _fail_job(self, reason: str) -> None:
        if self.job_active:
            self.job.fail(reason)  # type: ignore[union-attr]

    def _on_job(self, job: Job) -> None:
        if not job.active:
            self.save_state()
        self._emit(EV_JOB, job)

    def program_for_run(self, text: str) -> Program:
        """Parse with the current work position as the start point."""
        return parse(text, self.work_mm() if self.connected else (0.0, 0.0, 0.0))

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
        if self.job_active:
            self._require_ready(allow_job=True)
            self.job.resume()  # type: ignore[union-attr]

    def stop_job(self) -> None:
        if self.job_active:
            self.job.stop()  # type: ignore[union-attr]


__all__ = ["MachineController", "EV_STATE", "EV_CONNECTED", "EV_JOB"]
