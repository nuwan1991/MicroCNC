"""Board alignment from holes / pads seen by the camera (Stage 4.3).

Expected points come from the loaded PCB (work mm); measured points are
where the camera sees them (work mm). A rigid fit (rotation + offset) is
applied to the job like the height map.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

import numpy as np

from ..gcode import LINE, Move, Program


@dataclass
class Feature:
    x: float
    y: float
    kind: str        # "hole" | "pad"
    size: float      # diameter / pad size (mm)


def board_features(pb, max_pad_mm: float = 4.0) -> list[Feature]:
    """Holes and small copper pads of a placed board (work mm)."""
    out = [Feature(h.x, h.y, "hole", h.d) for h in getattr(pb, "holes", [])]
    from ..pcb.board import as_polygons
    for side, g in (getattr(pb, "copper", {}) or {}).items():
        for poly in as_polygons(g):
            x0, y0, x1, y1 = poly.bounds
            if max(x1 - x0, y1 - y0) <= max_pad_mm and poly.area > 0.05:
                c = poly.centroid
                if not any(math.hypot(f.x - c.x, f.y - c.y) < 0.2 for f in out):
                    out.append(Feature(c.x, c.y, "pad", max(x1 - x0, y1 - y0)))
    return out


@dataclass
class AlignPoint:
    ex: float          # expected (PCB) work mm
    ey: float
    mx: float          # measured work mm
    my: float
    kind: str = ""


@dataclass
class Alignment:
    angle_deg: float = 0.0
    tx: float = 0.0
    ty: float = 0.0
    scale: float = 1.0                       # reported only, not applied
    residuals: list[float] = field(default_factory=list)

    @property
    def max_error(self) -> float:
        return max(self.residuals, default=0.0)

    def matrix(self) -> np.ndarray:
        a = math.radians(self.angle_deg)
        return np.array([[math.cos(a), -math.sin(a), self.tx],
                         [math.sin(a), math.cos(a), self.ty]])

    def apply(self, xy) -> np.ndarray:
        p = np.atleast_2d(np.asarray(xy, float))
        m = self.matrix()
        return p @ m[:, :2].T + m[:, 2]

    def inverse(self, xy) -> np.ndarray:
        p = np.atleast_2d(np.asarray(xy, float)) - [self.tx, self.ty]
        m = self.matrix()[:, :2]
        return p @ m          # R^T applied to rows

    def is_identity(self) -> bool:
        return abs(self.angle_deg) < 1e-9 and abs(self.tx) < 1e-9 and abs(self.ty) < 1e-9


def fit_alignment(points: list[AlignPoint]) -> Alignment:
    """Least-squares rotation + translation (1 point: offset only)."""
    if not points:
        raise ValueError("no alignment points")
    e = np.array([(p.ex, p.ey) for p in points], float)
    m = np.array([(p.mx, p.my) for p in points], float)
    if len(points) == 1:
        t = m[0] - e[0]
        return Alignment(0.0, float(t[0]), float(t[1]), 1.0, [0.0])
    ce, cm = e.mean(axis=0), m.mean(axis=0)
    a, b = e - ce, m - cm
    if np.abs(a).max() < 1.0:
        raise ValueError("alignment points are less than 1 mm apart - use points far apart")
    num = np.sum(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0])
    den = np.sum(a[:, 0] * b[:, 0] + a[:, 1] * b[:, 1])
    ang = math.atan2(num, den)
    r = np.array([[math.cos(ang), -math.sin(ang)], [math.sin(ang), math.cos(ang)]])
    t = cm - r @ ce
    scale = float(np.sqrt((b ** 2).sum() / max((a ** 2).sum(), 1e-12)))
    res = np.hypot(*((e @ r.T + t) - m).T)
    return Alignment(math.degrees(ang), float(t[0]), float(t[1]), scale,
                     [float(v) for v in res])


def nearest_feature(features: list[Feature], x: float, y: float,
                    max_dist: float = 3.0) -> Feature | None:
    best, bd = None, max_dist
    for f in features:
        d = math.hypot(f.x - x, f.y - y)
        if d < bd:
            best, bd = f, d
    return best


def apply_alignment(prog: Program, al: Alignment) -> Program:
    """Rotate + shift every move in X/Y (Z unchanged)."""
    if al.is_identity():
        return prog
    out = replace(prog, moves=[], warnings=list(prog.warnings))
    for mv in prog.moves:
        if mv.kind != LINE:
            out.moves.append(mv)
            continue
        (s,), (e,) = al.apply(mv.start[:2]), al.apply(mv.end[:2])
        out.moves.append(Move(LINE, mv.line, (float(s[0]), float(s[1]), mv.start[2]),
                              (float(e[0]), float(e[1]), mv.end[2]), rapid=mv.rapid,
                              feed=mv.feed, value=mv.value))
    return out


def refine_feature(gray: np.ndarray, x: float, y: float, r_px: float):
    """Centre of the hole / pad blob under a click (pixels) or None.

    Otsu threshold in a window around the click; the connected region of the
    clicked pixel's class is taken if it does not touch the window border.
    Small windows are tried first so a nearby board edge does not disturb it.
    """
    for r in sorted({int(max(6, f * r_px)) for f in (0.35, 0.6, 1.0, 1.6)}):
        c = _refine(gray, x, y, r)
        if c is not None:
            return c
    return None


def _refine(gray: np.ndarray, x: float, y: float, r: int):
    import cv2
    h, w = gray.shape[:2]
    x0, y0 = max(0, int(x) - r), max(0, int(y) - r)
    x1, y1 = min(w, int(x) + r + 1), min(h, int(y) + r + 1)
    roi = gray[y0:y1, x0:x1]
    if roi.size < 16:
        return None
    roi = cv2.GaussianBlur(roi, (3, 3), 0)
    _, th = cv2.threshold(roi, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    cx, cy = int(x) - x0, int(y) - y0
    mask = th if th[cy, cx] else 255 - th
    n, lab, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
    k = lab[cy, cx]
    if k == 0:
        return None
    sx, sy, sw, sh, area = stats[k]
    if sx == 0 or sy == 0 or sx + sw >= roi.shape[1] or sy + sh >= roi.shape[0] or area < 4:
        return None
    return float(cents[k][0] + x0), float(cents[k][1] + y0)
