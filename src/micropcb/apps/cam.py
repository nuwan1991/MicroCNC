"""MicroPCB CAM - Gerber / drill files to G-code, no machine needed.

Split design phase 2 (docs/split-design.md). Reuses the PCB and CAM panels of
the all-in-one window. Must not import the machine side (serial_link,
controller, camera, autolevel) - tests/test_imports.py checks it.
"""
from __future__ import annotations

import os
import sys
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (QApplication, QComboBox, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QSplitter, QTabWidget, QVBoxLayout,
                               QWidget)

from .. import __version__
from ..gcode import GCodeError, parse
from ..pcb import cam as pcbcam
from ..planner import Planner
from ..settings import Settings
from ..toolpath import ERROR, Timeline, Toolpath, check_program
from ..ui.cam_panel import CamPanel
from ..ui.gcode_view import GCodeView
from ..ui.pcb_panel import PcbPanel
from ..ui.pcb_view import PcbView
from ..ui.preview import PreviewWidget

APP = "MicroPCB CAM"


def _hms(sec: float) -> str:
    sec = int(sec + 0.5)
    if sec >= 3600:
        return f"{sec // 3600}:{sec // 60 % 60:02d}:{sec % 60:02d}"
    return f"{sec // 60}:{sec % 60:02d}"


def _scroll(w: QWidget) -> QScrollArea:
    s = QScrollArea()
    s.setWidget(w)
    s.setWidgetResizable(True)
    s.setFrameShape(QScrollArea.NoFrame)
    s.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    return s


