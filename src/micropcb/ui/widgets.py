"""Small painted widgets for the MicroCNC window: switch, lamp, bracket, section."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QAbstractButton, QFrame, QHBoxLayout, QLabel, QVBoxLayout,
                               QWidget)

from . import theme


class ToggleSwitch(QAbstractButton):
    """On / off slide switch. The switch is the action; the state is shown
    next to it (lamp + word), so the button text never has to mean both."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(46, 24)

    def paintEvent(self, e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(1, 1, 44, 22)
        on = self.isChecked()
        bg = QColor(theme.OK if on else "#3a434d")
        if not self.isEnabled():
            bg = QColor("#2a3138")
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(r, 11, 11)
        p.setBrush(QColor("#cfd6dd" if self.isEnabled() else "#5b6672"))
        x = 25 if on else 4
        p.drawEllipse(QRectF(x, 4, 16, 16))
        p.end()


class Lamp(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.on = False
        self.color = theme.OK
        self.setFixedSize(16, 16)

    def set_on(self, on: bool, color: str | None = None) -> None:
        if on != self.on or (color and color != self.color):
            self.on = on
            if color:
                self.color = color
            self.update()

    def paintEvent(self, e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.on:
            glow = QColor(self.color)
            glow.setAlpha(80)
            p.setPen(Qt.NoPen)
            p.setBrush(glow)
            p.drawEllipse(QRectF(0, 0, 16, 16))
            p.setBrush(QColor(self.color))
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#4a535d"))
        p.drawEllipse(QRectF(3, 3, 10, 10))
        p.end()


class Bracket(QWidget):
    """A ] line joining the three axis zero buttons to the Zero all button."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedWidth(14)
        self.inset = 14          # px from top / bottom = middle of the first / last row

    def paintEvent(self, e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(theme.ACCENT), 1.5))
        t, b = self.inset, self.height() - self.inset
        x = 7
        p.drawLine(QPointF(0, t), QPointF(x, t))
        p.drawLine(QPointF(x, t), QPointF(x, b))
        p.drawLine(QPointF(0, b), QPointF(x, b))
        p.drawLine(QPointF(0, self.height() / 2), QPointF(x, self.height() / 2))
        p.drawLine(QPointF(x, self.height() / 2), QPointF(self.width(), self.height() / 2))
        p.end()


class Section(QFrame):
    """Left-column block: small caps title (+ optional right-hand widget) over a body."""

    def __init__(self, title: str, right: QWidget | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("section")
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 7, 10, 8)
        v.setSpacing(5)
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel(title.upper())
        self.title.setObjectName("sectionTitle")
        h.addWidget(self.title)
        h.addStretch(1)
        if right is not None:
            h.addWidget(right)
        v.addLayout(h)
        self.body = v


def muted(text: str = "") -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("muted")
    return lab
