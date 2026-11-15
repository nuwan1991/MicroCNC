"""PCB CAM (Stage 3.2–3.5): placed board -> G-code text.

All geometry is in work coordinates (mm) from `PlacedBoard`; Z 0 = copper
surface. Every operation returns an `OpResult` with G-code body lines, the
paths (for preview / tests) and notes. `combine()` joins operations into one
job with tool-change pauses (M6 + message) between different tools.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import shapely
from shapely.geometry import LineString, LinearRing, MultiLineString, Point, Polygon
from shapely.ops import unary_union

from .board import PlacedBoard, as_polygons
from .excellon import Hole
from .tools import DRILL, ENDMILL, VBIT, Tool

RES = 16  # buffer quad segments


@dataclass
class OpResult:
    name: str
    tool: Tool
    lines: list[str] = field(default_factory=list)       # G-code body (no header)
    paths: list[list[tuple[float, float]]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    problem_area: object = None                           # e.g. not-cleared copper gaps
    path_depths: list = field(default_factory=list)       # isolation: depth of each path

    @property
    def empty(self) -> bool:
        return not self.lines


class _W:
    """Small G-code writer that tracks the current position."""

    def __init__(self, safe_z: float) -> None:
        self.safe = safe_z
        self.out: list[str] = []
        self.z = None

    def lift(self) -> None:
        if self.z is None or self.z < self.safe - 1e-9:
            self.out.append(f"G0 Z{self.safe:.3f}")
            self.z = self.safe

    def rapid_xy(self, x: float, y: float) -> None:
        self.lift()
        self.out.append(f"G0 X{x:.4f} Y{y:.4f}")

    def plunge(self, z: float, feed: float) -> None:
        if self.z is not None and self.z > 0.2:
            self.out.append("G0 Z0.200")   # fast to just above the copper
        self.out.append(f"G1 Z{z:.4f} F{feed:g}")
        self.z = z

    def cut(self, pts, feed: float, zs=None) -> None:
        first = True
        for k, (x, y) in enumerate(pts):
            z = zs[k] if zs is not None else None
            zpart = f" Z{z:.4f}" if z is not None and z != self.z else ""
            if z is not None:
                self.z = z
            self.out.append(f"G1 X{x:.4f} Y{y:.4f}{zpart}" + (f" F{feed:g}" if first else ""))
            first = False


def _rings(geom) -> list[LinearRing]:
    out = []
    for p in as_polygons(geom):
        out.append(p.exterior)
        out.extend(p.interiors)
    return out


def _lines(geom) -> list[list[tuple[float, float]]]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, (LineString, LinearRing)):
        return [list(geom.coords)]
    out = []
    for g in getattr(geom, "geoms", []):
        out.extend(_lines(g))
    return out


def _orient(ring: LinearRing, outer: bool, climb: bool) -> list[tuple[float, float]]:
    """Climb milling with a CW spindle: outside contours CW, inside CCW."""
    pts = list(ring.coords)
    want_ccw = (not climb) if outer else climb
    if LinearRing(pts).is_ccw != want_ccw:
        pts.reverse()
    return pts


def _order(paths: list[list[tuple[float, float]]], start=(0.0, 0.0)):
    """Nearest-neighbour order; closed paths may start at their nearest vertex."""
    left = list(paths)
    out = []
    cur = start
    while left:
        best_i, best_d, best_k, best_rev = 0, float("inf"), 0, False
        for i, p in enumerate(left):
            closed = len(p) > 2 and p[0] == p[-1]
            cands = range(len(p) - 1) if closed else (0, len(p) - 1)
            step = max(1, (len(p) - 1) // 64) if closed else 1
            for k in (cands if not closed else range(0, len(p) - 1, step)):
                d = (p[k][0] - cur[0]) ** 2 + (p[k][1] - cur[1]) ** 2
                if d < best_d:
                    best_i, best_d, best_k, best_rev = i, d, k, (not closed and k != 0)
        p = left.pop(best_i)
        if len(p) > 2 and p[0] == p[-1] and best_k:
            p = p[best_k:-1] + p[:best_k] + [p[best_k]]
        elif best_rev:
            p = p[::-1]
        out.append(p)
        cur = p[-1]
    return out


# ------------------------------------------------------------------ isolation (3.2)
def not_isolated(copper, r: float):
    """Where two separate copper areas are closer than 2r: the tool cannot pass
    between them, so they would stay connected."""
    islands = as_polygons(copper)
    if len(islands) < 2:
        return Polygon()
    bufs = [i.buffer(r, quad_segs=8) for i in islands]
    tree = shapely.STRtree(bufs)
    a, b = tree.query(bufs, predicate="intersects")
    spots = [bufs[i].intersection(bufs[j]) for i, j in zip(a, b) if i < j]
    return unary_union([g for g in spots if not g.is_empty]) if spots else Polygon()



def problem_spots(geom) -> list[tuple[float, float, float]]:
    """Problem area -> [(x, y, size mm)], one per separate spot, left-to-right / front-to-back."""
    out = []
    for p in as_polygons(geom) if geom is not None else []:
        c = p.centroid
        x0, y0, x1, y1 = p.bounds
        out.append((c.x, c.y, max(x1 - x0, y1 - y0)))
    out.sort(key=lambda s: (round(s[1], 1), s[0]))
    return out


def copper_gaps(copper, look_mm: float = 1.0) -> list[float]:
    """Smallest distance from each copper island to its nearest other island (mm),
    only islands closer than look_mm. The design's isolation gaps, smallest first."""
    from .drc import gap_spots
    return [g[0] for g in gap_spots(copper, look_mm)]


