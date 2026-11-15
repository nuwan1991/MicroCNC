"""Board check: the smallest sizes in the Gerber / drill data, per copper side.

Clearance (gap between separate copper), track width, pad size, drill size and
annular ring (copper left around a drilled hole), each with the place of the
worst one, so the user can see what the board needs from the tool before CAM.
"""
from __future__ import annotations

from dataclasses import dataclass

import shapely
from shapely.geometry import Point

from .board import PlacedBoard, as_polygons


@dataclass
class CheckRow:
    side: str            # top | bottom | drill
    what: str            # e.g. "Clearance"
    value: float | None  # mm, smallest
    x: float | None = None
    y: float | None = None
    count: int = 0       # how many measured
    below: int = 0       # how many below `limit`
    limit: float | None = None
    note: str = ""

    @property
    def bad(self) -> bool:
        return self.below > 0


def gap_spots(copper, look_mm: float = 1.0) -> list[tuple[float, float, float]]:
    """Per copper island: (gap to its nearest other island mm, x, y of the gap centre).
    Only islands closer than look_mm to another. Smallest first."""
    islands = as_polygons(copper)
    if len(islands) < 2:
        return []
    tree = shapely.STRtree(islands)
    a, b = tree.query(islands, predicate="dwithin", distance=look_mm)
    keep = a < b
    a, b = a[keep], b[keep]
    if not len(a):
        return []
    ga = [islands[i] for i in a]
    gb = [islands[j] for j in b]
    d = shapely.distance(ga, gb)
    best: dict[int, tuple[float, int]] = {}
    for k, (i, j, v) in enumerate(zip(a, b, d)):
        for n in (int(i), int(j)):
            if v < best.get(n, (1e9, -1))[0]:
                best[n] = (float(v), k)
    out, seen = [], set()
    for n, (v, k) in best.items():
        line = shapely.shortest_line(ga[k], gb[k])
        c = line.interpolate(0.5, normalized=True)
        out.append((v, c.x, c.y))
        seen.add(k)
    out.sort()
    return out


def annular_rings(pb: PlacedBoard, side: str) -> list[tuple[float, float, float]]:
    """(ring width mm, x, y) for every hole inside copper of this side."""
    copper = pb.copper.get(side)
    polys = as_polygons(copper)
    if not polys or not pb.holes:
        return []
    tree = shapely.STRtree(polys)
    out = []
    for h in pb.holes:
        p = Point(h.x, h.y)
        for k in tree.query(p, predicate="within"):
            poly = polys[int(k)]
            edge = min([poly.exterior.distance(p)] + [r.distance(p) for r in poly.interiors
                                                      if r.distance(p) > h.d / 2 + 1e-6] or [9e9])
            out.append((edge - h.d / 2, h.x, h.y))
            break
    out.sort()
    return out


def board_check(pb: PlacedBoard, cut_width: float | None = None,
                min_ring: float = 0.15) -> list[CheckRow]:
    """All rows. cut_width: the isolation groove width - gaps below it are counted."""
    rows: list[CheckRow] = []
    for side in ("top", "bottom"):
        copper = pb.copper.get(side)
        if copper is None or copper.is_empty:
            continue
        gaps = gap_spots(copper)
        if gaps:
            v, x, y = gaps[0]
            below = sum(1 for g in gaps if cut_width and g[0] < cut_width)
            rows.append(CheckRow(side, "Clearance (copper to copper)", v, x, y, len(gaps),
                                 below, cut_width,
                                 f"{below} gap(s) narrower than the cut width {cut_width:.3f} mm"
                                 if below else ""))
        else:
            rows.append(CheckRow(side, "Clearance (copper to copper)", None,
                                 note="no copper closer than 1 mm"))
        tr = sorted(pb.tracks.get(side) or [])
        tr = [t for t in tr if t[0] > 0]
        if tr:
            w, x, y = tr[0]
            rows.append(CheckRow(side, "Track width", w, x, y, len(tr),
                                 note=_sizes(t[0] for t in tr)))
        pads = sorted(pb.pads.get(side) or [], key=lambda p: p[2])
        if pads:
            x, y, s = pads[0]
            rows.append(CheckRow(side, "Pad size (smaller side)", s, x, y, len(pads),
                                 note=_sizes(p[2] for p in pads)))
        rings = annular_rings(pb, side)
        if rings:
            v, x, y = rings[0]
            below = sum(1 for r in rings if r[0] < min_ring)
            rows.append(CheckRow(side, "Annular ring (copper around hole)", v, x, y, len(rings),
                                 below, min_ring,
                                 f"{below} hole(s) leave less than {min_ring:g} mm copper"
                                 if below else ""))
    if pb.holes:
        h = min(pb.holes, key=lambda h: h.d)
        rows.append(CheckRow("drill", "Drill size", h.d, h.x, h.y, len(pb.holes),
                             note=_sizes(hh.d for hh in pb.holes)))
    return rows


def _sizes(vals) -> str:
    """'0.25 mm ×40, 0.30 mm ×12, …' - the 4 smallest distinct sizes."""
    count: dict[float, int] = {}
    for v in vals:
        count[round(v, 3)] = count.get(round(v, 3), 0) + 1
    parts = [f"{k:g} ×{n}" for k, n in sorted(count.items())[:4]]
    more = len(count) - 4
    return ", ".join(parts) + (f", … ({more} more sizes)" if more > 0 else "")
