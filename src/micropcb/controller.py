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

from .jog import jog_ticks, pack
from .serial_link import (EV_CONNECTED, EV_DISCONNECTED, EV_IDLE, EV_LOST,
                          EV_PROBE, EV_RELEASED, SerialLink, find_machine)
from .settings import MachineState, Settings

AXES = "xyz"
EV_STATE = "state"  # controller-level: something about position validity changed


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
    def _require_ready(self) -> None:
        if not self.connected:
            raise RuntimeError("not connected")
        if self.link.released:
            raise RuntimeError("motors released - press the release button again")

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
        self.link.stop()

    def emergency_stop(self) -> None:
        """Stop everything and turn the spindle off."""
        if self.connected:
            self.link.stop()
            if not self.link.released:
                self.link.set_spindle(False)

    def busy(self) -> bool:
        return self.connected and not self.link.wait_idle(0)

    def wait_idle(self, timeout: float | None = None) -> bool:
        return self.link.wait_idle(timeout)


__all__ = ["MachineController", "EV_STATE", "EV_CONNECTED"]
