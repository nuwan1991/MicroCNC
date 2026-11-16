"""MicroCNC main window (0.3 layout).

Rows: name + status / menus + E-STOP / port + view buttons. Left column (300 px):
position, jog, spindle, job (summary + Start / Pause / Stop), layers. Right: the
views with the simulation bar, and the bottom panel (console, G-code, checks,
auto-level, PCB, CAM)."""
from __future__ import annotations

import os
import re
import threading
import time

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QDoubleValidator, QFont, QKeySequence
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox,
                               QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                               QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
                               QSlider, QVBoxLayout, QWidget, QAbstractSpinBox,
                               QLineEdit, QFileDialog, QProgressBar, QSplitter,
                               QTabWidget, QListWidget, QListWidgetItem, QScrollArea,
                               QFrame, QMenuBar, QSizePolicy)

from .. import APP_NAME, __version__
from ..autolevel import HeightMap
from ..layers import Layer, Project
from . import theme
from .help_dialogs import SHORTCUTS, AboutDialog, DocViewer, UpdateChecker, system_info
from .layers_box import LayersBox
from .widgets import Bracket, Lamp, Section, ToggleSwitch, muted
from ..controller import AXES, EV_JOB, EV_STATE, MachineController
from ..gcode import (GCodeError, Program, job_min_xy, limit_depth, parse, program_range,
                     shift_program)
from ..job import DONE, FAILED, PAUSED_STATE, PAUSING, RUNNING, STOPPED, STOPPING
from ..protocol import TICK_SECONDS
from ..serial_link import (EV_CONNECTED, EV_DISCONNECTED, EV_LOST, EV_OVERLOAD,
                           EV_PROBE, EV_RELEASED, EV_TRAFFIC, list_ports, probe_port)
