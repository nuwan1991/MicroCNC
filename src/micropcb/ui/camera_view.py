"""Live camera picture with overlays and click / drag input (Stage 4.1, 4.5)."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget


class CameraView(QWidget):
    clicked = Signal(float, float)            # image pixels
    rectDrawn = Signal(float, float, float, float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(320, 160)
        self.setMouseTracking(True)
        self.image: QImage | None = None
        self._buf = None
        self.zoom = 1.0
        self.center: QPointF | None = None      # image px at widget centre
        self.overlays: list[tuple[QPainterPath, QColor, bool]] = []
        self.marks: list[tuple[float, float, str]] = []   # px, label
        self.mode = ""                         # "" | "click" | "rect"
        self.hint = ""
        self.cursor_text = None                # fn(px, py) -> str
        self._cursor: QPointF | None = None
        self._drag: QPointF | None = None
        self._pan: QPointF | None = None
        self._rect_end: QPointF | None = None

    # ------------------------------------------------------------ data
    def set_frame(self, frame: np.ndarray | None) -> None:
        if frame is None:
            self.image = None
        else:
            rgb = np.ascontiguousarray(frame[:, :, ::-1])
            h, w, _ = rgb.shape
            self._buf = rgb
            self.image = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888)
        self.update()

    def set_overlays(self, overlays) -> None:
        self.overlays = overlays
        self.update()

    # ------------------------------------------------------------ geometry
    def _scale(self) -> float:
        if self.image is None:
            return 1.0
        fit = min(self.width() / self.image.width(), self.height() / self.image.height())
        return fit * self.zoom

    def _c(self) -> QPointF:
        if self.center is None and self.image is not None:
            return QPointF(self.image.width() / 2, self.image.height() / 2)
        return self.center or QPointF(0, 0)

    def to_img(self, p: QPointF) -> QPointF:
        s = self._scale()
        c = self._c()
        return QPointF(c.x() + (p.x() - self.width() / 2) / s,
                       c.y() + (p.y() - self.height() / 2) / s)

    # ------------------------------------------------------------ paint
    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#202326"))
        if self.image is None:
            p.setPen(QColor("#9aa0a6"))
            p.drawText(self.rect(), Qt.AlignCenter, "Camera off - choose a camera on the right "
                                                    "and press Start")
            return
        s = self._scale()
        c = self._c()
        p.translate(self.width() / 2, self.height() / 2)
        p.scale(s, s)
        p.translate(-c.x(), -c.y())
        p.setRenderHint(QPainter.SmoothPixmapTransform, s < 2)
        p.drawImage(0, 0, self.image)
        p.setRenderHint(QPainter.Antialiasing)
        for path, color, filled in self.overlays:
            pen = QPen(color, 1.5 / s)
            p.setPen(pen)
            p.setBrush(QColor(color.red(), color.green(), color.blue(), 70) if filled
                       else Qt.NoBrush)
            p.drawPath(path)
        f = QFont()
        f.setPointSizeF(max(1.0, 9 / s))
        p.setFont(f)
        for x, y, label in self.marks:
            p.setPen(QPen(QColor("#00e5ff"), 2 / s))
            p.setBrush(Qt.NoBrush)
            r = 7 / s
            p.drawEllipse(QPointF(x, y), r, r)
            p.drawText(QPointF(x + r * 1.3, y - r * 1.3), label)
        if self._drag is not None and self._rect_end is not None:
            p.setPen(QPen(QColor("#ff5252"), 2 / s, Qt.DashLine))
            p.drawRect(QRectF(self._drag, self._rect_end).normalized())
        p.resetTransform()
        if self._cursor is not None and self.mode:
            q = self._cursor
            p.setPen(QPen(QColor(255, 255, 0, 200), 1))
            p.drawLine(QPointF(q.x() - 14, q.y()), QPointF(q.x() + 14, q.y()))
            p.drawLine(QPointF(q.x(), q.y() - 14), QPointF(q.x(), q.y() + 14))
        p.setPen(QColor("#e8eaed"))
        lines = []
        if self.hint:
            lines.append(self.hint)
        if self._cursor is not None and self.cursor_text:
            ip = self.to_img(self._cursor)
            t = self.cursor_text(ip.x(), ip.y())
            if t:
                lines.append(t)
        for k, t in enumerate(lines):
            p.fillRect(QRectF(6, 6 + k * 20, p.fontMetrics().horizontalAdvance(t) + 10, 18),
                       QColor(0, 0, 0, 150))
            p.drawText(QPointF(11, 19 + k * 20), t)

    # ------------------------------------------------------------ input
    def wheelEvent(self, e) -> None:  # noqa: N802
        if self.image is None:
            return
        before = self.to_img(e.position())
        self.zoom = float(np.clip(self.zoom * (1.25 if e.angleDelta().y() > 0 else 0.8), 1, 30))
        after = self.to_img(e.position())
        c = self._c()
        self.center = QPointF(c.x() + before.x() - after.x(), c.y() + before.y() - after.y())
        self.update()

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if self.image is None:
            return
        if e.button() in (Qt.RightButton, Qt.MiddleButton) or not self.mode:
            self._pan = e.position()
            return
        if e.button() == Qt.LeftButton:
            if self.mode == "rect":
                self._drag = self.to_img(e.position())
                self._rect_end = self._drag
            else:
                q = self.to_img(e.position())
                self.clicked.emit(q.x(), q.y())

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        self._cursor = e.position()
        if self._pan is not None:
            s = self._scale()
            d = e.position() - self._pan
            self._pan = e.position()
            c = self._c()
            self.center = QPointF(c.x() - d.x() / s, c.y() - d.y() / s)
        elif self._drag is not None:
            self._rect_end = self.to_img(e.position())
        self.update()

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if self._drag is not None and self._rect_end is not None:
            r = QRectF(self._drag, self._rect_end).normalized()
            if r.width() > 3 and r.height() > 3:
                self.rectDrawn.emit(r.left(), r.top(), r.right(), r.bottom())
        self._drag = self._rect_end = self._pan = None
        self.update()

    def mouseDoubleClickEvent(self, e) -> None:  # noqa: N802
        if not self.mode:
            self.zoom, self.center = 1.0, None
            self.update()

    def leaveEvent(self, _e) -> None:  # noqa: N802
        self._cursor = None
        self.update()