@dataclass
class IsoAdvice:
    tool: Tool
    depth: float            # mm, positive
    width: float            # cut width at that depth
    narrow: int             # islands that still cannot be separated
    text: str


def recommend_isolation(copper, tools: list[Tool], min_depth: float = 0.04,
                        max_depth: float = 0.08, margin: float = 0.9) -> tuple[list[IsoAdvice], str]:
    """Best V-bit / end mill + depth for this copper.

    Aim: the cut width is at most `margin` x the smallest design gap, so every
    track is separated; depth at least `min_depth` (through the copper) and at
    most `max_depth`. Sorted best first: fewest unseparated places, then the
    deepest (most reliable) cut."""
    gaps = copper_gaps(copper)
    if not gaps:
        return [], "No neighbouring copper found - any isolation tool works."
    g_min = gaps[0]
    g5 = gaps[max(0, len(gaps) // 20)]
    target = margin * g_min
    out = []
    for t in tools:
        if t.kind == VBIT:
            tan = math.tan(math.radians(t.angle / 2)) or 1e-9
            depth = (target - t.tip) / (2 * tan)
            depth = min(max(depth, min_depth), max_depth)
        elif t.kind == ENDMILL:
            depth = min_depth
        else:
            continue
        w = t.cut_width(depth)
        narrow = sum(1 for g in gaps if g < w)
        out.append(IsoAdvice(t, round(depth, 3), w, narrow, ""))
    out.sort(key=lambda a: (a.narrow, -a.depth if a.narrow == 0 else a.width))
    for a in out:
        a.text = (f"{a.tool.label}: depth {a.depth:.3f} mm, cut width {a.width:.3f} mm - "
                  + ("separates all tracks" if a.narrow == 0 else
                     f"{a.narrow} island(s) still too close"))
    summary = (f"Smallest gap between copper: {g_min:.3f} mm (5 % of gaps are below "
               f"{g5:.3f} mm, {len(gaps)} islands measured).")
    return out, summary


def _iso_paths(copper, w: float, extra: float, passes: int, overlap: float, climb: bool,
               region) -> list[list[tuple[float, float]]]:
    """Contours around the copper for a groove of width w (inner groove edge on the copper)."""
    paths = []
    for k in range(max(1, passes)):
        off = w / 2 + extra + k * w * (1 - overlap)
        shape = copper.buffer(off, quad_segs=RES).simplify(0.002)
        for poly in as_polygons(shape):
            rings = [(poly.exterior, True)] + [(i, False) for i in poly.interiors]
            for ring, outer in rings:
                pts = _orient(ring, outer, climb)
                if region is not None:
                    clipped = LineString(pts).intersection(region)
                    if clipped.is_empty:
                        continue
                    if not (clipped.geom_type == "LineString" and
                            clipped.coords[0] == clipped.coords[-1]):
                        paths.extend(_lines(clipped))
                        continue
                paths.append(pts)
    return [p for p in paths if len(p) > 1]


def _clip(paths, area, inside: bool):
    """Parts of the paths inside (or outside) area."""
    out = []
    for p in paths:
        ls = LineString(p)
        g = ls.intersection(area) if inside else ls.difference(area)
        if g.is_empty:
            continue
        if g.equals(ls):
            out.append(p)
        else:
            out.extend(q for q in _lines(g) if len(q) > 1 and LineString(q).length > 0.02)
    return out


def depth_levels(tool: Tool, deep: float, shallow: float, step: float = 0.01,
                 max_levels: int = 4) -> list[float]:
    """Depths (positive mm, shallow -> deep) for variable-depth isolation."""
    deep, shallow = abs(deep), min(abs(shallow), abs(deep))
    if tool.kind != VBIT or deep - shallow < step / 2:
        return [deep]
    n = min(max_levels, int(round((deep - shallow) / step)) + 1)
    return [round(shallow + (deep - shallow) * k / (n - 1), 4) for k in range(n)]


def isolation(board: PlacedBoard, side: str, tool: Tool, depth: float = 0.08,
              passes: int = 1, overlap: float = 0.3, extra: float = 0.0,
              climb: bool = True, safe_z: float = 2.0, clip_to_outline: bool = True,
              min_depth: float | None = None) -> OpResult:
    """Isolation contours. min_depth (V-bit only) = variable depth by gap: cut at
    `depth` where the gap allows and only as shallow as min_depth in narrow gaps
    (narrower groove). The groove's copper-side edge is on the copper at every
    depth, so tracks are never thinned."""
    copper = board.copper.get(side)
    res = OpResult(f"Isolation {side}", tool)
    if copper is None or copper.is_empty:
        res.notes.append(f"no {side} copper")
        return res
    levels = depth_levels(tool, depth, min_depth) if min_depth is not None else [abs(depth)]
    widths = [tool.cut_width(d) for d in levels]
    if min(widths) <= 0:
        raise ValueError("tool cut width is 0 - check V-bit tip / depth")
    region = board.outline.buffer(max(widths)) if clip_to_outline and board.outline is not None \
        and not board.outline.is_empty else None
    if len(levels) == 1:
        res.notes.append(f"cut width {widths[0]:.3f} mm at depth {-levels[0]:.3f}")
    else:
        res.notes.append(f"variable depth {levels[0]:.3f}-{levels[-1]:.3f} mm "
                         f"(cut width {widths[0]:.3f}-{widths[-1]:.3f} mm)")
    # zone_k: where level k cannot separate the copper (+ room for its passes)
    zones = []
    for d, w in zip(levels, widths):
        bad = not_isolated(copper, w / 2 + extra)
        zones.append(bad.buffer(w * (1.5 + max(1, passes)) + 0.3, quad_segs=8)
                     if not bad.is_empty else Polygon())
    bad0 = not_isolated(copper, widths[0] / 2 + extra)
    if not bad0.is_empty:
        res.problem_area = bad0
        res.notes.append(f"⚠ {len(as_polygons(bad0))} place(s) too narrow for this tool "
                         f"(gap < {2 * (widths[0] / 2 + extra):.3f} mm) - copper may stay "
                         "connected there")
    groups: list[tuple[float, list]] = []
    n = len(levels)
    for k in range(n - 1, -1, -1):           # deepest first
        paths = _iso_paths(copper, widths[k], extra, passes, overlap, climb, region)
        if n > 1:
            if k == n - 1:
                use = None if zones[k].is_empty else (zones[k], False)
            else:
                if zones[k + 1].is_empty:
                    continue
                area = zones[k + 1].buffer(0.3)  # a little overlap at the change
                if k > 0 and not zones[k].is_empty:
                    area = area.difference(zones[k])
                use = (area, True)
            if use is not None:
                paths = _clip(paths, *use)
        if paths:
            groups.append((levels[k], paths))
    if n > 1:
        total = sum(LineString(p).length for _, ps in groups for p in ps) or 1
        res.notes.append("depths: " + ", ".join(
            f"{d:.3f} mm {100 * sum(LineString(p).length for p in ps) / total:.0f} %"
            for d, ps in groups))
    W = _W(safe_z)
    res.paths, res.path_depths = [], []
    for d, paths in groups:
        for p in _order(paths):
            W.rapid_xy(*p[0])
            W.plunge(-d, tool.plunge)
            W.cut(p[1:], tool.feed)
            res.paths.append(p)
            res.path_depths.append(d)
    W.lift()
    res.lines = W.out
    return res


# ------------------------------------------------------------------ copper clearing (3.4)
def _fill_rings(region, d: float, overlap: float, limit=None):
    """Contour-parallel rings filling `region` with a tool of diameter d."""
    paths = []
    k = 0
    step = d * (1 - overlap)
    while True:
        shape = region.buffer(-(d / 2 + k * step), quad_segs=RES)
        if shape.is_empty or k > 500:
            break
        for ring in _rings(shape.simplify(0.002)):
            line = LineString(ring.coords)
            if limit is not None:
                line = line.intersection(limit)
            paths.extend(p for p in _lines(line) if len(p) > 1)
        k += 1
    return paths


def clearing(board: PlacedBoard, side: str, tool: Tool, depth: float = 0.1,
             overlap: float = 0.4, keep: float = 0.2, rest_tool: Tool | None = None,
             margin: float = 0.5, safe_z: float = 2.0) -> list[OpResult]:
    """Remove all copper except the tracks/pads (+keep mm around them).

    With rest_tool (smaller), the big tool clears what it can and the small
    tool only the corners the big one could not reach.
    """
    copper = board.copper.get(side)
    if copper is None or copper.is_empty:
        return []
    depth = -abs(depth)
    area = board.outline.buffer(-margin) if board.outline is not None else copper.envelope
    region = area.difference(copper.buffer(keep, quad_segs=RES))
    out = []
    tools = [tool] + ([rest_tool] if rest_tool is not None else [])
    reached = Polygon()
    for n, t in enumerate(tools):
        d = t.cut_width(depth)
        if n == 0:
            paths = _fill_rings(region, d, overlap)
            reached = region.buffer(-d / 2, quad_segs=RES).buffer(d / 2, quad_segs=RES)
        else:
            rest = region.difference(reached.buffer(0.01))
            rest = unary_union([p for p in as_polygons(rest) if p.area > (d * d) / 20])
            if rest.is_empty:
                continue
            paths = _fill_rings(region, d, overlap, limit=rest.buffer(d))
        r = OpResult(f"Clearing {side}" + (" (rest)" if n else ""), t)
        W = _W(safe_z)
        for p in _order(paths):
            W.rapid_xy(*p[0])
            W.plunge(depth, t.plunge)
            W.cut(p[1:], t.feed)
        W.lift()
        r.lines, r.paths = W.out, paths
        length = sum(LineString(p).length for p in paths)
        r.notes.append(f"{len(paths)} passes, {length:.0f} mm of cutting "
                       f"(≈{length / max(min(t.feed, 170), 1):.0f} min)")
        out.append(r)
    return out


# ------------------------------------------------------------------ drilling (3.3)
def assign_drills(holes: list[Hole], bits: list[float], tolerance: float = 0.15,
                  mill_above: float | None = None):
    """Map hole sizes to available bits. Returns ({bit: [holes]}, [holes to mill])."""
    groups: dict[float, list[Hole]] = {}
    mill: list[Hole] = []
    bits = sorted(bits)
    for h in holes:
        if mill_above is not None and h.d > mill_above + 1e-6:
            mill.append(h)
            continue
        best = min(bits, key=lambda b: (abs(b - h.d), -b)) if bits else h.d
        if bits and abs(best - h.d) > tolerance:
            if mill_above is not None and h.d > max(bits):
                mill.append(h)
                continue
        groups.setdefault(best, []).append(h)
    return dict(sorted(groups.items())), mill


def drilling(holes: list[Hole], bit: Tool, depth: float = 1.8, peck: float | None = None,
             safe_z: float = 2.0) -> OpResult:
    res = OpResult(f"Drill {bit.diameter:g} mm ({len(holes)} holes)", bit)
    depth = -abs(depth)
    peck = abs(peck) if peck else None
    W = _W(safe_z)
    pts = _order([[(h.x, h.y)] for h in holes])
    for p in pts:
        x, y = p[0]
        W.rapid_xy(x, y)
        if peck:
            z = 0.0
            W.out.append("G0 Z0.200")
            while z > depth + 1e-9:
                z = max(depth, z - peck)
                W.out.append(f"G1 Z{z:.4f} F{bit.plunge:g}")
                W.out.append("G0 Z0.200")
            W.z = 0.2
        else:
            W.plunge(depth, bit.plunge)
        W.lift()
        res.paths.append([(x, y)])
    res.lines = W.out
    return res


def mill_holes(holes: list[Hole], tool: Tool, depth: float = 1.8, safe_z: float = 2.0) -> OpResult:
    """Holes bigger than the tool: helix down on a circle, then a clean-up ring."""
    res = OpResult(f"Mill holes ({len(holes)}) with Ø{tool.diameter:g}", tool)
    depth = -abs(depth)
    W = _W(safe_z)
    for p in _order([[(h.x, h.y)] for h in holes]):
        x, y = p[0]
        h = next(hh for hh in holes if (hh.x, hh.y) == (x, y))
        r = (h.d - tool.diameter) / 2
        if r <= 0.01:
            res.notes.append(f"hole Ø{h.d:g} at X{x:.2f} Y{y:.2f} not bigger than tool - drilled")
            W.rapid_xy(x, y)
            W.plunge(depth, tool.plunge)
            W.lift()
            continue
        W.rapid_xy(x + r, y)
        W.plunge(0.0, tool.plunge)
        turns = max(1, math.ceil(abs(depth) / tool.stepdown))
        n = 24
        circle = [(x + r * math.cos(2 * math.pi * k / n), y + r * math.sin(2 * math.pi * k / n))
                  for k in range(1, n + 1)]
        for t in range(turns):
            zs = [depth * (t + k / n) / turns for k in range(1, n + 1)]
            W.cut(circle, tool.feed, zs)
        W.cut(circle, tool.feed)
        W.lift()
        res.paths.append(circle)
    res.lines = W.out
    return res


# ------------------------------------------------------------------ cutout (3.3)
def cutout(board: PlacedBoard, tool: Tool, thickness: float = 1.6, extra_depth: float = 0.1,
           tabs: int = 4, tab_width: float = 3.0, tab_height: float = 0.6,
           safe_z: float = 2.0) -> OpResult:
    res = OpResult("Board cutout", tool)
    shape = board.outline
    if shape is None or shape.is_empty:
        res.notes.append("no board outline")
        return res
    d = tool.diameter
    total = -(abs(thickness) + extra_depth)
    passes = max(1, math.ceil(abs(total) / tool.stepdown))
    contours = []
    for poly in as_polygons(shape):
        outer = Polygon(poly.exterior).buffer(d / 2, quad_segs=RES).exterior
        contours.append((outer, True))
        for hole in poly.interiors:
            inner = Polygon(hole).buffer(-d / 2, quad_segs=RES)
            for p in as_polygons(inner):
                contours.append((p.exterior, False))
    W = _W(safe_z)
    tab_top = -(abs(thickness) - tab_height)
    for ring, outer in contours:
        pts = _orient(ring, outer, climb=False)
        line = LineString(pts)
        L = line.length
        n_tabs = tabs if outer else 0
        tab_ranges = [((k + 0.5) * L / n_tabs - (tab_width + d) / 2,
                       (k + 0.5) * L / n_tabs + (tab_width + d) / 2) for k in range(n_tabs)]
        # densify so tabs start/stop near the right place
        dense = [line.interpolate(s).coords[0] for s in
                 [i * 0.5 for i in range(int(L / 0.5) + 1)] + [L]]
        W.rapid_xy(*dense[0])
        for k in range(1, passes + 1):
            z = total * k / passes
            zs = []
            dist = 0.0
            prev = dense[0]
            for q in dense:
                dist += math.dist(prev, q)
                prev = q
                in_tab = any(a <= dist <= b for a, b in tab_ranges)
                zs.append(max(z, tab_top) if in_tab else z)
            W.plunge(zs[0], tool.plunge) if k == 1 else W.cut([dense[0]], tool.feed, [zs[0]])
            W.cut(dense[1:], tool.feed, zs[1:])
        W.lift()
        res.paths.append(dense)
    if tabs:
        res.notes.append(f"{tabs} tabs, {tab_width:g} mm wide, {tab_height:g} mm high")
    res.lines = W.out
    return res


# ------------------------------------------------------------------ double-sided (3.5)
def pin_positions(board: PlacedBoard, offset: float = 5.0) -> list[tuple[float, float]]:
    """Two alignment pins on the flip axis, `offset` mm outside the board."""
    w, h = board.size
    m = board.placement.margin
    if board.placement.flip_axis == "x":     # flip left<->right about x = centre
        cx = m + w / 2
        return [(cx, m - offset), (cx, m + h + offset)]
    cy = m + h / 2
    return [(m - offset, cy), (m + w + offset, cy)]


def pins(board: PlacedBoard, tool: Tool, diameter: float = 3.0, depth: float = 4.0,
         offset: float = 5.0, safe_z: float = 2.0,
         area: tuple[float, float] | None = None) -> OpResult:
    holes = [Hole(x, y, diameter) for x, y in pin_positions(board, offset)]
    if tool.kind == DRILL or abs(tool.diameter - diameter) < 0.05:
        r = drilling(holes, tool, depth, peck=tool.stepdown, safe_z=safe_z)
    else:
        r = mill_holes(holes, tool, depth, safe_z)
    r.name = f"Alignment pins Ø{diameter:g}, depth {depth:g}"
    for x, y in pin_positions(board, offset):
        if x < 0 or y < 0:
            r.notes.append("⚠ a pin is below work 0 - increase the board offset to at least "
                           f"{offset + diameter:.1f} mm, or the machine cannot reach it")
            break
        if area is not None and (x > area[0] or y > area[1]):
            r.notes.append(f"⚠ a pin (X{x:.1f} Y{y:.1f}) is outside the machine area "
                           f"{area[0]:g} × {area[1]:g} mm - use a smaller pin distance / offset "
                           "or flip the other way")
            break
    r.notes.append("pins at " + ", ".join(f"X{x:.2f} Y{y:.2f}" for x, y in pin_positions(board, offset)))
    return r


# ------------------------------------------------------------------ job assembly
def _safe(text: str) -> str:
    """G-code comments cannot contain brackets."""
    return text.replace("(", "[").replace(")", "]")


def combine(ops: list[OpResult], title: str, safe_z: float = 2.0,
            tool_change_z: float = 15.0) -> str:
    """One G-code job; a tool-change pause (M6) when the tool changes."""
    out = [f"(MicroPCB - {_safe(title)})", "G21 G90 G17", f"G0 Z{safe_z:.3f}"]
    cur = None
    for op in ops:
        if op.empty:
            continue
        if cur is not None and op.tool.name != cur.name:
            out += ["M5", f"G0 Z{tool_change_z:.3f}",
                    f"M6 (Change tool to {_safe(op.tool.label)} - then Auto-level tab: "
                    "Probe + set Z 0 - and Resume)"]
            cur = None
        if cur is None:
            if len(out) == 3:
                out.append(f"(First tool: {_safe(op.tool.label)} - zero Z on the copper)")
            out.append("M3")
            cur = op.tool
        out.append(f"({_safe(op.name)})")
        out.extend(op.lines)
    out += ["M5", f"G0 Z{safe_z:.3f}", "M30"]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- side file (split design)
def op_kind(op: OpResult) -> str:
    n = op.name.lower()
    for key, kind in (("isolation", "isolation"), ("clearing", "clearing"),
                      ("drill", "drilling"), ("mill holes", "holes"),
                      ("cutout", "cutout"), ("pin", "pins")):
        if key in n:
            return kind
    return "combined"


def tool_dict(t: Tool, depth: float | None = None) -> dict:
    d = {"name": t.name, "label": t.label, "kind": t.kind, "diameter_mm": t.diameter,
         "angle_deg": t.angle, "tip_mm": t.tip, "feed": t.feed, "plunge": t.plunge}
    if depth is not None:
        d["depth_mm"] = depth
        d["cut_width_mm"] = round(t.cut_width(depth), 4)
    return d


def job_info(pb, ops: list[OpResult], side: str, source: list[str] | None = None,
             app: str = "", max_pad_mm: float = 4.0):
    """Side-file data (micropcb.jobfile.JobInfo) for G-code made from these ops."""
    from shapely.geometry import MultiPolygon, Polygon

    from ..jobfile import JobFeature, JobInfo
    from .board import as_polygons
    ops = [o for o in ops if not o.empty]
    kinds = {op_kind(o) for o in ops}
    tools, seen = [], set()
    for o in ops:
        if o.tool.name not in seen:
            seen.add(o.tool.name)
            tools.append(tool_dict(o.tool))
    feats = [JobFeature("hole", h.x, h.y, h.d) for h in getattr(pb, "holes", [])]
    copper_rings = []
    for g in (getattr(pb, "copper", {}) or {}).values():
        for poly in sorted(as_polygons(g), key=lambda p: -p.area):
            x0, y0, x1, y1 = poly.bounds
            if max(x1 - x0, y1 - y0) <= max_pad_mm and poly.area > 0.05:
                c = poly.centroid
                if not any(abs(f.x - c.x) < 0.2 and abs(f.y - c.y) < 0.2 for f in feats):
                    feats.append(JobFeature("pad", c.x, c.y, max(x1 - x0, y1 - y0)))
            s = poly.simplify(0.01)
            if isinstance(s, Polygon) and not s.is_empty:
                copper_rings.append(list(s.exterior.coords))
    outline = []
    if pb.outline is not None and not pb.outline.is_empty:
        g = pb.outline
        polys = list(g.geoms) if isinstance(g, MultiPolygon) else as_polygons(g) or [g.convex_hull]
        outline = [list(p.exterior.coords) for p in polys if hasattr(p, "exterior")]
    xs = [p[0] for o in ops for path in o.paths for p in path]
    ys = [p[1] for o in ops for path in o.paths for p in path]
    pl = pb.placement
    return JobInfo(
        operation=kinds.pop() if len(kinds) == 1 else "combined",
        side=side, mirror_x_mm=(pb.size[0] / 2 + pl.margin) if pl.mirror else None,
        board_size=tuple(pb.size), outline=outline, tools=tools, features=feats,
        copper=copper_rings,
        cut_area=(min(xs), min(ys), max(xs), max(ys)) if xs else None,
        source=list(source or []), app=app)


def per_tool(ops: list[OpResult]) -> list[list[OpResult]]:
    """Group consecutive operations that use the same tool (one file each)."""
    groups: list[list[OpResult]] = []
    for o in ops:
        if o.empty:
            continue
        if groups and groups[-1][-1].tool.name == o.tool.name:
            groups[-1].append(o)
        else:
            groups.append([o])
    return groups
