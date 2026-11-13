"""2D toolpath preview (top view, work coordinates, Y up).

- cutting moves: blue, rapids: grey dashed, already done: green
- red cross: tool position, small axes: work zero
- dotted rectangle: machine work area (when machine home is set)

Wheel = zoom at cursor, drag = pan, double-click = fit.
"""
from __future__ import annotations

import bisect

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from ..gcode import Program


class PreviewWidget(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(300, 250)
        self.setMouseTracking(True)
        self._segs: list[tuple[float, float, float, float, bool, int]] = []
        self._seg_lines: list[int] = []
        self._cut = QPainterPath()
        self._rapid = QPainterPath()
        self._done = QPainterPath()
        self._done_idx = 0
        self._bounds: QRectF | None = None
        self.tool = (0.0, 0.0)
        self.area: QRectF | None = None        # machine work area in work coords
        self._scale = 1.0
        self._center = QPointF(0, 0)
        self._drag: QPointF | None = None
        self._cursor_mm: QPointF | None = None
        self.fit()

    # ------------------------------------------------------------ data
    def set_program(self, prog: Program | None) -> None:
        self._segs = []
        self._cut = QPainterPath()
        self._rapid = QPainterPath()
        if prog is not None:
            last_cut = last_rapid = None
            for m in prog.lines():
                x0, y0, _ = m.start
                x1, y1, _ = m.end
                self._segs.append((x0, y0, x1, y1, m.rapid, m.line))
                path, last = ((self._rapid, last_rapid) if m.rapid else (self._cut, last_cut))
                if last != (x0, y0):
                    path.moveTo(x0, y0)
                path.lineTo(x1, y1)
                if m.rapid:
                    last_rapid = (x1, y1)
                else:
                    last_cut = (x1, y1)
        self._seg_lines = [s[5] for s in self._segs]
        b = prog.bounds() if prog else None
        self._bounds = QRectF(QPointF(b[0][0], b[0][1]), QPointF(b[1][0], b[1][1])) if b else None
        self.reset_progress()
        self.fit()

    def reset_progress(self) -> None:
        self._done = QPainterPath()
        self._done_idx = 0
        self.update()

    def set_progress_line(self, line: int) -> None:
        """Mark all segments up to (and excluding) this source line as done."""
        end = bisect.bisect_left(self._seg_lines, line)
        if end < self._done_idx:
            self.reset_progress()
        for x0, y0, x1, y1, rapid, _ in self._segs[self._done_idx:end]:
            if rapid:
                continue
            if self._done.elementCount() == 0 or self._done.currentPosition() != QPointF(x0, y0):
                self._done.moveTo(x0, y0)
            self._done.lineTo(x1, y1)
        if end != self._done_idx:
            self._done_idx = end
            self.update()

    def set_tool(self, x: float, y: float) -> None:
        if (x, y) != self.tool:
            self.tool = (x, y)
            self.update()

    def set_area(self, rect: QRectF | None) -> None:
        if rect != self.area:
            self.area = rect
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
        after = self._to_mm(pos)
        self._center += before - after
        self.update()

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton:
            self._drag = e.position()

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if self._drag is not None:
            d = e.position() - self._drag
            self._center -= QPointF(d.x() / self._scale, -d.y() / self._scale)
            self._drag = e.position()
        self._cursor_mm = self._to_mm(e.position())
        self.update()

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        self._drag = None

    def mouseDoubleClickEvent(self, e) -> None:  # noqa: N802
        self.fit()

    def leaveEvent(self, e) -> None:  # noqa: N802
        self._cursor_mm = None
        self.update()

    # ------------------------------------------------------------ paint
    def paintEvent(self, e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("#1e1e1e"))

        # world transform: mm -> screen, Y up
        p.save()
        p.translate(self.width() / 2, self.height() / 2)
        p.scale(self._scale, -self._scale)
        p.translate(-self._center.x(), -self._center.y())

        def pen(color: str, width: float = 1.0, style=Qt.SolidLine) -> QPen:
            q = QPen(QColor(color), width, style)
            q.setCosmetic(True)
            return q

        if self.area is not None:
            p.setPen(pen("#9e9e9e", 1, Qt.DotLine))
            p.drawRect(self.area)
        self._draw_grid(p, pen)
        p.setPen(pen("#757575", 1, Qt.DashLine))
        p.drawPath(self._rapid)
        p.setPen(pen("#42a5f5", 1.5))
        p.drawPath(self._cut)
        p.setPen(pen("#66bb6a", 2.5))
        p.drawPath(self._done)
        p.restore()

        # work zero axes (screen space)
        o = self._to_screen(0, 0)
        p.setPen(QPen(QColor("#ef5350"), 2))
        p.drawLine(o, o + QPointF(30, 0))
        p.setPen(QPen(QColor("#66bb6a"), 2))
        p.drawLine(o, o + QPointF(0, -30))
        p.setPen(QColor("#bdbdbd"))
        p.drawText(o + QPointF(33, 4), "X")
        p.drawText(o + QPointF(-4, -33), "Y")

        # tool
        t = self._to_screen(*self.tool)
        p.setPen(QPen(QColor("#ff5252"), 2))
        p.drawLine(t + QPointF(-8, 0), t + QPointF(8, 0))
        p.drawLine(t + QPointF(0, -8), t + QPointF(0, 8))
        p.drawEllipse(t, 4, 4)

        # info text
        p.setPen(QColor("#bdbdbd"))
        y = 16
        if self._bounds is not None:
            b = self._bounds
            p.drawText(8, y, f"Job: {b.width():.2f} × {b.height():.2f} mm  "
                             f"(X {b.left():.2f}…{b.right():.2f}, Y {b.top():.2f}…{b.bottom():.2f})")
            y += 16
        if self._cursor_mm is not None:
            c = self._cursor_mm
            p.drawText(8, self.height() - 8, f"X {c.x():.2f}  Y {c.y():.2f}")
        p.drawText(self.width() - 230, self.height() - 8, "wheel: zoom · drag: pan · dbl-click: fit")
        p.end()

    def _draw_grid(self, p: QPainter, pen) -> None:
        # 10 mm grid when zoomed in enough, else nothing
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
