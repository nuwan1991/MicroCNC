"""Excellon drill file reader (KiCad, EasyEDA, Altium, Eagle style).

Supports METRIC/INCH with LZ/TZ and explicit formats, decimal coordinates,
tool tables (T1C0.8), modal X/Y, G85 slots and routed slots
(G00 … M15 G01 … M16/M17).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


class ExcellonError(ValueError):
    pass


@dataclass
class Hole:
    x: float
    y: float
    d: float           # diameter mm
    tool: int = 0


@dataclass
class Slot:
    x1: float
    y1: float
    x2: float
    y2: float
    d: float
    tool: int = 0


@dataclass
class DrillFile:
    name: str = ""
    plated: bool | None = None       # from the X2 comment, if any
    tools: dict = field(default_factory=dict)    # tool number -> diameter mm
    holes: list = field(default_factory=list)
    slots: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def sizes(self) -> dict[float, int]:
        """diameter -> number of holes (+ slots)"""
        out: dict[float, int] = {}
        for h in self.holes:
            out[round(h.d, 3)] = out.get(round(h.d, 3), 0) + 1
        for s in self.slots:
            out[round(s.d, 3)] = out.get(round(s.d, 3), 0) + 1
        return dict(sorted(out.items()))

    @property
    def bounds(self):
        pts = [(h.x, h.y) for h in self.holes] + \
              [p for s in self.slots for p in ((s.x1, s.y1), (s.x2, s.y2))]
        if not pts:
            return None
        xs, ys = zip(*pts)
        return min(xs), min(ys), max(xs), max(ys)


_COORD = re.compile(r"([XYIJ])([+-]?[\d.]+)")


def parse_excellon(text: str, name: str = "") -> DrillFile:
    df = DrillFile(name=name)
    scale = 1.0          # mm per unit
    zeros = "LZ"         # which zeros are KEPT
    int_d, dec_d = 3, 3  # metric default 3.3
    fmt_set = False
    tool = 0
    x = y = 0.0
    header = False
    routing = False      # M15 .. M16
    route_from = None
    rapid = False

    # Decimal files (KiCad "FORMAT={…/ decimal}", most CAD) may still write
    # whole numbers without a point (X20Y-10): read every number as written.
    decimal = bool(re.search(r"^[^;]*[XY][+-]?\d*\.\d", text, re.M)) or "decimal" in text[:2000].lower()

    def num(s: str, axis: str) -> float:
        if "." in s or decimal:
            return float(s) * scale
        neg = s.startswith("-")
        digits = s.lstrip("+-")
        if zeros == "LZ":            # leading kept -> trailing omitted
            digits = digits.ljust(int_d + dec_d, "0")
        v = int(digits) / 10 ** dec_d
        return (-v if neg else v) * scale

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(";"):
            low = line.lower()
            if "filefunction" in low:
                if "nonplated" in low:
                    df.plated = False
                elif "plated" in low:
                    df.plated = True
            continue
        if line == "M48":
            header = True
            continue
        if header and line in ("%", "M95"):
            header = False
            continue
        up = line.upper()
        m = re.match(r"^(METRIC|INCH)(?:,(LZ|TZ))?(?:,(0+)\.(0+))?", up)
        if m:
            scale = 1.0 if m.group(1) == "METRIC" else 25.4
            if m.group(2):
                zeros = m.group(2)
            if m.group(3):
                int_d, dec_d = len(m.group(3)), len(m.group(4))
                fmt_set = True
            elif not fmt_set:
                int_d, dec_d = (3, 3) if m.group(1) == "METRIC" else (2, 4)
            continue
        if up in ("M71",):
            scale = 1.0
            continue
        if up in ("M72",):
            scale = 25.4
            continue
        m = re.match(r"^T(\d+)((?:[CFSBHZ][\d.]+)*)$", up)
        if m:
            t = int(m.group(1))
            c = re.search(r"C([\d.]+)", m.group(2) or "")
            if c:
                df.tools[t] = float(c.group(1)) * scale
            if not header or not c:
                tool = t
            continue
        if header:
            continue          # FMAT, ICI, VER, … not needed
        if up.startswith(("M30", "M00")):
            break
        if up.startswith("M15"):
            routing = True
            route_from = (x, y)
            continue
        if up.startswith(("M16", "M17")):
            routing = False
            route_from = None
            continue
        if up.startswith(("G05", "G90", "G91", "G93", "M47", "M97", "M98", "%", "G40", "G41",
                          "G42", "FMAT", "ICI")):
            continue
        if up.startswith("G00"):
            rapid = True
            up = up[3:]
        elif up.startswith("G01"):
            rapid = False
            up = up[3:]
        if "G85" in up:   # slot: X1Y1G85X2Y2
            a, b = up.split("G85", 1)
            ca = dict(_COORD.findall(a))
            cb = dict(_COORD.findall(b))
            x1 = num(ca["X"], "X") if "X" in ca else x
            y1 = num(ca["Y"], "Y") if "Y" in ca else y
            x2 = num(cb["X"], "X") if "X" in cb else x1
            y2 = num(cb["Y"], "Y") if "Y" in cb else y1
            df.slots.append(Slot(x1, y1, x2, y2, df.tools.get(tool, 0.0), tool))
            x, y = x2, y2
            continue
        coords = dict(_COORD.findall(up))
        if "X" in coords or "Y" in coords:
            nx = num(coords["X"], "X") if "X" in coords else x
            ny = num(coords["Y"], "Y") if "Y" in coords else y
            d = df.tools.get(tool)
            if d is None:
                df.warnings.append(f"hole with unknown tool T{tool}")
                d = 0.0
            if routing and not rapid:
                df.slots.append(Slot(route_from[0], route_from[1], nx, ny, d, tool))
                route_from = (nx, ny)
            elif not routing and not rapid:
                df.holes.append(Hole(nx, ny, d, tool))
            x, y = nx, ny
            if routing:
                route_from = (nx, ny)
            rapid = False if not routing else rapid
            continue
        df.warnings.append(f"ignored: {line[:30]}")
    return df


def is_excellon(text: str) -> bool:
    head = text[:3000]
    return "M48" in head and ("T" in head) and ("METRIC" in head or "INCH" in head
                                                or re.search(r"^T\d+C", head, re.M))
