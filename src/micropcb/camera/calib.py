"""Bed camera calibration (Stage 4.2).

The camera is fixed above the bed. A homography H maps (undistorted) image
pixels to "frame" millimetres on the bed plane. Frame coordinates equal bed
(= machine) coordinates while the machine stands at the reference position
``ref``; axes listed in ``bed_axes`` move the bed under the camera, so

    bed = frame + S * (pos - ref)          S = 1 for bed axes, 0 otherwise

Points above the bed plane (board top, height z) look bigger; with the camera
height known this parallax is corrected around the point under the lens.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


def fit_homography(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Normalised DLT, src/dst (n, 2), n >= 4 -> 3x3 mapping src -> dst."""
    src = np.asarray(src, float)
    dst = np.asarray(dst, float)
    if len(src) < 4:
        raise ValueError("need at least 4 points")

    def norm(p):
        c = p.mean(axis=0)
        s = np.sqrt(2) / max(np.mean(np.hypot(*(p - c).T)), 1e-12)
        return np.array([[s, 0, -s * c[0]], [0, s, -s * c[1]], [0, 0, 1]])

    ts, td = norm(src), norm(dst)
    a = []
    for (x, y), (u, v) in zip(_apply(ts, src), _apply(td, dst)):
        a.append([-x, -y, -1, 0, 0, 0, u * x, u * y, u])
        a.append([0, 0, 0, -x, -y, -1, v * x, v * y, v])
    _, sv, vt = np.linalg.svd(np.array(a))
    if sv[-2] < 1e-9 * sv[0]:
        raise ValueError("points are in a line - spread them over the bed")
    h = vt[-1].reshape(3, 3)
    h = np.linalg.inv(td) @ h @ ts
    return h / h[2, 2]


def _apply(h: np.ndarray, pts: np.ndarray) -> np.ndarray:
    pts = np.atleast_2d(np.asarray(pts, float))
    q = np.c_[pts, np.ones(len(pts))] @ h.T
    return q[:, :2] / q[:, 2:3]


@dataclass
class CalibPoint:
    px: float
    py: float
    fx: float          # frame mm
    fy: float


