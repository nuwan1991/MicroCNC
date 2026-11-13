"""Read-only G-code viewer with line numbers (Stage 2.2).

- click a line -> lineClicked(line) (1-based)
- current job / playback line: green background, kept in view
- selected line: yellow; lines with problems: red / orange tint
"""
from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QTextCursor, QTextFormat
from PySide6.QtWidgets import QPlainTextEdit, QTextEdit, QWidget

CURRENT = QColor("#c8e6c9")
SELECTED = QColor("#fff59d")
ERROR = QColor("#ffcdd2")
WARNING = QColor("#ffe0b2")


class _Gutter(QWidget):
    def __init__(self, view: "GCodeView") -> None:
        super().__init__(view)
        self.view = view

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.view.gutter_width(), 0)

    def paintEvent(self, e) -> None:  # noqa: N802
        self.view.paint_gutter(e)


class GCodeView(QPlainTextEdit):
    lineClicked = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setFont(QFont("Consolas", 9))
        self.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self._gutter = _Gutter(self)
        self._current = 0
        self._selected = 0
        self._issues: dict[int, str] = {}
        self.blockCountChanged.connect(lambda _: self._update_margin())
        self.updateRequest.connect(self._on_update_request)
        self._update_margin()

    # ------------------------------------------------------------ api
    def set_text(self, text: str) -> None:
        self._current = self._selected = 0
        self._issues = {}
        self.setPlainText(text)
        self._apply()

    def set_issues(self, issues) -> None:
        """issues: iterable of (line, severity)."""
        self._issues = {}
        for line, sev in issues:
            if self._issues.get(line) != "error":
                self._issues[line] = sev
        self._apply()

    def set_current(self, line: int) -> None:
        if line != self._current:
            self._current = line
            self._apply()
            if line:
                self._scroll_to(line)

    def set_selected(self, line: int, scroll: bool = True) -> None:
        self._selected = line
        self._apply()
        if line and scroll:
            self._scroll_to(line)

    # ------------------------------------------------------------ internals
    def _scroll_to(self, line: int) -> None:
        block = self.document().findBlockByNumber(line - 1)
        if not block.isValid():
            return
        c = QTextCursor(block)
        self.setTextCursor(c)
        self.centerCursor()

    def _apply(self) -> None:
        sels = []

        def add(line: int, color: QColor) -> None:
            block = self.document().findBlockByNumber(line - 1)
            if not block.isValid():
                return
            s = QTextEdit.ExtraSelection()
            s.format.setBackground(color)
            s.format.setProperty(QTextFormat.FullWidthSelection, True)
            s.cursor = QTextCursor(block)
            sels.append(s)

        if len(self._issues) < 2000:
            for line, sev in self._issues.items():
                add(line, ERROR if sev == "error" else WARNING)
        if self._selected:
            add(self._selected, SELECTED)
        if self._current:
            add(self._current, CURRENT)
        self.setExtraSelections(sels)

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        super().mouseReleaseEvent(e)
        if e.button() == Qt.LeftButton and not self.textCursor().hasSelection():
            line = self.cursorForPosition(e.position().toPoint()).blockNumber() + 1
            self._selected = line
            self._apply()
            self.lineClicked.emit(line)

    def gutter_width(self) -> int:
        digits = len(str(max(1, self.blockCount())))
        return 14 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_margin(self) -> None:
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def _on_update_request(self, rect, dy) -> None:
        if dy:
            self._gutter.scroll(0, dy)
        else:
            self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        cr = self.contentsRect()
        self._gutter.setGeometry(QRect(cr.left(), cr.top(), self.gutter_width(), cr.height()))

    def paint_gutter(self, e) -> None:
        p = QPainter(self._gutter)
        p.fillRect(e.rect(), QColor("#eeeeee"))
        block = self.firstVisibleBlock()
        n = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        w = self._gutter.width() - 5
        h = self.fontMetrics().height()
        while block.isValid() and top <= e.rect().bottom():
            if block.isVisible() and bottom >= e.rect().top():
                line = n + 1
                sev = self._issues.get(line)
                if sev:
                    p.fillRect(0, top, 4, h, QColor("#e53935" if sev == "error" else "#fb8c00"))
                p.setPen(QColor("#2e7d32") if line == self._current else QColor("#9e9e9e"))
                p.drawText(0, top, w, h, Qt.AlignRight, str(line))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            n += 1
