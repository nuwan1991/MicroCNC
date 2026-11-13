"""3D toolpath view (Stage 2.1), pyqtgraph OpenGL.

Colours: cuts below the surface go yellow -> red with depth, feed moves in
the air are blue, rapids are faint grey, done path green, selected line
yellow-white. Z can be exaggerated (PCB depths are tiny next to the board).

Quality:
- "full":  antialiased, rapids shown, thicker lines
- "light": no antialiasing, thin lines, rapids hidden, fewer done points
  (for built-in graphics / big files)
If OpenGL is not available the widget shows a message instead.
"""
from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QVector3D
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout, QWidget)

from ..toolpath import Timeline, Toolpath

try:  # OpenGL can be missing or broken on some PCs
    import pyqtgraph.opengl as gl
    GL_ERROR = ""
except Exception as e:  # pragma: no cover - depends on the PC
    gl = None
    GL_ERROR = str(e)

VIEWS = {  # name: (elevation, azimuth)
    "Top": (90, -90),
    "Front": (0, -90),
    "Side": (0, 0),
    "Iso": (30, -60),
}
Z_SCALES = (1, 5, 20, 50)


def depth_colors(z: np.ndarray, max_depth: float) -> np.ndarray:
    """RGBA per point: yellow (just below 0) -> red (max_depth and deeper)."""
    t = np.clip(-z / max(max_depth, 1e-6), 0, 1)
    c = np.empty((len(z), 4), dtype=np.float32)
    c[:, 0] = 1.0
    c[:, 1] = 0.85 * (1 - t)
    c[:, 2] = 0.1
    c[:, 3] = 1.0
    return c


