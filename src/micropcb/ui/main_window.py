"""MicroPCB main window (milestone 1.2): connect, DRO, jog, zero, spindle, E-stop."""
from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QFont, QKeySequence
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox,
                               QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                               QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
                               QSlider, QVBoxLayout, QWidget, QAbstractSpinBox,
                               QLineEdit)

from .. import __version__
from ..controller import AXES, EV_STATE, MachineController
from ..protocol import TICK_SECONDS
from ..serial_link import (EV_CONNECTED, EV_DISCONNECTED, EV_LOST, EV_OVERLOAD,
                           EV_PROBE, EV_RELEASED, EV_TRAFFIC, list_ports, probe_port)
from ..settings import MachineState, Settings
from ..sim_machine import SimMachine
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
        self.estop_btn = QPushButton("E-STOP\n(Esc)")
        self.estop_btn.setMinimumSize(130, 56)
        self.estop_btn.setStyleSheet(
            "QPushButton { background:#c62828; color:white; font-size:16px;"
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
        self.banner.setStyleSheet("background:#ffb300; color:black; padding:8px;"
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
        con = QGroupBox("Console")
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
        body.addWidget(con, 1)

        self.home_lbl = QLabel()
        self.statusBar().addPermanentWidget(self.home_lbl)
        self.jog_widgets = [jog, self.zero_all_btn, self.goto_zero_btn, spg]
        self.resize(1100, 720)

    def _build_menu(self) -> None:
        m = self.menuBar().addMenu("&File")
        a = QAction("&Settings…", self)
        a.triggered.connect(self.open_settings)
        m.addAction(a)
        m.addSeparator()
        a = QAction("E&xit", self)
        a.setShortcut(QKeySequence.Quit)
        a.triggered.connect(self.close)
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
                self.ctl.connect(transport=SimMachine())
            else:
                self.ctl.connect(port)
            self.settings.save()
        except Exception as e:
            self.log(f"⚠ Connect failed: {e}")
            QMessageBox.warning(self, "Connect", f"Could not connect:\n{e}\n\n"
                                "Close the original Wegstr software and check the cable.")
        self._refresh()

    def emergency_stop(self) -> None:
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

        for w in self.jog_widgets:
            w.setEnabled(con and not rel)

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
        if key not in JOG_KEYS or not self.kb_jog.isChecked():
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
        if self.ctl.connected and self.ctl.busy():
            r = QMessageBox.question(self, "Exit", "The machine is moving. Stop and exit?")
            if r != QMessageBox.Yes:
                e.ignore()
                return
        self.reconnect_timer.stop()
        if self.ctl.connected and self.ctl.link.spindle:
            self.ctl.emergency_stop()
            self.ctl.wait_idle(1)
        self.ctl.disconnect()
        self.settings.save()
        e.accept()
