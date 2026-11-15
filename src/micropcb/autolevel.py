"""Auto-level: probe grid, height map and toolpath correction (Stage 2.4).

How the original Wegstr software does it (reverse-engineered, see
docs/stage2-design.md): probe with 0x3F packets; the machine stops on copper
contact and replies `04 hi lo` = its own absolute Z step counter (signed
16-bit). Height of a point = difference of its value to the first point's.

Heights here are in mm, relative to the first probed point (the reference,
where work Z = 0), positive = surface higher.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace

import numpy as np

from .gcode import LINE, Move, Program


def make_grid(lo_x: float, lo_y: float, hi_x: float, hi_y: float,
              spacing: float, margin: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """Evenly spaced X and Y probe positions covering the area (≥ 2 each)."""
    x0, x1 = lo_x - margin, hi_x + margin
    y0, y1 = lo_y - margin, hi_y + margin
    nx = max(2, math.ceil((x1 - x0) / spacing - 1e-9) + 1)
    ny = max(2, math.ceil((y1 - y0) / spacing - 1e-9) + 1)
    return np.linspace(x0, x1, nx), np.linspace(y0, y1, ny)


def probe_order(nx: int, ny: int) -> list[tuple[int, int]]:
    """Serpentine order (shortest travel): row 0 left->right, row 1 right->left…"""
    out = []
    for j in range(ny):
        cols = range(nx) if j % 2 == 0 else range(nx - 1, -1, -1)
        out.extend((i, j) for i in cols)
    return out


@dataclass
class HeightMap:
    xs: np.ndarray                       # (nx,) work mm
    ys: np.ndarray                       # (ny,) work mm
    z: np.ndarray                        # (ny, nx) mm, NaN = not probed yet
    meta: dict = field(default_factory=dict)

    @classmethod
    def empty(cls, xs: np.ndarray, ys: np.ndarray) -> "HeightMap":
        return cls(np.asarray(xs, float), np.asarray(ys, float),
                   np.full((len(ys), len(xs)), np.nan))

    @property
    def complete(self) -> bool:
        return bool(np.isfinite(self.z).all())

    @property
    def probed(self) -> int:
        return int(np.isfinite(self.z).sum())

    def points(self) -> list[tuple[float, float, float]]:
        return [(float(self.xs[i]), float(self.ys[j]), float(self.z[j, i]))
                for j in range(len(self.ys)) for i in range(len(self.xs))]

    def stats(self) -> dict:
        z = self.z[np.isfinite(self.z)]
        if not len(z):
            return {}
        # best-fit plane z = a*x + b*y + c -> tilt per 100 mm
        jj, ii = np.nonzero(np.isfinite(self.z))
        A = np.c_[self.xs[ii], self.ys[jj], np.ones(len(ii))]
        (a, b, _), *_ = np.linalg.lstsq(A, self.z[jj, ii], rcond=None)
        return {"min": float(z.min()), "max": float(z.max()),
                "range": float(z.max() - z.min()),
                "tilt_x_per_100": float(a * 100), "tilt_y_per_100": float(b * 100)}

    def at(self, x: float, y: float) -> float:
        """Bilinear interpolation; outside the grid the edge value is used."""
        xs, ys = self.xs, self.ys
        x = min(max(x, xs[0]), xs[-1])
        y = min(max(y, ys[0]), ys[-1])
        i = int(np.clip(np.searchsorted(xs, x) - 1, 0, len(xs) - 2))
        j = int(np.clip(np.searchsorted(ys, y) - 1, 0, len(ys) - 2))
        tx = (x - xs[i]) / (xs[i + 1] - xs[i])
        ty = (y - ys[j]) / (ys[j + 1] - ys[j])
        z = self.z
        return float((1 - ty) * ((1 - tx) * z[j, i] + tx * z[j, i + 1])
                     + ty * ((1 - tx) * z[j + 1, i] + tx * z[j + 1, i + 1]))

    # -------------------------------------------------------------- files
    def to_json(self) -> str:
        return json.dumps({"format": "micropcb-heightmap", "version": 1,
                           "xs": self.xs.tolist(), "ys": self.ys.tolist(),
                           "z": [[None if not np.isfinite(v) else round(float(v), 5)
                                  for v in row] for row in self.z],
                           "meta": self.meta}, indent=1)

    @classmethod
    def from_json(cls, text: str) -> "HeightMap":
        d = json.loads(text)
        if d.get("format") != "micropcb-heightmap":
            raise ValueError("not a MicroPCB height map file")
        z = np.array([[np.nan if v is None else v for v in row] for row in d["z"]], float)
        return cls(np.array(d["xs"], float), np.array(d["ys"], float), z, d.get("meta", {}))


def outliers(hm: HeightMap, limit_mm: float = 0.06) -> list[tuple[int, int, float]]:
    """Points that disagree with a plane fitted through their neighbours.

    Returns (i, j, residual mm), worst first. A real board bends smoothly, so a
    point more than limit_mm off its neighbours' plane is most likely dirt / a burr
    under the bit or a bad touch (map 6, 2026-11-15: 0.09 mm high at one point).
    """
    out = []
    ny, nx = hm.z.shape
    for j in range(ny):
        for i in range(nx):
            if not np.isfinite(hm.z[j, i]):
                continue
            pts = [(hm.xs[ii], hm.ys[jj], hm.z[jj, ii])
                   for jj in range(max(0, j - 1), min(ny, j + 2))
                   for ii in range(max(0, i - 1), min(nx, i + 2))
                   if (ii, jj) != (i, j) and np.isfinite(hm.z[jj, ii])]
            if len(pts) < 3:
                continue
            a = np.array(pts)
            A = np.c_[a[:, 0], a[:, 1], np.ones(len(a))]
            coef, *_ = np.linalg.lstsq(A, a[:, 2], rcond=None)
            pred = coef @ (hm.xs[i], hm.ys[j], 1.0)
            r = float(hm.z[j, i] - pred)
            if abs(r) > limit_mm:
                # a bump also makes its edge/corner neighbours look off (their plane
                # leans on it): rank points with more neighbours first
                out.append((abs(r) * len(pts) / 8, (i, j, r)))
    return [t for _, t in sorted(out, key=lambda t: -t[0])]


def _coarse_index(n: int, every: int) -> np.ndarray:
    idx = list(range(0, n, every))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return np.array(idx)


def _near(xs, ys, pts, r: float) -> np.ndarray:
    """(ny, nx) bool: grid points within r of any of pts (N x 2)."""
    out = np.zeros((len(ys), len(xs)), bool)
    if pts is None or not len(pts):
        return out
    pts = np.asarray(pts, float)
    for j, y in enumerate(ys):
        rows = pts[np.abs(pts[:, 1] - y) <= r]
        if not len(rows):
            continue
        d = np.abs(xs[:, None] - rows[None, :, 0])
        dy = np.abs(rows[:, 1] - y)
        out[j] = (np.hypot(d, dy[None, :]) <= r).any(axis=1)
    return out


@dataclass
class AutoPlan:
    """Job-based probe points on the fine grid (xs, ys).

    1. coarse grid (every `every`-th point) + fine points near fine features of
       the job (small gaps) that the job actually cuts near;
    2. after that, all job-needed fine points in coarse cells where the board
       bends more than `bend_mm` (coarse point off its coarse neighbours' plane);
    3. the rest is filled from the coarse grid (bilinear) and marked in meta."""
    xs: np.ndarray
    ys: np.ndarray
    every: int
    need: np.ndarray            # (ny, nx) bool: the job cuts near this point
    first: np.ndarray           # (ny, nx) bool: probe in pass 1
    bend_mm: float = 0.02

    @classmethod
    def build(cls, xs, ys, cuts, fine=(), every: int = 3, bend_mm: float = 0.02) -> "AutoPlan":
        xs, ys = np.asarray(xs, float), np.asarray(ys, float)
        sx = float(np.diff(xs).max()) if len(xs) > 1 else 1.0
        sy = float(np.diff(ys).max()) if len(ys) > 1 else 1.0
        r = max(sx, sy)
        need = _near(xs, ys, cuts, r) if cuts is not None and len(cuts) else \
            np.ones((len(ys), len(xs)), bool)
        first = np.zeros_like(need)
        cx, cy = _coarse_index(len(xs), every), _coarse_index(len(ys), every)
        first[np.ix_(cy, cx)] = True
        first |= _near(xs, ys, list(fine), r) & need
        return cls(xs, ys, every, need, first, bend_mm)

    @property
    def coarse(self) -> tuple[np.ndarray, np.ndarray]:
        return _coarse_index(len(self.xs), self.every), _coarse_index(len(self.ys), self.every)

    def count(self) -> tuple[int, int]:
        """(points in pass 1, full fine grid)."""
        return int(self.first.sum()), self.first.size

    def more(self, hm: "HeightMap") -> list[tuple[int, int]]:
        """Pass 2: job-needed fine points around coarse points where the board bends."""
        cx, cy = self.coarse
        sub = HeightMap(hm.xs[cx], hm.ys[cy], hm.z[np.ix_(cy, cx)].copy())
        want = np.zeros_like(self.need)
        for ci, cj, _ in outliers(sub, self.bend_mm):
            i0, i1 = cx[max(ci - 1, 0)], cx[min(ci + 1, len(cx) - 1)]
            j0, j1 = cy[max(cj - 1, 0)], cy[min(cj + 1, len(cy) - 1)]
            want[j0:j1 + 1, i0:i1 + 1] = True
        want &= self.need & ~np.isfinite(hm.z)
        return [(int(i), int(j)) for j, i in zip(*np.nonzero(want))]

    def fill(self, hm: "HeightMap") -> int:
        """Fill not-probed points (bilinear from the coarse grid). Returns how many."""
        cx, cy = self.coarse
        sub = HeightMap(hm.xs[cx], hm.ys[cy], hm.z[np.ix_(cy, cx)].copy())
        if not sub.complete:
            return 0
        miss = ~np.isfinite(hm.z)
        for j, i in zip(*np.nonzero(miss)):
            hm.z[j, i] = sub.at(float(hm.xs[i]), float(hm.ys[j]))
        hm.meta["filled"] = [[round(float(hm.xs[i]), 3), round(float(hm.ys[j]), 3)]
                             for j, i in zip(*np.nonzero(miss))]
        return int(miss.sum())


def job_cut_points(prog: Program, step_mm: float = 1.0) -> np.ndarray:
    """XY points along the cutting moves (Z below 0) of a program, every ~step_mm."""
    pts = []
    for m in prog.lines():
        if getattr(m, "rapid", False):
            continue
        (x0, y0, z0), (x1, y1, z1) = m.start, m.end
        if min(z0, z1) >= 0:
            continue
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) / step_mm))
        for k in range(n + 1):
            t = k / n
            pts.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    return np.array(pts) if pts else np.zeros((0, 2))


def outside_map(prog: Program, hm: HeightMap, tol_mm: float = 0.5) -> str | None:
    """Describe where the job's cutting moves leave the measured area (None = inside)."""
    xs = [p[0] for m in prog.moves if m.kind == LINE and not m.rapid for p in (m.start, m.end)]
    ys = [p[1] for m in prog.moves if m.kind == LINE and not m.rapid for p in (m.start, m.end)]
    if not xs:
        return None
    lo_x, hi_x, lo_y, hi_y = min(xs), max(xs), min(ys), max(ys)
    if (lo_x >= hm.xs[0] - tol_mm and hi_x <= hm.xs[-1] + tol_mm
            and lo_y >= hm.ys[0] - tol_mm and hi_y <= hm.ys[-1] + tol_mm):
        return None
    return (f"the job cuts X {lo_x:.1f} … {hi_x:.1f}, Y {lo_y:.1f} … {hi_y:.1f} mm but the "
            f"height map only covers X {hm.xs[0]:.1f} … {hm.xs[-1]:.1f}, "
            f"Y {hm.ys[0]:.1f} … {hm.ys[-1]:.1f} mm")


def apply_heightmap(prog: Program, hm: HeightMap, max_seg_mm: float = 1.0) -> Program:
    """Return a new program whose Z follows the measured surface.

    Feed moves are split into pieces of at most `max_seg_mm` so the path can
    follow the surface between grid points. Every point gets Z + height.
    """
    if not hm.complete:
        raise ValueError("height map is not complete")
    out = replace(prog, moves=[], warnings=list(prog.warnings))
    for m in prog.moves:
        if m.kind != LINE:
            out.moves.append(m)
            continue
        (x0, y0, z0), (x1, y1, z1) = m.start, m.end
        dxy = math.hypot(x1 - x0, y1 - y0)
        n = 1 if m.rapid else max(1, math.ceil(dxy / max_seg_mm))
        prev = (x0, y0, z0 + hm.at(x0, y0))
        for k in range(1, n + 1):
            t = k / n
            x, y, z = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, z0 + (z1 - z0) * t
            p = (x, y, z + hm.at(x, y))
            out.moves.append(Move(LINE, m.line, prev, p, rapid=m.rapid, feed=m.feed))
            prev = p
    return out
