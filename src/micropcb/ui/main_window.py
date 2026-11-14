"""MicroPCB main window: connect, DRO, jog, zero, spindle, E-stop (1.2),
G-code job with 2D preview, run / pause / stop (1.3),
3D view, G-code viewer, job checks, playback, live job view (2.1-2.3)."""
from __future__ import annotations

import os
import re
import threading
import time

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QKeySequence
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox,
                               QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                               QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
                               QSlider, QVBoxLayout, QWidget, QAbstractSpinBox,
                               QLineEdit, QFileDialog, QProgressBar, QSplitter,
                               QTabWidget, QListWidget, QListWidgetItem, QScrollArea)

from .. import __version__
from ..controller import AXES, EV_JOB, EV_STATE, MachineController
from ..gcode import GCodeError, Program, parse
from ..job import DONE, FAILED, PAUSED_STATE, PAUSING, RUNNING, STOPPED, STOPPING
from ..protocol import TICK_SECONDS
from ..serial_link import (EV_CONNECTED, EV_DISCONNECTED, EV_LOST, EV_OVERLOAD,
                           EV_PROBE, EV_RELEASED, EV_TRAFFIC, list_ports, probe_port)
from ..settings import MachineState, Settings
from ..sim_machine import SimMachine
from ..toolpath import ERROR, Timeline, Toolpath, check_program
from ..autolevel import apply_heightmap
from .autolevel_panel import AutoLevelPanel
from .cam_panel import CamPanel
from .camera_panel import CameraPanel
from .camera_view import CameraView
from ..camera.align import apply_alignment
from .pcb_panel import PcbPanel
from .pcb_view import PcbView
from .gcode_view import GCodeView
from .preview import PreviewWidget
from .settings_dialog import SettingsDialog

SIM_LABEL = "Simulator (no machine)"
STEP_SIZES = (0.01, 0.1, 1.0, 10.0)

# keyboard jog: key -> (axis, direction)
JOG_KEYS = {
    Qt.Key_Right: ("x", +1), Qt.Key_Left: ("x", -1),
    Qt.Key_Up: ("y", +1), Qt.Key_Down: ("y", -1),
    Qt.Key_PageUp: ("z", +1), Qt.Key_PageDown: ("z", -1),
}


