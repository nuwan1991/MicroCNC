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
