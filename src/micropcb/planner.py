"""Motion planner: G-code moves -> step ticks for the Wegstr.

Speeds are in mm per tick (one firmware tick = TICK_SECONDS).

- Each line gets a top speed from its feed and the per-axis step limit
  (`max_steps_per_tick`).
- Junction speeds come from the corner angle (GRBL-style junction
  deviation). A backward + forward pass makes every speed change reachable
  with the configured acceleration (trapezoid profile).
- Ticks are produced by walking along the path; each tick's target point is
  rounded to whole steps *absolutely*, so there is no position drift.
- A pause request ramps the speed down, then stops on the path; resuming
  ramps up again from the same point.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Iterator

from .gcode import DWELL, END, LINE, PAUSE, SPINDLE, Program
from .protocol import TICK_SECONDS, Tick

START_STEPS = 3.0          # steps/tick a stepper can start/stop at (like original ramp)
ACCEL_STEPS = 1.5          # steps/tick gained per tick (3 -> 14 in ~8 ticks)
JUNCTION_DEVIATION = 0.02  # mm
SPINUP_SECONDS = 1.0       # wait after spindle on

# items yielded by Planner.items()
TICK = "tick"
PAUSED = "pause"      # payload: message ("" = user pause)
FINISHED = "end"


@dataclass
class _Seg:
    p0: tuple[float, float, float]   # machine mm
    u: tuple[float, float, float]    # unit direction
    length: float
    vmax: float
    vmin: float
    accel: float
    line: int
    v_entry: float = 0.0
    v_exit: float = 0.0


class Planner:
    def __init__(self, program: Program, mm_per_step: list[float],
                 start_steps: tuple[int, int, int], offset_steps: list[int],
                 max_steps_per_tick: int = 14, accel_steps: float = ACCEL_STEPS,
                 start_steps_per_tick: float = START_STEPS) -> None:
        self.program = program
        self.mmps = list(mm_per_step)
        self.offset_mm = [offset_steps[i] * self.mmps[i] for i in range(3)]
        self.max_steps = max_steps_per_tick
        self.accel_steps = accel_steps
        self.start_spt = min(start_steps_per_tick, max_steps_per_tick)
        self.cur = list(start_steps)        # commanded step position
        self.spindle = False
        self._pause = threading.Event()
        self._paused_at = None   # (run, j, s) while a user pause holds inside a run
        self._skip_to = None     # segment index to continue from after the pause
        self._blocks = self._build()

    # ------------------------------------------------------------ public
    def request_pause(self) -> None:
        self._pause.set()

    def items(self) -> Iterator[tuple[str, object, int]]:
        """Yields (TICK, Tick, line), (PAUSED, message, line), (FINISHED, None, line)."""
        last_line = 0
        for kind, data in self._blocks:
            if kind == "run":
                for item in self._run(data):
                    last_line = item[2]
                    yield item
            elif kind == SPINDLE:
                self.spindle = data.value
                last_line = data.line
                if data.value:
                    yield from self._dwell(SPINUP_SECONDS, data.line)
                else:
                    yield TICK, Tick(spindle=False), data.line
            elif kind == DWELL:
                last_line = data.line
                yield from self._dwell(data.value, data.line)
            elif kind == PAUSE:
                last_line = data.line
                yield PAUSED, data.value, data.line
            elif kind == END:
                break
        if self.spindle:
            self.spindle = False
            yield TICK, Tick(spindle=False), last_line
        yield FINISHED, None, last_line

    def air_resume_target(self) -> tuple[int, list[int]] | None:
        """Paused with the tool above the copper (work Z >= 0)? Then the moves
        up to the next cut are only travel: returns (segment index, step position
        where that cut starts) so the resume can go there at safe height. None
        when paused in the copper or when there is no later cut in this run."""
        if self._paused_at is None:
            return None
        run, j, s = self._paused_at
        oz = self.offset_mm[2]
        seg = run[j]
        if seg.p0[2] + seg.u[2] * s - oz < -1e-6:
            return None                                  # in the middle of a cut
        for k in range(j, len(run)):
            sg = run[k]
            z0 = sg.p0[2] - oz
            z1 = sg.p0[2] + sg.u[2] * sg.length - oz
            if min(z0, z1) < -1e-6:                      # first move into the copper
                if k == j:
                    return None                          # already on that plunge
                return k, self._target_steps(sg, 0.0)
        return None

    def skip_to(self, k: int) -> None:
        """After air_resume_target: continue the paused run at segment k (the
        machine has been moved to its start)."""
        self._skip_to = k

    def shift(self, dx: int, dy: int, dz: int) -> None:
        """Move the not-yet-executed path by whole steps (work zero changed
        during a pause, e.g. Z re-zeroed after a tool change)."""
        d = (dx * self.mmps[0], dy * self.mmps[1], dz * self.mmps[2])
        self.offset_mm = [self.offset_mm[i] + d[i] for i in range(3)]
        for kind, data in self._blocks:
            if kind == "run":
                for seg in data:
                    seg.p0 = tuple(seg.p0[i] + d[i] for i in range(3))

    def estimate_ticks(self) -> int:
        """Ticks the job takes (pauses excluded). Uses a copy of this planner."""
        p = Planner(self.program, self.mmps, tuple(self.cur),  # type: ignore[arg-type]
                    [round(self.offset_mm[i] / self.mmps[i]) for i in range(3)],
                    self.max_steps, self.accel_steps, self.start_spt)
        return sum(1 for k, _, _ in p.items() if k == TICK)

    def estimate_seconds(self) -> float:
        return self.estimate_ticks() * TICK_SECONDS

    @property
    def max_feed_mm_min(self) -> float:
        """Fastest possible single-axis feed."""
        return self.max_steps * min(self.mmps) / TICK_SECONDS * 60

    # ------------------------------------------------------------ planning
    def _build(self) -> list[tuple[str, object]]:
        blocks: list[tuple[str, object]] = []
        run: list[_Seg] = []
        prev_end = None

        def flush() -> None:
            if run:
                self._plan_run(run)
                blocks.append(("run", list(run)))
                run.clear()

        for m in self.program.moves:
            if m.kind == LINE:
                p0 = tuple(m.start[i] + self.offset_mm[i] for i in range(3))
                p1 = tuple(m.end[i] + self.offset_mm[i] for i in range(3))
                if prev_end is not None:
                    p0 = prev_end  # chain exactly
                seg = self._segment(p0, p1, m.rapid, m.feed, m.line)  # type: ignore[arg-type]
                prev_end = p1
                if seg is not None:
                    run.append(seg)
            else:
                flush()
                blocks.append((m.kind, m))
                if m.kind == END:
                    break
        flush()
        return blocks

    def _segment(self, p0, p1, rapid: bool, feed: float, line: int) -> _Seg | None:
        d = [p1[i] - p0[i] for i in range(3)]
        length = math.sqrt(sum(x * x for x in d))
        if length < 1e-9:
            return None
        u = tuple(x / length for x in d)
        axis_lim = min(self.max_steps * self.mmps[i] / abs(u[i])
                       for i in range(3) if abs(u[i]) > 1e-12)
        vmax = axis_lim if rapid or feed <= 0 else min(axis_lim, feed / 60.0 * TICK_SECONDS)
        start_lim = min(self.start_spt * self.mmps[i] / abs(u[i])
                        for i in range(3) if abs(u[i]) > 1e-12)
        accel = min(self.accel_steps * self.mmps[i] / abs(u[i])
                    for i in range(3) if abs(u[i]) > 1e-12)
        return _Seg(p0, u, length, vmax, min(start_lim, vmax), accel, line)  # type: ignore[arg-type]

    def _plan_run(self, run: list[_Seg]) -> None:
        n = len(run)
        # junction limits
        run[0].v_entry = run[0].vmin
        for j in range(1, n):
            a, b = run[j - 1], run[j]
            cos = -sum(a.u[i] * b.u[i] for i in range(3))  # angle between -a and b
            vj = min(a.vmax, b.vmax)
            if cos > -0.999999:
                if cos > 0.999999:
                    vj = b.vmin  # full reversal
                else:
                    sin_half = math.sqrt((1 - cos) / 2)
                    acc = min(a.accel, b.accel)
                    vj = min(vj, math.sqrt(acc * JUNCTION_DEVIATION * sin_half / (1 - sin_half)))
            b.v_entry = max(min(vj, a.vmax, b.vmax), min(a.vmin, b.vmin))
        run[-1].v_exit = run[-1].vmin
        # backward pass
        for j in range(n - 1, -1, -1):
            s = run[j]
            if j < n - 1:
                s.v_exit = run[j + 1].v_entry
            s.v_entry = min(s.v_entry, math.sqrt(s.v_exit ** 2 + 2 * s.accel * s.length))
        # forward pass
        for j in range(n):
            s = run[j]
            reach = math.sqrt(s.v_entry ** 2 + 2 * s.accel * s.length)
            if j < n - 1:
                nxt = run[j + 1]
                nxt.v_entry = min(nxt.v_entry, reach)
                s.v_exit = nxt.v_entry
            else:
                s.v_exit = min(s.v_exit, reach)

    # ------------------------------------------------------------ tick generation
    def _target_steps(self, seg: _Seg, s: float) -> list[int]:
        return [round((seg.p0[i] + seg.u[i] * s) / self.mmps[i]) for i in range(3)]

    def _make_tick(self, target: list[int]) -> Tick:
        m = self.max_steps
        d = [max(-m, min(m, target[i] - self.cur[i])) for i in range(3)]
        for i in range(3):
            self.cur[i] += d[i]
        return Tick(d[0], d[1], d[2], self.spindle)

    def _catch_up(self, target: list[int], line: int) -> Iterator[tuple[str, object, int]]:
        while self.cur != target:
            yield TICK, self._make_tick(target), line

    def _run(self, run: list[_Seg]) -> Iterator[tuple[str, object, int]]:
        j, s, v = 0, 0.0, 0.0
        seg = run[0]
        # move from the commanded position onto the path start (normally 0 steps)
        yield from self._catch_up(self._target_steps(seg, 0.0), seg.line)
        while j < len(run):
            seg = run[j]
            r = seg.length - s
            if self._pause.is_set():
                v_new = v - seg.accel
                if v_new < seg.vmin:
                    # stopped on the path: make the step position exact, then pause
                    yield from self._catch_up(self._target_steps(seg, s), seg.line)
                    self._pause.clear()
                    self._paused_at = (run, j, s)
                    yield PAUSED, "", seg.line
                    self._paused_at = None
                    if self._skip_to is not None:     # resumed at safe height (air)
                        j, s = self._skip_to, 0.0
                        self._skip_to = None
                        seg = run[j]
                    v = 0.0
                    continue
            else:
                v_new = min(seg.vmax, v + seg.accel if v > 0 else seg.vmin,
                            math.sqrt(seg.v_exit ** 2 + 2 * seg.accel * max(r, 0.0)))
                v_new = max(v_new, seg.vmin)
            v = v_new
            budget = v
            while True:
                r = seg.length - s
                if budget < r:
                    s += budget
                    break
                budget -= r
                j += 1
                if j >= len(run):
                    s = seg.length
                    break
                seg, s = run[j], 0.0
                if budget <= 1e-12:
                    break
            yield TICK, self._make_tick(self._target_steps(seg, s)), seg.line
        last = run[-1]
        yield from self._catch_up(self._target_steps(last, last.length), last.line)

    def _dwell(self, seconds: float, line: int) -> Iterator[tuple[str, object, int]]:
        for _ in range(max(1, round(seconds / TICK_SECONDS))):
            if self._pause.is_set():
                self._pause.clear()
                yield PAUSED, "", line
            yield TICK, Tick(spindle=self.spindle), line
