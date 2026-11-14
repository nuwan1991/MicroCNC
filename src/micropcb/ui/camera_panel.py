"""Camera panel (Stage 4): camera, bed calibration, board alignment, obstacles."""
from __future__ import annotations

import json
import math

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainterPath
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QListWidget, QMessageBox,
                               QPushButton, QSlider, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from ..camera.align import (AlignPoint, Alignment, board_features, fit_alignment,
                            nearest_feature, refine_feature)
from ..camera.calib import CalibPoint, CameraCalib, calibrate_lens, mark_job, mark_points
from ..camera.device import SIM, Camera, CameraInfo, SimScene, list_cameras
from ..camera.obstacles import Obstacle, board_overlap, check_obstacles, detect_obstacles
from ..pcb.board import as_polygons
from ..settings import app_dir

RESOLUTIONS = [(640, 480), (1280, 720), (1920, 1080)]
SIM_ERROR = Alignment(0.8, 1.2, -0.6)   # simulated board placed slightly wrong


def _btn(text, fn, tip="", checkable=False):
    b = QPushButton(text)
    b.setCheckable(checkable)
    if tip:
        b.setToolTip(tip)
    (b.toggled if checkable else b.clicked).connect(fn)
    return b


def _spin(lo, hi, val, suffix=" mm", dec=1, tip=""):
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(dec)
    s.setValue(val)
    s.setSuffix(suffix)
    s.setToolTip(tip)
    return s