class View3D(QWidget):
    def __init__(self, quality: str = "full", parent=None) -> None:
        super().__init__(parent)
        self.quality = quality
        self.z_scale = 5
        self.color_depth = 0.3  # mm for full red
        self.tp: Toolpath | None = None
        self._tl: Timeline | None = None
        self._done_idx = 0
        self._area = None
        self._items: list = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        bar = QHBoxLayout()
        self.view_btns = QButtonGroup(self)
        for name in VIEWS:
            b = QPushButton(name)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(lambda _=False, n=name: self.set_view(n))
            bar.addWidget(b)
        fit = QPushButton("Fit")
        fit.setFocusPolicy(Qt.NoFocus)
        fit.clicked.connect(lambda: self.set_view(self._last_view))
        bar.addWidget(fit)
        bar.addSpacing(12)
        bar.addWidget(QLabel("Z scale:"))
        self.zs = QComboBox()
        self.zs.setFocusPolicy(Qt.NoFocus)
        for k in Z_SCALES:
            self.zs.addItem(f"×{k}", k)
        self.zs.setCurrentIndex(Z_SCALES.index(self.z_scale))
        self.zs.currentIndexChanged.connect(lambda _: self.set_z_scale(self.zs.currentData()))
        bar.addWidget(self.zs)
        bar.addStretch(1)
        self.legend = QLabel("<span style='color:#ffd54f'>■</span> shallow "
                             "<span style='color:#ff1a1a'>■</span> deep "
                             "<span style='color:#42a5f5'>■</span> air "
                             "<span style='color:#9e9e9e'>■</span> rapid "
                             "<span style='color:#66bb6a'>■</span> done")
        bar.addWidget(self.legend)
        lay.addLayout(bar)

        self._last_view = "Iso"
        self.w = None
        if gl is None:
            lay.addWidget(QLabel(f"3D view not available (OpenGL): {GL_ERROR}\n"
                                 "Use the 2D view, or set View quality to 2D only."), 1)
            return
        self.w = gl.GLViewWidget()
        self.w.setBackgroundColor((30, 30, 30, 255))
        lay.addWidget(self.w, 1)
        self.grid = gl.GLGridItem()
        self.grid.setColor((80, 80, 80, 120))
        self.w.addItem(self.grid)
        self.paths = []          # static path items
        self.done = gl.GLLinePlotItem(mode="line_strip", color=(0.4, 0.73, 0.42, 1.0),
                                      width=3)
        self.sel = gl.GLLinePlotItem(mode="lines", color=(1.0, 1.0, 0.6, 1.0), width=4)
        self.box = gl.GLLinePlotItem(mode="lines", color=(0.6, 0.6, 0.6, 0.6), width=1)
        self.tool = gl.GLLinePlotItem(mode="lines", color=(1.0, 0.32, 0.32, 1.0), width=3)
        self.marker = gl.GLScatterPlotItem(color=(0.88, 0.25, 0.98, 1.0), size=14)
        self.hm_pts = gl.GLScatterPlotItem(size=10)
        self.hm_mesh = gl.GLLinePlotItem(mode="lines", color=(0.8, 0.8, 0.8, 0.5), width=1)
        for it in (self.box, self.done, self.sel, self.tool, self.marker, self.hm_pts,
                   self.hm_mesh):
            it.setVisible(False)
            self.w.addItem(it)
        self._scaled = [self.done, self.sel, self.box, self.tool, self.marker, self.hm_pts,
                        self.hm_mesh]
        self.set_view("Iso")

    @property
    def available(self) -> bool:
        return self.w is not None

    # ------------------------------------------------------------ settings
    def set_quality(self, quality: str) -> None:
        if quality != self.quality:
            self.quality = quality
            self.set_toolpath(self.tp)
            if self._tl is not None:
                idx, self._done_idx = self._done_idx, 0
                self.set_done(self._tl, idx)

    def set_z_scale(self, k: float) -> None:
        self.z_scale = k
        if self.w is None:
            return
        for it in self.paths + self._scaled:
            it.resetTransform()
            it.scale(1, 1, k)
        self.set_view(self._last_view)

    # ------------------------------------------------------------ data
    def set_toolpath(self, tp: Toolpath | None) -> None:
        self.tp = tp
        if self.w is None:
            return
        for it in self.paths:
            self.w.removeItem(it)
        self.paths = []
        self.set_selected(None)
        if tp is None or not len(tp):
            return
        full = self.quality == "full"
        s, e = tp.start, tp.end
        zmin = np.minimum(s[:, 2], e[:, 2])
        deep = ~tp.rapid & (zmin < -1e-6)
        air = ~tp.rapid & ~deep

        def seg_pos(mask):
            p = np.empty((2 * int(mask.sum()), 3), dtype=np.float32)
            p[0::2] = s[mask]
            p[1::2] = e[mask]
            return p

        def add(pos, color, width):
            if not len(pos):
                return
            it = gl.GLLinePlotItem(pos=pos, color=color, width=width, mode="lines",
                                   antialias=full)
            it.scale(1, 1, self.z_scale)
            self.w.addItem(it)
            self.paths.append(it)

        if full:
            add(seg_pos(tp.rapid), (0.62, 0.62, 0.62, 0.35), 1)
        add(seg_pos(air), (0.26, 0.65, 0.96, 0.9), 1.5 if full else 1)
        dp = seg_pos(deep)
        add(dp, depth_colors(dp[:, 2], self.color_depth), 2 if full else 1)
        # keep overlay items on top
        for it in (self.done, self.sel, self.tool, self.marker):
            self.w.removeItem(it)
            self.w.addItem(it)
        self._fit_grid()
        self.set_view(self._last_view)

    def set_done(self, tl: Timeline | None, index: int) -> None:
        if self.w is None:
            return
        self._tl = tl
        if tl is None or index <= 0:
            self.done.setVisible(False)
            self._done_idx = 0
            return
        index = min(index, tl.ticks)
        if index == self._done_idx:
            return
        self._done_idx = index
        maxpts = 40000 if self.quality == "full" else 8000
        stride = max(1, tl.ticks // maxpts)
        idx = np.arange(0, index + 1, stride)
        if idx[-1] != index:
            idx = np.append(idx, index)
        self.done.setData(pos=tl.pos[idx].astype(np.float32),
                          antialias=self.quality == "full")
        self.done.setVisible(True)

    def set_selected(self, sl: slice | None) -> None:
        if self.w is None:
            return
        if sl is None or self.tp is None or sl.stop <= sl.start:
            self.sel.setVisible(False)
            return
        p = np.empty((2 * (sl.stop - sl.start), 3), dtype=np.float32)
        p[0::2] = self.tp.start[sl]
        p[1::2] = self.tp.end[sl]
        self.sel.setData(pos=p)
        self.sel.setVisible(True)

    def set_tool(self, x: float, y: float, z: float) -> None:
        if self.w is None:
            return
        h = 3.0 / self.z_scale  # 3 mm tall on screen whatever the Z scale
        self.tool.setData(pos=np.array([[x, y, z], [x, y, z + h]], dtype=np.float32))
        self.tool.setVisible(True)

    def set_marker(self, xyz) -> None:
        if self.w is None:
            return
        if xyz is None:
            self.marker.setVisible(False)
        else:
            self.marker.setData(pos=np.array([xyz], dtype=np.float32))
            self.marker.setVisible(True)

    def set_heightmap(self, hm) -> None:
        """Probe points coloured blue (low) -> red (high), joined by a mesh."""
        if self.w is None:
            return
        if hm is None or not np.isfinite(hm.z).any():
            self.hm_pts.setVisible(False)
            self.hm_mesh.setVisible(False)
            return
        X, Y = np.meshgrid(hm.xs, hm.ys)
        ok = np.isfinite(hm.z)
        pts = np.c_[X[ok], Y[ok], hm.z[ok]].astype(np.float32)
        span = max(float(np.abs(hm.z[ok]).max()), 0.01)
        t = np.clip(hm.z[ok] / span, -1, 1)
        col = np.ones((len(t), 4), dtype=np.float32)
        col[:, 0] = np.where(t >= 0, 1, 1 + t)
        col[:, 1] = 1 - np.abs(t)
        col[:, 2] = np.where(t <= 0, 1, 1 - t)
        self.hm_pts.setData(pos=pts, color=col)
        self.hm_pts.setVisible(True)
        segs = []
        ny, nx = hm.z.shape
        for j in range(ny):
            for i in range(nx):
                for dj, di in ((0, 1), (1, 0)):
                    jj, ii = j + dj, i + di
                    if jj < ny and ii < nx and ok[j, i] and ok[jj, ii]:
                        segs += [(hm.xs[i], hm.ys[j], hm.z[j, i]),
                                 (hm.xs[ii], hm.ys[jj], hm.z[jj, ii])]
        if segs:
            self.hm_mesh.setData(pos=np.array(segs, dtype=np.float32))
        self.hm_mesh.setVisible(bool(segs))

    def set_area(self, lo_hi) -> None:
        if self.w is None:
            return
        self._area = lo_hi
        if lo_hi is None:
            self.box.setVisible(False)
        else:
            (x0, y0, z0), (x1, y1, z1) = lo_hi
            c = np.array([[x, y, z] for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)])
            edges = [(0, 1), (2, 3), (4, 5), (6, 7), (0, 2), (1, 3), (4, 6), (5, 7),
                     (0, 4), (1, 5), (2, 6), (3, 7)]
            self.box.setData(pos=np.array([c[i] for e in edges for i in e], dtype=np.float32))
            self.box.setVisible(True)
        self._fit_grid()

    # ------------------------------------------------------------ camera
    def _bounds(self):
        if self.tp is not None and len(self.tp):
            return self.tp.bounds()
        return np.array([-5.0, -5.0, -1.0]), np.array([25.0, 25.0, 2.0])

    def _fit_grid(self) -> None:
        lo, hi = self._bounds()
        size = max(hi[0] - lo[0], hi[1] - lo[1], 10)
        sp = 1 if size < 40 else 10
        n = (int(size / sp) + 2) * sp
        self.grid.setSize(n, n)
        self.grid.setSpacing(sp, sp)
        self.grid.resetTransform()
        self.grid.translate((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, 0)

    def set_view(self, name: str) -> None:
        self._last_view = name
        if self.w is None:
            return
        lo, hi = self._bounds()
        c = (lo + hi) / 2
        el, az = VIEWS[name]
        zr = (hi[2] - lo[2]) * self.z_scale
        if name == "Top":
            size = max(hi[0] - lo[0], hi[1] - lo[1], 5)
        elif name == "Front":
            size = max(hi[0] - lo[0], zr, 5)
        elif name == "Side":
            size = max(hi[1] - lo[1], zr, 5)
        else:  # Iso: fit the board; tall Z retracts may go off-screen
            size = max(hi[0] - lo[0], hi[1] - lo[1], 5)
        # board surface (Z 0) is what matters; keep it in the middle for top/iso
        cz = 0.0 if name in ("Top", "Iso") else c[2] * self.z_scale
        self.w.opts["center"] = QVector3D(float(c[0]), float(c[1]), float(cz))
        # pyqtgraph's fov is horizontal: a wide, short view needs more distance
        w, h = max(self.w.width(), 1), max(self.w.height(), 1)
        fov = math.radians(self.w.opts.get("fov", 60))
        dist = 0.6 * size / math.tan(fov / 2) * max(1.0, w / h)
        self.w.setCameraPosition(distance=dist, elevation=el, azimuth=az)

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        if self.w is not None:
            self.set_view(self._last_view)
