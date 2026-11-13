"""PCB layer view (Stage 3.1): placed board in work coordinates, Y up.

Layers are drawn from shapely polygons converted once to QPainterPaths and
cached as a pixmap until the view or the board changes.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from ..pcb.board import PlacedBoard, as_polygons

COLORS = {
    "top": QColor(230, 90, 40, 200),
    "bottom": QColor(60, 140, 240, 200),
    "outline": QColor("#ffd54f"),
    "holes": QColor("#f5f5f5"),
    "other": QColor(235, 235, 235, 170),
}


def poly_path(geom) -> QPainterPath:
    path = QPainterPath()
    path.setFillRule(Qt.OddEvenFill)
    for poly in as_polygons(geom):
        for ring in [poly.exterior, *poly.interiors]:
            pts = list(ring.coords)
            if len(pts) < 3:
                continue
            path.moveTo(*pts[0])
            for x, y in pts[1:]:
                path.lineTo(x, y)
            path.closeSubpath()
    return path


class PcbView(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(300, 250)
        self.setMouseTracking(True)
        self.board: PlacedBoard | None = None
        self.visible: dict[str, bool] = {}
        self.paths: dict[str, QPainterPath] = {}
        self.area = (140.0, 200.0)
        self._scale = 1.0
        self._center = QPointF(0, 0)
        self._drag: QPointF | None = None
        self._cursor: QPointF | None = None
        self._cache: QPixmap | None = None
        self._key = None
        self._problem = QPainterPath()

    # ------------------------------------------------------------ data
    def set_board(self, pb: PlacedBoard | None, visible: dict[str, bool]) -> None:
        refit = self.board is None or pb is None or pb.size != self.board.size
        self.board = pb
        self.visible = dict(visible)
        self.paths = {}
        if pb is not None:
            for side in ("top", "bottom"):
                self.paths[side] = poly_path(pb.copper.get(side))
            self.paths["outline"] = poly_path(pb.outline)
            holes = QPainterPath()
            for h in pb.holes:
                holes.addEllipse(QPointF(h.x, h.y), h.d / 2, h.d / 2)
            for s in pb.slots:
                from shapely.geometry import LineString
                holes.addPath(poly_path(LineString([(s.x1, s.y1), (s.x2, s.y2)])
                                        .buffer(s.d / 2)))
            self.paths["holes"] = holes
            for name, g in pb.other:
                self.paths["other:" + name] = poly_path(g)
        self._cache = None
        if refit:
            self.fit()
        self.update()

    def set_problem(self, geom) -> None:
        """Red marks: places the isolation tool cannot separate."""
        self._problem = poly_path(geom.buffer(0.15)) if geom is not None else QPainterPath()
        self._cache = None
        self.update()

    def set_visible(self, visible: dict[str, bool]) -> None:
        self.visible = dict(visible)
        self._cache = None
        self.update()

    # ------------------------------------------------------------ view
    def fit(self) -> None:
        w, h = self.board.size if self.board else self.area
        r = QRectF(0, 0, max(w, 1), max(h, 1)).adjusted(-3, -3, 3, 3)
        self._center = r.center()
        self._scale = 0.92 * min(max(self.width(), 1) / r.width(),
                                 max(self.height(), 1) / r.height())
        self._cache = None
        self.update()

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        self.fit()

    def _to_mm(self, p: QPointF) -> QPointF:
        return QPointF(self._center.x() + (p.x() - self.width() / 2) / self._scale,
                       self._center.y() - (p.y() - self.height() / 2) / self._scale)

    def wheelEvent(self, e) -> None:  # noqa: N802
        pos = e.position()
        before = self._to_mm(pos)
        self._scale = max(0.05, min(self._scale * (1.25 if e.angleDelta().y() > 0 else 0.8), 5000))
        self._center += before - self._to_mm(pos)
        self._cache = None
        self.update()

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton:
            self._drag = e.position()

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if self._drag is not None:
            d = e.position() - self._drag
            self._center -= QPointF(d.x() / self._scale, -d.y() / self._scale)
            self._drag = e.position()
            self._cache = None
        self._cursor = self._to_mm(e.position())
        self.update()

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        self._drag = None

    def mouseDoubleClickEvent(self, e) -> None:  # noqa: N802
        self.fit()

    # ------------------------------------------------------------ paint
    def _world(self, p: QPainter) -> None:
        p.translate(self.width() / 2, self.height() / 2)
        p.scale(self._scale, -self._scale)
        p.translate(-self._center.x(), -self._center.y())

    def _layer_pixmap(self) -> QPixmap:
        key = (self.width(), self.height(), self._scale, self._center.x(), self._center.y())
        if self._cache is not None and self._key == key:
            return self._cache
        dpr = self.devicePixelRatioF()
        pm = QPixmap(int(self.width() * dpr), int(self.height() * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(QColor("#1b1b1b"))
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        self._world(p)

        def pen(c, w=1.0, style=Qt.SolidLine):
            q = QPen(c, w, style)
            q.setCosmetic(True)
            return q

        # machine work area (from work 0,0)
        p.setPen(pen(QColor("#616161"), 1, Qt.DashLine))
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(0, 0, *self.area))
        for side in ("top", "bottom"):
            if self.visible.get(side) and side in self.paths:
                p.setPen(Qt.NoPen)
                p.setBrush(COLORS[side])
                p.drawPath(self.paths[side])
        for name, path in self.paths.items():   # silk / mask … on top, like PCB tools
            if name.startswith("other:") and self.visible.get(name):
                p.setPen(Qt.NoPen)
                p.setBrush(COLORS["other"])
                p.drawPath(path)
        if self.visible.get("outline") and "outline" in self.paths:
            p.setPen(pen(COLORS["outline"], 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawPath(self.paths["outline"])
        if self.visible.get("drill") and "holes" in self.paths:
            p.setPen(pen(COLORS["holes"], 1))
            p.setBrush(QBrush(QColor(0, 0, 0, 200)))
            p.drawPath(self.paths["holes"])
        if not self._problem.isEmpty():
            p.setPen(pen(QColor("#ff1744"), 2))
            p.setBrush(QColor(255, 23, 68, 140))
            p.drawPath(self._problem)
        p.end()
        self._cache, self._key = pm, key
        return pm

    def paintEvent(self, e) -> None:  # noqa: N802
        p = QPainter(self)
        p.drawPixmap(0, 0, self._layer_pixmap())
        o = QPointF(self.width() / 2 + (0 - self._center.x()) * self._scale,
                    self.height() / 2 + (self._center.y()) * self._scale)
        p.setPen(QPen(QColor("#ef5350"), 2))
        p.drawLine(o, o + QPointF(30, 0))
        p.setPen(QPen(QColor("#66bb6a"), 2))
        p.drawLine(o, o + QPointF(0, -30))
        p.setPen(QColor("#bdbdbd"))
        if self.board is not None:
            w, h = self.board.size
            side = "bottom (mirrored)" if self.board.placement.mirror else "top"
            p.drawText(8, 16, f"Board {w:.2f} × {h:.2f} mm · seen from {side} · "
                              f"rotated {self.board.placement.rotation}° · dashed = machine area")
        else:
            p.drawText(8, 16, "No board loaded - PCB tab below → Open board")
        if self._cursor is not None:
            c = self._cursor
            p.drawText(8, self.height() - 8, f"X {c.x():.2f}  Y {c.y():.2f}")
        p.end()