class CameraPanel(QWidget):
    def __init__(self, win, view) -> None:
        super().__init__()
        self.win = win
        self.view = view
        self.camera: Camera | None = None
        self.cameras: list[CameraInfo] = []
        self.calibs: dict[str, CameraCalib] = {}
        self.obstacles: list[Obstacle] = []
        self.align_points: list[AlignPoint] = []
        self.alignment: Alignment | None = None
        self.sim = SimScene()
        self.marks: list[tuple[float, float]] = []      # bed mm of cut marks
        self.click_mode = ""                             # tip | marks | align
        self.reference = None                            # empty-bed picture
        self.reference_pos = None
        self.lens_frames: list = []
        self._last_id = -1
        self._load_config()

        root = QVBoxLayout(self)          # one column beside the camera picture
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(6)
        root.addWidget(self._build_camera())
        root.addWidget(self._build_calib())
        root.addWidget(self._build_align())
        root.addWidget(self._build_obstacles())
        root.addStretch(1)
        self.setMaximumWidth(380)

        view.clicked.connect(self._view_clicked)
        view.rectDrawn.connect(self._rect_drawn)
        view.cursor_text = self._cursor_text
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(66)
        self.refresh_cameras(probe=False)
        self._show_calib()

    # ================================================================ build
    def _build_camera(self) -> QGroupBox:
        g = QGroupBox("Camera")
        v = QVBoxLayout(g)
        v.setSpacing(3)
        self.cam_combo = QComboBox()
        h = QHBoxLayout()
        h.addWidget(self.cam_combo, 1)
        h.addWidget(_btn("↻", lambda: self.refresh_cameras(True), "Look for USB cameras"))
        v.addLayout(h)
        self.res_combo = QComboBox()
        for w, hh in RESOLUTIONS:
            self.res_combo.addItem(f"{w} × {hh}", (w, hh))
        self.res_combo.setCurrentIndex(1)
        self.start_btn = _btn("Start", self.toggle_camera, checkable=True)
        rh = QHBoxLayout()
        rh.addWidget(self.res_combo, 1)
        rh.addWidget(self.start_btn)
        v.addLayout(rh)
        self.driver_btn = _btn("Driver settings…", self._driver,
                               "Camera's own settings window (focus, exposure, white balance)")
        v.addWidget(self.driver_btn)
        self.autofocus = QCheckBox("Auto focus")
        self.autofocus.toggled.connect(lambda on: self.camera and self.camera.set_auto("focus", on))
        v.addWidget(self.autofocus)
        self.focus = QSlider(Qt.Horizontal)
        self.focus.setRange(0, 255)
        self.focus.valueChanged.connect(lambda x: self.camera and self.camera.set("focus", x))
        self.focus_lbl = QLabel("Focus")
        fh = QHBoxLayout()
        fh.addWidget(self.focus_lbl)
        fh.addWidget(self.focus, 1)
        v.addLayout(fh)
        v.addWidget(QLabel("Show on picture:"))
        self.show = {}
        sg = QGridLayout()
        sg.setVerticalSpacing(2)
        for k, (key, text) in enumerate((("pcb", "PCB"), ("path", "Toolpath"),
                                         ("tool", "Tool"), ("obst", "Obstacles"),
                                         ("grid", "10 mm grid"))):
            c = QCheckBox(text)
            c.setChecked(key != "grid")
            c.toggled.connect(lambda _: self._update_overlay())
            self.show[key] = c
            sg.addWidget(c, k // 2, k % 2)
        v.addLayout(sg)
        v.addStretch(1)
        self._camera_controls()
        return g

    def _build_calib(self) -> QGroupBox:
        g = QGroupBox("Bed calibration (once per camera mount)")
        grid = QGridLayout(g)
        grid.addWidget(QLabel("Bed moves under camera with"), 0, 0, 1, 2)
        self.bed_x = QCheckBox("X")
        self.bed_y = QCheckBox("Y")
        h = QHBoxLayout()
        h.addWidget(self.bed_x)
        h.addWidget(self.bed_y)
        h.addStretch(1)
        grid.addLayout(h, 0, 2)
        for c in (self.bed_x, self.bed_y):
            c.setToolTip("Tick the axes where the bed (table) moves, not the spindle")
            c.toggled.connect(self._bed_axes_changed)
        self.cam_h = _spin(0, 2000, 300, tip="Lens height above the bed - corrects parallax "
                                             "for the board top. 0 = no correction")
        self.board_t = _spin(0, 10, 1.6, dec=2, tip="Board thickness: the camera sees the "
                                                     "copper this much above the bed")
        grid.addWidget(QLabel("Camera height"), 1, 0)
        grid.addWidget(self.cam_h, 1, 1, 1, 2)
        grid.addWidget(QLabel("Board thickness"), 2, 0)
        grid.addWidget(self.board_t, 2, 1, 1, 2)
        self.cam_h.valueChanged.connect(self._height_changed)
        self.board_t.valueChanged.connect(lambda _: self._update_overlay())
        grid.addWidget(_btn("1. Mark job", self.make_mark_job,
                            "Job that spot-marks 9 points on a scrap board / the bed "
                            "(load it, set zero, run it)"), 3, 0)
        self.marks_btn = _btn("2. Click marks", lambda on: self._set_mode("marks", on),
                              "Click each mark in the picture, in the order shown",
                              checkable=True)
        grid.addWidget(self.marks_btn, 3, 1)
        self.tip_btn = _btn("or click tool tip", lambda on: self._set_mode("tip", on),
                            "Jog the tool tip to a spot, click the tip in the picture; "
                            "repeat at 4+ places (spindle axes only)", checkable=True)
        grid.addWidget(self.tip_btn, 3, 2)
        grid.addWidget(_btn("3. Compute", self.compute_calib), 4, 0)
        grid.addWidget(_btn("Undo point", self._undo_calib), 4, 1)
        grid.addWidget(_btn("Clear", self._clear_calib), 4, 2)
        self.calib_lbl = QLabel()
        self.calib_lbl.setWordWrap(True)
        grid.addWidget(self.calib_lbl, 5, 0, 1, 3)
        grid.addWidget(QLabel("Lens (optional, checkerboard 9×6):"), 6, 0, 1, 3)
        self.lens_sq = _spin(1, 50, 10, tip="Checkerboard square size")
        grid.addWidget(self.lens_sq, 7, 0)
        grid.addWidget(_btn("Capture", self._lens_capture), 7, 1)
        grid.addWidget(_btn("Compute lens", self._lens_compute), 7, 2)
        grid.setRowStretch(8, 1)
        return g

    def _build_align(self) -> QGroupBox:
        g = QGroupBox("Board alignment (holes / pads)")
        v = QVBoxLayout(g)
        h = QHBoxLayout()
        self.align_btn = _btn("Click points", lambda on: self._set_mode("align", on),
                              "Click 2-3 holes or pads far apart on the real board; each is "
                              "matched to the nearest one of the loaded PCB", checkable=True)
        h.addWidget(self.align_btn)
        h.addWidget(_btn("Remove last", self._undo_align))
        h.addWidget(_btn("Clear", self._clear_align))
        v.addLayout(h)
        self.align_table = QTableWidget(0, 4)
        self.align_table.setHorizontalHeaderLabels(["Feature", "PCB X, Y", "Seen X, Y", "Error"])
        self.align_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.align_table.verticalHeader().hide()
        self.align_table.setMinimumHeight(90)
        v.addWidget(self.align_table, 1)
        self.max_err = _spin(0.01, 2, 0.2, dec=2, tip="Warn when a point is further off")
        h = QHBoxLayout()
        h.addWidget(QLabel("Warn above"))
        h.addWidget(self.max_err)
        self.apply_align = QCheckBox("Apply to job")
        self.apply_align.toggled.connect(self._apply_changed)
        h.addWidget(self.apply_align)
        v.addLayout(h)
        self.align_lbl = QLabel("Load the PCB, calibrate, then click holes / pads.")
        self.align_lbl.setWordWrap(True)
        v.addWidget(self.align_lbl)
        return g

    def _build_obstacles(self) -> QGroupBox:
        g = QGroupBox("Obstacles (clamps, screws)")
        v = QVBoxLayout(g)
        h = QHBoxLayout()
        h.addWidget(_btn("Empty-bed picture", self._take_reference,
                         "Picture of the bed without clamps - used to find them"))
        h.addWidget(_btn("Detect", self._detect))
        v.addLayout(h)
        h = QHBoxLayout()
        self.draw_btn = _btn("Draw", lambda on: self._set_mode("rect", on),
                             "Drag a rectangle around a clamp", checkable=True)
        h.addWidget(self.draw_btn)
        h.addWidget(_btn("Delete", self._del_obstacle))
        h.addWidget(_btn("Clear", self._clear_obstacles))
        v.addLayout(h)
        self.obst_list = QListWidget()
        self.obst_list.setMinimumHeight(60)
        v.addWidget(self.obst_list, 1)
        h = QHBoxLayout()
        h.addWidget(QLabel("Safe height"))
        self.safe_z = _spin(0, 40, self._cfg.get("obstacle_safe_z", 10.0),
                            tip="Work Z above which moves may pass over obstacles")
        self.safe_z.valueChanged.connect(lambda _: self._obstacles_changed())
        h.addWidget(self.safe_z)
        v.addLayout(h)
        self._fill_obstacles()
        return g

    # ================================================================ config
    def _load_config(self) -> None:
        try:
            self._cfg = json.loads((app_dir() / "camera.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._cfg = {}
        self.calibs = {k: CameraCalib.from_dict(d) for k, d in self._cfg.get("cameras", {}).items()}
        self.obstacles = [Obstacle.from_dict(d) for d in self._cfg.get("obstacles", [])]
        self.marks = [tuple(m) for m in self._cfg.get("marks", [])]

    def save_config(self) -> None:
        d = {"cameras": {k: c.to_dict() for k, c in self.calibs.items()},
             "obstacles": [o.to_dict() for o in self.obstacles],
             "obstacle_safe_z": self.safe_z.value(), "marks": [list(m) for m in self.marks],
             "selected": self.key()}
        try:
            (app_dir() / "camera.json").write_text(json.dumps(d, indent=1), encoding="utf-8")
        except OSError as e:
            self.win.log(f"⚠ camera settings not saved: {e}")

    # ================================================================ camera
    def key(self) -> str:
        info = self.cam_combo.currentData() if hasattr(self, "cam_combo") else None
        return info.key if info else "sim"

    def calib(self) -> CameraCalib:
        k = self.key()
        if k not in self.calibs:
            self.calibs[k] = CameraCalib()
        return self.calibs[k]

    def refresh_cameras(self, probe: bool = True) -> None:
        if probe and self.camera is not None:
            self.toggle_camera(False)
        self.cameras = list_cameras() if probe else [CameraInfo(0, "USB camera 0"),
                                                     CameraInfo(SIM, "Simulated camera")]
        self.cam_combo.blockSignals(True)
        self.cam_combo.clear()
        for c in self.cameras:
            self.cam_combo.addItem(c.name, c)
        sel = self._cfg.get("selected")
        for i, c in enumerate(self.cameras):
            if c.key == sel:
                self.cam_combo.setCurrentIndex(i)
        self.cam_combo.blockSignals(False)
        self.cam_combo.currentIndexChanged.connect(lambda _: self._show_calib())
        if probe:
            self.win.log(f"Cameras: {', '.join(c.name for c in self.cameras)}")

    def toggle_camera(self, on: bool) -> None:
        if not on:
            if self.camera:
                self.camera.close()
            self.camera = None
            self.view.set_frame(None)
            self.start_btn.setChecked(False)
            self.start_btn.setText("Start")
            self._camera_controls()
            return
        info = self.cam_combo.currentData()
        self._sync_sim()
        cam = Camera(info, self.res_combo.currentData(), self.sim)
        try:
            cam.open()
        except Exception as e:
            QMessageBox.warning(self, "Camera", str(e))
            self.start_btn.setChecked(False)
            return
        self.camera = cam
        self.start_btn.setText("Stop")
        self.win.view_tabs.setCurrentWidget(getattr(self.win, "camera_page", self.view))
        self._camera_controls()
        self.win.log(f"Camera started: {info.name}")

    def _camera_controls(self) -> None:
        cam = self.camera
        real = cam is not None and cam.info.index != SIM
        self.driver_btn.setEnabled(real)
        has_focus = real and cam.supports("focus")
        self.focus.setEnabled(has_focus)
        self.autofocus.setEnabled(has_focus)
        self.focus_lbl.setText("Focus" if has_focus or not real else "Focus (fixed)")
        self.focus_lbl.setToolTip("" if has_focus or not real else
                                  "Focus is not adjustable on this camera")
        if has_focus:
            self.focus.blockSignals(True)
            self.focus.setValue(int(cam.get("focus")))
            self.focus.blockSignals(False)

    def _driver(self) -> None:
        if self.camera and not self.camera.driver_dialog():
            self.win.log("This camera driver has no settings window")

    def _tick(self) -> None:
        cam = self.camera
        if cam is None:
            return
        if cam.error:
            self.win.log(f"⚠ camera: {cam.error}")
            cam.error = ""
        if cam.frame_id != self._last_id:
            self._last_id = cam.frame_id
            f = cam.latest()
            if f is not None:
                c = self.calib()
                if c.image_size != f.shape[1::-1]:
                    if c.ok and c.image_size[0]:
                        self.win.log("⚠ picture size differs from the calibration - "
                                     "choose the calibrated resolution")
                    c.image_size = f.shape[1::-1]
                self.view.set_frame(f)
                self._update_overlay()

    # ================================================================ coordinates
    def machine_pos(self) -> tuple[float, float, float]:
        c = self.win.ctl
        return c.machine_mm() if c.connected else (0.0, 0.0, 0.0)

    def work_offset_mm(self) -> tuple[float, float]:
        c = self.win.ctl
        o = c.state.work_offset
        return o[0] * c.mmps(0), o[1] * c.mmps(1)

    def _px_to_work(self, px, z: float) -> np.ndarray:
        bed = self.calib().pixel_to_bed(px, self.machine_pos(), z)
        return bed - self.work_offset_mm()

    def _work_to_px(self, xy, z: float) -> np.ndarray:
        return self.calib().bed_to_pixel(np.asarray(xy, float) + self.work_offset_mm(),
                                         self.machine_pos(), z)

    def _cursor_text(self, px: float, py: float) -> str:
        c = self.calib()
        if not c.ok:
            return f"pixel {px:.0f}, {py:.0f} - not calibrated"
        try:
            x, y = self._px_to_work([(px, py)], self.board_t.value())[0]
        except ValueError:
            return ""
        return f"work X {x:.2f}  Y {y:.2f} mm (on board top)"

    def _gray(self):
        import cv2
        f = self.camera.latest() if self.camera else None
        return None if f is None else cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)

    def _refine(self, x: float, y: float, size_mm: float) -> tuple[float, float]:
        g = self._gray()
        c = self.calib()
        mmpx = c.mm_per_px() or 0.25
        r = refine_feature(g, x, y, size_mm / mmpx) if g is not None else None
        return r if r is not None else (x, y)

    # ================================================================ click modes
    def _set_mode(self, mode: str, on: bool) -> None:
        btns = {"marks": self.marks_btn, "tip": self.tip_btn, "align": self.align_btn,
                "rect": self.draw_btn}
        for m, b in btns.items():
            if m != mode or not on:
                b.blockSignals(True)
                b.setChecked(m == mode and on)
                b.blockSignals(False)
        self.click_mode = mode if on else ""
        self.view.mode = ("rect" if mode == "rect" else "click") if on else ""
        if on and self.camera is None:
            self.win.log("Start the camera first")
        if on and mode in ("align",) and not self.calib().ok:
            self.win.log("⚠ calibrate the camera first")
        if on:
            self.win.view_tabs.setCurrentWidget(getattr(self.win, "camera_page", self.view))
        self._hint()

    def _hint(self) -> None:
        m = self.click_mode
        c = self.calib()
        if m == "marks":
            n = len(c.points)
            if not self.marks:
                self.view.hint = "Make and run the mark job first (button 1)"
            elif n < len(self.marks):
                ox, oy = self.work_offset_mm()
                mx, my = self.marks[n]
                self.view.hint = (f"Click mark {n + 1} of {len(self.marks)}: work X {mx - ox:.0f} "
                                  f"Y {my - oy:.0f} (rows from the front-left, X first)")
            else:
                self.view.hint = "All marks clicked - press Compute"
        elif m == "tip":
            self.view.hint = f"Click the tool tip ({len(c.points)} points, need 4+ spread out)"
        elif m == "align":
            self.view.hint = f"Click a hole or pad ({len(self.align_points)} points)"
        elif m == "rect":
            self.view.hint = "Drag a rectangle around the obstacle"
        else:
            self.view.hint = ""
        self._update_overlay()

    def _view_clicked(self, x: float, y: float) -> None:
        m = self.click_mode
        c = self.calib()
        pos = self.machine_pos()
        if m in ("marks", "tip") and not c.points:
            c.ref = (pos[0], pos[1])       # reference position for moving-bed axes
            c.bed_axes = self._bed_axes()
        if m == "marks":
            n = len(c.points)
            if n >= len(self.marks):
                return
            x, y = self._refine(x, y, 1.5)
            f = c.frame_of(self.marks[n], pos)
            c.points.append(CalibPoint(x, y, float(f[0]), float(f[1])))
        elif m == "tip":
            f = c.frame_of(pos[:2], pos)
            c.points.append(CalibPoint(x, y, float(f[0]), float(f[1])))
            self.win.log(f"  calibration point {len(c.points)}: pixel {x:.0f},{y:.0f} = "
                         f"machine {pos[0]:.2f},{pos[1]:.2f}")
        elif m == "align":
            self._add_align_point(x, y)
        self._show_calib()
        self._hint()

    # ================================================================ calibration
    def _bed_axes(self) -> str:
        return ("X" if self.bed_x.isChecked() else "") + ("Y" if self.bed_y.isChecked() else "")

    def _bed_axes_changed(self) -> None:
        c = self.calib()
        if c.points and c.bed_axes != self._bed_axes():
            self.win.log("⚠ bed axes changed - calibrate again")
            c.points.clear()
            c.H = None
        c.bed_axes = self._bed_axes()
        self._sync_sim()
        self._show_calib()

    def _height_changed(self, v: float) -> None:
        self.calib().cam_height_mm = v
        self._update_overlay()

    def make_mark_job(self) -> None:
        area = tuple(self.win.settings.work_area_mm[:2])
        pts = mark_points(area, 3, margin=12)
        ox, oy = self.work_offset_mm()
        self.marks = [(x + ox, y + oy) for x, y in pts]
        self.calib().points.clear()
        self.win.load_job_text(mark_job(pts), "camera-marks.nc")
        self.win.log("Calibration mark job loaded: 9 spots over the work area, 0.1 mm deep. "
                     "Set work zero at the machine's front-left (X/Y) and on the surface (Z), "
                     "keep the zero until you have clicked the marks.")
        if self.key() == "sim":
            self.sim.marks = list(self.marks)    # simulated: marks appear at once
        self.save_config()
        self._show_calib()

    def compute_calib(self) -> None:
        c = self.calib()
        try:
            rms = c.fit()
        except ValueError as e:
            QMessageBox.warning(self, "Calibration", str(e))
            return
        c.cam_height_mm = self.cam_h.value()
        self.win.log(f"Camera calibrated: {len(c.points)} points, error {rms:.3f} mm, "
                     f"{c.mm_per_px() * 1000:.0f} µm per pixel")
        self._set_mode("", False)
        self.save_config()
        self._show_calib()

    def _undo_calib(self) -> None:
        if self.calib().points:
            self.calib().points.pop()
        self._show_calib()
        self._hint()

    def _clear_calib(self) -> None:
        c = self.calib()
        c.points.clear()
        c.H = None
        self.save_config()
        self._show_calib()
        self._hint()

    def _show_calib(self) -> None:
        c = self.calib()
        for w, on in ((self.bed_x, "X" in c.bed_axes), (self.bed_y, "Y" in c.bed_axes)):
            w.blockSignals(True)
            w.setChecked(on)
            w.blockSignals(False)
        self.cam_h.blockSignals(True)
        self.cam_h.setValue(c.cam_height_mm or self.cam_h.value())
        self.cam_h.blockSignals(False)
        if c.ok:
            t = (f"<span style='color:#2e7d32'>✓ calibrated</span>: error {c.rms_mm:.3f} mm, "
                 f"{c.mm_per_px() * 1000:.0f} µm/pixel, {c.image_size[0]}×{c.image_size[1]}")
            if c.K is not None:
                t += ", lens corrected"
        else:
            t = "<b>Not calibrated.</b> 1. run the mark job, 2. click the marks, 3. Compute."
        t += f"<br>{len(c.points)} points"
        self.calib_lbl.setText(t)
        self._sync_sim()

    def _lens_capture(self) -> None:
        f = self.camera.latest() if self.camera else None
        if f is None:
            return
        self.lens_frames.append(f)
        self.win.log(f"Checkerboard picture {len(self.lens_frames)} taken "
                     "(move/tilt the board between pictures, 10 is good)")

    def _lens_compute(self) -> None:
        try:
            k, dist, rms, n = calibrate_lens(self.lens_frames, (9, 6), self.lens_sq.value())
        except ValueError as e:
            QMessageBox.warning(self, "Lens", str(e))
            return
        c = self.calib()
        c.K, c.dist = k, dist
        c.points.clear()
        c.H = None
        self.lens_frames.clear()
        self.win.log(f"Lens corrected from {n} pictures ({rms:.2f} px) - now calibrate the bed")
        self.save_config()
        self._show_calib()

    # ================================================================ alignment
    def _features(self):
        pb = getattr(self.win.pcb, "placed", None)
        return board_features(pb) if pb is not None else []

    def _add_align_point(self, x: float, y: float) -> None:
        if not self.calib().ok:
            QMessageBox.information(self, "Alignment", "Calibrate the camera first.")
            return
        feats = self._features()
        z = self.board_t.value()
        x, y = self._refine(x, y, 2.5)
        mx, my = self._px_to_work([(x, y)], z)[0]
        guess = self.alignment.inverse([(mx, my)])[0] if self.alignment else (mx, my)
        f = nearest_feature(feats, guess[0], guess[1], max_dist=4.0)
        if f is None:
            QMessageBox.information(self, "Alignment", "No hole or pad of the loaded PCB "
                                    "within 4 mm of that point. Load the PCB (PCB tab) and "
                                    "click closer to a feature.")
            return
        self.align_points.append(AlignPoint(f.x, f.y, float(mx), float(my), f.kind))
        self._fit_align()

    def _fit_align(self) -> None:
        pts = self.align_points
        self.alignment = None
        if pts:
            try:
                self.alignment = fit_alignment(pts)
            except ValueError as e:
                self.align_lbl.setText(f"⚠ {e}")
        al = self.alignment
        self.align_table.setRowCount(len(pts))
        for r, p in enumerate(pts):
            err = al.residuals[r] if al and r < len(al.residuals) else 0.0
            for col, t in enumerate((p.kind, f"{p.ex:.2f}, {p.ey:.2f}",
                                     f"{p.mx:.2f}, {p.my:.2f}", f"{err:.3f}")):
                it = QTableWidgetItem(t)
                if col == 3 and err > self.max_err.value():
                    it.setForeground(QColor("#c62828"))
                self.align_table.setItem(r, col, it)
        if al:
            bad = al.max_error > self.max_err.value()
            note = "" if len(pts) > 1 else " (1 point: offset only - add a second point)"
            self.align_lbl.setText(
                f"Rotation <b>{al.angle_deg:+.3f}°</b>, offset <b>X {al.tx:+.3f} Y {al.ty:+.3f}"
                f"</b> mm{note}<br>" +
                (f"<span style='color:#c62828'>⚠ max error {al.max_error:.3f} mm - wrong "
                 "point? remove it</span>" if bad else
                 f"max error {al.max_error:.3f} mm") +
                (f", scale {al.scale:.4f}" + (" ⚠ check calibration / board thickness"
                                               if abs(al.scale - 1) > 0.003 else "")
                 if len(pts) > 1 else ""))
        elif not pts:
            self.align_lbl.setText("Click holes / pads on the board.")
        if self.apply_align.isChecked():
            self.win.reapply_job()
        self._update_overlay()

    def _undo_align(self) -> None:
        if self.align_points:
            self.align_points.pop()
        self._fit_align()

    def _clear_align(self) -> None:
        self.align_points.clear()
        self._fit_align()

    def _apply_changed(self, on: bool) -> None:
        if on and self.alignment is None:
            self.win.log("No alignment yet - click points first")
        self.win.reapply_job()

    def active_alignment(self) -> Alignment | None:
        if self.apply_align.isChecked() and self.alignment and not self.alignment.is_identity():
            return self.alignment
        return None

    # ================================================================ obstacles
    def _take_reference(self) -> None:
        f = self.camera.latest() if self.camera else None
        if f is None:
            self.win.log("Start the camera first")
            return
        self.reference = f
        self.reference_pos = self.machine_pos()
        if self.key() == "sim":   # simulated: two clamps appear on the bed
            a = self.win.settings.work_area_mm
            self.sim.objects = [[(a[0] - 18, 8), (a[0] - 6, 8), (a[0] - 6, 30), (a[0] - 18, 30)],
                                [(6, a[1] - 30), (18, a[1] - 30), (18, a[1] - 8), (6, a[1] - 8)]]
        self.win.log("Empty-bed picture taken - now put the board and clamps on, press Detect")

    def _detect(self) -> None:
        c = self.calib()
        f = self.camera.latest() if self.camera else None
        if f is None or self.reference is None or not c.ok:
            QMessageBox.information(self, "Obstacles", "Needs: camera running, calibrated, "
                                    "and an empty-bed picture.")
            return
        pos = self.machine_pos()
        if np.abs(c.shift(pos) - c.shift(self.reference_pos)).max() > 0.05:
            QMessageBox.information(self, "Obstacles", "The bed has moved since the empty-bed "
                                    "picture - move back to the same position.")
            return
        try:
            found = detect_obstacles(self.reference, f, lambda px: c.pixel_to_bed(px, pos))
        except ValueError as e:
            QMessageBox.warning(self, "Obstacles", str(e))
            return
        # the board itself is not an obstacle
        pb = getattr(self.win.pcb, "placed", None)
        if pb is not None and pb.outline is not None:
            from shapely.affinity import translate
            b = translate(pb.outline.convex_hull, *self.work_offset_mm()).buffer(1.0)
            found = [o for o in found if o.polygon().difference(b).area > 4]
        n0 = len(self.obstacles)
        for o in found:
            o.name = f"obstacle {n0 + 1}"
            n0 += 1
        self.obstacles += found
        self.win.log(f"{len(found)} obstacle(s) found")
        self._obstacles_changed()

    def _rect_drawn(self, x0, y0, x1, y1) -> None:
        c = self.calib()
        if not c.ok:
            self.win.log("⚠ calibrate the camera first")
            return
        bed = c.pixel_to_bed([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], self.machine_pos())
        self.obstacles.append(Obstacle([tuple(map(float, p)) for p in bed],
                                       f"obstacle {len(self.obstacles) + 1}"))
        self._obstacles_changed()

    def _del_obstacle(self) -> None:
        r = self.obst_list.currentRow()
        if 0 <= r < len(self.obstacles):
            del self.obstacles[r]
            self._obstacles_changed()

    def _clear_obstacles(self) -> None:
        self.obstacles.clear()
        self._obstacles_changed()

    def _fill_obstacles(self) -> None:
        self.obst_list.clear()
        for o in self.obstacles:
            x0, y0, x1, y1 = o.polygon().bounds
            self.obst_list.addItem(f"{o.name}: X {x0:.0f}…{x1:.0f}  Y {y0:.0f}…{y1:.0f} mm")

    def _obstacles_changed(self) -> None:
        self._fill_obstacles()
        self.save_config()
        self._sync_sim()
        self.win.recheck_job()
        self._update_overlay()

    def obstacle_issues(self, tp) -> list:
        if not self.obstacles:
            return []
        from ..toolpath import WARNING, Issue
        off = self.work_offset_mm()
        out = check_obstacles(tp, self.obstacles, off, self.safe_z.value())
        pb = getattr(self.win.pcb, "placed", None)
        for name in board_overlap(pb.outline if pb is not None else None, self.obstacles, off):
            out.append(Issue(WARNING, 0, f"The board outline lies over {name}"))
        return out

    # ================================================================ simulator
    def _sync_sim(self) -> None:
        s = self.sim
        s.bed_axes = self._bed_axes() if hasattr(self, "bed_x") else ""
        s.area = tuple(self.win.settings.work_area_mm[:2])
        s.get_pos = self.machine_pos
        s.marks = list(self.marks)
        pb = getattr(self.win.pcb, "placed", None) if hasattr(self.win, "pcb") else None
        ox, oy = self.work_offset_mm()
        if pb is not None and pb.outline is not None:
            from shapely.affinity import rotate, translate
            g = translate(rotate(pb.outline.convex_hull, SIM_ERROR.angle_deg, origin=(0, 0)),
                          SIM_ERROR.tx + ox, SIM_ERROR.ty + oy)
            s.board = g
            hs = SIM_ERROR.apply([(h.x, h.y) for h in pb.holes]) if pb.holes else []
            s.holes = [(x + ox, y + oy, h.d) for (x, y), h in zip(hs, pb.holes)]
        else:
            s.board, s.holes = None, []

    # ================================================================ overlay
    def _path_px(self, rings, z: float) -> QPainterPath:
        path = QPainterPath()
        for ring in rings:
            pts = np.asarray(ring, float)
            if len(pts) < 2:
                continue
            q = self._work_to_px(pts, z)
            path.moveTo(*q[0])
            for x, y in q[1:]:
                path.lineTo(x, y)
        return path

    def _update_overlay(self) -> None:
        c = self.calib()
        ov = []
        marks = []
        if self.click_mode == "marks" and self.marks and len(c.points) < len(self.marks):
            if c.ok:
                m = self.marks[len(c.points)]
                p = c.bed_to_pixel([m], self.machine_pos())[0]
                marks.append((p[0], p[1], str(len(c.points) + 1)))
        for p in c.points:
            marks.append((p.px, p.py, "·"))
        if not c.ok or self.view.image is None:
            self.view.marks = marks
            self.view.set_overlays(ov)
            return
        z = self.board_t.value()
        al = self.alignment if self.alignment else None
        try:
            pb = getattr(self.win.pcb, "placed", None)
            if self.show["pcb"].isChecked() and pb is not None:
                rings = []
                for g in (pb.outline,):
                    for poly in as_polygons(g.convex_hull if g is not None else None):
                        rings.append(list(poly.exterior.coords))
                holes = []
                for h in pb.holes[:2000]:
                    t = np.linspace(0, 2 * math.pi, 13)
                    holes.append(np.c_[h.x + h.d / 2 * np.cos(t), h.y + h.d / 2 * np.sin(t)])
                if al:
                    rings = [al.apply(r) for r in rings]
                    holes = [al.apply(r) for r in holes]
                ov.append((self._path_px(rings, z), QColor("#ffd54f"), False))
                ov.append((self._path_px(holes, z), QColor("#00e676"), False))
            tp = getattr(self.win, "tp", None)
            if self.show["path"].isChecked() and tp is not None and len(tp):
                cut = ~tp.rapid
                segs = np.stack([tp.start[cut, :2], tp.end[cut, :2]], axis=1)[:20000]
                path = QPainterPath()
                if len(segs):
                    q = self._work_to_px(segs.reshape(-1, 2), z).reshape(-1, 2, 2)
                    for a, b in q:
                        path.moveTo(*a)
                        path.lineTo(*b)
                ov.append((path, QColor("#40c4ff"), False))
            if self.show["obst"].isChecked() and self.obstacles:
                path = QPainterPath()
                pos = self.machine_pos()
                for o in self.obstacles:
                    q = c.bed_to_pixel(o.points, pos)
                    path.moveTo(*q[0])
                    for x, y in q[1:]:
                        path.lineTo(x, y)
                    path.closeSubpath()
                ov.append((path, QColor("#ff1744"), True))
            if self.show["grid"].isChecked():
                a = self.win.settings.work_area_mm
                rings = [[(x, 0), (x, a[1])] for x in np.arange(0, a[0] + 0.1, 10)]
                rings += [[(0, y), (a[0], y)] for y in np.arange(0, a[1] + 0.1, 10)]
                ox, oy = self.work_offset_mm()
                rings = [np.asarray(r, float) - (ox, oy) for r in rings]
                ov.append((self._path_px(rings, 0), QColor(255, 255, 255, 90), False))
            if self.show["tool"].isChecked() and self.win.ctl.connected:
                pos = self.machine_pos()
                p = c.bed_to_pixel([pos[:2]], pos, 0)[0]
                path = QPainterPath()
                path.addEllipse(p[0] - 6, p[1] - 6, 12, 12)
                ov.append((path, QColor("#ff9100"), False))
        except ValueError:
            pass
        self.view.marks = marks
        self.view.set_overlays(ov)

    def shutdown(self) -> None:
        if self.camera:
            self.camera.close()
        self.save_config()