from ..settings import MachineState, Settings
from ..sim_machine import SimMachine
from ..toolpath import ERROR, Timeline, Toolpath, check_program
from .. import jobfile
from ..autolevel import apply_heightmap, outside_map
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
STEP_SIZES = (0.004, 0.01, 0.1, 1.0, 5.0, 10.0)   # 0.004 mm = one motor step
DEFAULT_STEP = 3                                    # 1 mm

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
        self.setWindowTitle(f"{APP_NAME} {__version__}")
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
        self.layers: list[Layer] = []
        self.updates = UpdateChecker(self)

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
        if self.settings.check_updates and not os.environ.get("MICROCNC_NO_UPDATE_CHECK") \
                and not os.environ.get("PYTEST_CURRENT_TEST"):
            QTimer.singleShot(4000, lambda: self.updates.check(manual=False))

    # ================================================================ UI
    LEFT_W = 300   # left column (port, position, jog, spindle, job, layers)

    def _build_ui(self) -> None:
        # --- rows 1 + 2: name / status, then menus + E-STOP (window top)
        top = QWidget()
        tv = QVBoxLayout(top)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.setSpacing(0)
        head = QWidget()
        head.setStyleSheet(f"background:#0f1317; border-bottom:1px solid {theme.LINE};")
        hh = QHBoxLayout(head)
        hh.setContentsMargins(10, 4, 10, 4)
        hh.setSpacing(14)
        name = QLabel(f"{APP_NAME} {__version__}")
        name.setStyleSheet("color:#fff; font-weight:700; font-size:10pt; border:none;")
        name.setMinimumWidth(self.LEFT_W - 20)
        hh.addWidget(name)
        self.status_lbl = QLabel()
        hh.addWidget(self.status_lbl)
        self.info_lbl = QLabel()
        self.info_lbl.setStyleSheet(f"color:{theme.MUTE}; border:none;")
        self.info_lbl.setTextFormat(Qt.RichText)
        hh.addWidget(self.info_lbl, 1)
        tv.addWidget(head)
        self.menu_bar = QMenuBar()
        self.estop_btn = QPushButton("■  E-STOP  (Esc)")
        theme.set_role(self.estop_btn, "estop")
        self.estop_btn.setFocusPolicy(Qt.NoFocus)
        self.estop_btn.setToolTip("Stop all motion and switch spindle off.\n"
                                  "The hardware E-stop cuts machine power.")
        self.estop_btn.clicked.connect(self.emergency_stop)
        mrow = QWidget()
        mrow.setStyleSheet(f"background:{theme.PANEL}; border-bottom:1px solid {theme.LINE};")
        mh = QHBoxLayout(mrow)
        mh.setContentsMargins(4, 2, 8, 2)
        mh.addWidget(self.menu_bar)
        mh.addStretch(1)
        mh.addWidget(self.estop_btn)
        tv.addWidget(mrow)
        self.setMenuWidget(top)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- warning banner
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("background:#d29922; color:black; padding:4px 10px;"
                                  " font-weight:bold;")
        self.banner.hide()
        root.addWidget(self.banner)

        body = QHBoxLayout()
        body.setSpacing(0)
        root.addLayout(body, 1)

        # ================= left column
        left_w = QWidget()
        left = QVBoxLayout(left_w)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(0)

        # row 3 (left part): port, aligned with the view row on the right
        port_row = QFrame()
        port_row.setObjectName("section")
        port_row.setFixedHeight(36)
        pr = QHBoxLayout(port_row)
        pr.setContentsMargins(10, 2, 10, 2)
        pr.setSpacing(4)
        pr.addWidget(muted("Port"))
        self.port_box = QComboBox()
        self.port_box.setMinimumWidth(60)
        self.port_box.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.port_box.setMinimumContentsLength(6)
        pr.addWidget(self.port_box, 1)
        refresh = QPushButton("⟳")
        refresh.setToolTip("Refresh port list")
        refresh.setFixedWidth(28)
        refresh.clicked.connect(self.refresh_ports)
        pr.addWidget(refresh)
        self.connect_btn = QPushButton("Connect")
        theme.set_role(self.connect_btn, "primary")
        self.connect_btn.clicked.connect(self.toggle_connect)
        pr.addWidget(self.connect_btn)
        left.addWidget(port_row)

        # --- Position
        dro = Section("Position (mm)", muted("Work · Machine"))
        g = QGridLayout()
        g.setHorizontalSpacing(5)
        g.setVerticalSpacing(3)
        self.work_lbl, self.mach_lbl = [], []
        for i, a in enumerate(AXES):
            nm = QLabel(a.upper())
            nm.setStyleSheet(f"color:{theme.AXIS[a]}; font-weight:700; font-size:12pt;")
            w = QLabel("0.000")
            w.setObjectName("dro")
            w.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            m = QLabel("0.000")
            m.setObjectName("droMachine")
            m.setMinimumWidth(48)
            m.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            z = QPushButton("0")
            z.setFixedWidth(28)
            z.setToolTip(f"Zero {a.upper()}: this position becomes work {a.upper()} 0")
            z.clicked.connect(lambda _=False, ax=a: self._do(lambda: self.ctl.zero_axes(ax)))
            g.addWidget(nm, i, 0)
            g.addWidget(w, i, 1)
            g.addWidget(m, i, 2)
            g.addWidget(z, i, 3)
            self.work_lbl.append(w)
            self.mach_lbl.append(m)
        g.addWidget(Bracket(), 0, 4, 3, 1)
        self.zero_all_btn = QPushButton("Zero\nall")
        self.zero_all_btn.setToolTip("Zero X, Y and Z: this position becomes work 0")
        self.zero_all_btn.setStyleSheet(f"border-color:{theme.ACCENT}; padding:6px 4px;")
        self.zero_all_btn.setFixedWidth(44)
        self.zero_all_btn.clicked.connect(lambda: self._do(self.ctl.zero_axes))
        g.addWidget(self.zero_all_btn, 0, 5, 3, 1, Qt.AlignVCenter)
        g.setColumnStretch(1, 1)
        dro.body.addLayout(g)
        left.addWidget(dro)

        # --- Jog
        self.kb_jog = QPushButton("⌨ Keys")
        self.kb_jog.setCheckable(True)
        self.kb_jog.setChecked(True)
        self.kb_jog.setFocusPolicy(Qt.NoFocus)
        self.kb_jog.setStyleSheet("padding:0 6px; font-size:8pt;")
        self.kb_jog.setToolTip("Keyboard jog: hold arrows (X/Y) and PgUp/PgDn (Z) to move,\n"
                               "release to stop")
        jog = Section("Jog", self.kb_jog)
        steps = QHBoxLayout()
        steps.setSpacing(3)
        self.step_group = QButtonGroup(self)
        for k, s in enumerate(STEP_SIZES):
            b = QPushButton(f"{s:g}")
            b.setCheckable(True)
            b.setFocusPolicy(Qt.NoFocus)
            b.setStyleSheet("padding:3px 0;")
            b.setMinimumWidth(10)
            b.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            b.setToolTip(f"Step {s:g} mm" + ("  (one motor step - the smallest move)"
                                             if k == 0 else ""))
            self.step_group.addButton(b, k)
            steps.addWidget(b)
        self.step_group.button(DEFAULT_STEP).setChecked(True)
        jog.body.addWidget(muted("Step (mm)"))
        jog.body.addLayout(steps)

        pad = QGridLayout()
        pad.setSpacing(4)
        tip_xy = ("Arrows show TOOL direction relative to the board.\n"
                  "The bed moves the opposite way.")
        tip_z = "Z+ moves the spindle UP, Z− moves it DOWN (towards the board)."

        def jbtn(text: str, axis: str, d: int) -> QPushButton:
            b = QPushButton(text)
            b.setFixedSize(46, 34)
            b.setStyleSheet("font-weight:bold;")
            b.setFocusPolicy(Qt.NoFocus)
            b.setToolTip(tip_z if axis == "z" else tip_xy)
            b.clicked.connect(lambda: self._step_jog(axis, d))
            return b

        pad.addWidget(jbtn("Y+", "y", +1), 0, 1)
        pad.addWidget(jbtn("X−", "x", -1), 1, 0)
        pad.addWidget(jbtn("X+", "x", +1), 1, 2)
        pad.addWidget(jbtn("Y−", "y", -1), 2, 1)
        pad.addWidget(jbtn("Z+", "z", +1), 0, 4)
        pad.addWidget(jbtn("Z−", "z", -1), 2, 4)
        stop_btn = QPushButton("■")
        theme.set_role(stop_btn, "stop")
        stop_btn.setToolTip("Stop motion")
        stop_btn.setFixedSize(46, 34)
        stop_btn.setFocusPolicy(Qt.NoFocus)
        stop_btn.clicked.connect(self.ctl.stop)
        pad.addWidget(stop_btn, 1, 1)
        pad.setColumnMinimumWidth(3, 12)
        padw = QHBoxLayout()
        padw.addStretch(1)
        padw.addLayout(pad)
        padw.addStretch(1)
        jog.body.addLayout(padw)

        row = QHBoxLayout()
        self.goto_zero_btn = QPushButton("⌂ Go to work zero")
        self.goto_zero_btn.setToolTip("Raise Z to safe height, move X/Y to 0, lower Z to 0")
        self.goto_zero_btn.clicked.connect(lambda: self._do(self.ctl.go_to_work_zero))
        self.safe_z_btn = QPushButton("⤒ Z safe height")
        self.safe_z_btn.setToolTip("Raise Z to the safe height (Settings) - X/Y stay")
        self.safe_z_btn.clicked.connect(self.z_to_safe)
        for b in (self.goto_zero_btn, self.safe_z_btn):
            b.setFocusPolicy(Qt.NoFocus)
            b.setMinimumWidth(10)
            b.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            row.addWidget(b)
        jog.body.addLayout(row)

        go = QHBoxLayout()
        go.setSpacing(3)
        self.goto_edits = []
        for a in AXES:
            lab = QLabel(a.upper())
            lab.setStyleSheet(f"color:{theme.AXIS[a]}; font-weight:700;")
            e = QLineEdit()
            e.setPlaceholderText("—")
            e.setMinimumWidth(20)
            e.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            e.setValidator(QDoubleValidator(-1000.0, 1000.0, 3))
            e.setToolTip(f"Work {a.upper()} to go to (mm). Empty = {a.upper()} stays.")
            e.returnPressed.connect(self.go_to_position)
            go.addWidget(lab)
            go.addWidget(e, 1)
            self.goto_edits.append(e)
        self.goto_btn = QPushButton("Go to")
        theme.set_role(self.goto_btn, "primary")
        self.goto_btn.setToolTip("Move to the work position in the boxes (empty box = axis "
                                 "stays).\nZ first goes up to the safe height, then X/Y move, "
                                 "Z goes to its value last.")
        self.goto_btn.clicked.connect(self.go_to_position)
        go.addWidget(self.goto_btn)
        jog.body.addLayout(go)

        sp = QHBoxLayout()
        sp.addWidget(muted("Speed"))
        self.speed = QSlider(Qt.Horizontal)
        self.speed.setRange(0, 100)
        self.speed.setSingleStep(5)
        self.speed.setPageStep(10)
        self.speed.setValue(max(0, min(100, int(getattr(self.settings, "jog_speed_pct", 100)))))
        self.speed.setFocusPolicy(Qt.NoFocus)
        self.speed.setToolTip("Jog speed, % of the machine's maximum (0 % = slowest move)")
        self.speed.valueChanged.connect(self._speed_changed)
        self.speed_lbl = QLabel()
        self.speed_lbl.setMinimumWidth(92)
        self.speed_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        sp.addWidget(self.speed, 1)
        sp.addWidget(self.speed_lbl)
        jog.body.addLayout(sp)
        self._speed_changed(self.speed.value())
        left.addWidget(jog)

        # --- Spindle: lamp + word = state, switch = action
        spg = Section("Spindle")
        sh = QHBoxLayout()
        self.spindle_lamp = Lamp()
        self.spindle_state = QLabel("Stopped")
        self.spindle_state.setStyleSheet("font-weight:600;")
        self.spindle_btn = ToggleSwitch()
        self.spindle_btn.setToolTip("Slide right to start the spindle, left to stop it")
        self.spindle_btn.clicked.connect(self._spindle_clicked)
        sh.addWidget(self.spindle_lamp)
        sh.addWidget(self.spindle_state)
        sh.addStretch(1)
        sh.addWidget(muted("Off"))
        sh.addWidget(self.spindle_btn)
        sh.addWidget(muted("On"))
        spg.body.addLayout(sh)
        left.addWidget(spg)

        # --- Job: what Start will run, then Start / Pause / Stop
        self.job_head = muted("")
        jobs = Section("Job", self.job_head)
        jobs.setStyleSheet("QFrame#section { background:#1a2129; }")
        self.job_info = QLabel("No file loaded")
        self.job_info.setObjectName("summary")
        self.job_info.setWordWrap(True)
        self.job_info.setTextFormat(Qt.RichText)
        jobs.body.addWidget(self.job_info)
        row = QHBoxLayout()
        self.run_btn = QPushButton("▶ Start")
        theme.set_role(self.run_btn, "start")
        self.run_btn.setToolTip("Run the selected layer on the machine")
        self.run_btn.clicked.connect(self.run_job)
        self.pause_btn = QPushButton("⏸ Pause")
        theme.set_role(self.pause_btn, "pause")
        self.pause_btn.clicked.connect(self.pause_resume_job)
        self.stopjob_btn = QPushButton("■ Stop")
        theme.set_role(self.stopjob_btn, "stop")
        self.stopjob_btn.clicked.connect(lambda: self._do(self.ctl.stop_job))
        for b in (self.run_btn, self.pause_btn, self.stopjob_btn):
            b.setMinimumHeight(32)
            b.setMinimumWidth(10)
            b.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            b.setFocusPolicy(Qt.NoFocus)
            row.addWidget(b)
        jobs.body.addLayout(row)
        self.job_bar = QProgressBar()
        self.job_bar.setRange(0, 1000)
        self.job_bar.setTextVisible(False)
        self.job_bar.setFixedHeight(6)
        jobs.body.addWidget(self.job_bar)
        self.job_status = muted("")
        self.job_status.setWordWrap(True)
        self.job_status.hide()
        jobs.body.addWidget(self.job_status)
        left.addWidget(jobs)

        # --- Layers
        self.layers_box = LayersBox()
        lay = Section("Layers", self.layers_box.header_buttons())
        lay.body.addWidget(self.layers_box, 1)
        left.addWidget(lay, 1)
        self.origin_combo = self.layers_box.origin_combo
        self.depth_chk = self.layers_box.depth_chk
        self.depth_spin = self.layers_box.depth_spin
        self.origin_combo.setCurrentIndex(
            max(0, self.origin_combo.findData(getattr(self.settings, "job_origin", "file"))))
        md = float(getattr(self.settings, "job_max_depth", 0.0) or 0.0)
        self.depth_spin.setValue(md or 0.05)
        self.depth_chk.setChecked(md > 0)
        self.depth_spin.setEnabled(md > 0)
        self.layers_box.map_state = self._map_state
        self.layers_box.selected.connect(self.select_layer)
        self.layers_box.visibility.connect(self._update_layer_overlays)
        self.layers_box.options.connect(self._layer_options_changed)
        self.layers_box.add.connect(self.add_layers)
        self.layers_box.remove.connect(self.remove_layer)

        self.left_scroll = QScrollArea()
        self.left_scroll.setWidget(left_w)
        self.left_scroll.setWidgetResizable(True)
        self.left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.left_scroll.setFixedWidth(self.LEFT_W)
        left_w.setMaximumWidth(self.LEFT_W - 2)
        for wdg in left_w.findChildren(QWidget):
            if isinstance(wdg, (QLabel,)) and wdg.objectName() in ("summary",):
                wdg.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.left_scroll.setStyleSheet(f"QScrollArea {{ background:{theme.PANEL};"
                                       f" border-right:1px solid {theme.LINE}; }}")
        left_w.setStyleSheet(f"background:{theme.PANEL};")
        body.addWidget(self.left_scroll)

        # ================= right: view row, views (+ simulation bar), bottom panel
        right_w = QWidget()
        rv = QVBoxLayout(right_w)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)
        vrow = QFrame()
        vrow.setObjectName("section")
        vrow.setFixedHeight(36)
        vh = QHBoxLayout(vrow)
        vh.setContentsMargins(8, 2, 8, 2)
        vh.setSpacing(4)
        self.view_group = QButtonGroup(self)
        self.view_btns: dict[str, QPushButton] = {}
        for key, text in (("2d", "2D"), ("3d", "3D"), ("pcb", "PCB"), ("camera", "Camera")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setFocusPolicy(Qt.NoFocus)
            theme.set_role(b, "view")
            b.clicked.connect(lambda _=False, k=key: self.show_view(k))
            self.view_group.addButton(b)
            self.view_btns[key] = b
            vh.addWidget(b)
        vh.addStretch(1)
        self.tool_btns: dict[str, QPushButton] = {}
        for key, text, tip in (("fit", "⤢ Fit", "Fit the job in the view (double-click works too)"),
                               ("top", "Top", "3D: view from the top"),
                               ("iso", "Iso", "3D: angled view"),
                               ("map", "Map overlay", "Show the height map / auto-level points"),
                               ("panel", "▾ Panel", "Show / hide the bottom panel (F6)"),
                               ("full", "⛶", "Full-screen view: hide the side and bottom "
                                             "panels (F11)")):
            b = QPushButton(text)
            b.setFocusPolicy(Qt.NoFocus)
            b.setToolTip(tip)
            b.setStyleSheet("padding:2px 8px;")
            if key in ("map", "panel", "full"):
                b.setCheckable(True)
            vh.addWidget(b)
            self.tool_btns[key] = b
        self.tool_btns["map"].setChecked(True)
        self.tool_btns["panel"].setChecked(True)
        self.tool_btns["fit"].clicked.connect(self.fit_view)
        self.tool_btns["top"].clicked.connect(lambda: self.view3d and self.view3d.set_view("Top"))
        self.tool_btns["iso"].clicked.connect(lambda: self.view3d and self.view3d.set_view("Iso"))
        self.tool_btns["map"].toggled.connect(lambda _: self._refresh_map_overlay())
        self.tool_btns["panel"].toggled.connect(self.set_bottom_visible)
        self.tool_btns["full"].toggled.connect(self.set_full_view)
        rv.addWidget(vrow)

        right = QSplitter(Qt.Vertical)
        right.setChildrenCollapsible(False)
        self.vsplit = right
        self.view_tabs = QTabWidget()
        self.view_tabs.tabBar().hide()
        self.view_tabs.setDocumentMode(True)
        self.preview = PreviewWidget()
        self.preview.lineClicked.connect(self.select_line)
        self.view_tabs.addTab(self.preview, "2D")
        self.view_tabs.currentChanged.connect(lambda _: self._sync_views())
        self.view_tabs.currentChanged.connect(lambda _: self._sync_view_buttons())
        self.view_tabs.currentChanged.connect(
            lambda _: self._bottom_fitted and self._fit_bottom())
        view_area = QWidget()
        va = QVBoxLayout(view_area)
        va.setContentsMargins(0, 0, 0, 0)
        va.setSpacing(0)
        va.addWidget(self.view_tabs, 1)
        va.addWidget(self._build_sim_bar())
        right.addWidget(view_area)

        # --- Console
        con = QWidget()
        cl = QVBoxLayout(con)
        cl.setContentsMargins(4, 4, 4, 4)
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
        self.rec_btn = QPushButton("● Record")
        self.rec_btn.setCheckable(True)
        self.rec_btn.setToolTip(
            "Record every packet / reply with its time (µs) plus the program's own actions\n"
            "(zero, probes, auto-level, jobs) to a CSV in Documents\\MicroPCB captures.\n"
            "On stop a report and the motion as G-code are written - send them for analysis.")
        self.rec_btn.toggled.connect(self.toggle_record)
        crow.addWidget(self.rec_btn)
        for text, fn in (("Analyze…", self.analyze_capture), ("Folder", self.open_capture_folder)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            crow.addWidget(b)
        crow.addWidget(clear)
        cl.addLayout(crow)
        self.bottom_tabs = QTabWidget()
        self.bottom_tabs.setDocumentMode(True)
        self.bottom_tabs.addTab(con, "Console")
        self.gcode_view = GCodeView()
        self.gcode_view.lineClicked.connect(lambda ln: self.select_line(ln, from_text=True))
        self.bottom_tabs.addTab(self.gcode_view, "G-code")
        self.checks_list = QListWidget()
        self.checks_list.itemClicked.connect(
            lambda it: self.select_line(it.data(Qt.UserRole)))
        self.bottom_tabs.addTab(self.checks_list, "Checks")
        self.al = AutoLevelPanel(self)
        self.al_scroll = QScrollArea()
        self.al_scroll.setWidget(self.al)
        self.al_scroll.setWidgetResizable(True)
        self.al_scroll.setFrameShape(QScrollArea.NoFrame)
        self.bottom_tabs.addTab(self.al_scroll, "Auto-level")
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
        right.setStretchFactor(1, 1)
        self.bottom_tabs.currentChanged.connect(lambda _: self._fit_bottom())
        self.bottom_tabs.currentChanged.connect(lambda _: self._bottom_tab_changed())
        self._bottom_fitted = False
        rv.addWidget(right, 1)
        body.addWidget(right_w, 1)
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
        self._sync_view_buttons()

        self.statusBar().hide()
        self.jog_widgets = [jog, self.zero_all_btn, spg]
        self.resize(1360, 900)

    def _build_sim_bar(self) -> QWidget:
        """Simulation (views only, no machine motion) + run part of the job."""
        bar = QFrame()
        bar.setObjectName("section")
        bar.setStyleSheet(f"QFrame#section {{ background:{theme.PANEL};"
                          f" border-top:1px solid {theme.LINE}; }}")
        pb = QHBoxLayout(bar)
        pb.setContentsMargins(8, 3, 8, 3)
        pb.setSpacing(4)
        self.play_btn = QPushButton("▶ Simulate")
        self.play_btn.setToolTip("Play the job in the views only - the machine does not move")
        self.play_btn.clicked.connect(self.toggle_playback)
        self.play_speed = QComboBox()
        for k in (1, 5, 20, 100, 500):
            self.play_speed.addItem(f"{k}×", k)
        self.play_speed.setCurrentIndex(2)
        self.play_slider = QSlider(Qt.Horizontal)
        self.play_slider.setRange(0, 0)
        self.play_slider.valueChanged.connect(self._slider_moved)
        self.to_sel_btn = QPushButton("⇥ Line")
        self.to_sel_btn.setToolTip("Move the simulation to the selected G-code line")
        self.to_sel_btn.clicked.connect(self._play_to_selected)
        self.reset_play_btn = QPushButton("⟲")
        self.reset_play_btn.setToolTip("Reset simulation")
        self.reset_play_btn.clicked.connect(lambda: self._set_play_idx(0, stop=True))
        self.play_lbl = QLabel()
        self.play_lbl.setMinimumWidth(96)
        self.play_lbl.setStyleSheet("font-family:Consolas;")
        self.range_start_btn = QPushButton("⟦ Start here")
        self.range_start_btn.setToolTip("Run part of the job: it starts at the G-code line\n"
                                        "the simulation is on now (move the bar there)")
        self.range_start_btn.clicked.connect(lambda: self._set_range_end(0))
        self.range_end_btn = QPushButton("End here ⟧")
        self.range_end_btn.setToolTip("Run part of the job: it ends after the G-code line\n"
                                      "the simulation is on now")
        self.range_end_btn.clicked.connect(lambda: self._set_range_end(1))
        self.range_clear_btn = QPushButton("✕ Whole job")
        self.range_clear_btn.setToolTip("Run the whole job again")
        self.range_clear_btn.clicked.connect(self._clear_range)
        self.range_lbl = QLabel("Run: whole job")
        for w in (self.play_btn, self.play_speed, self.reset_play_btn):
            w.setFocusPolicy(Qt.NoFocus)
            pb.addWidget(w)
        self.play_slider.setFocusPolicy(Qt.NoFocus)
        pb.addWidget(self.play_slider, 1)
        pb.addWidget(self.play_lbl)
        for w in (self.to_sel_btn, self.range_start_btn, self.range_end_btn,
                  self.range_clear_btn):
            w.setFocusPolicy(Qt.NoFocus)
            pb.addWidget(w)
        pb.addWidget(self.range_lbl)
        self._range_pick = [None, None]
        self.play_timer = QTimer(self, interval=30, timeout=self._play_step)
        return bar

    # ---------------------------------------------------------------- layout
    def _fit_bottom(self) -> None:
        """Give the bottom panel the height its current tab needs (max 55 %).

        Console / G-code / Checks stay small so the views get the space;
        Auto-level, PCB and CAM grow so their controls are not cut off.
        """
        if not (not self.bottom_tabs.isHidden()):
            return
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
            need = 170
        bottom = max(130, min(need, int(total * 0.55)))
        if self.view_tabs.currentWidget() is getattr(self, "camera_page", None):
            bottom = 130          # camera picture + its side column get the room
        self.vsplit.setSizes([max(150, total - bottom), bottom])

    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        if not self._bottom_fitted:
            self._bottom_fitted = True
            QTimer.singleShot(0, self._fit_bottom)

    def set_bottom_visible(self, on: bool) -> None:
        self.bottom_tabs.setVisible(on)
        if self.tool_btns["panel"].isChecked() != on:
            self.tool_btns["panel"].setChecked(on)
        if on:
            QTimer.singleShot(0, self._fit_bottom)

    def set_full_view(self, on: bool) -> None:
        """Views only: hide the left column and the bottom panel (F11)."""
        self.left_scroll.setVisible(not on)
        self.set_bottom_visible(not on)
        if self.tool_btns["full"].isChecked() != on:
            self.tool_btns["full"].setChecked(on)

    # ---------------------------------------------------------------- views row
    def _view_widget(self, key: str):
        return {"2d": self.preview, "3d": self.view3d, "pcb": getattr(self, "pcb_view", None),
                "camera": getattr(self, "camera_page", None)}.get(key)

    def show_view(self, key: str) -> None:
        w = self._view_widget(key)
        if w is not None and self.view_tabs.indexOf(w) >= 0:
            self.view_tabs.setCurrentWidget(w)
        self._sync_view_buttons()

    def _sync_view_buttons(self) -> None:
        if not hasattr(self, "view_btns"):
            return
        cur = self.view_tabs.currentWidget()
        for key, b in self.view_btns.items():
            w = self._view_widget(key)
            shown = w is not None and self.view_tabs.indexOf(w) >= 0 and \
                self.view_tabs.isTabVisible(self.view_tabs.indexOf(w))
            b.setVisible(shown)
            b.setChecked(w is cur and w is not None)
        is3d = self.view3d is not None and cur is self.view3d
        for k in ("top", "iso"):
            self.tool_btns[k].setVisible(is3d)

    def fit_view(self) -> None:
        cur = self.view_tabs.currentWidget()
        if cur is self.preview:
            self.preview.fit()
        elif self.view3d is not None and cur is self.view3d:
            self.view3d.set_view(getattr(self.view3d, "_last_view", "Iso") or "Iso")
        elif cur is getattr(self, "pcb_view", None):
            self.pcb_view.fit()

    def _bottom_tab_changed(self) -> None:
        if self.bottom_tabs.currentWidget() is getattr(self, "al_scroll", None):
            self.al.preview_plan()
        self._refresh_map_overlay()

    # ---------------------------------------------------------------- menus
    def _build_menu(self) -> None:
        mb = self.menu_bar

        def act(menu, text, fn, shortcut=None):
            a = QAction(text, self)
            if shortcut:
                a.setShortcut(shortcut)
            a.triggered.connect(fn)
            menu.addAction(a)
            return a

        m = mb.addMenu("&File")
        act(m, "&New project", self.new_project)
        act(m, "&Open project…", self.open_project, QKeySequence.Open)
        act(m, "&Save project", self.save_project, QKeySequence.Save)
        act(m, "Save project &as…", lambda: self.save_project(ask=True))
        m.addSeparator()
        act(m, "Add G-code &layer(s)…", self.add_layers, "Ctrl+L")
        act(m, "&Reload layers", self.reload_layers, "F5")
        act(m, "Open &PCB board (Gerber / drill)…", lambda: (self.bottom_tabs.setCurrentIndex(
            self.bottom_tabs.indexOf(self.pcb.parentWidget().parentWidget())),
            self.pcb.open_folder()), "Ctrl+B")
        m.addSeparator()
        act(m, "Open &height map…", lambda: (self._show_al_tab(), self.al.load()))
        act(m, "Sa&ve height map…", lambda: (self._show_al_tab(), self.al.save()))
        m.addSeparator()
        act(m, "Open &captures folder", self.open_capture_folder)
        m.addSeparator()
        act(m, "E&xit", self.close, QKeySequence.Quit)

        m = mb.addMenu("&View")
        for key, text, sc in (("2d", "&2D view", "Ctrl+1"), ("3d", "&3D view", "Ctrl+2"),
                              ("pcb", "&PCB view", "Ctrl+3"), ("camera", "&Camera", "Ctrl+4")):
            act(m, text, lambda _=False, k=key: self.show_view(k), sc)
        m.addSeparator()
        act(m, "Show / hide &bottom panel", lambda: self.set_bottom_visible(
            not (not self.bottom_tabs.isHidden())), "F6")
        act(m, "&Full-screen view", lambda: self.set_full_view(
            not self.tool_btns["full"].isChecked()), "F11")
        act(m, "Fi&t to job", self.fit_view)
        m.addSeparator()
        grp = QActionGroup(self)
        for key, text in (("full", "3D quality: &Standard"),
                          ("light", "3D quality: &Light (built-in graphics / big files)"),
                          ("2d", "2D &only (no 3D view)")):
            a = QAction(text, self, checkable=True)
            a.setChecked(self.settings.view_quality == key)
            a.triggered.connect(lambda _=False, k=key: self.set_quality(k))
            grp.addAction(a)
            m.addAction(a)

        m = mb.addMenu("&Settings")
        act(m, "&Preferences…", self.open_settings, "Ctrl+,")
        act(m, "&Safety switches…", self.open_settings)
        m.addSeparator()
        h = m.addAction("Machine")
        h.setEnabled(False)
        act(m, "    Set machine &home here…", self.set_home)
        act(m, "    &Camera calibration…", lambda: self.show_view("camera"))
        act(m, "    &Tool library…", self._open_tools)

        m = mb.addMenu("&Help")
        act(m, "&User guide", lambda: DocViewer("User guide", "user-guide.md", self).exec(),
            "F1")
        act(m, "&Safety features", lambda: DocViewer("Safety features", "safety.md", self).exec())
        act(m, "&Keyboard shortcuts",
            lambda: QMessageBox.information(self, "Keyboard shortcuts", SHORTCUTS))
        m.addSeparator()
        act(m, "&Copy system info", lambda: (QApplication.clipboard().setText(system_info()),
                                             self.log("System info copied to the clipboard")))
        act(m, "Open &log / captures folder", self.open_capture_folder)
        m.addSeparator()
        act(m, "Check for &updates…", lambda: self.updates.check(manual=True))
        act(m, f"&About {APP_NAME}", lambda: AboutDialog(self).exec())

    def _show_al_tab(self) -> None:
        self.set_bottom_visible(True)
        self.bottom_tabs.setCurrentWidget(self.al_scroll)

    def _open_tools(self) -> None:
        self.set_bottom_visible(True)
        self.bottom_tabs.setCurrentIndex(self.bottom_tabs.count() - 1)   # CAM: tool library

    # ================================================================ actions
    def log(self, text: str, record: bool = True) -> None:
        self.console.appendPlainText(time.strftime("%H:%M:%S ") + text)
        rec = getattr(self, "recorder", None)
        if rec is not None and record:   # the program's actions go into the capture as notes
            rec.record("EV", b"", text)

    # ---------------------------------------------------------------- recording
    def toggle_record(self, on: bool) -> None:
        from ..bridge.recorder import Recorder
        if on and getattr(self, "recorder", None) is None:
            c = self.ctl
            meta = {"app": f"{APP_NAME} {__version__} (direct)",
                    "mode": "simulator" if isinstance(c.link._t, SimMachine) else "real machine",
                    "machine port": self.port_box.currentData() or "-",
                    "mm per step": self.settings.mm_per_step[0],
                    "dir": "TX = program -> machine, RX = machine -> program, "
                           "EV = program console line"}
            try:
                self.recorder = Recorder(None, "micropcb", meta)
            except OSError as e:
                QMessageBox.warning(self, "Record", f"Cannot create the file:\n{e}")
                self.rec_btn.setChecked(False)
                return
            self.ctl.recorder = self.ctl.link.recorder = self.recorder
            self.rec_btn.setText("■ Stop rec")
            self.rec_btn.setStyleSheet("color:#c62828; font-weight:bold;")
            self.log(f"Recording to {self.recorder.path}")
        elif not on and getattr(self, "recorder", None) is not None:
            rec, self.recorder = self.recorder, None
            self.ctl.recorder = self.ctl.link.recorder = None
            path = rec.close()
            self.rec_btn.setText("● Record")
            self.rec_btn.setStyleSheet("")
            self.log(f"Recording stopped: {rec.rows} rows in {path}")
            self._analyze_file(path)

    def _analyze_file(self, path) -> None:
        from ..bridge.analyze import analyze_file
        try:
            rp, mp, rep = analyze_file(path, self.settings.mm_per_step[0])
        except Exception as e:
            self.log(f"⚠ analysis failed: {e}")
            return
        self.log(f"Report: {rp}")
        self.log(f"Motion as G-code: {mp}")
        self.log("Send the .csv (and report) for analysis.")

    def analyze_capture(self) -> None:
        from ..bridge.recorder import default_folder
        p, _ = QFileDialog.getOpenFileName(self, "Capture file", str(default_folder()),
                                           "Capture (*.csv)")
        if p:
            self._analyze_file(p)

    def open_capture_folder(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        from ..bridge.recorder import default_folder
        d = default_folder()
        d.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))

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

    _low_z_ok = False   # user accepted X/Y moves with the tool low (until Z goes up)

    def _xy_jog_allowed(self, axis: str) -> bool:
        """Ask before an X/Y jog with the tool at / near / below work Z 0: a
        sideways move there drags the bit through the board and breaks it."""
        s = self.settings
        if axis == "z" or not s.jog_low_z_warning or not self.ctl.connected:
            return True
        z = self.ctl.work_mm()[2]
        if z >= s.jog_low_z_mm:
            self._low_z_ok = False
            return True
        if self._low_z_ok:
            return True
        box = QMessageBox(QMessageBox.Warning, "Tool is low",
                          f"The tool is at work Z {z:+.3f} mm (below {s.jog_low_z_mm:g} mm).\n\n"
                          f"Moving {axis.upper()} now can drag the bit across the board and "
                          "break it. Lift Z first, or move anyway?\n\n"
                          "(Asked again after Z has been above the limit. Switch off in "
                          "Settings.)", parent=self)
        lift = box.addButton("Cancel - I will lift Z", QMessageBox.RejectRole)
        go = box.addButton(f"Move {axis.upper()} anyway", QMessageBox.AcceptRole)
        box.setDefaultButton(lift)
        box.exec()
        if box.clickedButton() is go:
            self._low_z_ok = True
            self.log(f"⚠ {axis.upper()} jog with the tool low (Z {z:+.3f} mm) - accepted")
            return True
        return False

    def _step_jog(self, axis: str, d: int) -> None:
        if not self._xy_jog_allowed(axis):
            return
        step = STEP_SIZES[self.step_group.checkedId()]
        spt = self._jog_spt()
        self._do(lambda: self._report_clamp(axis, d * step, self.ctl.jog(axis, d * step, spt)))

    def _report_clamp(self, axis: str, want: float, got: float) -> None:
        if abs(want - got) > 1e-9:
            self.log(f"Soft limit: {axis.upper()} move reduced to {got:.3f} mm")

    def _speed_changed(self, v: int) -> None:
        self.settings.jog_speed_pct = int(v)
        spt = self._jog_spt()
        self.settings.jog_steps_per_tick = spt
        mmpm = spt * self.settings.mm_per_step[0] / TICK_SECONDS * 60
        self.speed_lbl.setText(f"{v}% · {mmpm:.0f} mm/min")

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
            self._speed_changed(self.speed.value())
            self.log("Settings saved")

    # ================================================================ layers / project
    _active_layer: Layer | None = None
    _selecting = False

    def current_side(self) -> str:
        ly = self._active_layer
        return ly.side if ly is not None else self.al.side

    def _map_state(self, side: str):
        """Layer list: True = map applied, None = map measured but Apply off, False = none."""
        if not hasattr(self, "al") or self.al.map_for(side) is None:
            return False
        return True if self.al.apply_chk.isChecked() else None

    def add_layers(self) -> None:
        if self.ctl.job_active:
            return
        start = os.path.dirname(self.job_path) if self.job_path else self.settings.last_dir
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add G-code layer(s)", start or "",
            "G-code (*.nc *.gcode *.ngc *.tap *.cnc *.gc *.txt);;All files (*)")
        last = -1
        for p in paths:
            last = self.add_layer_file(p, select=False)
        if last >= 0:
            self.layers_box.set_layers(self.layers, last)

    open_job = add_layers   # old name (File > Open G-code)

    def add_layer_file(self, path: str, select: bool = True) -> int:
        """Read a G-code file into a layer (an already listed file is re-read)."""
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            QMessageBox.warning(self, "Add layer", f"Cannot load the file:\n{e}")
            return -1
        self.settings.last_dir = os.path.dirname(path)
        info, msg = jobfile.read(path, text)
        if msg:
            self.log(f"  {os.path.basename(path)}: {msg}")
        norm = os.path.normcase(os.path.abspath(path))
        for k, ly in enumerate(self.layers):
            if os.path.normcase(os.path.abspath(ly.path)) == norm:
                ly.text, ly.meta, ly.missing = text, info, False
                if select:
                    self.layers_box.set_layers(self.layers, k)
                return k
        md = float(getattr(self.settings, "job_max_depth", 0.0) or 0.0)
        ly = Layer.new(path, text, info, md, getattr(self.settings, "job_origin", "file"))
        self.layers.append(ly)
        self.log(f"Layer added: {ly.label} ({os.path.basename(path)}, {ly.side} side)")
        if select:
            self.layers_box.set_layers(self.layers, len(self.layers) - 1)
        return len(self.layers) - 1

    def load_job(self, path: str) -> None:
        """Open a G-code file: added as a layer and selected."""
        self.add_layer_file(path)

    def remove_layer(self, i: int) -> None:
        if self.ctl.job_active or not 0 <= i < len(self.layers):
            return
        ly = self.layers.pop(i)
        self.log(f"Layer removed: {ly.label}")
        if ly is self._active_layer:
            self._active_layer = None
        self.layers_box.set_layers(self.layers, min(i, len(self.layers) - 1))
        if not self.layers:
            self._clear_job()
        self._update_layer_overlays()

    def _clear_job(self) -> None:
        self.job_text = self.job_path = self.job_prog = self.job_prog_raw = None
        self.tp = self.timeline = None
        self.issues = []
        self.gcode_view.set_text("")
        for v in self._views():
            v.set_toolpath(None)
        self._fill_checks()
        self._show_job_info()

    def select_layer(self, i: int) -> None:
        if not 0 <= i < len(self.layers) or self.ctl.job_active:
            return
        ly = self.layers[i]
        if ly.missing or not ly.text:
            self.log(f"⚠ layer {ly.label}: file not found ({ly.path})")
            return
        self._selecting = True
        try:
            self._active_layer = ly
            self.al.set_side(ly.side)
            self.load_job_text(ly.text, ly.path)
            self.job_meta = ly.meta
        finally:
            self._selecting = False
        self._update_layer_overlays()
        if self.bottom_tabs.currentWidget() is self.al_scroll:
            self.al.preview_plan()

    def _layer_options_changed(self) -> None:
        """Origin / max depth / side / type of the selected layer changed."""
        self.settings.job_origin = self.origin_combo.currentData()
        self.settings.job_max_depth = (self.depth_spin.value() if self.depth_chk.isChecked()
                                       else 0.0)
        ly = self._active_layer
        if ly is not None and ly.side != self.al.side:
            self.al.set_side(ly.side)           # also re-applies the job
            return
        self._origin_reload()
        self._update_layer_overlays()

    def reload_layers(self) -> None:
        if self.ctl.job_active:
            return
        for ly in self.layers:
            try:
                with open(ly.path, encoding="utf-8", errors="replace") as f:
                    ly.text, ly.missing = f.read(), False
            except OSError:
                ly.missing = True
        self.log(f"Reloaded {len(self.layers)} layer(s) from their files")
        self.layers_box.set_layers(self.layers, self.layers_box.current())

    _overlay_cache: dict = {}

    def _layer_segments(self, ly: Layer):
        """Cutting moves of a layer as (n, 2, 2) X/Y segments (cached)."""
        import numpy as np
        key = (id(ly), len(ly.text), hash(ly.text[:2000]), ly.origin)
        hit = self._overlay_cache.get(id(ly))
        if hit is not None and hit[0] == key:
            return hit[1]
        try:
            prog = parse(ly.text, (0.0, 0.0, 0.0))
            lo = job_min_xy(prog)
            if ly.origin == "corner" and lo is not None:
                prog = shift_program(prog, -lo[0], -lo[1])
            tp = Toolpath(prog)
            s, e = tp.start, tp.end
            deep = ~tp.rapid & (np.minimum(s[:, 2], e[:, 2]) < -1e-6)
            seg = np.stack([s[deep, :2], e[deep, :2]], axis=1)
        except Exception:   # noqa: BLE001 - overlay only
            seg = np.zeros((0, 2, 2))
        self._overlay_cache[id(ly)] = (key, seg)
        return seg

    def _update_layer_overlays(self) -> None:
        """Other ticked layers are drawn faint in the 2D view under the selected one."""
        if not hasattr(self, "preview"):
            return
        out = [(self._layer_segments(ly), ly.color) for ly in self.layers
               if ly.visible and ly is not self._active_layer and ly.text]
        self.preview.set_layers([o for o in out if len(o[0])])
        self.layers_box.refresh()

    # ---------------------------------------------------------------- project file
    def new_project(self) -> None:
        if self.ctl.job_active:
            return
        if self.layers and QMessageBox.question(
                self, "New project", "Close the layers of this project?\n"
                "(Files and height maps on disk are not changed.)") != QMessageBox.Yes:
            return
        self.layers = []
        self._active_layer = None
        self.project_path = ""
        self.layers_box.set_layers(self.layers, -1)
        self._clear_job()
        self._update_layer_overlays()
        self._refresh()

    def open_project(self, path: str | None = None) -> None:
        if self.ctl.job_active:
            return
        if not path:
            path, _ = QFileDialog.getOpenFileName(
                self, "Open project", self.settings.last_dir or "",
                "MicroCNC project (*.mcnc.json *.json);;All files (*)")
            if not path:
                return
        try:
            pr = Project.load(path)
        except (OSError, ValueError, KeyError, TypeError) as e:
            QMessageBox.warning(self, "Open project", f"Cannot open the project:\n{e}")
            return
        for ly in pr.layers:
            if not ly.missing:
                ly.meta, _ = jobfile.read(ly.path, ly.text)
        for side, txt in pr.maps.items():
            try:
                self.al.maps[side] = HeightMap.from_json(txt) if txt else None
            except (ValueError, KeyError, TypeError) as e:
                self.log(f"⚠ {side} height map in the project not read: {e}")
        self.layers = pr.layers
        self._active_layer = None
        self.project_path = path
        self.settings.last_dir = os.path.dirname(path)
        self.settings.last_project = path
        miss = [ly.path for ly in pr.layers if ly.missing]
        self.log(f"Project opened: {path} ({len(pr.layers)} layer(s))")
        if miss:
            self.log("⚠ files not found: " + ", ".join(miss))
        self.layers_box.set_layers(self.layers, pr.selected)
        self.al._show_result()
        self.al.refresh()
        self.show_heightmap(self.al.hm)
        if any(self.al.maps.values()):
            self.log("Height maps restored from the project - re-zero Z (Probe + set Z 0) "
                     "on the first map point before applying them")
        self._refresh()

    project_path = ""

    def save_project(self, ask: bool = False) -> None:
        path = self.project_path
        if ask or not path:
            start = path or os.path.join(self.settings.last_dir or "", "project.mcnc.json")
            path, _ = QFileDialog.getSaveFileName(self, "Save project", start,
                                                  "MicroCNC project (*.mcnc.json)")
            if not path:
                return
            if not path.endswith(".json"):
                path += ".mcnc.json"
        pr = Project(self.layers, self.layers_box.current(),
                     {s: (m.to_json() if m is not None else None)
                      for s, m in self.al.maps.items()}, path)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(pr.to_json(path))
        except OSError as e:
            QMessageBox.warning(self, "Save project", f"Cannot save:\n{e}")
            return
        self.project_path = path
        self.settings.last_project = path
        self.log(f"Project saved: {path}")
        self._refresh()

    # ---------------------------------------------------------------- jog extras
    def _jog_spt(self) -> int:
        """Jog speed slider (%) -> steps per tick (1 … max)."""
        mx = max(1, int(self.settings.max_steps_per_tick))
        return max(1, min(mx, round(self.speed.value() / 100 * mx)))

    def z_to_safe(self) -> None:
        z = self.ctl.work_mm()[2]
        safe = self.settings.safe_height_mm
        if z < safe:
            self._do(lambda: self.ctl.jog("z", safe - z, self._jog_spt()))

    def go_to_position(self) -> None:
        vals = []
        for e in self.goto_edits:
            t = e.text().strip().replace(",", ".")
            try:
                vals.append(float(t) if t else None)
            except ValueError:
                QMessageBox.warning(self, "Go to", f"Not a number: {t}")
                return
        if all(v is None for v in vals):
            self.statusBar().showMessage("Enter X, Y and / or Z first", 4000)
            self.log("Go to: enter X, Y and / or Z first")
            return
        x, y, z = vals
        if z is not None and z < 0:
            if QMessageBox.question(
                    self, "Go to", f"Z {z:+.3f} is BELOW work zero (into the board).\n\n"
                    "Go there anyway?") != QMessageBox.Yes:
                return
        self.log("Go to work " + "  ".join(f"{a.upper()} {v:.3f}" for a, v in
                                           zip(AXES, vals) if v is not None))
        self._do(lambda: self.ctl.go_to(x, y, z))

    # ---------------------------------------------------------------- auto-level preview
    _probe_plan = None

    def show_probe_plan(self, plan) -> None:
        """Auto-level panel: points the next run will probe (updates while typing)."""
        self._probe_plan = plan
        self._refresh_map_overlay()

    def _refresh_map_overlay(self) -> None:
        if not hasattr(self, "tool_btns") or not hasattr(self, "al_scroll"):
            return
        on = self.tool_btns["map"].isChecked()
        al_tab = self.bottom_tabs.currentWidget() is self.al_scroll and \
            (not self.bottom_tabs.isHidden())
        hm = self.al.hm
        self.preview.set_probe_plan(self._probe_plan if on and al_tab and not self.al.running
                                    else None)
        for v in self._views():
            v.set_heightmap(hm if on else None)

    # ================================================================ job
    job_meta = None   # jobfile.JobInfo of the loaded G-code (side file), or None

    def set_job_info(self, info, path) -> None:
        self.job_meta = info

    def job_name(self) -> str:
        return os.path.basename(self.job_path or "job.nc")

    def load_job_text(self, text: str, path: str, keep_range: bool = False) -> None:
        """Load G-code from a file or generated by the CAM panel."""
        if not self._selecting and self._active_layer is not None:
            self._active_layer = None          # not one of the layers
            self.layers_box.clear_selection()
            self._update_layer_overlays()
        if not keep_range and self.ctl.job_range is not None:
            self.ctl.job_range = None          # another file: whole job
            if hasattr(self, "range_lbl"):
                self._show_range()
        self.job_meta = None
        try:
            raw = parse(text, self._job_start())
            raw = self._shift_job(raw)
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

    def _shift_job(self, raw: Program) -> Program:
        """Apply the Job origin choice; sets ctl.job_shift for the real run."""
        dx = dy = 0.0
        lo = job_min_xy(raw)
        if self.origin_combo.currentData() == "corner" and lo is not None:
            dx, dy = -lo[0], -lo[1]
        self.ctl.job_shift = (dx, dy)
        if abs(dx) > 1e-6 or abs(dy) > 1e-6:
            self.log(f"  Job origin: moved by X {dx:+.3f} Y {dy:+.3f} mm "
                     "(front-left corner of the job = work 0)")
            raw = shift_program(raw, dx, dy)
        else:
            self.ctl.job_shift = (0.0, 0.0)
        rng = self.ctl.job_range
        md = self.depth_spin.value() if self.depth_chk.isChecked() else 0.0
        self.ctl.job_max_depth = md
        if md:
            raw, n, deepest = limit_depth(raw, md)
            if n:
                self.log(f"  Max cut depth {md:.3f} mm: {n} move(s) raised (deepest in the "
                         f"file {deepest:.3f} mm)")
            else:
                self.log(f"  Max cut depth {md:.3f} mm: nothing deeper in the file "
                         f"(deepest {deepest:.3f} mm)")
        if rng is not None:
            try:
                raw = program_range(raw, *rng, self.settings.safe_height_mm)
            except ValueError as e:
                self.ctl.job_range = None
                self.log(f"⚠ Run part: {e} - whole job")
        return raw

    def _origin_changed(self) -> None:
        self._layer_options_changed()

    def _origin_reload(self) -> None:
        self.settings.job_max_depth = (self.depth_spin.value() if self.depth_chk.isChecked()
                                       else 0.0)
        if self.job_text is not None and not self.ctl.job_active:
            meta = self.job_meta
            self.load_job_text(self.job_text, self.job_path or "job.nc", keep_range=True)
            self.job_meta = meta

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
        if hasattr(self, "al_scroll") and self.bottom_tabs.currentWidget() is self.al_scroll:
            self.al.preview_plan()

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
        """Job section summary: what Start will run."""
        prog = self.job_prog
        if prog is None:
            self.job_info.setText("<span style='color:#8b97a5'>No layer loaded - "
                                  "File ▸ Add G-code layer (Ctrl+L)</span>")
            return
        ly = self._active_layer
        mute = "#8b97a5"

        def row(k, v):
            return (f"<tr><td style='color:{mute}; padding-right:8px'>{k}</td>"
                    f"<td align='right'>{v}</td></tr>")

        name = ly.label if ly is not None else os.path.basename(self.job_path or "job")
        rows = [row("Layer", f"<b>{name}</b>")]
        if ly is not None:
            rows.append(row("File", os.path.basename(ly.path)))
        md = self.ctl.job_max_depth
        rows.append(row("Max cut depth", f"{md:.3f} mm" if md else "off"))
        side = self.current_side()
        hm = self.al.active_map()
        if hm is not None:
            st = f"<span style='color:#7ee2a8'>{side.title()} map ✓</span>"
        elif self.al.map_for(side) is not None:
            st = f"<span style='color:{mute}'>{side.title()} map not applied</span>"
        else:
            st = f"<span style='color:{mute}'>none ({side})</span>"
        rows.append(row("Height map", st))
        r = self.ctl.job_range
        part = "whole job" if r is None else f"<b>lines {r[0]} … {r[1]}</b>"
        rows.append(row("Run", f"{part} · {_hms(self._job_secs)}"))
        n_err = sum(1 for i in self.issues if i.severity == ERROR)
        if n_err:
            rows.append(row("Checks", f"<span style='color:#ff7b72'><b>{n_err} problem(s)"
                                      "</b></span>"))
        elif self.issues:
            rows.append(row("Checks", f"<span style='color:#d29922'>{len(self.issues)} "
                                      "warning(s)</span>"))
        self.job_info.setText("<table width='100%' cellspacing='0' cellpadding='1'>"
                              + "".join(rows) + "</table>")

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
        if self.ctl.job_range is not None:
            a, b = self.ctl.job_range
            msg += (f"\n\nOnly PART of the job: G-code lines {a} … {b}. Z goes up to the safe "
                    f"height ({self.settings.safe_height_mm:g} mm), to the start of line {a}, "
                    "then down. Use the same Z zero and height map as the first run.")
        hm = self.al.active_map()
        if hm is not None and self.job_prog_raw is not None:
            out = outside_map(self.job_prog_raw, hm)
            if out:
                msg += ("\n\n⚠ The job is bigger than the height map: " + out + ".\n"
                        "Outside the map the edge height is used - measure a bigger grid "
                        "(Auto-level, From job).")
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
            if self.ctl.z_ref_xy is not None and self.ctl.connected:
                QTimer.singleShot(300, self.al.check_drift)
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
        self.layers_box.setEnabled(not active)
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
                f"{names.get(job.state, job.state)} · {frac * 100:.0f}% · "
                f"{_hms(job.elapsed())} / left {_hms(left)}")
            self.job_status.show()
            self.job_head.setText(f"line {job.line} / {job.program.line_count}")
        else:
            self.job_bar.setValue(0)
            self.job_status.setText("")
            self.job_status.hide()
            n = self.job_prog.line_count if self.job_prog is not None else 0
            self.job_head.setText(f"{n} lines" if n else "")
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
        if hasattr(self, "layers_box"):
            self.layers_box.refresh()
        if hm is not None:
            self.log(f"{self.current_side().title()} height map applied to the job")
            if self.job_prog_raw is not None:
                out = outside_map(self.job_prog_raw, hm)
                if out:
                    self.log(f"⚠ {out} - outside it the edge height is used")
        if al is not None:
            self.log(f"Camera alignment applied: {al.angle_deg:+.3f}°, "
                     f"X {al.tx:+.3f} Y {al.ty:+.3f} mm")
        if self.job_prog_raw is not None and not self.ctl.job_active:
            self._set_program(self._transformed(self.job_prog_raw))
        elif hasattr(self, "job_info"):
            self._show_job_info()

    def recheck_job(self) -> None:
        """Obstacles changed: run the checks again."""
        if getattr(self, "job_prog", None) is not None and not self.ctl.job_active:
            self._set_program(self.job_prog)

    def show_heightmap(self, hm) -> None:
        on = not hasattr(self, "tool_btns") or self.tool_btns["map"].isChecked()
        for v in self._views():
            v.set_heightmap(hm if on else None)
        if hasattr(self, "layers_box"):
            self.layers_box.refresh()
        self._refresh_map_overlay()

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
            self._sync_view_buttons()
            return
        if self.view3d is None:
            from .view3d import View3D  # OpenGL only loaded when wanted
            self.view3d = View3D(q)
            self.view_tabs.insertTab(1, self.view3d, "3D")
        else:
            self.view_tabs.setTabVisible(self.view_tabs.indexOf(self.view3d), True)
            self.view3d.set_quality(q)
        if self.tp is not None and self.view3d.tp is not self.tp:
            self.view3d.set_toolpath(self.tp)
        self._sync_views()
        self._sync_view_buttons()

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

    # ---------------------------------------------------------------- run part
    def _set_range_end(self, which: int) -> None:
        """Start (0) / end (1) of the part to run = line at the simulation position."""
        if self.timeline is None or self.job_text is None:
            return
        idx = min(max(self.play_idx, 0), len(self.timeline.lines) - 1)
        line = int(self.timeline.lines[idx])
        if not line:
            return
        rng = self.ctl.job_range
        pick = list(rng) if rng else [None, None]
        pick[which] = line
        if pick[0] is None:
            pick[0] = 1
        if pick[1] is None:
            pick[1] = self.job_prog_raw.line_count if self.job_prog_raw else line
        if pick[0] > pick[1]:
            pick = [pick[1], pick[0]]
        self.ctl.job_range = (pick[0], pick[1])
        self.log(f"Run part: G-code lines {pick[0]} … {pick[1]}")
        self._reload_job()

    def _clear_range(self) -> None:
        if self.ctl.job_range is not None:
            self.ctl.job_range = None
            self.log("Run part: whole job again")
            self._reload_job()

    def _reload_job(self) -> None:
        if self.job_text is not None and not self.ctl.job_active:
            meta = self.job_meta
            self.load_job_text(self.job_text, self.job_path or "job.nc", keep_range=True)
            self.job_meta = meta
        self._show_range()

    def _show_range(self) -> None:
        r = self.ctl.job_range
        self.range_lbl.setText("Run: whole job" if r is None else
                               f"<b>Run: lines {r[0]} … {r[1]}</b>")
        self.range_clear_btn.setEnabled(r is not None)
        self._show_job_info()

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
                    self.log(f"{d} {b.hex(' ')}", record=False)   # packets are recorded anyway
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
            QMessageBox.warning(self, APP_NAME, str(payload))
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
        # during a probe the link marks the position unknown until the machine's
        # counter arrives - don't flash the warning for that
        valid = c.position_valid or c.probing or (hasattr(self, "al") and self.al.running)
        self.connect_btn.setText("Disconnect" if con else "Connect")
        self.port_box.setEnabled(not con)

        if not con:
            st, col = ("Reconnecting…", "#d29922") if self._lost_port else ("Disconnected", "#8b97a5")
        elif rel:
            st, col = "Motors released", "#d29922"
        elif c.job_active:
            st, col = (("Job paused", "#d29922") if c.job.state == PAUSED_STATE
                       else ("Running job", "#58a6ff"))
        elif c.busy():
            st, col = "Moving", "#58a6ff"
        else:
            st, col = "Idle", "#3fb950"
        self.status_lbl.setText(f"● {'Connected · ' if con and st != 'Reconnecting…' else ''}{st}")
        self.status_lbl.setStyleSheet(
            f"font-weight:600; padding:1px 9px; border-radius:9px; color:{col};"
            f" border:1px solid {col}; background:#11161b;")

        msgs = []
        if self._lost_port:
            msgs.append("Connection lost (hardware E-stop / power off?). "
                        "Will reconnect when the machine is back.")
        if rel:
            msgs.append("Motors are RELEASED by the button on the machine. "
                        "Press it again to engage the motors.")
        if not valid:
            msgs.append("Position is NOT reliable. Jog to your work origin and press "
                        "'Zero all' (or Settings ▸ Set machine home).")
        self.banner.setText("\n".join(msgs))
        self.banner.setVisible(bool(msgs))

        al_busy = self.al.running
        for w in self.jog_widgets:
            w.setEnabled(con and not rel and (not c.job_active or c.job_paused)
                         and not al_busy)
        self.port_box.setEnabled(not con)
        self.al.refresh()

        wp, mp = c.work_mm(), c.machine_mm()
        color = "" if valid else "color:#9e9e9e;"
        for i in range(3):
            self.work_lbl[i].setText(f"{wp[i]:9.3f}")
            self.work_lbl[i].setStyleSheet(color)
            self.mach_lbl[i].setText(f"{mp[i]:9.3f}")

        sp = bool(con and c.link.spindle)
        if self.spindle_btn.isChecked() != sp:
            self.spindle_btn.setChecked(sp)
        self.spindle_lamp.set_on(sp)
        self.spindle_state.setText("Running" if sp else "Stopped")
        self.spindle_state.setStyleSheet("font-weight:600; color:%s;" % ("#3fb950" if sp else "#dfe5ec"))
        self._refresh_job()
        self._refresh_info()

    def _refresh_info(self) -> None:
        c = self.ctl
        parts = [f"Jog {self.speed_lbl.text().split('· ')[-1]}"]
        if self.project_path or self.layers:
            name = os.path.splitext(os.path.splitext(os.path.basename(
                self.project_path))[0])[0] if self.project_path else "unsaved project"
            parts.append(f"Project: {name} · {len(self.layers)} layer(s)")
        elif self.job_path:
            parts.append(f"File: {os.path.basename(self.job_path)}")
        parts.append("Soft limits ON" if c.limits_active() else "Soft limits off (home not set)")
        if getattr(self, "recorder", None) is not None:
            parts.append("<span style='color:#ff7b72'>● Recording</span>")
        txt = " &nbsp;·&nbsp; ".join(parts)
        if txt != getattr(self, "_info_txt", None):
            self._info_txt = txt
            self.info_lbl.setText(txt)

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
            if not self._xy_jog_allowed(axis):
                return True
            self._held_key = key
            self._do(lambda: self.ctl.jog_continuous(axis, d, self._jog_spt()))
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
        if getattr(self, "recorder", None) is not None:
            self.rec_btn.setChecked(False)
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
