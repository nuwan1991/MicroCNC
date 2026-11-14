"""USB camera sources (Stage 4.1).

Any UVC camera through OpenCV; on Windows DirectShow (fast open, driver
settings dialog). A simulated camera draws the bed for trying the workflow
without hardware. Several cameras can be listed; each has its own
calibration (multi-camera use later).
"""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass

import numpy as np

SIM = -1


@dataclass
class CameraInfo:
    index: int
    name: str

    @property
    def key(self) -> str:
        return "sim" if self.index == SIM else f"usb{self.index}"


def _backend():
    import cv2
    return cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY


def list_cameras(max_index: int = 4) -> list[CameraInfo]:
    """Cameras that open and deliver a frame, plus the simulator."""
    out = []
    try:
        import cv2
        for i in range(max_index):
            cap = cv2.VideoCapture(i, _backend())
            try:
                if cap.isOpened() and cap.read()[0]:
                    out.append(CameraInfo(i, f"USB camera {i}"))
            finally:
                cap.release()
    except ImportError:
        pass
    out.append(CameraInfo(SIM, "Simulated camera"))
    return out


# control name -> (cv2 property name, auto property or None)
CONTROLS = {
    "focus": ("CAP_PROP_FOCUS", "CAP_PROP_AUTOFOCUS"),
    "exposure": ("CAP_PROP_EXPOSURE", "CAP_PROP_AUTO_EXPOSURE"),
    "brightness": ("CAP_PROP_BRIGHTNESS", None),
}


