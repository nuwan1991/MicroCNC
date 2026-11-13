"""MicroPCB Bridge window:  python -m micropcb.bridge"""
from __future__ import annotations

import sys
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox,
                               QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
                               QLabel, QMainWindow, QMessageBox, QPlainTextEdit,
                               QPushButton, QRadioButton, QVBoxLayout, QWidget)

from .. import __version__
from ..protocol import BAUDRATE
from ..sim_machine import SimMachine
from .core import EV_LOG, EV_REASSEMBLED, EV_RX, EV_STOPPED, EV_TX, Bridge

MM_PER_STEP = 0.004


def _ports() -> list[tuple[str, str]]:
    try:
        from serial.tools import list_ports
        return sorted(((p.device, p.description) for p in list_ports.comports()),
                      key=lambda t: (len(t[0]), t[0]))
    except Exception:
        return []


def _open(port: str):
    import serial
    return serial.Serial(port, BAUDRATE, bytesize=8, parity="N", stopbits=1,
                         timeout=0.02, write_timeout=1)


class _Sig(QObject):
    event = Signal(str, object)


class BridgeWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"MicroPCB Bridge {__version__}")
        self.bridge: Bridge | None = None
        self.sim: SimMachine | None = None
        self.sig = _Sig()
        self.sig.event.connect(self._on_event, Qt.QueuedConnection)
        self._build()
        self.refresh_ports()
        self._mode_changed()
        QTimer(self, interval=150, timeout=self._tick).start()

    # ------------------------------------------------------------ UI
    def _build(self) -> None:
        c = QWidget()
        self.setCentralWidget(c)
        root = QHBoxLayout(c)
        left = QVBoxLayout()
        root.addLayout(left, 0)

        mode = QGroupBox("Mode")
        ml = QVBoxLayout(mode)
        self.rb_bridge = QRadioButton("Bridge to real machine (fixes auto-level freeze)")
        self.rb_sim = QRadioButton("Simulated machine (no hardware)")
        self.rb_bridge.setChecked(True)
        self.rb_bridge.toggled.connect(self._mode_changed)
        ml.addWidget(self.rb_bridge)
        ml.addWidget(self.rb_sim)
        left.addWidget(mode)

        ports = QGroupBox("Ports")
        pf = QFormLayout(ports)
        self.front_box = QComboBox()
        self.front_box.setToolTip("The com0com port the bridge uses (e.g. COM21).\n"
                                  "Wegstr software must use the OTHER port of the pair (e.g. COM20).")
        self.back_box = QComboBox()
        self.back_box.setToolTip("The real USB-TTL adapter (CH340) of the machine")
        refresh = QPushButton("Refresh ports")
        refresh.clicked.connect(self.refresh_ports)
        pf.addRow("Bridge side virtual port:", self.front_box)
        pf.addRow("Machine port:", self.back_box)
        pf.addRow(refresh)
        left.addWidget(ports)

        row = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.start_btn.setMinimumHeight(40)
        self.start_btn.clicked.connect(self.toggle)
        self.state_lbl = QLabel("● Stopped")
        self.state_lbl.setStyleSheet("font-weight:bold; color:#757575;")
        row.addWidget(self.start_btn)
        row.addWidget(self.state_lbl)
        left.addLayout(row)

        # --- simulator controls
        self.simbox = QGroupBox("Simulated machine")
        sg = QGridLayout(self.simbox)
        mono = QFont("Consolas", 13, QFont.Bold)
        self.pos_lbl = QLabel("X 0.000  Y 0.000  Z 0.000")
        self.pos_lbl.setFont(mono)
        self.spindle_lbl = QLabel("Spindle: off")
        sg.addWidget(self.pos_lbl, 0, 0, 1, 3)
        sg.addWidget(self.spindle_lbl, 1, 0, 1, 3)

        self.surf_z = QDoubleSpinBox(minimum=-40, maximum=0, decimals=3, singleStep=0.1, value=-10)
        self.tilt_x = QDoubleSpinBox(minimum=-5, maximum=5, decimals=3, singleStep=0.01, value=0)
        self.tilt_y = QDoubleSpinBox(minimum=-5, maximum=5, decimals=3, singleStep=0.01, value=0)
        self.warp = QDoubleSpinBox(minimum=-2, maximum=2, decimals=3, singleStep=0.01, value=0)
        self.probe_on = QCheckBox("Probe surface enabled")
        self.probe_on.setChecked(True)
        form = QFormLayout()
        form.addRow(self.probe_on)
        form.addRow("Surface Z at start position (mm):", self.surf_z)
        form.addRow("Tilt X (mm per 100 mm):", self.tilt_x)
        form.addRow("Tilt Y (mm per 100 mm):", self.tilt_y)
        form.addRow("Centre bulge (mm, 140×200 board):", self.warp)
        sg.addLayout(form, 2, 0, 1, 3)
        for s in (self.surf_z, self.tilt_x, self.tilt_y, self.warp):
            s.valueChanged.connect(self._cache_surface)
        self.probe_on.toggled.connect(self._cache_surface)
        self._cache_surface()

        self.rel_btn = QPushButton("Release button")
        self.rel_btn.setCheckable(True)
        self.rel_btn.setToolTip("Simulate the motor-release button (machine replies 03)")
        self.rel_btn.toggled.connect(lambda on: self._sim_set("released", on))
        self.ovl_btn = QPushButton("Spindle overload")
        self.ovl_btn.setCheckable(True)
        self.ovl_btn.toggled.connect(lambda on: self._sim_set("overload", on))
        self.pwr_btn = QPushButton("Power cut")
        self.pwr_btn.setCheckable(True)
        self.pwr_btn.setToolTip("Machine stops answering (like the hardware E-stop)")
        self.pwr_btn.toggled.connect(lambda on: self._sim_set("silent", on))
        sg.addWidget(self.rel_btn, 3, 0)
        sg.addWidget(self.ovl_btn, 3, 1)
        sg.addWidget(self.pwr_btn, 3, 2)
        reset = QPushButton("Reset position to 0,0,0")
        reset.clicked.connect(self._sim_reset)
        sg.addWidget(reset, 4, 0, 1, 3)
        left.addWidget(self.simbox)
        left.addStretch(1)

        # --- log
        logbox = QGroupBox("Traffic")
        ll = QVBoxLayout(logbox)
        self.stats_lbl = QLabel()
        ll.addWidget(self.stats_lbl)
        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setMaximumBlockCount(3000)
        self.log_view.setFont(QFont("Consolas", 9))
        ll.addWidget(self.log_view)
        lr = QHBoxLayout()
        self.show_traffic = QCheckBox("Show packets")
        self.show_ka = QCheckBox("incl. keep-alive")
        clear = QPushButton("Clear")
        clear.clicked.connect(self.log_view.clear)
        lr.addWidget(self.show_traffic)
        lr.addWidget(self.show_ka)
        lr.addStretch(1)
        lr.addWidget(clear)
        ll.addLayout(lr)
        root.addWidget(logbox, 1)
        self.resize(1050, 640)

    def log(self, s: str) -> None:
        self.log_view.appendPlainText(time.strftime("%H:%M:%S ") + s)

    def refresh_ports(self) -> None:
        ports = _ports()
        for box in (self.front_box, self.back_box):
            cur = box.currentData()
            box.clear()
            for dev, desc in ports:
                box.addItem(f"{dev} — {desc}", dev)
            i = box.findData(cur)
            if i >= 0:
                box.setCurrentIndex(i)
        # sensible defaults
        for i in range(self.back_box.count()):
            if "CH340" in self.back_box.itemText(i).upper():
                self.back_box.setCurrentIndex(i)
                break
        for i in range(self.front_box.count()):
            t = self.front_box.itemText(i).lower()
            if "com0com" in t or "null-modem" in t or "virtual" in t:
                self.front_box.setCurrentIndex(i)
                break

    def _mode_changed(self) -> None:
        sim = self.rb_sim.isChecked()
        self.back_box.setEnabled(not sim and not self.running)
        self.simbox.setVisible(sim)

    @property
    def running(self) -> bool:
        return bool(self.bridge and self.bridge.running)

    # ------------------------------------------------------------ simulator
    def _cache_surface(self, *_: object) -> None:
        # plain values: the simulator reads them from the bridge thread
        self._sp = (self.probe_on.isChecked(), self.surf_z.value(), self.tilt_x.value(),
                    self.tilt_y.value(), self.warp.value())

    def _surface(self, x: int, y: int) -> int | None:
        on, base, tx, ty, w = self._sp
        if not on:
            return None
        xm, ym = x * MM_PER_STEP, y * MM_PER_STEP
        z = base + tx * xm / 100 + ty * ym / 100
        if w:
            u, v = (xm - 70) / 70, (ym - 100) / 100
            z += w * max(0.0, 1 - (u * u + v * v))
        return round(z / MM_PER_STEP)

    def _sim_set(self, attr: str, on: bool) -> None:
        if self.sim:
            setattr(self.sim, attr, on)
            self.log(f"Simulator: {attr} = {on}")

    def _sim_reset(self) -> None:
        if self.sim:
            self.sim.pos = [0, 0, 0]
            self.log("Simulator: position reset")

    # ------------------------------------------------------------ run
    def toggle(self) -> None:
        if self.running:
            self.bridge.stop("stopped by user")
            return
        front_port = self.front_box.currentData()
        if not front_port:
            QMessageBox.warning(self, "Bridge", "Select the virtual port (install com0com first).")
            return
        try:
            front = _open(front_port)
        except Exception as e:
            QMessageBox.warning(self, "Bridge", f"Cannot open {front_port}:\n{e}")
            return
        if self.rb_sim.isChecked():
            # machine Z counter with its own origin, like a real machine after power-on
            self.sim = SimMachine(realtime=True, surface=self._surface, fw_z_offset=-7000)
            for b, a in ((self.rel_btn, "released"), (self.ovl_btn, "overload"),
                         (self.pwr_btn, "silent")):
                setattr(self.sim, a, b.isChecked())
            back = self.sim
            what = "simulated machine"
        else:
            self.sim = None
            back_port = self.back_box.currentData()
            if not back_port or back_port == front_port:
                front.close()
                QMessageBox.warning(self, "Bridge", "Select the machine's real COM port.")
                return
            try:
                back = _open(back_port)
            except Exception as e:
                front.close()
                QMessageBox.warning(self, "Bridge", f"Cannot open {back_port}:\n{e}\n\n"
                                    "Close any other program using it.")
                return
            what = back_port
        self.bridge = Bridge(front, back, lambda e, p: self.sig.event.emit(e, p))
        self.bridge.start()
        self.log(f"Started: {front_port} ⇄ {what}. Connect Wegstr software to the "
                 f"other port of the com0com pair.")
        self._update_state()

    def _on_event(self, ev: str, p: object) -> None:
        if ev in (EV_TX, EV_RX):
            if self.show_traffic.isChecked():
                b: bytes = p  # type: ignore[assignment]
                ka = (ev == EV_TX and len(b) == 2) or (ev == EV_RX and getattr(self, "_ka", False))
                if ev == EV_TX:
                    self._ka = len(b) == 2
                if not ka or self.show_ka.isChecked():
                    self.log(f"{'PC→M' if ev == EV_TX else 'M→PC'}  {b.hex(' ')}")
        elif ev == EV_REASSEMBLED:
            self.log(f"✔ split probe reply re-assembled (value {p})")
        elif ev == EV_LOG:
            self.log(str(p))
        elif ev == EV_STOPPED:
            self.log(f"Stopped ({p})")
            self._update_state()

    def _update_state(self) -> None:
        r = self.running
        self.start_btn.setText("Stop" if r else "Start")
        self.state_lbl.setText("● Running" if r else "● Stopped")
        self.state_lbl.setStyleSheet(f"font-weight:bold; color:{'#2e7d32' if r else '#757575'};")
        self.front_box.setEnabled(not r)
        self.rb_bridge.setEnabled(not r)
        self.rb_sim.setEnabled(not r)
        self._mode_changed()

    def _tick(self) -> None:
        if self.bridge:
            s = self.bridge.stats
            self.stats_lbl.setText(f"Packets {s['packets']}   Replies {s['replies']}   "
                                   f"Probe touches {s['probes']}   Re-assembled {s['reassembled']}")
        if self.sim:
            x, y, z = (v * MM_PER_STEP for v in self.sim.pos)
            self.pos_lbl.setText(f"X {x:8.3f}  Y {y:8.3f}  Z {z:8.3f}")
            self.spindle_lbl.setText(f"Spindle: {'ON' if self.sim.spindle else 'off'}"
                                     f"   Performance byte: {self.sim.performance}")

    def closeEvent(self, e) -> None:  # noqa: N802
        if self.running:
            self.bridge.stop("closing")
        e.accept()


def main() -> int:
    app = QApplication(sys.argv)
    w = BridgeWindow()
    w.show()
    return app.exec()
