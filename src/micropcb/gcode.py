"""G-code parser (Stage 1 subset, see docs/stage1-design.md §5).

Turns G-code text into a flat list of `Move` objects in **work coordinates
(mm)**. Arcs are split into short lines here, so the planner only sees
straight lines, dwells, pauses and spindle changes.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace

ARC_TOLERANCE_MM = 0.005  # max chord error when splitting arcs

# Move kinds
LINE = "line"
DWELL = "dwell"        # value = seconds
PAUSE = "pause"        # value = message (M0/M1/M6)
SPINDLE = "spindle"    # value = bool
END = "end"


@dataclass
class Move:
    kind: str
    line: int                      # 1-based source line number
    start: tuple[float, float, float] = (0.0, 0.0, 0.0)
    end: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rapid: bool = False
    feed: float = 0.0              # mm/min (0 = not set)
    value: object = None


@dataclass
class Program:
    moves: list[Move] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    line_count: int = 0
    max_feed: float = 0.0          # highest F used by cutting moves (mm/min)
    uses_spindle: bool = False

    def bounds(self) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
        pts = [p for m in self.moves if m.kind == LINE for p in (m.start, m.end)]
        if not pts:
            return None
        return (tuple(min(p[i] for p in pts) for i in range(3)),   # type: ignore[return-value]
                tuple(max(p[i] for p in pts) for i in range(3)))

    def lines(self) -> list[Move]:
        return [m for m in self.moves if m.kind == LINE]


def job_min_xy(prog: Program) -> tuple[float, float] | None:
    """Lowest X and Y the program moves to (end points; the start point is where
    the machine happens to be, not part of the job)."""
    pts = [m.end for m in prog.moves if m.kind == LINE]
    if not pts:
        return None
    return min(p[0] for p in pts), min(p[1] for p in pts)


def shift_program(prog: Program, dx: float, dy: float) -> Program:
    """Program moved by dx, dy (work mm). The first move still starts where the
    machine is."""
    if not dx and not dy:
        return prog
    out = Program(warnings=list(prog.warnings), line_count=prog.line_count,
                  max_feed=prog.max_feed)
    first = True
    for m in prog.moves:
        if m.kind != LINE:
            out.moves.append(m)
            continue
        s, e = m.start, m.end
        start = s if first else (s[0] + dx, s[1] + dy, s[2])
        first = False
        out.moves.append(replace(m, start=start, end=(e[0] + dx, e[1] + dy, e[2])))
    return out


class GCodeError(ValueError):
    pass


_WORD = re.compile(r"([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")
_IGNORED_G = {17, 40, 49, 54, 61, 64, 80, 94}   # harmless, no effect for us
_IGNORED_M = {7, 8, 9}                           # coolant


def _strip_comments(text: str) -> str:
    text = re.sub(r"\([^)]*\)", " ", text)
    return text.split(";", 1)[0]


def parse(text: str, start: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> Program:
    """Parse G-code. `start` is the tool's work position when the job begins."""
    prog = Program()
    pos = list(start)
    absolute = True
    scale = 1.0              # 25.4 for inches
    motion = 0               # modal G0/G1/G2/G3
    feed = 0.0
    g92 = [0.0, 0.0, 0.0]    # G92 shift: program coord = work coord - g92
    spindle = False
    warned: set[str] = set()

    def warn(msg: str) -> None:
        if msg not in warned:
            warned.add(msg)
            prog.warnings.append(msg)

    lines = text.splitlines()
    prog.line_count = len(lines)
    for ln, raw in enumerate(lines, 1):
        comment = " ".join(c.strip() for c in re.findall(r"\(([^)]*)\)", raw)).strip()
        s = _strip_comments(raw).strip().upper()
        if not s or s == "%":
            continue
        words: list[tuple[str, float]] = [(w, float(v)) for w, v in _WORD.findall(s)]
        leftover = _WORD.sub("", s).strip()
        if leftover:
            raise GCodeError(f"line {ln}: cannot read '{raw.strip()}'")
        vals: dict[str, float] = {}
        gs: list[float] = []
        ms: list[float] = []
        for w, v in words:
            if w == "G":
                gs.append(v)
            elif w == "M":
                ms.append(v)
            elif w == "N":
                continue
            else:
                vals[w] = v

        dwell_cmd = False
        g92_cmd = False
        for g in gs:
            gi = int(g)
            if g != gi:
                warn(f"G{g:g} not supported (ignored)")
            elif gi in (0, 1, 2, 3):
                motion = gi
            elif gi == 4:
                dwell_cmd = True
            elif gi == 20:
                scale = 25.4
            elif gi == 21:
                scale = 1.0
            elif gi == 90:
                absolute = True
            elif gi == 91:
                absolute = False
            elif gi == 92:
                g92_cmd = True
            elif gi in (18, 19):
                raise GCodeError(f"line {ln}: G{gi} (arc plane) not supported - only G17 (XY)")
            elif gi in _IGNORED_G:
                pass
            else:
                warn(f"G{gi} not supported (ignored)")

        if "F" in vals:
            feed = vals["F"] * scale
        if "S" in vals:
            warn("S (spindle speed) ignored - the Wegstr spindle has one speed")

        # --- spindle / program flow (before motion on the same line? Common
        # CAM puts M3 alone; we apply M3/M5 before motion, M0/M2 after.)
        after: list[Move] = []
        for m in ms:
            mi = int(m)
            if mi in (3, 4):
                if not spindle:
                    spindle = True
                    prog.uses_spindle = True
                    prog.moves.append(Move(SPINDLE, ln, value=True))
            elif mi == 5:
                if spindle:
                    spindle = False
                    prog.moves.append(Move(SPINDLE, ln, value=False))
            elif mi in (0, 1):
                after.append(Move(PAUSE, ln, value=comment or f"Program pause (M{mi})"))
            elif mi == 6:
                t = vals.get("T")
                after.append(Move(PAUSE, ln, value=comment or (
                    f"Change tool to T{int(t)}" if t is not None else "Change tool")))
            elif mi in (2, 30):
                after.append(Move(END, ln))
            elif mi in _IGNORED_M:
                pass
            else:
                warn(f"M{mi} not supported (ignored)")

        if g92_cmd:
            for i, a in enumerate("XYZ"):
                if a in vals:
                    g92[i] = pos[i] - vals[a] * scale
        elif dwell_cmd:
            secs = vals.get("P", vals.get("X", 0.0))
            if secs > 0:
                prog.moves.append(Move(DWELL, ln, start=tuple(pos), end=tuple(pos),
                                       value=secs))
        elif any(a in vals for a in "XYZ") or (motion in (2, 3) and any(a in vals for a in "IJR")):
            target = list(pos)
            for i, a in enumerate("XYZ"):
                if a in vals:
                    v = vals[a] * scale
                    target[i] = (v + g92[i]) if absolute else pos[i] + v
            if motion != 0 and feed <= 0:
                raise GCodeError(f"line {ln}: feed move without F")
            if motion in (0, 1):
                prog.moves.append(Move(LINE, ln, tuple(pos), tuple(target),
                                       rapid=motion == 0, feed=feed))
            else:
                _arc(prog, ln, pos, target, vals, scale, motion == 2, feed)
            if motion != 0:
                prog.max_feed = max(prog.max_feed, feed)
            pos = target
        prog.moves.extend(after)
        if any(m.kind == END for m in after):
            break

    if spindle:
        prog.moves.append(Move(SPINDLE, prog.line_count, value=False))
    return prog