class Camera:
    """Grabs frames in a thread; `latest()` gives the newest BGR frame."""

    def __init__(self, info: CameraInfo, size=(1280, 720), sim_scene=None) -> None:
        self.info = info
        self.size = size
        self.sim_scene = sim_scene
        self._cap = None
        self._frame: np.ndarray | None = None
        self._lock = threading.Lock()
        self._run = False
        self._thread: threading.Thread | None = None
        self.frame_id = 0
        self.error = ""

    def open(self) -> None:
        if self.info.index != SIM:
            import cv2
            cap = cv2.VideoCapture(self.info.index, _backend())
            if not cap.isOpened():
                raise RuntimeError(f"cannot open {self.info.name}")
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.size[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.size[1])
            self._cap = cap
        self._run = True
        self._thread = threading.Thread(target=self._loop, name="micropcb-camera", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._run = False
        if self._thread:
            self._thread.join(timeout=2)
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def _loop(self) -> None:
        while self._run:
            if self._cap is None:
                f = self.sim_scene.render() if self.sim_scene else None
                time.sleep(1 / 15)
            else:
                ok, f = self._cap.read()
                if not ok:
                    self.error = "camera stopped delivering pictures"
                    time.sleep(0.2)
                    continue
            if f is not None:
                with self._lock:
                    self._frame = f
                    self.frame_id += 1

    def latest(self) -> np.ndarray | None:
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    # ---------------------------------------------------------------- controls
    def supports(self, name: str) -> bool:
        if self._cap is None:
            return False
        import cv2
        prop = getattr(cv2, CONTROLS[name][0])
        return self._cap.get(prop) != -1 and self._cap.set(prop, self._cap.get(prop))

    def get(self, name: str) -> float:
        import cv2
        return float(self._cap.get(getattr(cv2, CONTROLS[name][0]))) if self._cap else 0.0

    def set(self, name: str, value: float) -> bool:
        if self._cap is None:
            return False
        import cv2
        auto = CONTROLS[name][1]
        if auto:     # manual value -> auto off
            self._cap.set(getattr(cv2, auto), 0 if name == "focus" else 1)
        return bool(self._cap.set(getattr(cv2, CONTROLS[name][0]), value))

    def set_auto(self, name: str, on: bool) -> bool:
        auto = CONTROLS[name][1]
        if self._cap is None or not auto:
            return False
        import cv2
        val = (1 if on else 0) if name == "focus" else (3 if on else 1)
        return bool(self._cap.set(getattr(cv2, auto), val))

    def driver_dialog(self) -> bool:
        """Windows DirectShow settings window (focus, exposure, …)."""
        if self._cap is None:
            return False
        import cv2
        return bool(self._cap.set(cv2.CAP_PROP_SETTINGS, 1))


class SimScene:
    """Synthetic top view of the bed for the simulated camera.

    `true_H` maps frame mm -> pixels (the simulated camera's real pose).
    get_pos() gives the machine position (mm); bed axes move the bed.
    """

    def __init__(self, size=(1280, 720), area=(140.0, 200.0), bed_axes: str = "") -> None:
        self.size = size
        self.area = area
        self.bed_axes = bed_axes
        w, h = size
        s = min(w / (area[1] + 40), h / (area[0] + 40)) * 0.95
        # bed X to the right is image -y (camera rotated 90 deg), slight tilt
        a = np.radians(91.0)
        self.true_H = np.array([[s * np.cos(a), -s * np.sin(a), w / 2 + s * area[1] / 2],
                                [s * np.sin(a), s * np.cos(a), h / 2 - s * area[0] / 2],
                                [0.0, 0.0, 1.0]])
        self.true_H[2, :2] = [1e-5, -2e-5]   # a little perspective
        self.get_pos = lambda: (0.0, 0.0, 0.0)
        self.marks: list[tuple[float, float]] = []     # bed mm
        self.holes: list[tuple[float, float, float]] = []   # bed mm + d
        self.board = None                               # bed mm polygon (shapely)
        self.objects: list[list[tuple[float, float]]] = []
        self.cam_height = 300.0      # lens above the bed (parallax of the board top)
        self.board_z = 1.6

    def to_px(self, bed_xy, z: float = 0.0) -> np.ndarray:
        pos = self.get_pos()
        s = np.array([1.0 if "X" in self.bed_axes else 0.0,
                      1.0 if "Y" in self.bed_axes else 0.0])
        f = np.atleast_2d(np.asarray(bed_xy, float)) - s * np.asarray(pos[:2])
        if z and self.cam_height > 0:
            q = np.linalg.solve(self.true_H, [self.size[0] / 2, self.size[1] / 2, 1.0])
            c = q[:2] / q[2]
            f = c + (f - c) * self.cam_height / (self.cam_height - z)
        q = np.c_[f, np.ones(len(f))] @ self.true_H.T
        return q[:, :2] / q[:, 2:3]

    def render(self) -> np.ndarray:
        import cv2
        w, h = self.size
        img = np.full((h, w, 3), (52, 56, 60), np.uint8)
        a = self.area
        bed = self.to_px([(0, 0), (a[0], 0), (a[0], a[1]), (0, a[1])])
        cv2.fillPoly(img, [bed.astype(np.int32)], (150, 140, 120))
        if self.board is not None and not self.board.is_empty:
            pts = self.to_px(list(self.board.exterior.coords), self.board_z).astype(np.int32)
            cv2.fillPoly(img, [pts], (60, 120, 190))
        sc = np.hypot(*self.true_H[:2, 0])
        for x, y, d in self.holes:
            c = self.to_px([(x, y)], self.board_z)[0]
            cv2.circle(img, tuple(int(v) for v in c), max(2, int(d / 2 * sc)), (25, 25, 25), -1,
                       cv2.LINE_AA)
        for x, y in self.marks:
            c = self.to_px([(x, y)])[0]
            cv2.circle(img, tuple(int(v) for v in c), max(3, int(0.6 * sc)), (20, 20, 20), -1,
                       cv2.LINE_AA)
        for poly in self.objects:
            cv2.fillPoly(img, [self.to_px(poly).astype(np.int32)], (40, 40, 200))
        x, y, _ = self.get_pos()
        t = self.to_px([(x, y)])[0] if "X" not in self.bed_axes or "Y" not in self.bed_axes \
            else None
        if t is not None:   # tool tip (spindle seen from above)
            cv2.circle(img, tuple(int(v) for v in t), 4, (230, 230, 230), -1, cv2.LINE_AA)
        noise = np.random.default_rng().integers(0, 6, img.shape, dtype=np.uint8)
        return cv2.add(img, noise)
