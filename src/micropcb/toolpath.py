"""Toolpath data for the views (Stage 2). No Qt here.

- `Toolpath`: the program's line moves as numpy arrays (work mm), with source
  line numbers and spindle state, plus lookups line <-> segments.
- `Timeline`: the planner's real tick stream as cumulative positions, so
  playback and the live job view follow exactly what the machine does.
- `check_program`: problems worth a look before cutting.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .gcode import LINE, SPINDLE, Program
from .planner import PAUSED, TICK, Planner
from .protocol import TICK_SECONDS


class Toolpath:
    def __init__(self, prog: Program) -> None:
        self.program = prog
        starts, ends, rapid, lines, feed, spindle = [], [], [], [], [], []
        sp = False
        for m in prog.moves:
            if m.kind == SPINDLE:
                sp = bool(m.value)
            elif m.kind == LINE:
                starts.append(m.start)
                ends.append(m.end)
                rapid.append(m.rapid)
                lines.append(m.line)
                feed.append(m.feed)
                spindle.append(sp)
        n = len(starts)
        self.start = np.array(starts, dtype=float).reshape(n, 3)
        self.end = np.array(ends, dtype=float).reshape(n, 3)
        self.rapid = np.array(rapid, dtype=bool)
        self.line = np.array(lines, dtype=np.int64)
        self.feed = np.array(feed, dtype=float)
        self.spindle = np.array(spindle, dtype=bool)

    def __len__(self) -> int:
        return len(self.line)

    def bounds(self) -> tuple[np.ndarray, np.ndarray] | None:
        if not len(self):
            return None
        pts = np.vstack([self.start, self.end])
        return pts.min(axis=0), pts.max(axis=0)

    def segments_for_line(self, line: int) -> slice:
        """Segments created by one source line (arcs give many)."""
        a = int(np.searchsorted(self.line, line, side="left"))
        b = int(np.searchsorted(self.line, line, side="right"))
        return slice(a, b)

    def nearest(self, x: float, y: float, tol: float, cuts_only: bool = False) -> int | None:
        """Index of the segment closest to (x, y) in the XY plane, within tol mm."""
        if not len(self):
            return None
        p0 = self.start[:, :2]
        d = self.end[:, :2] - p0
        ll = (d * d).sum(axis=1)
        ll[ll == 0] = 1e-12
        t = np.clip(((np.array([x, y]) - p0) * d).sum(axis=1) / ll, 0, 1)
        proj = p0 + d * t[:, None]
        dist = np.hypot(proj[:, 0] - x, proj[:, 1] - y)
        if cuts_only:
            dist = np.where(self.rapid, np.inf, dist)
        i = int(np.argmin(dist))
        return i if dist[i] <= tol else None


class Timeline:
    """Tool position after every tick the planner produces (work mm).

    Index i = after i ticks; index 0 = start position. Time = i * TICK_SECONDS.
    """

    def __init__(self, positions: np.ndarray, lines: np.ndarray, spindle: np.ndarray,
                 pauses: list[int]) -> None:
        self.pos = positions        # (n+1, 3) float, work mm
        self.lines = lines          # (n+1,) source line being executed
        self.spindle = spindle      # (n+1,) bool
        self.pauses = pauses        # tick indices where the program pauses (M0/M6)

    @property
    def ticks(self) -> int:
        return len(self.pos) - 1

    @property
    def seconds(self) -> float:
        return self.ticks * TICK_SECONDS

    def index_at(self, seconds: float) -> int:
        return max(0, min(self.ticks, int(seconds / TICK_SECONDS)))

    def index_for_line(self, line: int) -> int:
        """First tick at or after the given source line."""
        i = int(np.searchsorted(self.lines[1:], line, side="left"))
        return min(i + 1, self.ticks) if self.ticks else 0

    @classmethod
    def build(cls, planner: Planner) -> "Timeline":
        mmps = np.array(planner.mmps)
        off_steps = np.array([round(planner.offset_mm[i] / planner.mmps[i]) for i in range(3)])
        start = np.array(planner.cur, dtype=np.int64)
        deltas: list[tuple[int, int, int]] = []
        lines: list[int] = []
        spin: list[bool] = []
        pauses: list[int] = []
        first_line = 0
        for kind, t, ln in planner.items():
            if kind == TICK:
                deltas.append((t.x, t.y, t.z))
                lines.append(ln)
                spin.append(t.spindle)
            elif kind == PAUSED:
                pauses.append(len(deltas))
            if not first_line:
                first_line = ln
        d = np.array(deltas, dtype=np.int64).reshape(len(deltas), 3)
        steps = np.vstack([start[None, :], start[None, :] + np.cumsum(d, axis=0)])
        pos = (steps - off_steps[None, :]) * mmps[None, :]
        ln = np.array([first_line] + lines, dtype=np.int64)
        sp = np.array([False] + spin, dtype=bool)
        return cls(pos, ln, sp, pauses)


# ------------------------------------------------------------------ checks
ERROR = "error"
WARNING = "warning"


@dataclass
class Issue:
    severity: str
    line: int
    text: str


def check_program(tp: Toolpath, *, max_depth_mm: float = 2.0,
                  max_feed_mm_min: float | None = None,
                  area: tuple[np.ndarray, np.ndarray] | None = None,
                  limit: int = 300, surface=None) -> list[Issue]:
    """Look for likely mistakes. Surface is at work Z = 0, or at surface(x, y)
    when a height map is applied (depths are then measured from the copper).

    area: (min, max) allowed work coordinates (from the machine work area),
    or None if machine home is not set.
    """
    issues: list[Issue] = []
    if not len(tp):
        return issues

    def add(sev: str, idx: np.ndarray, text: str) -> None:
        seen: set[int] = set()
        for i in idx:
            ln = int(tp.line[i])
            if ln in seen:
                continue
            seen.add(ln)
            if len(issues) >= limit:
                return
            issues.append(Issue(sev, ln, text))

    zs, ze = tp.start[:, 2], tp.end[:, 2]
    if surface is not None:      # depth below the measured copper, not below Z 0
        surf = np.vectorize(surface, otypes=[float])
        zs = zs - surf(tp.start[:, 0], tp.start[:, 1])
        ze = ze - surf(tp.end[:, 0], tp.end[:, 1])
    zmin_seg = np.minimum(zs, ze)
    xy_move = np.hypot(*(tp.end[:, :2] - tp.start[:, :2]).T) > 1e-6
    plunge = tp.end[:, 2] < tp.start[:, 2] - 1e-9

    # rapid below the surface
    bad = np.nonzero(tp.rapid & (zmin_seg < -1e-6) & xy_move)[0]
    add(ERROR, bad, "Rapid (G0) sideways move below the surface - tool may break")
    bad = np.nonzero(tp.rapid & ~xy_move & plunge & (ze < -1e-6))[0]
    add(ERROR, bad, "Rapid (G0) plunge into the material")
    # cutting with spindle off
    bad = np.nonzero(~tp.rapid & (zmin_seg < -1e-6) & ~tp.spindle)[0]
    add(ERROR, bad, "Cutting below the surface with the spindle OFF (no M3)")
    # too deep
    bad = np.nonzero(zmin_seg < -abs(max_depth_mm) - 1e-9)[0]
    add(WARNING, bad, f"Deeper than {max_depth_mm:g} mm - unusual for PCB work")
    # feed
    if max_feed_mm_min:
        bad = np.nonzero(~tp.rapid & (tp.feed > max_feed_mm_min + 0.5))[0]
        if len(bad):
            add(WARNING, bad[:1], f"Feed above the machine maximum ({max_feed_mm_min:.0f} mm/min)"
                                  " - these moves will run slower")
    # outside work area
    if area is not None:
        lo, hi = area
        out = np.zeros(len(tp), dtype=bool)
        for pts in (tp.start, tp.end):
            out |= ((pts < lo[None, :] - 1e-6) | (pts > hi[None, :] + 1e-6)).any(axis=1)
        add(ERROR, np.nonzero(out)[0], "Outside the machine work area")
    issues.sort(key=lambda i: (i.line, i.severity != ERROR))
    return issues