@dataclass
class CameraCalib:
    H: np.ndarray | None = None              # pixel -> frame mm
    ref: tuple[float, float] = (0.0, 0.0)   # machine X/Y at calibration (mm)
    bed_axes: str = ""                       # "Y", "X", "XY" or ""
    cam_height_mm: float = 0.0               # lens above bed plane, 0 = no parallax fix
    image_size: tuple[int, int] = (0, 0)
    K: np.ndarray | None = None              # lens model (optional)
    dist: np.ndarray | None = None
    points: list[CalibPoint] = field(default_factory=list)
    rms_mm: float = 0.0

    @property
    def ok(self) -> bool:
        return self.H is not None

    # ---------------------------------------------------------------- fit
    def fit(self) -> float:
        if len(self.points) < 4:
            raise ValueError("need at least 4 calibration points")
        px = self.undistort(np.array([(p.px, p.py) for p in self.points]))
        fr = np.array([(p.fx, p.fy) for p in self.points])
        span = fr.max(axis=0) - fr.min(axis=0)
        if min(span) < 5:
            raise ValueError("calibration points span less than 5 mm in X or Y - "
                             "with the bed moving under the camera use marks, "
                             "not the tool tip")
        self.H = fit_homography(px, fr)
        err = np.hypot(*(_apply(self.H, px) - fr).T)
        self.rms_mm = float(np.sqrt(np.mean(err ** 2)))
        return self.rms_mm

    def shift(self, pos) -> np.ndarray:
        s = np.array([1.0 if "X" in self.bed_axes else 0.0,
                      1.0 if "Y" in self.bed_axes else 0.0])
        return s * (np.asarray(pos[:2], float) - np.asarray(self.ref, float))

    def frame_of(self, bed_xy, pos) -> np.ndarray:
        """Where a bed point is in frame mm with the machine at pos."""
        return np.asarray(bed_xy, float) - self.shift(pos)

    def mm_per_px(self) -> float:
        if self.H is None or not self.image_size[0]:
            return 0.0
        w, h = self.image_size
        c = _apply(self.H, np.array([[w / 2, h / 2], [w / 2 + 100, h / 2]]))
        return float(np.hypot(*(c[1] - c[0])) / 100)

    # ---------------------------------------------------------------- lens
    def undistort(self, px: np.ndarray) -> np.ndarray:
        px = np.atleast_2d(np.asarray(px, float))
        if self.K is None or self.dist is None:
            return px
        import cv2
        return cv2.undistortPoints(px.reshape(-1, 1, 2), self.K, self.dist,
                                   P=self.K).reshape(-1, 2)

    def distort(self, px: np.ndarray) -> np.ndarray:
        px = np.atleast_2d(np.asarray(px, float))
        if self.K is None or self.dist is None:
            return px
        k = self.K
        d = np.zeros(5)
        d[:min(5, self.dist.size)] = self.dist.ravel()[:5]
        x = (px[:, 0] - k[0, 2]) / k[0, 0]
        y = (px[:, 1] - k[1, 2]) / k[1, 1]
        r2 = x * x + y * y
        rad = 1 + d[0] * r2 + d[1] * r2 ** 2 + d[4] * r2 ** 3
        xd = x * rad + 2 * d[2] * x * y + d[3] * (r2 + 2 * x * x)
        yd = y * rad + d[2] * (r2 + 2 * y * y) + 2 * d[3] * x * y
        return np.c_[xd * k[0, 0] + k[0, 2], yd * k[1, 1] + k[1, 2]]

    # ---------------------------------------------------------------- mapping
    def _centre(self) -> np.ndarray:
        w, h = self.image_size if self.image_size[0] else (0, 0)
        c = self.K[:2, 2] if self.K is not None else np.array([w / 2, h / 2])
        return _apply(self.H, c[None, :])[0]

    def _scale(self, z: float) -> float:
        hc = self.cam_height_mm
        if hc <= 0 or not z:
            return 1.0
        if z >= hc - 1:
            raise ValueError("height above the camera")
        return (hc - z) / hc

    def pixel_to_bed(self, px, pos=(0.0, 0.0), z: float = 0.0) -> np.ndarray:
        """Pixels (n, 2) -> bed/machine mm (n, 2) of points at height z."""
        if self.H is None:
            raise ValueError("camera not calibrated")
        f0 = _apply(self.H, self.undistort(px))
        c = self._centre()
        f = c + (f0 - c) * self._scale(z)
        return f + self.shift(pos)

    def bed_to_pixel(self, xy, pos=(0.0, 0.0), z: float = 0.0) -> np.ndarray:
        if self.H is None:
            raise ValueError("camera not calibrated")
        f = np.atleast_2d(np.asarray(xy, float)) - self.shift(pos)
        c = self._centre()
        f0 = c + (f - c) / self._scale(z)
        return self.distort(_apply(np.linalg.inv(self.H), f0))

    # ---------------------------------------------------------------- JSON
    def to_dict(self) -> dict:
        arr = lambda a: None if a is None else np.asarray(a).tolist()  # noqa: E731
        return {"H": arr(self.H), "ref": list(self.ref), "bed_axes": self.bed_axes,
                "cam_height_mm": self.cam_height_mm, "image_size": list(self.image_size),
                "K": arr(self.K), "dist": arr(self.dist), "rms_mm": self.rms_mm,
                "points": [[p.px, p.py, p.fx, p.fy] for p in self.points]}

    @classmethod
    def from_dict(cls, d: dict) -> "CameraCalib":
        arr = lambda a: None if a is None else np.array(a, float)  # noqa: E731
        return cls(H=arr(d.get("H")), ref=tuple(d.get("ref", (0, 0))),
                   bed_axes=d.get("bed_axes", ""), cam_height_mm=d.get("cam_height_mm", 0.0),
                   image_size=tuple(d.get("image_size", (0, 0))), K=arr(d.get("K")),
                   dist=arr(d.get("dist")), rms_mm=d.get("rms_mm", 0.0),
                   points=[CalibPoint(*p) for p in d.get("points", [])])


def calibrate_lens(frames: list[np.ndarray], pattern: tuple[int, int] = (9, 6),
                   square_mm: float = 10.0):
    """Checkerboard lens calibration. Returns (K, dist, rms_px, used)."""
    import cv2
    obj = np.zeros((pattern[0] * pattern[1], 3), np.float32)
    obj[:, :2] = np.mgrid[0:pattern[0], 0:pattern[1]].T.reshape(-1, 2) * square_mm
    objs, imgs, size = [], [], None
    for f in frames:
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) if f.ndim == 3 else f
        size = g.shape[::-1]
        ok, corners = cv2.findChessboardCorners(g, pattern)
        if not ok:
            continue
        corners = cv2.cornerSubPix(g, corners, (11, 11), (-1, -1),
                                   (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 1e-3))
        objs.append(obj)
        imgs.append(corners)
    if len(objs) < 3:
        raise ValueError(f"checkerboard found in {len(objs)} pictures - need at least 3")
    rms, k, dist, _, _ = cv2.calibrateCamera(objs, imgs, size, None, None)
    return k, dist, float(rms), len(objs)


def mark_points(area: tuple[float, float], n: int = 3, margin: float = 10.0) -> list[tuple]:
    """n x n grid of calibration mark positions (work mm)."""
    xs = np.linspace(margin, area[0] - margin, n)
    ys = np.linspace(margin, area[1] - margin, n)
    return [(float(x), float(y)) for y in ys for x in xs]


def mark_job(points, depth: float = 0.1, safe: float = 2.0, feed: float = 30) -> str:
    """Small G-code job that spot-marks the calibration points."""
    out = ["(MicroPCB camera calibration marks)", "G21 G90", f"G0 Z{safe:g}", "M3"]
    for x, y in points:
        out += [f"G0 X{x:.3f} Y{y:.3f}", f"G1 Z{-depth:g} F{feed:g}", f"G0 Z{safe:g}"]
    out += ["M5", "G0 X0 Y0", "M2"]
    return "\n".join(out) + "\n"
