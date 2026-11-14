"""2D toolpath view (top view, work coordinates, Y up).

- cuts below the surface: orange, feed moves in the air: blue,
  rapids: grey dashed, already done: green, selected line: yellow
- red cross: tool, magenta X: where the job paused / stopped
- dotted rectangle: machine work area (when machine home is set)

Wheel = zoom at cursor, drag = pan, click = select line, double-click = fit.
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from ..toolpath import Timeline, Toolpath

MAX_DONE_POINTS = 15000


def _path(starts: np.ndarray, ends: np.ndarray) -> QPainterPath:
    path = QPainterPath()
    last = None
    for (x0, y0), (x1, y1) in zip(starts.tolist(), ends.tolist()):
        if last != (x0, y0):
            path.moveTo(x0, y0)
        path.lineTo(x1, y1)
        last = (x1, y1)
    return path


class PreviewWidget(QWidget):
    lineClicked = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(300, 140)
        self.setMouseTracking(True)
        self.tp: Toolpath | None = None
        self._deep = QPainterPath()
        self._air = QPainterPath()
        self._rapid = QPainterPath()
        self._done = QPainterPath()
        self._done_n = 0
        self._sel = QPainterPath()
        self._bounds: QRectF | None = None
        self.tool = (0.0, 0.0)
        self.marker: tuple[float, float] | None = None
        self.area: QRectF | None = None
        self.hm = None
        self._scale = 1.0
        self._center = QPointF(0, 0)
        self._drag: QPointF | None = None
        self._moved = False
        self._cursor_mm: QPointF | None = None
        self._cache: QPixmap | None = None   # static paths, redrawn only on view change
        self._cache_key = None
        self.fit()

    # ------------------------------------------------------------ data
    def set_toolpath(self, tp: Toolpath | None) -> None:
        self.tp = tp
        self._deep = QPainterPath()
        self._air = QPainterPath()
        self._rapid = QPainterPath()
        self._sel = QPainterPath()
        self._bounds = None
        if tp is not None and len(tp):
            s, e = tp.start, tp.end
            deep = ~tp.rapid & (np.minimum(s[:, 2], e[:, 2]) < -1e-6)
            air = ~tp.rapid & ~deep
            self._deep = _path(s[deep, :2], e[deep, :2])
            self._air = _path(s[air, :2], e[air, :2])
            self._rapid = _path(s[tp.rapid, :2], e[tp.rapid, :2])
            lo, hi = tp.bounds()
            self._bounds = QRectF(QPointF(lo[0], lo[1]), QPointF(hi[0], hi[1]))
        self._cache = None
        self.set_done(None, 0)
        self.fit()

    def set_done(self, tl: Timeline | None, index: int) -> None:
        """Draw the path already travelled: timeline positions 0..index."""
        if tl is None or index <= 0:
            if self._done_n:
                self._done = QPainterPath()
                self._done_n = 0
                self.update()
            return
        index = min(index, tl.ticks)
        if index < self._done_n or self._done_n == 0:
            self._done = QPainterPath()
            self._done.moveTo(*tl.pos[0, :2])
            self._done_n = 0
        stride = max(1, tl.ticks // MAX_DONE_POINTS)
        idx = list(range(self._done_n + stride, index + 1, stride))
        if not idx or idx[-1] != index:
            idx.append(index)
        for x, y in tl.pos[idx, :2].tolist():
            self._done.lineTo(x, y)
        self._done_n = index
        self.update()

    def set_selected(self, sl: slice | None) -> None:
        self._sel = QPainterPath()
        if sl is not None and self.tp is not None and sl.stop > sl.start:
            self._sel = _path(self.tp.start[sl, :2], self.tp.end[sl, :2])
        self.update()

    def set_tool(self, x: float, y: float, z: float = 0.0) -> None:
        if (x, y) != self.tool:
            self.tool = (x, y)
            self.update()

    def set_marker(self, xyz: tuple[float, float, float] | None) -> None:
        m = None if xyz is None else (xyz[0], xyz[1])
        if m != self.marker:
            self.marker = m
            self.update()

    def set_heightmap(self, hm) -> None:
        self.hm = hm
        self.update()

    def set_area(self, lo_hi: tuple[np.ndarray, np.ndarray] | None) -> None:
        r = None if lo_hi is None else QRectF(QPointF(lo_hi[0][0], lo_hi[0][1]),
                                               QPointF(lo_hi[1][0], lo_hi[1][1]))
        if r != self.area:
            self.area = r
            self._cache = None
            self.update()

    # ------------------------------------------------------------ view
    def fit(self) -> None:
        r = QRectF(self._bounds) if self._bounds else QRectF(-5, -5, 30, 30)
        r = r.united(QRectF(-1, -1, 2, 2))
        w, h = max(r.width(), 1.0), max(r.height(), 1.0)
        self._center = r.center()
        self._scale = 0.9 * min(max(self.width(), 1) / w, max(self.height(), 1) / h)
        self.update()

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        self.fit()

    def _to_screen(self, x: float, y: float) -> QPointF:
        return QPointF(self.width() / 2 + (x - self._center.x()) * self._scale,
                       self.height() / 2 - (y - self._center.y()) * self._scale)

    def _to_mm(self, p: QPointF) -> QPointF:
        return QPointF(self._center.x() + (p.x() - self.width() / 2) / self._scale,
                       self._center.y() - (p.y() - self.height() / 2) / self._scale)

    def wheelEvent(self, e) -> None:  # noqa: N802
        pos = e.position()
        before = self._to_mm(pos)
        f = 1.25 if e.angleDelta().y() > 0 else 0.8
        self._scale = max(0.05, min(self._scale * f, 5000))
        self._center += before - self._to_mm(pos)
        self.update()

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton:
            self._drag = e.position()
            self._moved = False

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if self._drag is not None:
            d = e.position() - self._drag
            if abs(d.x()) + abs(d.y()) > 2:
                self._moved = True
            if self._moved:
                self._center -= QPointF(d.x() / self._scale, -d.y() / self._scale)
                self._drag = e.position()
        self._cursor_mm = self._to_mm(e.position())
        self.update()

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if self._drag is not None and not self._moved and self.tp is not None:
            p = self._to_mm(e.position())
            i = self.tp.nearest(p.x(), p.y(), tol=8 / self._scale)
            if i is not None:
                self.lineClicked.emit(int(self.tp.line[i]))
        self._drag = None

    def mouseDoubleClickEvent(self, e) -> None:  # noqa: N802
        self.fit()

    def leaveEvent(self, e) -> None:  # noqa: N802
        self._cursor_mm = None
        self.update()

    # ------------------------------------------------------------ paint
    @staticmethod
    def _pen(color: str, width: float = 1.0, style=Qt.SolidLine) -> QPen:
        q = QPen(QColor(color), width, style)
        q.setCosmetic(True)
        return q

    def _world(self, p: QPainter) -> None:
        p.translate(self.width() / 2, self.height() / 2)
        p.scale(self._scale, -self._scale)
        p.translate(-self._center.x(), -self._center.y())

    def _static_layer(self) -> QPixmap:
        """Background, grid, work area and the job paths (expensive for big files)."""
        key = (self.width(), self.height(), self._scale, self._center.x(), self._center.y())
        if self._cache is not None and self._cache_key == key:
            return self._cache
        dpr = self.devicePixelRatioF()
        pm = QPixmap(int(self.width() * dpr), int(self.height() * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(QColor("#1e1e1e"))
        p = QPainter(pm)
        big = self.tp is not None and len(self.tp) > 30000
        p.setRenderHint(QPainter.Antialiasing, not big)
        self._world(p)
        pen = self._pen
        self._draw_grid(p, pen)
        if self.area is not None:
            p.setPen(pen("#9e9e9e", 1, Qt.DotLine))
            p.drawRect(self.area)
        p.setPen(pen("#757575", 1, Qt.DashLine))
        p.drawPath(self._rapid)
        p.setPen(pen("#42a5f5", 1.2))
        p.drawPath(self._air)
        p.setPen(pen("#ffa726", 1.5))
        p.drawPath(self._deep)
        p.end()
        self._cache, self._cache_key = pm, key
        return pm

    def paintEvent(self, e) -> None:  # noqa: N802
        p = QPainter(self)
        p.drawPixmap(0, 0, self._static_layer())
        p.setRenderHint(QPainter.Antialiasing)
        p.save()
        self._world(p)
        pen = self._pen
        p.setPen(pen("#66bb6a", 2.5))
        p.drawPath(self._done)
        p.setPen(pen("#ffee58", 3.5))
        p.drawPath(self._sel)
        p.restore()

        o = self._to_screen(0, 0)
        p.setPen(QPen(QColor("#ef5350"), 2))
        p.drawLine(o, o + QPointF(30, 0))
        p.setPen(QPen(QColor("#66bb6a"), 2))
        p.drawLine(o, o + QPointF(0, -30))
        p.setPen(QColor("#bdbdbd"))
        p.drawText(o + QPointF(33, 4), "X")
        p.drawText(o + QPointF(-4, -33), "Y")

        if self.hm is not None:
            self._draw_heightmap(p)

        if self.marker is not None:
            m = self._to_screen(*self.marker)
            p.setPen(QPen(QColor("#e040fb"), 3))
            p.drawLine(m + QPointF(-7, -7), m + QPointF(7, 7))
            p.drawLine(m + QPointF(-7, 7), m + QPointF(7, -7))

        t = self._to_screen(*self.tool)
        p.setPen(QPen(QColor("#ff5252"), 2))
        p.drawLine(t + QPointF(-8, 0), t + QPointF(8, 0))
        p.drawLine(t + QPointF(0, -8), t + QPointF(0, 8))
        p.drawEllipse(t, 4, 4)

        p.setPen(QColor("#bdbdbd"))
        if self._bounds is not None:
            b = self._bounds
            p.drawText(8, 16, f"Job: {b.width():.2f} × {b.height():.2f} mm")
        if self._cursor_mm is not None:
            c = self._cursor_mm
            p.drawText(8, self.height() - 8, f"X {c.x():.2f}  Y {c.y():.2f}")
        p.drawText(self.width() - 300, self.height() - 8,
                   "wheel: zoom · drag: pan · click: select · dbl-click: fit")
        p.end()

    def _draw_heightmap(self, p: QPainter) -> None:
        hm = self.hm
        z = hm.z[np.isfinite(hm.z)]
        span = max(float(np.abs(z).max()) if len(z) else 0.0, 0.01)
        labels = self._scale * float(np.diff(hm.xs).min() if len(hm.xs) > 1 else 10) > 45
        for j, y in enumerate(hm.ys):
            for i, x in enumerate(hm.xs):
                s = self._to_screen(float(x), float(y))
                v = hm.z[j, i]
                if not np.isfinite(v):
                    p.setPen(QPen(QColor("#9e9e9e"), 1.5))
                    p.setBrush(Qt.NoBrush)
                    p.drawEllipse(s, 4, 4)
                    continue
                t = max(-1.0, min(1.0, v / span))  # blue low -> white -> red high
                c = (QColor.fromRgbF(1, 1 - t, 1 - t) if t >= 0
                     else QColor.fromRgbF(1 + t, 1 + t, 1))
                p.setPen(QPen(QColor("#000000"), 1))
                p.setBrush(c)
                p.drawEllipse(s, 5, 5)
                if labels:
                    p.setPen(QColor("#e0e0e0"))
                    p.drawText(s + QPointF(7, -6), f"{v:+.3f}")
        p.setBrush(Qt.NoBrush)

    def _draw_grid(self, p: QPainter, pen) -> None:
        if self._scale < 2:
            return
        tl = self._to_mm(QPointF(0, 0))
        br = self._to_mm(QPointF(self.width(), self.height()))
        step = 10.0 if self._scale < 20 else 1.0
        p.setPen(pen("#2e2e2e", 1))
        x = (tl.x() // step) * step
        while x <= br.x():
            p.drawLine(QPointF(x, br.y()), QPointF(x, tl.y()))
            x += step
        y = (br.y() // step) * step
        while y <= tl.y():
            p.drawLine(QPointF(tl.x(), y), QPointF(br.x(), y))
            y += step