class CamWindow(QMainWindow):
    """Gerber in, G-code (+ side files) out."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP} {__version__}")
        self.resize(1280, 800)
        self.settings = Settings.load()
        self.job_text: str | None = None
        self.job_path: str | None = None
        self.job_meta = None
        self.tp = None
        self.issues = []
        self.ops = []            # last generated operations (for "Show")
        self._ops_meta = None    # (title, safe_z, name)

        # ---- views (right)
        self.view_tabs = QTabWidget()
        self.pcb_view = PcbView()
        self.view_tabs.addTab(self.pcb_view, "Board")
        self.preview = PreviewWidget()
        self.preview.lineClicked.connect(self.select_line)
        self.view_tabs.addTab(self.preview, "Toolpath")

        self.bottom_tabs = QTabWidget()
        self.console = QPlainTextEdit(readOnly=True)
        self.console.setMaximumBlockCount(5000)
        self.bottom_tabs.addTab(self.console, "Console")
        self.gcode_view = GCodeView()
        self.gcode_view.lineClicked.connect(lambda ln: self.select_line(ln, from_text=True))
        self.bottom_tabs.addTab(self.gcode_view, "G-code")
        self.checks_list = QListWidget()
        self.checks_list.itemClicked.connect(self._check_clicked)
        self.bottom_tabs.addTab(self.checks_list, "Checks")

        # "Show": all operations or one (per tool / side layer)
        views = QWidget()
        vl = QVBoxLayout(views)
        vl.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Show:"))
        self.show_op = QComboBox()
        self.show_op.setMinimumWidth(320)
        self.show_op.addItem("All operations")
        self.show_op.setEnabled(False)
        self.show_op.setToolTip("Toolpath + G-code of one operation only (viewing - "
                                "Save / Export always use the whole job)")
        self.show_op.currentIndexChanged.connect(self._show_op)
        bar.addWidget(self.show_op)
        bar.addStretch(1)
        clear = QPushButton("Clear console")
        clear.clicked.connect(self.console.clear)
        bar.addWidget(clear)
        vl.addLayout(bar)
        vl.addWidget(self.view_tabs, 1)

        right = QSplitter(Qt.Vertical)
        right.addWidget(views)
        right.addWidget(self.bottom_tabs)
        right.setStretchFactor(0, 3)
        right.setStretchFactor(1, 1)

        # ---- panels (left): 1. board  2. operations
        self.pcb = PcbPanel(self, self.pcb_view)
        self.cam = CamPanel(self)
        self.left_tabs = QTabWidget()
        self.left_tabs.addTab(_scroll(self.pcb), "1. Board")
        self.left_tabs.addTab(_scroll(self.cam), "2. Operations")
        self.left_tabs.setMinimumWidth(380)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.left_tabs)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([420, 860])
        self.setCentralWidget(split)

        self.info_lbl = QLabel("Open Gerber / drill files or a ZIP to start.")
        self.statusBar().addWidget(self.info_lbl, 1)
        self.statusBar().addPermanentWidget(
            QLabel("No machine needed - run the files with MicroPCB Control"))
        self._menus()
        self.log(f"{APP} {__version__}. Steps: 1. Board - Open files, "
                 "2. Operations - Generate, then Save G-code or Export per tool.")

    # ------------------------------------------------------------------ menus
    def _menus(self) -> None:
        m = self.menuBar().addMenu("&File")
        for text, fn, key in (("Open Gerber / drill files…", self.pcb.open_files, "Ctrl+O"),
                              ("Open folder…", self.pcb.open_folder, "Ctrl+Shift+O"),
                              ("Save G-code…", self.cam.save, "Ctrl+S"),
                              ("Export per tool…", self.cam.export_per_tool, "Ctrl+E")):
            a = QAction(text, self)
            a.setShortcut(key)
            a.triggered.connect(fn)
            m.addAction(a)
        m.addSeparator()
        q = QAction("Quit", self)
        q.setShortcut("Ctrl+Q")
        q.triggered.connect(self.close)
        m.addAction(q)
        h = self.menuBar().addMenu("&Help")
        a = QAction("About", self)
        a.triggered.connect(lambda: QMessageBox.about(
            self, APP, f"{APP} {__version__}\nGerber / Excellon to G-code for PCB milling.\n"
                       "Run the files with MicroPCB Control."))
        h.addAction(a)

    # ------------------------------------------------------------------ used by the panels
    def log(self, text: str, record: bool = True) -> None:
        self.console.appendPlainText(time.strftime("%H:%M:%S ") + text)

    def job_name(self) -> str:
        return os.path.basename(self.job_path or "job.nc")

    def set_job_info(self, info, path) -> None:
        self.job_meta = info

    def load_job_text(self, text: str, path: str) -> None:
        """Show G-code made by the CAM panel: toolpath, time, CAM checks."""
        try:
            prog = parse(text, (0.0, 0.0, 0.0))
        except (GCodeError, ValueError) as e:
            QMessageBox.warning(self, APP, f"Cannot show the G-code:\n{e}")
            return
        self._set_job(text, path, prog)

    def _set_job(self, text: str, path: str, prog) -> None:
        self.job_text, self.job_path, self.job_meta = text, path, None
        self.tp = Toolpath(prog)
        s = self.settings
        try:
            pl = Planner(prog, s.mm_per_step, (0, 0, 0), [0, 0, 0], s.max_steps_per_tick)
            max_feed = pl.max_feed_mm_min
            secs = Timeline.build(pl).seconds
        except Exception as e:      # noqa: BLE001 - show, do not crash
            self.log(f"⚠ planner: {e}")
            secs, max_feed = 0.0, None
        # CAM part of the checks; machine / height-map checks are done in Control
        self.issues = check_program(self.tp, max_depth_mm=s.check_max_depth_mm,
                                    max_feed_mm_min=max_feed)
        self._fill_checks()
        self.gcode_view.set_text(text)
        self.gcode_view.set_issues((i.line, i.severity) for i in self.issues)
        self.preview.set_toolpath(self.tp)
        self.preview.set_done(None, 0)
        self.preview.set_marker(None)
        n_err = sum(1 for i in self.issues if i.severity == ERROR)
        self.info_lbl.setText(f"{os.path.basename(path)}: {len(prog.lines())} moves, "
                              f"about {_hms(secs)} on the machine"
                              + (f" - {n_err} problem(s), see Checks" if n_err else ""))
        self.log(f"G-code ready: {len(prog.lines())} moves, about {_hms(secs)}")
        self.view_tabs.setCurrentWidget(self.preview)

    # ------------------------------------------------------------------ views
    def _spots(self):
        """Places the isolation tool cannot separate (CAM panel), for the shown operation."""
        spots = getattr(self.cam, "problems", [])
        i = self.show_op.currentIndex()
        if i > 0 and i - 1 < len(self.ops):
            spots = [p for p in spots if p[0] == self.ops[i - 1].name]
        return spots

    def _fill_checks(self) -> None:
        self.checks_list.clear()
        spots = self._spots()
        for name, x, y, size in spots:
            it = QListWidgetItem(f"⚠  {name}: too narrow at X {x:.2f} Y {y:.2f} "
                                 "- copper may stay connected (click to zoom)")
            it.setData(Qt.UserRole, ("xy", x, y, size))
            it.setForeground(QColor("#e65100"))
            self.checks_list.addItem(it)
        n = len(self.issues) + len(spots)
        if not n:
            it = QListWidgetItem("✓ No problems found")
            it.setForeground(QColor("#2e7d32"))
            self.checks_list.addItem(it)
            self.bottom_tabs.setTabText(2, "Checks")
            return
        self.bottom_tabs.setTabText(2, f"Checks ({n})")
        for i in self.issues:
            icon = "✖" if i.severity == ERROR else "⚠"
            it = QListWidgetItem(f"{icon}  line {i.line}: {i.text}")
            it.setData(Qt.UserRole, i.line)
            it.setForeground(QColor("#c62828" if i.severity == ERROR else "#e65100"))
            self.checks_list.addItem(it)

    def _check_clicked(self, it) -> None:
        d = it.data(Qt.UserRole)
        if isinstance(d, (tuple, list)) and d and d[0] == "xy":
            self.view_tabs.setCurrentWidget(self.pcb_view)
            self.pcb_view.zoom_to(d[1], d[2], max(3.0, d[3] * 4))
        else:
            self.select_line(d)

    def set_ops(self, ops, title: str, safe_z: float, name: str) -> None:
        """Called by the CAM panel after Generate: fill the "Show" list."""
        self.ops = [o for o in ops if not o.empty]
        self._ops_meta = (title, safe_z, name)
        self.show_op.blockSignals(True)
        self.show_op.clear()
        self.show_op.addItem(f"All operations ({title})")
        for k, o in enumerate(self.ops, 1):
            self.show_op.addItem(f"{k}. {o.name} - {o.tool.label}")
        self.show_op.setCurrentIndex(0)
        self.show_op.setEnabled(len(self.ops) > 1)
        self.show_op.blockSignals(False)
        self._full = (self.job_text, self.job_path)

    def _show_op(self, i: int) -> None:
        if not self._ops_meta:
            return
        title, safe, name = self._ops_meta
        if i <= 0:
            text, path = self._full
        else:
            o = self.ops[i - 1]
            text = pcbcam.combine([o], f"{title}: {o.name}", safe)
            path = name
        meta = self.job_meta
        self.load_job_text(text, path)
        self.job_meta = meta
        self.view_tabs.setCurrentWidget(self.preview)

    def select_line(self, line: int, from_text: bool = False) -> None:
        if not line or self.tp is None:
            return
        self.preview.set_selected(self.tp.segments_for_line(line))
        if not from_text:
            self.gcode_view.set_selected(line)

    def closeEvent(self, e) -> None:  # noqa: N802
        self.settings.save()
        super().closeEvent(e)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP)
    w = CamWindow()
    w.show()
    files = [p for p in sys.argv[1:] if os.path.exists(p)]
    if files:
        w.pcb.load(files)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