class Bridge(QObject):
    """Carries link events from the worker thread to the GUI thread."""

    event = Signal(str, object)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"MicroPCB {__version__}")
        self.settings = Settings.load()
        self.bridge = Bridge()
        self.bridge.event.connect(self._on_event, Qt.QueuedConnection)
        self.ctl = MachineController(self.settings, MachineState.load(),
                                     listener=lambda e, p: self.bridge.event.emit(e, p))
        self._held_key: int | None = None
        self._lost_port: str | None = None
        self._overload = False
        self.job_text: str | None = None
        self.job_path: str | None = None
        self.job_prog: Program | None = None
        self._job_secs = 0.0
        self.job_prog_raw: Program | None = None
        self.tp: Toolpath | None = None
        self.timeline: Timeline | None = None
        self.issues = []
        self.view3d = None
        self.play_idx = 0
        self.playing = False
        self._play_t = 0.0
        self._marker = None
        self._sel_line = 0

        self._build_ui()
        self._build_menu()
        self.refresh_ports()

        self.timer = QTimer(self, interval=100, timeout=self._refresh)
        self.timer.start()
        self.reconnect_timer = QTimer(self, interval=2000, timeout=self._try_reconnect)

        QApplication.instance().installEventFilter(self)
        self._refresh()
        if self.ctl.was_unclean:
            self.log("Last session did not close normally - position may be wrong.")

    # ================================================================ UI
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(6, 4, 6, 2)
        root.setSpacing(4)

        # --- top bar
        top = QHBoxLayout()
        self.port_box = QComboBox()
        self.port_box.setMinimumWidth(260)
        refresh = QPushButton("⟳")
        refresh.setToolTip("Refresh port list")
        refresh.setFixedWidth(32)
        refresh.clicked.connect(self.refresh_ports)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self.toggle_connect)
        self.status_lbl = QLabel()
        self.status_lbl.setStyleSheet("font-weight: bold; padding: 0 10px;")
        self.estop_btn = QPushButton("E-STOP  (Esc)")
        self.estop_btn.setMinimumSize(150, 34)
        self.estop_btn.setStyleSheet(
            "QPushButton { background:#c62828; color:white; font-size:15px;"
            " font-weight:bold; border-radius:6px; }"
            "QPushButton:pressed { background:#8e0000; }")
        self.estop_btn.setToolTip("Stop all motion and switch spindle off.\n"
                                  "The hardware E-stop cuts machine power.")
        self.estop_btn.clicked.connect(self.emergency_stop)
        for w in (QLabel("Port:"), self.port_box, refresh, self.connect_btn, self.status_lbl):
            top.addWidget(w)
        top.addStretch(1)
        top.addWidget(self.estop_btn)
        root.addLayout(top)

        # --- warning banner
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("background:#ffb300; color:black; padding:4px 8px;"
                                  " font-weight:bold; border-radius:4px;")
        self.banner.hide()
        root.addWidget(self.banner)

        body = QHBoxLayout()
        root.addLayout(body, 1)
        left = QVBoxLayout()
        body.addLayout(left, 0)

        # --- DRO
        dro = QGroupBox("Position (mm)")
        g = QGridLayout(dro)
        g.addWidget(QLabel("Work"), 0, 1, Qt.AlignRight)
        g.addWidget(QLabel("Machine"), 0, 2, Qt.AlignRight)
        big = QFont("Consolas", 22, QFont.Bold)
        small = QFont("Consolas", 11)
        self.work_lbl, self.mach_lbl = [], []
        for i, a in enumerate(AXES):
            name = QLabel(a.upper())
            name.setFont(big)
            w = QLabel("0.000")
            w.setFont(big)
            w.setMinimumWidth(170)
            w.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            m = QLabel("0.000")
            m.setFont(small)
            m.setMinimumWidth(90)
            m.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            z = QPushButton(f"Zero {a.upper()}")
            z.clicked.connect(lambda _=False, ax=a: self._do(lambda: self.ctl.zero_axes(ax)))
            g.addWidget(name, i + 1, 0)
            g.addWidget(w, i + 1, 1)
            g.addWidget(m, i + 1, 2)
            g.addWidget(z, i + 1, 3)
            self.work_lbl.append(w)
            self.mach_lbl.append(m)
        row = QHBoxLayout()
        self.zero_all_btn = QPushButton("Zero all")
        self.zero_all_btn.clicked.connect(lambda: self._do(self.ctl.zero_axes))
        self.goto_zero_btn = QPushButton("Go to work zero")
        self.goto_zero_btn.setToolTip("Raise Z to safe height, move X/Y to 0, lower Z to 0")
        self.goto_zero_btn.clicked.connect(lambda: self._do(self.ctl.go_to_work_zero))
        row.addWidget(self.zero_all_btn)
        row.addWidget(self.goto_zero_btn)
        g.addLayout(row, 4, 0, 1, 4)
        left.addWidget(dro)

        # --- Jog
        jog = QGroupBox("Jog")
        jl = QVBoxLayout(jog)
        steps = QHBoxLayout()
        steps.addWidget(QLabel("Step (mm):"))
        self.step_group = QButtonGroup(self)
        for k, s in enumerate(STEP_SIZES):
            b = QPushButton(f"{s:g}")
            b.setCheckable(True)
            b.setFixedWidth(52)
            self.step_group.addButton(b, k)
            steps.addWidget(b)
        self.step_group.button(2).setChecked(True)
        steps.addStretch(1)
        jl.addLayout(steps)

        pad = QGridLayout()
        tip = ("Arrows show TOOL direction relative to the board.\n"
               "The bed moves the opposite way.")

        def jbtn(text: str, axis: str, d: int) -> QPushButton:
            b = QPushButton(text)
            b.setMinimumSize(64, 48)
            b.setStyleSheet("font-size:15px; font-weight:bold;")
            b.setToolTip(tip)
            b.setAutoRepeat(False)
            b.clicked.connect(lambda: self._step_jog(axis, d))
            return b

        pad.addWidget(jbtn("Y+ ▲", "y", +1), 0, 1)
        pad.addWidget(jbtn("◀ X−", "x", -1), 1, 0)
        pad.addWidget(jbtn("X+ ▶", "x", +1), 1, 2)
        pad.addWidget(jbtn("Y− ▼", "y", -1), 2, 1)
        pad.addWidget(jbtn("Z+ ▲", "z", +1), 0, 4)
        pad.addWidget(jbtn("Z− ▼", "z", -1), 2, 4)
        stop_btn = QPushButton("■")
        stop_btn.setToolTip("Stop motion")
        stop_btn.setMinimumSize(64, 48)
        stop_btn.clicked.connect(self.ctl.stop)
        pad.addWidget(stop_btn, 1, 1)
        pad.setColumnMinimumWidth(3, 20)
        jl.addLayout(pad)

        sp = QHBoxLayout()
        sp.addWidget(QLabel("Speed:"))
        self.speed = QSlider(Qt.Horizontal)
        self.speed.setRange(1, self.settings.max_steps_per_tick)
        self.speed.setValue(min(self.settings.jog_steps_per_tick, self.settings.max_steps_per_tick))
        self.speed.setFocusPolicy(Qt.NoFocus)
        self.speed.valueChanged.connect(self._speed_changed)
        self.speed_lbl = QLabel()
        self.speed_lbl.setMinimumWidth(90)
        sp.addWidget(self.speed, 1)
        sp.addWidget(self.speed_lbl)
        jl.addLayout(sp)
        self._speed_changed(self.speed.value())

        kb = QHBoxLayout()
        self.kb_jog = QCheckBox("Keyboard jog (arrows, PgUp/PgDn - hold to move)")
        self.kb_jog.setChecked(True)
        self.kb_jog.setFocusPolicy(Qt.NoFocus)
        kb.addWidget(self.kb_jog)
        jl.addLayout(kb)
        left.addWidget(jog)

        # --- Spindle
        spg = QGroupBox("Spindle")
        sl = QHBoxLayout(spg)
        self.spindle_btn = QPushButton("Spindle OFF")
        self.spindle_btn.setCheckable(True)
        self.spindle_btn.setMinimumHeight(40)
        self.spindle_btn.clicked.connect(self._spindle_clicked)
        sl.addWidget(self.spindle_btn)
        left.addWidget(spg)
        left.addStretch(1)

        # --- Console
        con = QWidget()
        cl = QVBoxLayout(con)
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(3000)
        self.console.setFont(QFont("Consolas", 9))
        cl.addWidget(self.console)
        crow = QHBoxLayout()
        self.show_traffic = QCheckBox("Show raw traffic")
        self.show_keepalive = QCheckBox("incl. keep-alive")
        clear = QPushButton("Clear")
        clear.clicked.connect(self.console.clear)
        crow.addWidget(self.show_traffic)
        crow.addWidget(self.show_keepalive)
        crow.addStretch(1)
        crow.addWidget(clear)
        cl.addLayout(crow)
        right = QSplitter(Qt.Vertical)
        right.setChildrenCollapsible(False)
        self.vsplit = right
        self.view_tabs = QTabWidget()
        self.preview = PreviewWidget()
        self.preview.lineClicked.connect(self.select_line)
        self.view_tabs.addTab(self.preview, "2D")
        self.view_tabs.currentChanged.connect(lambda _: self._sync_views())
        self.view_tabs.currentChanged.connect(
            lambda _: self._bottom_fitted and self._fit_bottom())
        right.addWidget(self.view_tabs)
        right.addWidget(self._build_job_panel())
        self.bottom_tabs = QTabWidget()
        self.bottom_tabs.addTab(con, "Console")
        self.gcode_view = GCodeView()
        self.gcode_view.lineClicked.connect(lambda ln: self.select_line(ln, from_text=True))
        self.bottom_tabs.addTab(self.gcode_view, "G-code")
        self.checks_list = QListWidget()
        self.checks_list.itemClicked.connect(
            lambda it: self.select_line(it.data(Qt.UserRole)))
        self.bottom_tabs.addTab(self.checks_list, "Checks")
        self.al = AutoLevelPanel(self)
        al_scroll = QScrollArea()
        al_scroll.setWidget(self.al)
        al_scroll.setWidgetResizable(True)
        al_scroll.setFrameShape(QScrollArea.NoFrame)
        self.bottom_tabs.addTab(al_scroll, "Auto-level")
        self.pcb_view = PcbView()
        self.pcb = PcbPanel(self, self.pcb_view)
        pcb_scroll = QScrollArea()
        pcb_scroll.setWidget(self.pcb)
        pcb_scroll.setWidgetResizable(True)
        pcb_scroll.setFrameShape(QScrollArea.NoFrame)
        self.bottom_tabs.addTab(pcb_scroll, "PCB")
        self.cam = CamPanel(self)
        cam_scroll = QScrollArea()
        cam_scroll.setWidget(self.cam)
        cam_scroll.setWidgetResizable(True)
        cam_scroll.setFrameShape(QScrollArea.NoFrame)
        self.bottom_tabs.addTab(cam_scroll, "CAM")
        right.addWidget(self.bottom_tabs)
        right.setStretchFactor(0, 5)
        right.setStretchFactor(1, 0)
        right.setStretchFactor(2, 1)
        self.bottom_tabs.currentChanged.connect(lambda _: self._fit_bottom())
        self._bottom_fitted = False
        body.addWidget(right, 1)
        self._apply_quality()
        self.view_tabs.addTab(self.pcb_view, "PCB")
        # camera: picture with its controls in a side column (fits small screens)
        self.camera_view = CameraView()
        self.camera = CameraPanel(self, self.camera_view)
        camera_scroll = QScrollArea()
        camera_scroll.setWidget(self.camera)
        camera_scroll.setWidgetResizable(True)
        camera_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        camera_scroll.setMinimumWidth(self.camera.sizeHint().width() + 18)
        cam_split = QSplitter(Qt.Horizontal)
        cam_split.addWidget(self.camera_view)
        cam_split.addWidget(camera_scroll)
        cam_split.setStretchFactor(0, 1)
        cam_split.setStretchFactor(1, 0)
        cam_split.setChildrenCollapsible(False)
        self.camera_page = cam_split
        self.view_tabs.addTab(cam_split, "Camera")

        self.home_lbl = QLabel()
        self.statusBar().addPermanentWidget(self.home_lbl)
        self.jog_widgets = [jog, self.zero_all_btn, self.goto_zero_btn, spg]
        self.resize(1280, 860)

    def _build_job_panel(self) -> QWidget:
        box = QGroupBox("Job")
        v = QVBoxLayout(box)
        v.setContentsMargins(6, 2, 6, 4)
        v.setSpacing(3)
        row = QHBoxLayout()
        self.open_btn = QPushButton("Open…")
        self.open_btn.clicked.connect(self.open_job)
        self.run_btn = QPushButton("▶ Run")
        self.run_btn.clicked.connect(self.run_job)
        self.pause_btn = QPushButton("⏸ Pause")
        self.pause_btn.clicked.connect(self.pause_resume_job)
        self.stopjob_btn = QPushButton("■ Stop")
        self.stopjob_btn.clicked.connect(lambda: self._do(self.ctl.stop_job))
        for b in (self.open_btn, self.run_btn, self.pause_btn, self.stopjob_btn):
            b.setMinimumHeight(30)
            b.setFocusPolicy(Qt.NoFocus)
            row.addWidget(b)
        self.run_btn.setStyleSheet("font-weight:bold;")
        self.job_bar = QProgressBar()
        self.job_bar.setRange(0, 1000)
        self.job_bar.setTextVisible(False)
        self.job_bar.setMaximumHeight(14)
        self.job_status = QLabel()
        self.job_status.setMinimumWidth(260)
        row.addSpacing(8)
        row.addWidget(self.job_bar, 1)
        row.addWidget(self.job_status)
        v.addLayout(row)
        self.job_info = QLabel("No file loaded")
        self.job_info.setWordWrap(True)
        v.addWidget(self.job_info)

        # playback (simulation of the job in the views, no machine motion)
        pb = QHBoxLayout()
        self.play_btn = QPushButton("▶ Simulate")
        self.play_btn.setFocusPolicy(Qt.NoFocus)
        self.play_btn.setToolTip("Play the job in the views only - the machine does not move")
        self.play_btn.clicked.connect(self.toggle_playback)
        self.play_speed = QComboBox()
        self.play_speed.setFocusPolicy(Qt.NoFocus)
        for k in (1, 5, 20, 100, 500):
            self.play_speed.addItem(f"{k}×", k)
        self.play_speed.setCurrentIndex(2)
        self.play_slider = QSlider(Qt.Horizontal)
        self.play_slider.setFocusPolicy(Qt.NoFocus)
        self.play_slider.setRange(0, 0)
        self.play_slider.valueChanged.connect(self._slider_moved)
        self.to_sel_btn = QPushButton("⇥ Selected line")
        self.to_sel_btn.setFocusPolicy(Qt.NoFocus)
        self.to_sel_btn.setToolTip("Move the simulation to the selected G-code line")
        self.to_sel_btn.clicked.connect(self._play_to_selected)
        self.reset_play_btn = QPushButton("⟲")
        self.reset_play_btn.setFocusPolicy(Qt.NoFocus)
        self.reset_play_btn.setToolTip("Reset simulation")
        self.reset_play_btn.clicked.connect(lambda: self._set_play_idx(0, stop=True))
        self.play_lbl = QLabel()
        self.play_lbl.setMinimumWidth(150)
        for w in (self.play_btn, self.play_speed):
            pb.addWidget(w)
        pb.addWidget(self.play_slider, 1)
        for w in (self.play_lbl, self.to_sel_btn, self.reset_play_btn):
            pb.addWidget(w)
        v.addLayout(pb)
        self.play_timer = QTimer(self, interval=30, timeout=self._play_step)
        return box

    # ---------------------------------------------------------------- layout
    def _fit_bottom(self) -> None:
        """Give the bottom panel the height its current tab needs (max 60 %).

        Console / G-code / Checks stay small so the views get the space;
        Auto-level, PCB, CAM and Camera grow so their controls are not cut off.
        """
        sizes = self.vsplit.sizes()
        total = sum(sizes)
        if total <= 0:
            return
        w = self.bottom_tabs.currentWidget()
        inner = w.widget() if isinstance(w, QScrollArea) else None
        bar = self.bottom_tabs.tabBar().sizeHint().height()
        if inner is not None:
            need = max(inner.sizeHint().height(), inner.minimumSizeHint().height()) + bar + 12
        else:
            need = 190
        bottom = max(150, min(need, int(total * 0.6)))
        if self.view_tabs.currentWidget() is getattr(self, "camera_page", None):
            bottom = 150          # camera picture + its side column get the room
        job = sizes[1]
        self.vsplit.setSizes([max(150, total - job - bottom), job, bottom])

    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        if not self._bottom_fitted:
            self._bottom_fitted = True
            QTimer.singleShot(0, self._fit_bottom)

    def _build_menu(self) -> None:
        m = self.menuBar().addMenu("&File")
        a = QAction("&Open G-code…", self)
        a.setShortcut(QKeySequence.Open)
        a.triggered.connect(self.open_job)
        m.addAction(a)
        a = QAction("&Reload file", self)
        a.setShortcut("F5")
        a.triggered.connect(lambda: self.load_job(self.job_path)
                            if self.job_path and not self.ctl.job_active else None)
        m.addAction(a)
        a = QAction("Open &PCB board (Gerber / drill)…", self)
        a.setShortcut("Ctrl+B")
        a.triggered.connect(lambda: (self.bottom_tabs.setCurrentIndex(
            self.bottom_tabs.count() - 1), self.pcb.open_folder()))
        m.addAction(a)
        m.addSeparator()
        a = QAction("&Settings…", self)
        a.triggered.connect(self.open_settings)
        m.addAction(a)
        m.addSeparator()
        a = QAction("E&xit", self)
        a.setShortcut(QKeySequence.Quit)
        a.triggered.connect(self.close)
        m.addAction(a)

        m = self.menuBar().addMenu("&View")
        grp = QActionGroup(self)
        for key, text in (("full", "3D quality: &Standard"),
                          ("light", "3D quality: &Light (built-in graphics / big files)"),
                          ("2d", "&2D only (no 3D view)")):
            a = QAction(text, self, checkable=True)
            a.setChecked(self.settings.view_quality == key)
            a.triggered.connect(lambda _=False, k=key: self.set_quality(k))
            grp.addAction(a)
            m.addAction(a)

        m = self.menuBar().addMenu("&Machine")
        a = QAction("Set machine &home here…", self)
        a.triggered.connect(self.set_home)
        m.addAction(a)

        m = self.menuBar().addMenu("&Help")
        a = QAction("&Keyboard shortcuts", self)
        a.triggered.connect(lambda: QMessageBox.information(self, "Keyboard", (
            "Esc  —  E-STOP (always)\n\n"
            "Hold to jog (when 'Keyboard jog' is ticked):\n"
            "  ← / →     X− / X+\n  ↓ / ↑     Y− / Y+\n  PgDn / PgUp   Z− / Z+\n\n"
            "Releasing the key stops the motion.")))
        m.addAction(a)
        a = QAction("&About", self)
        a.triggered.connect(lambda: QMessageBox.about(
            self, "About MicroPCB",
            f"MicroPCB {__version__}\nControl software for Wegstr CNC machines."))
        m.addAction(a)

    # ================================================================ actions
    def log(self, text: str) -> None:
        self.console.appendPlainText(time.strftime("%H:%M:%S ") + text)

    def _do(self, fn) -> None:
        try:
            fn()
        except (RuntimeError, ValueError, ConnectionError, OSError) as e:
            self.log(f"⚠ {e}")
            self.statusBar().showMessage(str(e), 5000)

    def refresh_ports(self) -> None:
        cur = self.port_box.currentData()
        self.port_box.clear()
        try:
            ports = list_ports()
        except Exception:
            ports = []
        for dev, desc in ports:
            self.port_box.addItem(f"{dev} — {desc}", dev)
        self.port_box.addItem(SIM_LABEL, "SIM")
        want = cur or self.settings.last_port
        idx = self.port_box.findData(want)
        if idx >= 0:
            self.port_box.setCurrentIndex(idx)

    def toggle_connect(self) -> None:
        if self.ctl.connected:
            self.reconnect_timer.stop()
            self._lost_port = None
            self.ctl.disconnect()
            return
        port = self.port_box.currentData()
        try:
            if port == "SIM":
                self.ctl.connect(transport=SimMachine(surface=demo_board_surface,
                                                      fw_z_offset=-20000))
            else:
                self.ctl.connect(port)
            self.settings.save()
        except Exception as e:
            self.log(f"⚠ Connect failed: {e}")
            QMessageBox.warning(self, "Connect", f"Could not connect:\n{e}\n\n"
                                "Close the original Wegstr software and check the cable.")
        self._refresh()

    def emergency_stop(self) -> None:
        if hasattr(self, "al") and self.al.running:
            self.al.stop()
        self.ctl.emergency_stop()
        self.log("E-STOP: motion stopped, spindle off")

    def _step_jog(self, axis: str, d: int) -> None:
        step = STEP_SIZES[self.step_group.checkedId()]
        self._do(lambda: self._report_clamp(axis, d * step, self.ctl.jog(axis, d * step, self.speed.value())))

    def _report_clamp(self, axis: str, want: float, got: float) -> None:
        if abs(want - got) > 1e-9:
            self.log(f"Soft limit: {axis.upper()} move reduced to {got:.3f} mm")

    def _speed_changed(self, v: int) -> None:
        self.settings.jog_steps_per_tick = v
        mmpm = v * self.settings.mm_per_step[0] / TICK_SECONDS * 60
        self.speed_lbl.setText(f"{mmpm:.0f} mm/min")

    def _spindle_clicked(self, on: bool) -> None:
        self._do(lambda: self.ctl.set_spindle(on))

    def set_home(self) -> None:
        r = QMessageBox.question(
            self, "Set machine home",
            "Move the machine to: X fully at minimum, Y at minimum and Z at the top.\n\n"
            "Use the current position as machine home?\n"
            "(Soft limits will use the work area from Settings.)")
        if r == QMessageBox.Yes:
            self._do(self.ctl.set_home)
            self.log("Machine home set")

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec():
            self.settings.save()
            self.ctl.link.performance = self.settings.performance
            self.speed.setMaximum(self.settings.max_steps_per_tick)
            self._speed_changed(self.speed.value())
            self.log("Settings saved")

    # ================================================================ job
    def open_job(self) -> None:
        if self.ctl.job_active:
            return
        start = os.path.dirname(self.job_path) if self.job_path else self.settings.last_dir
        path, _ = QFileDialog.getOpenFileName(
            self, "Open G-code", start or "",
            "G-code (*.nc *.gcode *.ngc *.tap *.cnc *.gc *.txt);;All files (*)")
        if path:
            self.load_job(path)

    def load_job(self, path: str) -> None:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            QMessageBox.warning(self, "Open G-code", f"Cannot load the file:\n{e}")
            return
        self.load_job_text(text, path)
        self.settings.last_dir = os.path.dirname(path)

    def job_name(self) -> str:
        return os.path.basename(self.job_path or "job.nc")

    def load_job_text(self, text: str, path: str) -> None:
        """Load G-code from a file or generated by the CAM panel."""
        try:
            raw = parse(text, self._job_start())
            prog = self._transformed(raw)
        except (OSError, GCodeError, ValueError) as e:
            QMessageBox.warning(self, "Open G-code", f"Cannot load the file:\n{e}")
            return
        self.job_text, self.job_path, self.job_prog_raw = text, path, raw
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.gcode_view.set_text(text)
            self._set_program(prog)
        finally:
            QApplication.restoreOverrideCursor()
        self.log(f"Loaded {os.path.basename(path)}: {len(prog.lines())} moves, "
                 f"est. {_hms(self._job_secs)}")
        for w in prog.warnings:
            self.log(f"  note: {w}")
        n_err = sum(1 for i in self.issues if i.severity == ERROR)
        if self.issues:
            self.log(f"  checks: {n_err} problem(s), {len(self.issues) - n_err} warning(s) "
                     "- see the Checks tab")
            self.bottom_tabs.setCurrentWidget(self.checks_list)
        self._refresh()

    def _set_program(self, prog: Program) -> None:
        """Toolpath, timeline, checks and views for a parsed program."""
        self.job_prog = prog
        self.tp = Toolpath(prog)
        try:
            self.timeline = Timeline.build(self.ctl.make_planner(prog))
            self._job_secs = self.timeline.seconds
        except Exception as e:
            self.timeline = None
            self._job_secs = 0.0
            self.log(f"⚠ planner: {e}")
        self.issues = check_program(
            self.tp, max_depth_mm=self.settings.check_max_depth_mm,
            max_feed_mm_min=self.ctl.make_planner(prog).max_feed_mm_min,
            area=self._area_lohi(),
            surface=self._check_surface())
        if hasattr(self, "camera"):   # Stage 4.4 obstacles
            self.issues += self.camera.obstacle_issues(self.tp)
            self.issues.sort(key=lambda i: (i.line, i.severity != ERROR))
        self._fill_checks()
        self.gcode_view.set_issues((i.line, i.severity) for i in self.issues)
        self._marker = None
        self._sel_line = 0
        self.playing = False
        self.play_timer.stop()
        self.play_idx = 0
        self.play_slider.blockSignals(True)
        self.play_slider.setRange(0, self.timeline.ticks if self.timeline else 0)
        self.play_slider.setValue(0)
        self.play_slider.blockSignals(False)
        for v in self._views():
            v.set_toolpath(self.tp)
            v.set_done(None, 0)
            v.set_marker(None)
        self._show_job_info()
        self._sync_views()

    def _check_surface(self):
        """Copper height for the checks when the height map is applied to the job."""
        hm = self.al.active_map() if hasattr(self, "al") else None
        return hm.at if hm is not None else None

    def _fill_checks(self) -> None:
        self.checks_list.clear()
        if not self.issues:
            it = QListWidgetItem("✓ No problems found")
            it.setForeground(QColor("#2e7d32"))
            self.checks_list.addItem(it)
            self.bottom_tabs.setTabText(2, "Checks")
            return
        for i in self.issues:
            icon = "✖" if i.severity == ERROR else "⚠"
            it = QListWidgetItem(f"{icon}  line {i.line}: {i.text}")
            it.setData(Qt.UserRole, i.line)
            it.setForeground(QColor("#c62828" if i.severity == ERROR else "#e65100"))
            self.checks_list.addItem(it)
        self.bottom_tabs.setTabText(2, f"Checks ({len(self.issues)})")

    def _job_start(self) -> tuple[float, float, float]:
        return self.ctl.work_mm() if self.ctl.connected else (0.0, 0.0, 0.0)

    def _area_lohi(self):
        """Machine work area in work coordinates, or None if home not set."""
        c = self.ctl
        if not c.state.home_set:
            return None
        lo, hi = [], []
        for i in range(3):
            a, b = c.limit_range(i)
            off = c.state.work_offset[i]
            lo.append((a - off) * c.mmps(i))
            hi.append((b - off) * c.mmps(i))
        import numpy as np
        return np.array(lo), np.array(hi)

    def _show_job_info(self) -> None:
        prog = self.job_prog
        if prog is None:
            self.job_info.setText("No file loaded")
            return
        parts = [f"<b>{os.path.basename(self.job_path or '')}</b> — "
                 f"{prog.line_count} lines, est. time {_hms(self._job_secs)}"]
        b = prog.bounds()
        if b:
            parts.append(f"X {b[0][0]:.2f}…{b[1][0]:.2f}  Y {b[0][1]:.2f}…{b[1][1]:.2f}  "
                         f"Z {b[0][2]:.2f}…{b[1][2]:.2f} mm")
        n_err = sum(1 for i in self.issues if i.severity == ERROR)
        if n_err:
            parts.append(f"<span style='color:#c62828'><b>{n_err} problem(s)</b> - "
                         "see the Checks tab</span>")
        elif self.issues:
            parts.append(f"<span style='color:#e65100'>{len(self.issues)} warning(s) - "
                         "see the Checks tab</span>")
        if prog.warnings:
            parts.append(f"<span style='color:#757575'>{len(prog.warnings)} G-code note(s) "
                         "in the console</span>")
        self.job_info.setText("<br>".join(parts))

    def run_job(self) -> None:
        if self.job_text is None or self.ctl.job_active:
            return
        try:
            prog = self.ctl.program_for_run(self.job_text)
        except GCodeError as e:
            QMessageBox.warning(self, "Run", str(e))
            return
        problems = self.ctl.check_program(prog)
        if problems:
            QMessageBox.warning(self, "Run", "Cannot run this job:\n\n" + "\n".join(problems))
            return
        self._set_program(prog)  # re-plan from the real start position
        wp = self.ctl.work_mm()
        msg = (f"Run {os.path.basename(self.job_path or 'job')}?\n\n"
               f"Tool is at work X {wp[0]:.3f}  Y {wp[1]:.3f}  Z {wp[2]:.3f}.\n"
               f"Estimated time: {_hms(self._job_secs)}.\n")
        n_err = sum(1 for i in self.issues if i.severity == ERROR)
        if n_err:
            msg += (f"\n⚠ The checks found {n_err} PROBLEM(S) (see the Checks tab).\n"
                    "Run anyway?\n")
        if not self.ctl.limits_active():
            msg += "\nSoft limits are OFF (machine home not set) - bounds are not checked."
        if prog.uses_spindle:
            msg += "\nThe spindle will switch on. Keep hands clear."
        if self.ctl.probed_since_job:
            msg += ("\n\n⚠ REMOVE THE PROBE CLIP from the board first. While the clip is on,\n"
                    "the machine stops going down as soon as the tool touches the copper,\n"
                    "so the job would only scratch the surface.")
        if QMessageBox.question(self, "Run job", msg) != QMessageBox.Yes:
            return
        try:
            self.ctl.start_job(self.job_text, os.path.basename(self.job_path or "job"))
        except (RuntimeError, GCodeError) as e:
            QMessageBox.warning(self, "Run", str(e))
            return
        self.log(f"Job started: {self.ctl.job.name}")

    def pause_resume_job(self) -> None:
        job = self.ctl.job
        if job is None or not job.active:
            return
        if job.state == PAUSED_STATE:
            # may first drive back to the path (tool change) - keep the window responsive
            def work():
                try:
                    self.ctl.resume_job()
                except Exception as e:
                    self.bridge.event.emit("ui_error", f"Resume: {e}")
            threading.Thread(target=work, name="micropcb-resume", daemon=True).start()
        else:
            self.ctl.pause_job()

    def _on_job_event(self, job) -> None:
        if job.state == PAUSED_STATE:
            self._marker = self.ctl.work_mm()
            self.log(f"Job paused at line {job.line}: {job.message}")
            if job.message and job.message != "Paused":
                QMessageBox.information(self, "Job paused",
                                        f"{job.message}\n\nPress Resume to continue.")
        elif job.state == DONE:
            self._marker = None
            self.log(f"Job finished in {_hms(job.elapsed())}")
        elif job.state in (STOPPED, FAILED):
            self._marker = self.ctl.work_mm()
            self.log(f"Job stopped at line {job.line}: {job.message}")
            if job.state == FAILED:
                QMessageBox.warning(self, "Job stopped", job.message)
        elif job.state == RUNNING:
            self._marker = None
            if job.ticks_sent:
                self.log("Job resumed")
        self._sync_views()

    def _job_index(self) -> int:
        """Ticks the machine has (about) executed, as a timeline index."""
        job = self.ctl.job
        if job is None or self.timeline is None:
            return 0
        if job.state == DONE:
            return self.timeline.ticks
        q = self.ctl.link.queue_size() if self.ctl.connected else 0
        return max(0, min(self.timeline.ticks, job.ticks_sent - (q + 1) * 3))

    def _refresh_job(self) -> None:
        job = self.ctl.job
        active = self.ctl.job_active
        con = self.ctl.connected
        rel = con and self.ctl.link.released
        self.open_btn.setEnabled(not active)
        self.run_btn.setEnabled(bool(self.job_text) and con and not rel and not active
                                and self.ctl.position_valid and not self.al.running)
        self.pause_btn.setEnabled(active and job.state in (RUNNING, PAUSED_STATE))
        self.pause_btn.setText("▶ Resume" if active and job.state == PAUSED_STATE else "⏸ Pause")
        self.stopjob_btn.setEnabled(active)
        for w in (self.play_btn, self.play_slider, self.play_speed, self.to_sel_btn,
                  self.reset_play_btn):
            w.setEnabled(not active and self.timeline is not None)
        if job is not None:
            frac = job.progress()
            self.job_bar.setValue(int(frac * 1000))
            left = max(job.total_ticks - job.ticks_sent, 0) * TICK_SECONDS
            names = {RUNNING: "Running", PAUSING: "Pausing…", PAUSED_STATE: "Paused",
                     STOPPING: "Stopping…", DONE: "Done", STOPPED: "Stopped",
                     FAILED: "Failed"}
            self.job_status.setText(
                f"{names.get(job.state, job.state)} · line {job.line}/"
                f"{job.program.line_count} · {frac * 100:.0f}% · "
                f"{_hms(job.elapsed())} / left {_hms(left)}")
        else:
            self.job_bar.setValue(0)
            self.job_status.setText("")
        self._sync_views()

    # ---------------------------------------------------------------- auto-level
    def _transformed(self, prog: Program) -> Program:
        """Camera alignment (X/Y, Stage 4.3) first, then the height map (Z)."""
        al = self.camera.active_alignment() if hasattr(self, "camera") else None
        if al is not None:
            prog = apply_alignment(prog, al)
        hm = self.al.active_map() if hasattr(self, "al") else None
        return apply_heightmap(prog, hm) if hm is not None else prog

    def reapply_job(self) -> None:
        """Height map / alignment switched on/off: re-plan preview and future runs."""
        hm = self.al.active_map()
        al = self.camera.active_alignment() if hasattr(self, "camera") else None
        self.ctl.program_transform = self._transformed if (hm or al) else None
        if hm is not None:
            self.log("Height map applied to the job")
        if al is not None:
            self.log(f"Camera alignment applied: {al.angle_deg:+.3f}°, "
                     f"X {al.tx:+.3f} Y {al.ty:+.3f} mm")
        if self.job_prog_raw is not None and not self.ctl.job_active:
            self._set_program(self._transformed(self.job_prog_raw))

    def recheck_job(self) -> None:
        """Obstacles changed: run the checks again."""
        if getattr(self, "job_prog", None) is not None and not self.ctl.job_active:
            self._set_program(self.job_prog)

    def show_heightmap(self, hm) -> None:
        for v in self._views():
            v.set_heightmap(hm)

    # ---------------------------------------------------------------- views
    def _views(self) -> list:
        use3d = self.view3d is not None and self.settings.view_quality != "2d"
        return [self.preview] + ([self.view3d] if use3d else [])

    def _sync_views(self) -> None:
        """Push tool / progress / marker / area to the visible view."""
        if not hasattr(self, "play_lbl"):
            return
        c = self.ctl
        job_view = c.job is not None and (c.job_active or c.job.state in (DONE, STOPPED, FAILED)) \
            and self.play_idx == 0
        tl = self.timeline
        if job_view:
            idx = self._job_index()
            tool = c.work_mm()
            line = c.job.line if c.job_active else 0
        elif tl is not None and self.play_idx > 0:
            idx = self.play_idx
            tool = tuple(tl.pos[idx])
            line = int(tl.lines[idx])
        else:
            idx, tool, line = 0, c.work_mm(), 0
        visible = self.view_tabs.currentWidget()
        area = self._area_lohi()
        for v in self._views():
            if v is not visible:
                continue
            v.set_tool(*tool)
            v.set_done(tl, idx)
            v.set_marker(self._marker)
            v.set_area(area)
        if self.bottom_tabs.currentWidget() is self.gcode_view:
            self.gcode_view.set_current(line)
        if tl is not None:
            self.play_lbl.setText(f"{_hms(self.play_idx * TICK_SECONDS)} / {_hms(tl.seconds)}")

    def set_quality(self, q: str) -> None:
        self.settings.view_quality = q
        self.settings.save()
        self._apply_quality()

    def _apply_quality(self) -> None:
        """2D only: hide the 3D tab (the OpenGL widget is kept alive - pyqtgraph
        cannot safely create a second one after the first is destroyed)."""
        q = self.settings.view_quality
        if q == "2d":
            if self.view3d is not None:
                self.view_tabs.setTabVisible(self.view_tabs.indexOf(self.view3d), False)
                self.view_tabs.setCurrentWidget(self.preview)
            return
        if self.view3d is None:
            from .view3d import View3D  # OpenGL only loaded when wanted
            self.view3d = View3D(q)
            self.view_tabs.addTab(self.view3d, "3D")
        else:
            self.view_tabs.setTabVisible(self.view_tabs.indexOf(self.view3d), True)
            self.view3d.set_quality(q)
        if self.tp is not None and self.view3d.tp is not self.tp:
            self.view3d.set_toolpath(self.tp)
        self._sync_views()

    def select_line(self, line: int, from_text: bool = False) -> None:
        if not line:
            return
        self._sel_line = line
        sl = self.tp.segments_for_line(line) if self.tp is not None else None
        for v in self._views():
            v.set_selected(sl)
        if not from_text:
            self.gcode_view.set_selected(line)
            if self.bottom_tabs.currentWidget() is self.console.parentWidget():
                self.bottom_tabs.setCurrentWidget(self.gcode_view)

    # ---------------------------------------------------------------- playback
    def toggle_playback(self) -> None:
        if self.timeline is None or self.ctl.job_active:
            return
        self.playing = not self.playing
        if self.playing:
            if self.play_idx >= self.timeline.ticks:
                self._set_play_idx(0)
            self._play_t = time.monotonic()
            self.play_timer.start()
            self.bottom_tabs.setCurrentWidget(self.gcode_view)
        else:
            self.play_timer.stop()
        self.play_btn.setText("⏸ Simulate" if self.playing else "▶ Simulate")

    def _play_step(self) -> None:
        now = time.monotonic()
        dt, self._play_t = now - self._play_t, now
        if self.timeline is None:
            return
        k = self.play_speed.currentData()
        nxt = self.play_idx + max(1, int(dt * k / TICK_SECONDS))
        if nxt >= self.timeline.ticks:
            nxt = self.timeline.ticks
            self.toggle_playback()
        self._set_play_idx(nxt)

    def _set_play_idx(self, idx: int, stop: bool = False) -> None:
        if stop and self.playing:
            self.toggle_playback()
        self.play_idx = idx
        self.play_slider.blockSignals(True)
        self.play_slider.setValue(idx)
        self.play_slider.blockSignals(False)
        self._sync_views()

    def _slider_moved(self, v: int) -> None:
        self.play_idx = v
        self._sync_views()

    def _play_to_selected(self) -> None:
        if self.timeline is not None and self._sel_line:
            self._set_play_idx(self.timeline.index_for_line(self._sel_line))

    # ================================================================ events
    def _on_event(self, ev: str, payload: object) -> None:
        if ev == EV_TRAFFIC:
            if self.show_traffic.isChecked():
                d, b = payload  # type: ignore[misc]
                is_ka = (d == "tx" and len(b) == 2) or getattr(self, "_last_ka", False)
                if d == "tx":
                    self._last_ka = len(b) == 2
                if not is_ka or self.show_keepalive.isChecked():
                    self.log(f"{d} {b.hex(' ')}")
            return
        if ev == EV_CONNECTED:
            self.log(f"Connected ({self.ctl.port})")
            self._lost_port = None
            self.reconnect_timer.stop()
        elif ev == EV_DISCONNECTED:
            self.log("Disconnected")
        elif ev == EV_LOST:
            self.log(f"⚠ Connection lost: {payload}")
            if self.settings.auto_reconnect and self.ctl.port not in (None, "SIM"):
                self._lost_port = self.ctl.port
                self.reconnect_timer.start()
        elif ev == EV_RELEASED:
            self.log("⚠ Motors released (button on machine)" if payload
                     else "Motors engaged again - re-zero before cutting")
        elif ev == EV_OVERLOAD:
            self._overload = True
            self.log("⚠ SPINDLE OVERLOAD - motion stopped")
            QMessageBox.warning(self, "Spindle overload",
                                "The machine reported spindle overload.\nMotion stopped.")
            self._overload = False
        elif ev == EV_PROBE:
            self.log(f"Probe touch (machine value {payload})")
            self.al.on_probe_event(payload)
        elif ev.startswith("al_"):
            self.al.on_event(ev, payload)
        elif ev == EV_JOB:
            self._on_job_event(payload)
        elif ev == "ui_error":
            self.log(f"⚠ {payload}")
            QMessageBox.warning(self, "MicroPCB", str(payload))
        self._refresh()

    def _try_reconnect(self) -> None:
        if self.ctl.connected or not self._lost_port:
            self.reconnect_timer.stop()
            return
        port = self._lost_port
        if probe_port(port):
            self._do(lambda: self.ctl.connect(port))
            if self.ctl.connected:
                self.log("Reconnected - position must be re-zeroed")
                self.ctl.link.position_valid = False

    def _refresh(self) -> None:
        c = self.ctl
        con = c.connected
        rel = con and c.link.released
        valid = c.position_valid
        self.connect_btn.setText("Disconnect" if con else "Connect")
        self.port_box.setEnabled(not con)

        if not con:
            st, col = ("Reconnecting…", "#e65100") if self._lost_port else ("Disconnected", "#757575")
        elif rel:
            st, col = "Motors released", "#e65100"
        elif c.job_active:
            st, col = (("Job paused", "#e65100") if c.job.state == PAUSED_STATE
                       else ("Running job", "#1565c0"))
        elif c.busy():
            st, col = "Moving", "#1565c0"
        else:
            st, col = "Idle", "#2e7d32"
        self.status_lbl.setText(f"● {st}")
        self.status_lbl.setStyleSheet(f"font-weight:bold; padding:0 10px; color:{col};")

        msgs = []
        if self._lost_port:
            msgs.append("Connection lost (hardware E-stop / power off?). "
                        "Will reconnect when the machine is back.")
        if rel:
            msgs.append("Motors are RELEASED by the button on the machine. "
                        "Press it again to engage the motors.")
        if not valid:
            msgs.append("Position is NOT reliable. Jog to your work origin and press "
                        "'Zero all' (or Machine → Set machine home).")
        self.banner.setText("\n".join(msgs))
        self.banner.setVisible(bool(msgs))

        al_busy = self.al.running
        for w in self.jog_widgets:
            w.setEnabled(con and not rel and (not c.job_active or c.job_paused)
                         and not al_busy)
        self.al.refresh()

        wp, mp = c.work_mm(), c.machine_mm()
        color = "" if valid else "color:#9e9e9e;"
        for i in range(3):
            self.work_lbl[i].setText(f"{wp[i]:9.3f}")
            self.work_lbl[i].setStyleSheet(color)
            self.mach_lbl[i].setText(f"{mp[i]:9.3f}")

        sp = con and c.link.spindle
        self.spindle_btn.setChecked(sp)
        self.spindle_btn.setText("Spindle ON" if sp else "Spindle OFF")
        self.spindle_btn.setStyleSheet("background:#fdd835; font-weight:bold;" if sp else "")
        self._refresh_job()
        self.home_lbl.setText("Soft limits: ON" if c.limits_active()
                              else "Soft limits: off (home not set)")

    # ================================================================ keyboard
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        t = event.type()
        if t not in (QEvent.KeyPress, QEvent.KeyRelease):
            return False
        if not self.isActiveWindow():
            return False
        key = event.key()
        if key == Qt.Key_Escape and t == QEvent.KeyPress:
            self.emergency_stop()
            return True
        if key not in JOG_KEYS or not self.kb_jog.isChecked() or (
                self.ctl.job_active and not self.ctl.job_paused):
            return False
        if isinstance(QApplication.focusWidget(), (QLineEdit, QAbstractSpinBox)):
            return False
        if event.isAutoRepeat():
            return True
        axis, d = JOG_KEYS[key]
        if t == QEvent.KeyPress and self._held_key is None:
            self._held_key = key
            self._do(lambda: self.ctl.jog_continuous(axis, d, self.speed.value()))
        elif t == QEvent.KeyRelease and key == self._held_key:
            self._held_key = None
            self.ctl.stop()
        return True

    def closeEvent(self, e) -> None:  # noqa: N802
        if self.ctl.connected and (self.ctl.busy() or self.ctl.job_active):
            r = QMessageBox.question(self, "Exit", "The machine is moving / a job is active. "
                                     "Stop and exit?")
            if r != QMessageBox.Yes:
                e.ignore()
                return
        self.reconnect_timer.stop()
        if self.ctl.job_active:
            self.ctl.stop_job()
            self.ctl.job.join(2)
        if self.ctl.connected and self.ctl.link.spindle:
            self.ctl.emergency_stop()
            self.ctl.wait_idle(1)
        self.camera.shutdown()
        self.ctl.disconnect()
        self.settings.save()
        e.accept()


def demo_board_surface(x: int, y: int) -> int:
    """Simulator's virtual PCB (steps): 3 mm below the start position, tilted
    0.1 mm per 50 mm in X and warped 0.08 mm in the middle - for trying auto-level."""
    import math
    xm, ym = x * 0.004, y * 0.004
    z = -3.0 + 0.002 * xm + 0.08 * math.sin(min(max(xm / 60, 0), 1) * math.pi)         * math.sin(min(max(ym / 40, 0), 1) * math.pi)
    return round(z / 0.004)


def _hms(secs: float) -> str:
    secs = int(round(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
