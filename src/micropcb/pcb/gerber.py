"""Gerber RS-274X reader -> shapely geometry (mm).

Supports what KiCad and EasyEDA write: FS/MO, apertures C/R/O/P and
aperture macros (primitives 1, 4, 5, 7, 20, 21 + variables/expressions),
D01/D02/D03, linear and circular interpolation (G01/G02/G03, G74/G75),
regions (G36/G37), polarity (LPD/LPC), step & repeat (SR) and X2
attributes (TF.FileFunction).

Result: `GerberLayer.geometry` = dark area as (Multi)Polygon, and
`GerberLayer.paths` = centre lines of all draws (used for board outlines).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import shapely
from shapely import affinity
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

ARC_TOL = 0.005      # mm chord error
CIRCLE_RES = 16      # quad segments for round shapes (64 per circle)


class GerberError(ValueError):
    pass


@dataclass
class GerberLayer:
    name: str = ""
    file_function: str = ""          # X2 TF.FileFunction, e.g. "Copper,L1,Top"
    geometry: object = None          # shapely geometry (dark area)
    paths: list = field(default_factory=list)   # [[(x, y), ...], ...] draw centre lines
    flashes: int = 0
    draws: int = 0
    regions: int = 0
    warnings: list = field(default_factory=list)
    tracks: list = field(default_factory=list)   # [(width, x, y)] dark round-aperture draws
    pads: list = field(default_factory=list)     # [(x, y, size)] dark flashes, size = smaller side

    @property
    def bounds(self):
        if self.geometry is not None and not self.geometry.is_empty:
            return self.geometry.bounds
        pts = [p for path in self.paths for p in path]
        if not pts:
            return None
        xs, ys = zip(*pts)
        return min(xs), min(ys), max(xs), max(ys)


# ------------------------------------------------------------------ apertures
def _rot(g, deg):
    return affinity.rotate(g, deg, origin=(0, 0)) if deg else g


def _circle(d, x=0.0, y=0.0):
    return Point(x, y).buffer(d / 2, quad_segs=CIRCLE_RES)


def _regular_polygon(od, n, rot_deg):
    r = od / 2
    pts = [(r * math.cos(math.radians(rot_deg + 360 * k / n)),
            r * math.sin(math.radians(rot_deg + 360 * k / n))) for k in range(int(n))]
    return Polygon(pts)


def _with_hole(g, hole):
    return g.difference(_circle(hole)) if hole else g


def standard_aperture(kind: str, p: list[float]):
    if kind == "C":
        return _with_hole(_circle(p[0]), p[1] if len(p) > 1 else 0)
    if kind == "R":
        w, h = p[0], p[1]
        return _with_hole(box(-w / 2, -h / 2, w / 2, h / 2), p[2] if len(p) > 2 else 0)
    if kind == "O":
        w, h = p[0], p[1]
        r = min(w, h) / 2
        if w >= h:
            core = LineString([(-w / 2 + r, 0), (w / 2 - r, 0)])
        else:
            core = LineString([(0, -h / 2 + r), (0, h / 2 - r)])
        g = core.buffer(r, quad_segs=CIRCLE_RES) if core.length > 0 else _circle(2 * r)
        return _with_hole(g, p[2] if len(p) > 2 else 0)
    if kind == "P":
        g = _regular_polygon(p[0], p[1], p[2] if len(p) > 2 else 0)
        return _with_hole(g, p[3] if len(p) > 3 else 0)
    raise GerberError(f"unknown aperture type {kind}")


_EXPR_OK = re.compile(r"^[0-9.+\-*/() ]*$")


def _eval(expr: str, vars_: dict[int, float]) -> float:
    e = expr.strip().replace("x", "*").replace("X", "*")
    e = re.sub(r"\$(\d+)", lambda m: repr(vars_.get(int(m.group(1)), 0.0)), e)
    if not _EXPR_OK.match(e):
        raise GerberError(f"bad macro expression {expr!r}")
    if not e.strip():
        return 0.0
    return float(eval(e, {"__builtins__": {}}, {}))  # noqa: S307 - whitelisted characters


def macro_aperture(body: list[str], params: list[float]):
    vars_ = {i + 1: v for i, v in enumerate(params)}
    shapes: list[tuple[bool, object]] = []   # (dark, geometry) in drawing order

    def put(dark, g):
        shapes.append((dark, g))

    for part in body:
        part = part.strip()
        if not part or part.startswith("0"):   # primitive 0 = comment
            continue
        m = re.match(r"^\$(\d+)\s*=(.*)$", part)
        if m:
            vars_[int(m.group(1))] = _eval(m.group(2), vars_)
            continue
        f = [x for x in part.split(",")]
        code = int(_eval(f[0], vars_))
        a = [_eval(x, vars_) for x in f[1:]]
        if code == 1:          # circle: exp, diam, cx, cy[, rot]
            g = _circle(a[1], a[2], a[3])
            put(a[0] >= 0.5, _rot(g, a[4] if len(a) > 4 else 0))
        elif code in (20, 2):  # vector line: exp, width, sx, sy, ex, ey, rot
            line = LineString([(a[2], a[3]), (a[4], a[5])])
            g = line.buffer(a[1] / 2, cap_style="flat") if line.length else Polygon()
            put(a[0] >= 0.5, _rot(g, a[6] if len(a) > 6 else 0))
        elif code == 21:       # centre line: exp, w, h, cx, cy, rot
            w, h, cx, cy = a[1], a[2], a[3], a[4]
            g = box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
            put(a[0] >= 0.5, _rot(g, a[5] if len(a) > 5 else 0))
        elif code == 4:        # outline: exp, n, x0, y0, ... , rot
            n = int(a[1])
            pts = [(a[2 + 2 * k], a[3 + 2 * k]) for k in range(n + 1)]
            g = Polygon(pts).buffer(0)
            put(a[0] >= 0.5, _rot(g, a[2 + 2 * (n + 1)] if len(a) > 2 + 2 * (n + 1) else 0))
        elif code == 5:        # polygon: exp, n, cx, cy, diam, rot
            g = affinity.translate(_regular_polygon(a[4], a[1], 0), a[2], a[3])
            put(a[0] >= 0.5, _rot(g, a[5] if len(a) > 5 else 0))
        elif code == 7:        # thermal: cx, cy, od, id, gap, rot
            cx, cy, od, idd, gap = a[:5]
            ring = _circle(od).difference(_circle(idd))
            cut = unary_union([box(-od, -gap / 2, od, gap / 2), box(-gap / 2, -od, gap / 2, od)])
            g = affinity.translate(ring.difference(cut), cx, cy)
            put(True, _rot(g, a[5] if len(a) > 5 else 0))
        else:
            raise GerberError(f"unsupported macro primitive {code}")
    out = Polygon()
    for dark, g in shapes:
        out = out.union(g) if dark else out.difference(g)
    return out


# ------------------------------------------------------------------ parser
_TOKENS = re.compile(r"%([^%]*)%|([^%*]*)\*", re.S)
_WORDS = re.compile(r"([GXYIJDM])([+-]?\d+(?:\.\d*)?)")


class _Parser:
    def __init__(self, name: str) -> None:
        self.L = GerberLayer(name=name)
        self.int_x = self.dec_x = self.int_y = self.dec_y = None
        self.trailing = False
        self.scale = 1.0            # 25.4 for inches
        self.apertures: dict[int, object] = {}
        self.ap_size: dict[int, float] = {}     # circle diameter (for draws)
        self.ap_kind: dict[int, str] = {}
        self.macros: dict[str, list[str]] = {}
        self.cur = None
        self.x = self.y = 0.0
        self.interp = 1             # 1 linear, 2 cw, 3 ccw
        self.multi = True           # G75
        self.region = False
        self.contour: list[tuple[float, float]] = []
        self.contours: list[list[tuple[float, float]]] = []
        self.dark = True
        self.groups: list[tuple[bool, list]] = [(True, [])]
        self.path: list[tuple[float, float]] | None = None
        self.sr = None              # (nx, ny, dx, dy, start index in groups)

    # ---- helpers
    def add(self, g) -> None:
        if g is None or g.is_empty:
            return
        if self.groups[-1][0] != self.dark:
            self.groups.append((self.dark, []))
        self.groups[-1][1].append(g)

    def coord(self, s: str, axis: str) -> float:
        if "." in s:
            return float(s) * self.scale
        dec = self.dec_x if axis in "XI" else self.dec_y
        integ = self.int_x if axis in "XI" else self.int_y
        if dec is None:
            raise GerberError("coordinate before %FS")
        neg = s.startswith("-")
        digits = s.lstrip("+-")
        if self.trailing:
            digits = digits.ljust(integ + dec, "0")
        v = int(digits) / 10 ** dec
        return (-v if neg else v) * self.scale

    def arc_points(self, x0, y0, x1, y1, i, j, cw):
        if self.multi:
            cx, cy = x0 + i, y0 + j
        else:  # single quadrant: pick signs giving equal radii and <= 90 deg
            best = None
            for sx in (1, -1):
                for sy in (1, -1):
                    cx, cy = x0 + sx * abs(i), y0 + sy * abs(j)
                    err = abs(math.hypot(x0 - cx, y0 - cy) - math.hypot(x1 - cx, y1 - cy))
                    a0 = math.atan2(y0 - cy, x0 - cx)
                    a1 = math.atan2(y1 - cy, x1 - cx)
                    sw = (a0 - a1) if cw else (a1 - a0)
                    sw %= 2 * math.pi
                    if sw <= math.pi / 2 + 1e-6 and (best is None or err < best[0]):
                        best = (err, cx, cy)
            _, cx, cy = best if best else (0, x0 + i, y0 + j)
        r = math.hypot(x0 - cx, y0 - cy)
        a0 = math.atan2(y0 - cy, x0 - cx)
        a1 = math.atan2(y1 - cy, x1 - cx)
        if cw:
            sweep = a1 - a0
            if sweep >= -1e-9:
                sweep -= 2 * math.pi
        else:
            sweep = a1 - a0
            if sweep <= 1e-9:
                sweep += 2 * math.pi
        if not self.multi:
            sweep = math.copysign(min(abs(sweep), math.pi / 2), sweep)
        if r < 1e-9:
            return [(x1, y1)]
        step = 2 * math.acos(max(-1.0, 1 - ARC_TOL / r)) if r > ARC_TOL else math.pi / 4
        n = max(2, math.ceil(abs(sweep) / step))
        pts = [(cx + r * math.cos(a0 + sweep * k / n), cy + r * math.sin(a0 + sweep * k / n))
               for k in range(1, n)]
        return pts + [(x1, y1)]

    def end_path(self) -> None:
        if self.path and len(self.path) > 1:
            self.L.paths.append(self.path)
        self.path = None

    def draw(self, pts: list[tuple[float, float]]) -> None:
        """Stroke from current point through pts with the current aperture."""
        self.L.draws += 1
        line = LineString([(self.x, self.y)] + pts)
        kind = self.ap_kind.get(self.cur, "C")
        if self.cur in self.ap_size and kind == "C":
            r = self.ap_size[self.cur] / 2
            g = line.buffer(r, quad_segs=8) if r > 0 else None
        else:
            # non-circular aperture: sweep = hull of the shape at each vertex
            ap = self.apertures.get(self.cur)
            if ap is None:
                raise GerberError(f"draw with undefined aperture D{self.cur}")
            coords = list(line.coords)
            g = unary_union([shapely.convex_hull(unary_union([
                affinity.translate(ap, *coords[k]), affinity.translate(ap, *coords[k + 1])]))
                for k in range(len(coords) - 1)])
        if g is not None:
            self.add(g)
            if self.dark and kind == "C" and self.cur in self.ap_size:
                c = line.interpolate(0.5, normalized=True)
                self.L.tracks.append((self.ap_size[self.cur], c.x, c.y))
        if self.path is None:
            self.path = [(self.x, self.y)]
        self.path.extend(pts)

    # ---- commands
    def extended(self, block: str) -> None:
        parts = [p for p in block.split("*")]
        head = parts[0].strip()
        if head.startswith("FS"):
            m = re.match(r"FS([LT])?([AI])?X(\d)(\d)Y(\d)(\d)", head)
            if not m:
                raise GerberError(f"bad format {head}")
            self.trailing = m.group(1) == "T"
            self.int_x, self.dec_x, self.int_y, self.dec_y = map(int, m.group(3, 4, 5, 6))
        elif head.startswith("MO"):
            self.scale = 25.4 if head[2:4] == "IN" else 1.0
        elif head.startswith("AD"):
            m = re.match(r"ADD(\d+)([A-Za-z_.$][\w.$]*|[CROP])(?:,(.*))?$", head)
            if not m:
                raise GerberError(f"bad aperture {head}")
            num, kind, args = int(m.group(1)), m.group(2), m.group(3)
            p = [float(v) for v in (args.split("X") if args else [])]
            if kind in ("C", "R", "O", "P"):
                pp = list(p)
                for k in range(len(pp)):
                    if not (kind == "P" and k in (1, 2)):
                        pp[k] *= self.scale
                self.apertures[num] = standard_aperture(kind, pp)
                if kind == "C":
                    self.ap_size[num] = pp[0]
            else:
                if kind not in self.macros:
                    raise GerberError(f"unknown macro {kind}")
                g = macro_aperture(self.macros[kind], p)
                if self.scale != 1:
                    g = affinity.scale(g, self.scale, self.scale, origin=(0, 0))
                self.apertures[num] = g
            self.ap_kind[num] = kind
        elif head.startswith("AM"):
            self.macros[head[2:]] = [p for p in parts[1:] if p.strip()]
        elif head.startswith("LP"):
            self.dark = head[2] == "D"
        elif head.startswith("SR"):
            self._step_repeat(head)
        elif head.startswith("TF"):
            if head.startswith("TF.FileFunction,"):
                self.L.file_function = head[len("TF.FileFunction,"):]
        elif head[:2] in ("TA", "TO", "TD", "IP", "IN", "LN", "OF", "SF", "MI", "AS", "IR",
                          "LM", "LR", "LS"):
            pass
        else:
            self.L.warnings.append(f"ignored %{head[:12]}")
        # some writers put several commands in one %..% block
        for extra in parts[1:]:
            e = extra.strip()
            if e and not head.startswith("AM") and e[:2] in ("FS", "MO", "LP", "AD"):
                self.extended(e)

    def _step_repeat(self, head: str) -> None:
        if self.sr is not None:   # close the open block
            nx, ny, dx, dy, start = self.sr
            block = self.groups[start:]
            del self.groups[start:]
            for ix in range(nx):
                for iy in range(ny):
                    for dark, geoms in block:
                        self.groups.append((dark, [affinity.translate(g, ix * dx, iy * dy)
                                                   for g in geoms]))
            self.sr = None
        m = re.match(r"SRX(\d+)Y(\d+)I([\d.]+)J([\d.]+)", head)
        if m and (int(m.group(1)) > 1 or int(m.group(2)) > 1):
            self.groups.append((self.dark, []))
            self.sr = (int(m.group(1)), int(m.group(2)), float(m.group(3)) * self.scale,
                       float(m.group(4)) * self.scale, len(self.groups) - 1)

    def data(self, block: str) -> None:
        b = block.strip()
        if not b:
            return
        if b.startswith("G04") or b.startswith("G4 "):
            # KiCad writes X2 attributes as comments: G04 #@! TF.FileFunction,...
            m = re.search(r"#@!\s*TF\.FileFunction,(.*)$", b)
            if m:
                self.L.file_function = m.group(1).strip()
            return
        if b in ("M02", "M00", "M01", "M2"):
            return
        words = _WORDS.findall(b)
        nx, ny = self.x, self.y
        i = j = 0.0
        dcode = None
        for w, v in words:
            if w == "G":
                g = int(float(v))
                if g in (1, 2, 3):
                    self.interp = g
                elif g == 36:
                    self.region = True
                    self.contour = []
                    self.contours = []
                elif g == 37:
                    self._close_contour()
                    self._finish_region()
                    self.region = False
                elif g == 74:
                    self.multi = False
                elif g == 75:
                    self.multi = True
                elif g in (54, 55, 70, 71, 90, 91):
                    if g == 70:
                        self.scale = 25.4
                    elif g == 71:
                        self.scale = 1.0
            elif w == "X":
                nx = self.coord(v, "X")
            elif w == "Y":
                ny = self.coord(v, "Y")
            elif w == "I":
                i = self.coord(v, "I")
            elif w == "J":
                j = self.coord(v, "J")
            elif w == "D":
                d = int(v)
                if d >= 10:
                    self.end_path()
                    self.cur = d
                else:
                    dcode = d
        if dcode is None and any(w in "XY" for w, _ in words):
            dcode = 1  # deprecated modal D01
        if dcode == 1:
            pts = ([(nx, ny)] if self.interp == 1 else
                   self.arc_points(self.x, self.y, nx, ny, i, j, self.interp == 2))
            if self.region:
                if not self.contour:
                    self.contour = [(self.x, self.y)]
                self.contour.extend(pts)
            else:
                self.draw(pts)
        elif dcode == 2:
            if self.region:
                self._close_contour()
            self.end_path()
        elif dcode == 3:
            self.end_path()
            ap = self.apertures.get(self.cur)
            if ap is None:
                raise GerberError(f"flash with undefined aperture D{self.cur}")
            self.add(affinity.translate(ap, nx, ny))
            self.L.flashes += 1
            if self.dark and not ap.is_empty:
                x0, y0, x1, y1 = ap.bounds
                self.L.pads.append((nx, ny, min(x1 - x0, y1 - y0)))
        self.x, self.y = nx, ny

    def _close_contour(self) -> None:
        if len(self.contour) >= 3:
            self.contours.append(self.contour)
        self.contour = []

    def _finish_region(self) -> None:
        for c in self.contours:
            poly = shapely.make_valid(Polygon(c))
            self.add(poly)
            self.L.regions += 1
        self.contours = []

    def finish(self) -> GerberLayer:
        self.end_path()
        if self.sr is not None:
            self._step_repeat("SR")
        result = Polygon()
        for dark, geoms in self.groups:
            if not geoms:
                continue
            u = unary_union(geoms)
            result = result.union(u) if dark else result.difference(u)
        self.L.geometry = result
        return self.L


def parse_gerber(text: str, name: str = "") -> GerberLayer:
    p = _Parser(name)
    for m in _TOKENS.finditer(text):
        if m.group(1) is not None:
            p.extended(m.group(1))
        else:
            p.data(m.group(2))
    return p.finish()


def is_gerber(text: str) -> bool:
    head = text[:4000]
    return "%FS" in head or ("%MO" in head and "%AD" in text[:20000])
