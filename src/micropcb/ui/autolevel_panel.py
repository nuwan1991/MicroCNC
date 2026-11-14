"""Auto-level panel (Stage 2.4): probe test, grid probing, height map apply.

Probing runs in a background thread; results come back through the main
window's event bridge ("al_point", "al_done", "al_error", "al_probe").
"""
from __future__ import annotations

import json
import threading

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QSpinBox, QVBoxLayout, QWidget)

from ..autolevel import HeightMap, make_grid
from ..controller import ProbeCancelled


def _dspin(lo, hi, val, dec=2, step=0.1, suffix=" mm") -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(dec)
    s.setSingleStep(step)
    s.setValue(val)
    s.setSuffix(suffix)
    return s


class AutoLevelPanel(QWidget):
    def __init__(self, win) -> None:
        super().__init__()
        self.win = win
        self.ctl = win.ctl
        self.hm: HeightMap | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._touch_test = False

        root = QHBoxLayout(self)

        # ---- 1. probe test
        g1 = QGroupBox("1. Probe test")
        v1 = QVBoxLayout(g1)
        hint = QLabel("Clip on the PCB copper, tool in the spindle (spindle OFF).")
        hint.setWordWrap(True)
        v1.addWidget(hint)
        self.touch_btn = QPushButton("Test touch…")
        self.touch_btn.setCheckable(True)
        self.touch_btn.setToolTip("Then touch the clip to the tool by hand - the machine must report it")
        self.touch_btn.toggled.connect(self._touch_toggled)
        self.probe_here_btn = QPushButton("Probe here")
        self.probe_here_btn.setToolTip("Probe down at the current X/Y and show the result")
        self.probe_here_btn.clicked.connect(lambda: self._probe_here(zero=False))
        self.zero_probe_btn = QPushButton("Probe + set Z 0")
        self.zero_probe_btn.setToolTip("Probe down here and make the copper surface work Z 0\n"
                                       "(also use this after a tool change)")
        self.zero_probe_btn.clicked.connect(lambda: self._probe_here(zero=True))
        for b in (self.touch_btn, self.probe_here_btn, self.zero_probe_btn):
            v1.addWidget(b)
        self.probe_lbl = QLabel("")
        self.probe_lbl.setWordWrap(True)
        v1.addWidget(self.probe_lbl)
        v1.addStretch(1)
        root.addWidget(g1)

        # ---- 2. grid
        g2 = QGroupBox("2. Grid (work coordinates)")
        f2 = QFormLayout(g2)
        self.area = [_dspin(-500, 500, v, 2, 1) for v in (0, 0, 30, 20)]
        r = QGridLayout()
        for k, (lab, w) in enumerate(zip(("X from", "to", "Y from", "to"), self.area)):
            r.addWidget(QLabel(lab), k // 2, (k % 2) * 2)
            r.addWidget(w, k // 2, (k % 2) * 2 + 1)
        f2.addRow(r)
        self.from_job_btn = QPushButton("Use job size")
        self.from_job_btn.clicked.connect(self.area_from_job)
        f2.addRow(self.from_job_btn)
        self.spacing = _dspin(2, 100, 10, 1, 1)
        self.margin = _dspin(0, 20, 1, 1, 0.5)
        self.clear_mm = _dspin(0.2, 10, 1.0, 2, 0.1)
        self.first_mm = _dspin(0.5, 40, 5.0, 1, 0.5)
        self.speed = QSpinBox()
        self.speed.setRange(1, 14)
        self.speed.setValue(14)
        self.speed.setSuffix(" steps/tick")
        self.speed.setToolTip("Z steps per probe move (one move per packet, ≈57 mm/min at 14).\n"
                              "Your machine does not report touches at 3; the original uses 14.")
        f2.addRow("Spacing", self.spacing)
        f2.addRow("Margin", self.margin)
        f2.addRow("Clearance between points", self.clear_mm)
        f2.addRow("First probe max travel", self.first_mm)
        f2.addRow("Probe speed", self.speed)
        self.grid_lbl = QLabel()
        f2.addRow(self.grid_lbl)
        for w in self.area + [self.spacing, self.margin]:
            w.valueChanged.connect(self._grid_changed)
        root.addWidget(g2)

        # ---- 3. run / result
        g3 = QGroupBox("3. Measure and apply")
        v3 = QVBoxLayout(g3)
        h = QHBoxLayout()
        self.start_btn = QPushButton("▶ Start auto-level")
        self.start_btn.setStyleSheet("font-weight:bold;")
        self.start_btn.clicked.connect(self.start)
        self.stop_btn = QPushButton("■ Stop")
        self.stop_btn.clicked.connect(self.stop)
        h.addWidget(self.start_btn)
        h.addWidget(self.stop_btn)
        v3.addLayout(h)
        h = QHBoxLayout()
        self.save_btn = QPushButton("Save map…")
        self.save_btn.clicked.connect(self.save)
        self.load_btn = QPushButton("Load map…")
        self.load_btn.clicked.connect(self.load)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self.clear)
        for b in (self.save_btn, self.load_btn, self.clear_btn):
            h.addWidget(b)
        v3.addLayout(h)
        self.apply_chk = QCheckBox("Apply height map to the job")
        self.apply_chk.toggled.connect(lambda _: self.win.reapply_job())
        v3.addWidget(self.apply_chk)
        self.result_lbl = QLabel("No height map")
        self.result_lbl.setWordWrap(True)
        self.result_lbl.setTextFormat(Qt.RichText)
        v3.addWidget(self.result_lbl, 1)
        root.addWidget(g3, 1)
        self._grid_changed()
        self.refresh()

    # ------------------------------------------------------------ state
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def active_map(self) -> HeightMap | None:
        if self.apply_chk.isChecked() and self.hm is not None and self.hm.complete:
            return self.hm
        return None

    def refresh(self) -> None:
        c = self.ctl
        ready = (c.connected and not c.link.released and not c.job_active
                 and not self.running)
        for b in (self.probe_here_btn, self.zero_probe_btn):
            # also during a paused job: tool change -> Probe + set Z 0
            b.setEnabled(c.connected and not c.link.released and not self.running
                         and (not c.job_active or c.job_paused))
        self.touch_btn.setEnabled(c.connected and not self.running)
        self.start_btn.setEnabled(ready and c.position_valid)
        self.stop_btn.setEnabled(self.running)
        for w in self.area + [self.spacing, self.margin, self.clear_mm, self.first_mm,
                              self.speed, self.from_job_btn, self.load_btn, self.clear_btn]:
            w.setEnabled(not self.running)
        done = self.hm is not None and self.hm.complete
        self.save_btn.setEnabled(self.hm is not None and not self.running)
        self.apply_chk.setEnabled(done and not c.job_active)
        if not done and self.apply_chk.isChecked():
            self.apply_chk.setChecked(False)

    def _grid(self):
        a = [w.value() for w in self.area]
        return make_grid(min(a[0], a[1]), min(a[2], a[3]), max(a[0], a[1]), max(a[2], a[3]),
                         self.spacing.value(), self.margin.value())

    def _grid_changed(self) -> None:
        xs, ys = self._grid()
        n = len(xs) * len(ys)
        self.grid_lbl.setText(f"{len(xs)} × {len(ys)} = {n} points, about {n * 6 // 60 + 1} min")

    def area_from_job(self) -> None:
        prog = self.win.job_prog_raw
        b = prog.bounds() if prog is not None else None
        if not b:
            QMessageBox.information(self, "Auto-level", "Load a G-code file first.")
            return
        for w, v in zip(self.area, (b[0][0], b[1][0], b[0][1], b[1][1])):
            w.setValue(v)

    # ------------------------------------------------------------ probe test
    TOUCH_TEST_SECONDS = 20

    def _touch_toggled(self, on: bool) -> None:
        """Wiring test. The machine reports contact (reply 04) only while Z moves
        down fast; it ignores keep-alives and zero-motion packets (hardware test
        2026-11-14). So the bit wiggles 0.056 mm up and down ~7 times a second."""
        self._touch_test = on
        if not hasattr(self, "_touch_timer"):
            self._touch_timer = QTimer(self, interval=150, timeout=self._touch_poll)
        if on:
            if not self.ctl.connected:
                self.probe_lbl.setText("Connect to the machine first.")
                self.touch_btn.setChecked(False)
                return
            self._touch_until = time.monotonic() + self.TOUCH_TEST_SECONDS
            self._touch_polls = 0
            self.probe_lbl.setText("The bit now wiggles 0.06 mm. Touch the clip to the tool and hold it 1 s…")
            self._touch_timer.start()
        else:
            self._touch_timer.stop()
            self.ctl.end_touch_test()
            if self.probe_lbl.text().startswith("Touch the clip"):
                self.probe_lbl.setText("")

    def _touch_poll(self) -> None:
        c = self.ctl
        if not self._touch_test or not c.connected:
            self.touch_btn.setChecked(False)
            return
        left = self._touch_until - time.monotonic()
        if left <= 0:
            self._touch_test = False
            self.touch_btn.setChecked(False)
            self.probe_lbl.setText(
                "<b style='color:#c62828'>✗ No touch reported</b> in "
                f"{self.TOUCH_TEST_SECONDS} s ({self._touch_polls} checks). Check: clip on bare "
                "copper (not solder mask), the probe lead plugged into the machine, the bit "
                "clean. If it works in the Wegstr software, record both through the bridge.")
            self.win.log(f"Touch test: no 04 reply to {self._touch_polls} probe checks")
            return
        try:
            if c.touch_check():
                self._touch_polls += 1
        except RuntimeError as e:
            self._touch_test = False
            self.touch_btn.setChecked(False)
            self.probe_lbl.setText(str(e))
            return
        self.probe_lbl.setText(f"Touch the clip to the tool now and hold it 1 s… "
                               f"({left:.0f} s)")

    def on_probe_event(self, value: int) -> None:
        if self._touch_test:
            self._touch_test = False
            self.touch_btn.setChecked(False)
            self._touch_timer.stop()
            self.probe_lbl.setText(f"<b style='color:#2e7d32'>✓ Touch detected</b> "
                                   f"(machine Z counter {value}). Wiring OK.")

    def _probe_here(self, zero: bool) -> None:
        if self.running:
            return
        travel = self.first_mm.value()
        spt = self.speed.value()
        self._stop.clear()

        def work():
            try:
                z = self.ctl.probe(travel, spt, self._stop)
                if zero:
                    self.ctl.zero_axes("z")
                self.ctl.jog("z", self.clear_mm.value())
                self.ctl.wait_idle(10)
                self.win.bridge.event.emit("al_probe", (z, self.ctl.last_probe_value, zero))
            except Exception as e:
                self.win.bridge.event.emit("al_error", str(e))

        self._start_thread(work)

    # ------------------------------------------------------------ grid run
    def start(self) -> None:
        if self.running:
            return
        xs, ys = self._grid()
        msg = (f"Probe {len(xs) * len(ys)} points?\n\n"
               "• Clip on the copper, spindle OFF\n"
               f"• Tool above the board, not more than {self.first_mm.value():g} mm above it\n"
               "• The first point becomes work Z 0\n\n"
               "Keep a hand near E-STOP.")
        if QMessageBox.question(self, "Auto-level", msg) != QMessageBox.Yes:
            return
        self.hm = HeightMap.empty(xs, ys)
        self.hm.meta["work_offset"] = list(self.ctl.state.work_offset[:2])
        self.apply_chk.setChecked(False)
        self._stop.clear()
        hm = self.hm

        def work():
            try:
                self.ctl.run_autolevel(
                    hm, clearance_mm=self.clear_mm.value(),
                    first_travel_mm=self.first_mm.value(), probe_spt=self.speed.value(),
                    stop=self._stop,
                    on_point=lambda i, j, z: self.win.bridge.event.emit("al_point", (i, j, z)))
                self.win.bridge.event.emit("al_done", None)
            except ProbeCancelled:
                self.win.bridge.event.emit("al_error", "Auto-level stopped.")
            except Exception as e:
                self.win.bridge.event.emit("al_error", str(e))

        self._start_thread(work)
        self.win.show_heightmap(self.hm)

    def _start_thread(self, fn) -> None:
        self._thread = threading.Thread(target=fn, name="micropcb-autolevel", daemon=True)
        self._thread.start()
        self.refresh()

    def stop(self) -> None:
        self._stop.set()
        self.ctl.stop()

    # ------------------------------------------------------------ results
    def on_event(self, ev: str, payload) -> None:
        if ev == "al_point":
            i, j, z = payload
            self.win.log(f"Auto-level point X {self.hm.xs[i]:.2f} Y {self.hm.ys[j]:.2f}: "
                         f"{z:+.3f} mm")
            self._show_result()
            self.win.show_heightmap(self.hm)
        elif ev == "al_done":
            self.win.log("Auto-level finished")
            self._show_result()
            self.apply_chk.setChecked(True)
            self.win.show_heightmap(self.hm)
        elif ev == "al_probe":
            z, value, zero = payload
            txt = (f"Touch at work Z <b>{z:+.3f} mm</b> (machine counter {value}).")
            if zero:
                txt += " Copper is now work Z 0."
            self.probe_lbl.setText(txt)
            self.win.log(f"Probe: Z {z:+.3f} mm, counter {value}" + (" -> Z zeroed" if zero else ""))
        elif ev == "al_error":
            self.win.log(f"⚠ {payload}")
            self.probe_lbl.setText(f"<span style='color:#c62828'>{payload}</span>")
            if "stopped" not in str(payload).lower():
                QMessageBox.warning(self, "Auto-level", str(payload))
            self._show_result()
        for line in self.ctl.probe_log:
            self.win.log(line)
        self.ctl.probe_log.clear()
        self.refresh()

    def _show_result(self) -> None:
        hm = self.hm
        if hm is None:
            self.result_lbl.setText("No height map")
            return
        n = len(hm.xs) * len(hm.ys)
        st = hm.stats()
        txt = f"<b>{hm.probed} / {n} points</b>"
        if st:
            txt += (f"<br>min {st['min']:+.3f} · max {st['max']:+.3f} · "
                    f"range <b>{st['range']:.3f} mm</b>"
                    f"<br>tilt X {st['tilt_x_per_100']:+.3f}, Y {st['tilt_y_per_100']:+.3f} "
                    "mm per 100 mm")
        if hm.complete:
            off = hm.meta.get("work_offset")
            if off is not None and list(off) != list(self.ctl.state.work_offset[:2]):
                txt += ("<br><span style='color:#c62828'>Work X/Y zero changed since "
                        "measuring - the map no longer fits. Measure again.</span>")
        self.result_lbl.setText(txt)

    def save(self) -> None:
        if self.hm is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save height map", self.win.settings.last_dir,
                                              "Height map (*.hmap.json)")
        if path:
            if not path.endswith(".json"):
                path += ".hmap.json"
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.hm.to_json())
            self.win.log(f"Height map saved: {path}")

    def load(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load height map", self.win.settings.last_dir,
                                              "Height map (*.hmap.json *.json)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                self.hm = HeightMap.from_json(f.read())
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as e:
            QMessageBox.warning(self, "Load height map", str(e))
            return
        self.win.log(f"Height map loaded: {path}. Re-zero Z on the first point "
                     f"(X {self.hm.xs[0]:.2f} Y {self.hm.ys[0]:.2f}) with 'Probe + set Z 0'.")
        self._show_result()
        self.win.show_heightmap(self.hm)
        self.refresh()

    def clear(self) -> None:
        self.hm = None
        self.apply_chk.setChecked(False)
        self._show_result()
        self.win.show_heightmap(None)
        self.refresh()
