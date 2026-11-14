"""Obstacles on the bed (Stage 4.4): clamps, screws, tape.

Stored as polygons in bed (machine) mm. For now one safe height for all:
any move below it inside an obstacle is a warning in the Checks tab.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

from ..toolpath import WARNING, Issue, Toolpath


@dataclass
class Obstacle:
    points: list[tuple[float, float]]     # bed mm
    name: str = "obstacle"

    def polygon(self) -> Polygon:
        return Polygon(self.points).buffer(0)

    def to_dict(self) -> dict:
        return {"name": self.name, "points": [list(p) for p in self.points]}

    @classmethod
    def from_dict(cls, d: dict) -> "Obstacle":
        return cls([tuple(p) for p in d["points"]], d.get("name", "obstacle"))


def check_obstacles(tp: Toolpath, obstacles: list[Obstacle], work_offset_mm,
                    safe_z: float, clearance: float = 0.5, limit: int = 100) -> list[Issue]:
    """Warnings for moves below safe_z (work Z) inside an obstacle."""
    if not obstacles or not len(tp):
        return []
    ox, oy = float(work_offset_mm[0]), float(work_offset_mm[1])
    polys = [o.polygon().buffer(clearance) for o in obstacles]
    names = [o.name for o in obstacles]
    low = np.minimum(tp.start[:, 2], tp.end[:, 2]) < safe_z - 1e-6
    if not low.any():
        return []
    s = tp.start[:, :2] + [ox, oy]
    e = tp.end[:, :2] + [ox, oy]
    lo, hi = np.minimum(s, e), np.maximum(s, e)
    issues: list[Issue] = []
    seen: set[tuple[int, int]] = set()
    for k, poly in enumerate(polys):
        bx0, by0, bx1, by1 = poly.bounds
        cand = np.nonzero(low & (hi[:, 0] >= bx0) & (lo[:, 0] <= bx1)
                          & (hi[:, 1] >= by0) & (lo[:, 1] <= by1))[0]
        if not len(cand):
            continue
        segs = [LineString([s[i], e[i]]) if (s[i] != e[i]).any() else
                LineString([s[i], s[i] + 1e-6]) for i in cand]
        hit = STRtree(segs).query(poly, predicate="intersects")
        for j in sorted(hit):
            ln = int(tp.line[cand[j]])
            if (ln, k) in seen:
                continue
            seen.add((ln, k))
            issues.append(Issue(WARNING, ln, f"Below safe height {safe_z:g} mm over "
                                             f"{names[k]} - tool may hit it"))
            if len(issues) >= limit:
                return issues
    return issues


def board_overlap(outline, obstacles: list[Obstacle], work_offset_mm) -> list[str]:
    """Names of obstacles under the board outline (work mm)."""
    if outline is None or outline.is_empty or not obstacles:
        return []
    from shapely.affinity import translate
    b = translate(outline, float(work_offset_mm[0]), float(work_offset_mm[1]))
    return [o.name for o in obstacles if o.polygon().intersects(b)]


def detect_obstacles(reference: np.ndarray, image: np.ndarray, to_bed,
                     min_area_px: int = 400, threshold: int = 35) -> list[Obstacle]:
    """Things that are in `image` but not in the empty-bed `reference`.

    to_bed: function pixels (n, 2) -> bed mm (n, 2).
    """
    import cv2
    if reference.shape != image.shape:
        raise ValueError("reference picture has another size - take it again")
    g0 = cv2.GaussianBlur(_gray(reference), (7, 7), 0)
    g1 = cv2.GaussianBlur(_gray(image), (7, 7), 0)
    diff = cv2.absdiff(g0, g1)
    _, mask = cv2.threshold(diff, threshold, 255, cv2.THRESH_BINARY)
    k = np.ones((9, 9), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    cs, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cs:
        if cv2.contourArea(c) < min_area_px:
            continue
        hull = cv2.convexHull(c)
        approx = cv2.approxPolyDP(hull, 3, True).reshape(-1, 2)
        if len(approx) < 3:
            continue
        pts = to_bed(approx.astype(float))
        out.append(Obstacle([tuple(map(float, p)) for p in pts], f"obstacle {len(out) + 1}"))
    return out


def merged(obstacles: list[Obstacle]):
    return unary_union([o.polygon() for o in obstacles]) if obstacles else Polygon()


def _gray(img):
    import cv2
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
