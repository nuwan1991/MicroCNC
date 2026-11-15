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

from ..autolevel import AutoPlan, HeightMap, job_cut_points, make_grid
from ..gcode import cut_bounds
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
        self.drift_btn = QPushButton("Check Z drift")
        self.drift_btn.setToolTip("Go back to where Z 0 was set and touch the copper again.\n"
                                  "0.000 = Z did not drift (use after a job, clip on)")
        self.drift_btn.clicked.connect(lambda: self.check_drift(ask=False))
        for b in (self.touch_btn, self.probe_here_btn, self.zero_probe_btn, self.drift_btn):
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
        self.margin = _dspin(0, 20, 0, 1, 0.5)
        self.margin.setToolTip("Extra distance around the area. Keep every probe point ON the "
                               "copper:\noff the board the touch is wrong (map 7: the X -1 / "
                               "Y -1 points read 0.02-0.06 mm high)")
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
        self.job_pts = QCheckBox("Job-based points")
        self.job_pts.setToolTip(
            "Spacing = the fine grid. Probe a coarse grid (every N-th point) plus the fine\n"
            "points near small gaps of the job; more fine points only where the board bends;\n"
            "no fine points where the job does not cut. The rest is filled from the coarse grid.")
        self.job_every = QSpinBox()
        self.job_every.setRange(2, 6)
        self.job_every.setValue(3)
        self.job_every.setPrefix("coarse every ")
        self.job_every.setEnabled(False)
        self.job_pts.toggled.connect(self.job_every.setEnabled)
        jr = QHBoxLayout()
        jr.addWidget(self.job_pts)
        jr.addWidget(self.job_every)
        f2.addRow(jr)
        self.grid_lbl = QLabel()
        self.grid_lbl.setWordWrap(True)
        f2.addRow(self.grid_lbl)
        for w in self.area + [self.spacing, self.margin, self.job_every]:
            w.valueChanged.connect(self._grid_changed)
        self.job_pts.toggled.connect(lambda _: self._grid_changed())
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
        self.why_lbl = QLabel("")          # why Start is disabled
        self.why_lbl.setWordWrap(True)
        self.why_lbl.setStyleSheet("color:#c62828;")
        v3.addWidget(self.why_lbl)
        self.time_lbl = QLabel("")         # elapsed / remaining while measuring
        self.time_lbl.setStyleSheet("font-weight:bold;")
        self.time_lbl.setVisible(False)
        v3.addWidget(self.time_lbl)
        self._clock = QTimer(self, interval=1000, timeout=self._tick_clock)
        self._t0 = 0.0
        self._t_points: list[float] = []   # time of each finished point
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

    DRIFT_LIMIT = 0.03   # mm, closing re-probe of the first point

    def refresh(self) -> None:
        c = self.ctl
        ready = (c.connected and not c.link.released and not c.job_active
                 and not self.running)
        for b in (self.probe_here_btn, self.zero_probe_btn):
            # also during a paused job: tool change -> Probe + set Z 0
            b.setEnabled(c.connected and not c.link.released and not self.running
                         and (not c.job_active or c.job_paused))
        self.touch_btn.setEnabled(c.connected and not self.running)
        self.drift_btn.setEnabled(ready and c.position_valid and c.z_ref_xy is not None)
        self.start_btn.setEnabled(ready and c.position_valid)
        why = ""
        if self.running:
            why = ""
        elif not c.connected:
            why = "Connect to the machine to start."
        elif c.link.released:
            why = "Motors are released - press the machine's release button again."
        elif c.job_active:
            why = "A job is active - stop it first."
        elif not c.position_valid:
            why = ("Position not trusted (connection lost, motors released or a touch "
                   "during a move). Set work zero again: Zero all, or Zero X, Y and Z.")
        self.why_lbl.setText(why)
        self.why_lbl.setVisible(bool(why))
        self.stop_btn.setEnabled(self.running)
        for w in self.area + [self.spacing, self.margin, self.job_pts, self.clear_mm,
                              self.first_mm,
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

    def _fine(self):
        """Small-gap places of the loaded job: side file, else the CAM panel's board."""
        info = getattr(self.win, "job_meta", None)
        if info is not None and getattr(info, "fine", None):
            return list(info.fine)
        cam = getattr(self.win, "cam", None)
        if cam is not None and hasattr(cam, "fine_spots") and \
                getattr(self.win, "job_text", None) is not None and cam.text == self.win.job_text:
            return cam.fine_spots()
        return []

    def _plan(self, xs, ys):
        if not self.job_pts.isChecked():
            return None
        prog = getattr(self.win, "job_prog_raw", None)
        cuts = job_cut_points(prog) if prog is not None else None
        return AutoPlan.build(xs, ys, cuts, self._fine(), self.job_every.value())

    def _grid_changed(self) -> None:
        xs, ys = self._grid()
        n = len(xs) * len(ys)
        plan = self._plan(xs, ys)
        if plan is None:
            self.grid_lbl.setText(f"{len(xs)} × {len(ys)} = {n} points, "
                                  f"about {n * 6 // 60 + 1} min")
            return
        first, _ = plan.count()
        fine = len(self._fine())
        self.grid_lbl.setText(
            f"{len(xs)} × {len(ys)} fine grid: about {first} of {n} points probed "
            f"(about {first * 6 // 60 + 1} min) + more where the board bends. "
            + (f"{fine} small-gap place(s) from the job." if fine else
               "No small-gap data (open a G-code with its side file, or Generate in CAM)."))

    def area_from_job(self) -> None:
        prog = self.win.job_prog_raw
        # cutting moves only: the start move (from wherever the machine is) and
        # travel moves would put probe points outside the board (2026-11-15)
        b = cut_bounds(prog) if prog is not None else None
        if not b:
            QMessageBox.information(self, "Auto-level", "Load a G-code file with cuts first.")
            return
        for w, v in zip(self.area, (b[0], b[2], b[1], b[3])):
            w.setValue(v)
        self.win.log(f"Auto-level area = cut area of the job: X {b[0]:.2f} … {b[2]:.2f}, "
                     f"Y {b[1]:.2f} … {b[3]:.2f} mm (+ margin {self.margin.value():g} mm)")

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
        if not zero and not self.ctl.position_valid:
            # 2026-11-15: after an unclean restart Probe here showed Z +10.2 mm -
            # the Z position was the last saved one, not where the tool really was
            QMessageBox.warning(
                self, "Probe here",
                "The Z position is not trusted (the program was closed without a normal "
                "exit, the connection was lost or the motors were released), so a probe "
                "here would show a wrong Z.\n\n"
                "Jog to the board corner, Zero X and Zero Y, then use 'Probe + set Z 0'.")
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
        plan = self._plan(xs, ys)
        self._total = plan.count()[0] if plan is not None else len(xs) * len(ys)
        msg = ((f"Probe about {self._total} of {len(xs) * len(ys)} points (job-based, more "
                "where the board bends)?\n\n") if plan is not None else
               f"Probe {len(xs) * len(ys)} points?\n\n") + (
               "• Clip on the copper, spindle OFF\n"
               f"• Tool above the board, not more than {self.first_mm.value():g} mm above it\n"
               "• The first point becomes work Z 0\n\n"
               "Keep a hand near E-STOP.")
        prog = getattr(self.win, "job_prog_raw", None)
        cb = cut_bounds(prog) if prog is not None else None
        if cb is not None and (xs[0] < min(cb[0], 0) - 0.01 or ys[0] < min(cb[1], 0) - 0.01):
            msg += ("\n\n⚠ Some probe points are left of / in front of work 0 "
                    f"(X from {xs[0]:.2f}, Y from {ys[0]:.2f}). If work 0 is at the board edge "
                    "they are off the copper and give wrong heights - set Margin 0.")
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
                    stop=self._stop, plan=plan,
                    on_point=lambda i, j, z: self.win.bridge.event.emit("al_point", (i, j, z)))
                self.win.bridge.event.emit("al_done", None)
            except ProbeCancelled:
                self.win.bridge.event.emit("al_error", "Auto-level stopped.")
            except Exception as e:
                self.win.bridge.event.emit("al_error", str(e))

        self._start_thread(work)
        self._t0 = time.monotonic()
        self._t_points = []
        self.time_lbl.setVisible(True)
        self._clock.start()
        self._tick_clock()
        self.win.show_heightmap(self.hm)

    @staticmethod
    def _mmss(sec: float) -> str:
        sec = int(max(sec, 0) + 0.5)
        return f"{sec // 60}:{sec % 60:02d}"

    def _tick_clock(self) -> None:
        if not self.running and time.monotonic() - self._t0 > 2:
            self._clock.stop()
        hm = self.hm
        el = time.monotonic() - self._t0
        txt = f"⏱ {self._mmss(el)} elapsed"
        if hm is not None:
            n_pts = max(getattr(self, "_total", hm.z.size), len(self._t_points))
            total = n_pts + 1              # + closing re-probe of the first point
            done = min(len(self._t_points), n_pts)
            if done >= 2:
                # average of the points so far (the first one has a longer travel)
                per = (self._t_points[-1] - self._t_points[0]) / (done - 1)
                txt += f" · about {self._mmss(per * (total - done))} left"
            txt += f" · point {done} / {n_pts}"
        self.time_lbl.setText(txt)

    def _start_thread(self, fn) -> None:
        self._thread = threading.Thread(target=fn, name="micropcb-autolevel", daemon=True)
        self._thread.start()
        self.refresh()

    def check_drift(self, ask: bool = True) -> None:
        """Touch the copper where Z 0 was set again (after a job)."""
        if self.running or self.ctl.z_ref_xy is None:
            return
        if ask and QMessageBox.question(
                self, "Z drift check",
                "Check whether Z drifted during the job?\n\n"
                "Put the probe clip on the copper, then press Yes. The tool goes back to "
                "where Z 0 was set and touches the copper (should read 0.000).")                 != QMessageBox.Yes:
            return
        self._stop.clear()

        def work():
            try:
                z = self.ctl.check_z_drift(clearance_mm=self.clear_mm.value(),
                                           probe_spt=self.speed.value(), stop=self._stop)
                self.win.bridge.event.emit("al_drift", z)
            except ProbeCancelled:
                self.win.bridge.event.emit("al_error", "Drift check stopped.")
            except Exception as e:
                self.win.bridge.event.emit("al_error", f"Drift check: {e}")

        self._start_thread(work)

    def stop(self) -> None:
        self._stop.set()
        self.ctl.stop()

    # ------------------------------------------------------------ results
    def on_event(self, ev: str, payload) -> None:
        if ev == "al_point":
            i, j, z = payload
            self._t_points.append(time.monotonic())
            self.win.log(f"Auto-level point X {self.hm.xs[i]:.2f} Y {self.hm.ys[j]:.2f}: "
                         f"{z:+.3f} mm")
            self._show_result()
            self.win.show_heightmap(self.hm)
        elif ev == "al_done":
            self._clock.stop()
            self.time_lbl.setText(f"⏱ Finished in {self._mmss(time.monotonic() - self._t0)}")
            sus = self.hm.meta.get("suspect") or []
            fixed = len(self.hm.meta.get("reprobed") or []) - len(sus)
            if fixed > 0:
                self.win.log(f"Auto-level: {fixed} point(s) re-probed because they were off "
                             "their neighbours - corrected")
            if sus:
                pts = ", ".join(f"X {x:.1f} Y {y:.1f} ({r:+.3f} mm)" for x, y, r in sus)
                self.win.log(f"⚠ auto-level: still off after re-probing: {pts}")
                QMessageBox.warning(
                    self, "Auto-level",
                    "These points measured off their neighbours twice:\n\n" + pts +
                    "\n\nCheck the board there (dirt, burr, chip, clip wire). The map is "
                    "kept as measured - if the board is clean there, it is a real bump.")
            drift = float(self.hm.meta.get("drift_mm", 0.0))
            self.win.log(f"Auto-level finished - first point re-probed: {drift:+.3f} mm")
            self._show_result()
            if abs(drift) <= self.DRIFT_LIMIT:
                self.apply_chk.setChecked(True)
            else:
                self.win.log("⚠ height map NOT applied: Z drifted during auto-level")
                QMessageBox.warning(
                    self, "Auto-level",
                    f"The first point measured again at {drift:+.3f} mm instead of 0.\n\n"
                    "Z position drifted during the grid (lost or not executed steps), so the "
                    "height map and Z zero are not reliable and the map was NOT applied.\n\n"
                    "Do 'Probe + set Z 0' and run auto-level again. If it happens again, "
                    "record the run with the bridge and send it.")
            self.win.show_heightmap(self.hm)
        elif ev == "al_probe":
            z, value, zero = payload
            txt = (f"Touch at work Z <b>{z:+.3f} mm</b> (machine counter {value}).")
            if zero:
                txt += " Copper is now work Z 0."
            self.probe_lbl.setText(txt)
            self.win.log(f"Probe: Z {z:+.3f} mm, counter {value}" + (" -> Z zeroed" if zero else ""))
        elif ev == "al_drift":
            z = float(payload)
            ok = abs(z) <= self.DRIFT_LIMIT
            self.win.log(f"Z drift check at the Z 0 point: {z:+.3f} mm" +
                         ("" if ok else " ⚠"))
            self.probe_lbl.setText(
                f"Z drift check: copper at <b>{z:+.3f} mm</b> where Z 0 was set" +
                ("" if ok else " - <span style='color:#c62828'>Z drifted</span>"))
            if ok:
                QMessageBox.information(self, "Z drift check",
                                        f"Copper at the Z 0 point: {z:+.3f} mm.\n\nNo drift.")
            else:
                QMessageBox.warning(
                    self, "Z drift check",
                    f"Copper at the Z 0 point now measures {z:+.3f} mm instead of 0.\n\n"
                    + ("The tool ended LOWER than at the start (cuts got deeper during the "
                       "job). " if z > 0 else
                       "The tool ended HIGHER than at the start (cuts got shallower). ")
                    + "Usual causes: bit slipping in the collet, Z losing steps, heat.\n"
                    "Tighten the collet, do Probe + set Z 0 and record the next job.")
        elif ev == "al_error":
            self._clock.stop()
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