def _arc(prog: Program, ln: int, pos: list[float], target: list[float],
         vals: dict[str, float], scale: float, cw: bool, feed: float) -> None:
    x0, y0, z0 = pos
    x1, y1, z1 = target
    if "R" in vals:
        r = vals["R"] * scale
        dx, dy = x1 - x0, y1 - y0
        d = math.hypot(dx, dy)
        if d == 0:
            raise GCodeError(f"line {ln}: R arc with same start and end")
        if d > 2 * abs(r) + 1e-6:
            raise GCodeError(f"line {ln}: arc radius too small for end point")
        h = math.sqrt(max(r * r - d * d / 4, 0.0))
        # centre on the left (CCW, r>0) or right side of the chord
        side = -1 if cw else 1
        if r < 0:
            side = -side
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        cx = mx - side * h * dy / d
        cy = my + side * h * dx / d
    else:
        cx = x0 + vals.get("I", 0.0) * scale
        cy = y0 + vals.get("J", 0.0) * scale
    r0 = math.hypot(x0 - cx, y0 - cy)
    r1 = math.hypot(x1 - cx, y1 - cy)
    if r0 < 1e-9:
        raise GCodeError(f"line {ln}: arc with zero radius")
    if abs(r0 - r1) > max(0.01, 0.001 * r0):
        prog.warnings.append(f"line {ln}: arc end not on circle (r {r0:.4f} vs {r1:.4f})")
    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    sweep = a1 - a0
    if cw:
        if sweep >= -1e-12:
            sweep -= 2 * math.pi
    elif sweep <= 1e-12:
        sweep += 2 * math.pi
    # chord error e = r(1-cos(θ/2))  ->  θ = 2 acos(1 - e/r)
    tol = min(ARC_TOLERANCE_MM, r0 * 0.5)
    step = 2 * math.acos(1 - tol / r0)
    n = max(1, math.ceil(abs(sweep) / step))
    prev = (x0, y0, z0)
    for k in range(1, n + 1):
        if k == n:
            p = (x1, y1, z1)
        else:
            a = a0 + sweep * k / n
            p = (cx + r0 * math.cos(a), cy + r0 * math.sin(a), z0 + (z1 - z0) * k / n)
        prog.moves.append(Move(LINE, ln, prev, p, rapid=False, feed=feed))
        prev = p


def load_file(path: str, start: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> Program:
    with open(path, encoding="utf-8", errors="replace") as f:
        return parse(f.read(), start)
