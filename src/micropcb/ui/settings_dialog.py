from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFormLayout, QHBoxLayout, QSpinBox, QWidget)

from ..settings import Settings


def _dspin(lo: float, hi: float, val: float, dec: int, step: float) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(dec)
    s.setSingleStep(step)
    s.setValue(val)
    return s


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.s = settings
        form = QFormLayout(self)

        self.mmps = [_dspin(0.0001, 1.0, v, 6, 0.0001) for v in settings.mm_per_step]
        form.addRow("mm per step  X / Y / Z", self._row(self.mmps))
        self.area = [_dspin(1, 2000, v, 1, 1) for v in settings.work_area_mm]
        form.addRow("Work area mm  X / Y / Z", self._row(self.area))

        self.max_spt = QSpinBox()
        self.max_spt.setRange(1, 15)
        self.max_spt.setValue(settings.max_steps_per_tick)
        self.max_spt.setToolTip("Hard speed cap. Original software never exceeds 14.")
        form.addRow("Max steps per tick", self.max_spt)

        self.perf = QSpinBox()
        self.perf.setRange(0, 255)
        self.perf.setValue(settings.performance)
        self.perf.setToolTip("Value sent in the 7F keep-alive packet (original default 4).")
        form.addRow("Computer performance byte", self.perf)

        self.safe = _dspin(0, 40, settings.safe_height_mm, 2, 0.5)
        form.addRow("Go-to-zero safe height (mm)", self.safe)

        self.limits = QCheckBox("Enforce work area (needs machine home set)")
        self.limits.setChecked(settings.soft_limits)
        form.addRow("Soft limits", self.limits)

        self.reconnect = QCheckBox("Reconnect automatically after power loss")
        self.reconnect.setChecked(settings.auto_reconnect)
        form.addRow("Auto reconnect", self.reconnect)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    @staticmethod
    def _row(widgets: list[QWidget]) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        for x in widgets:
            h.addWidget(x)
        return w

    def _accept(self) -> None:
        s = self.s
        s.mm_per_step = [w.value() for w in self.mmps]
        s.work_area_mm = [w.value() for w in self.area]
        s.max_steps_per_tick = self.max_spt.value()
        s.jog_steps_per_tick = min(s.jog_steps_per_tick, s.max_steps_per_tick)
        s.performance = self.perf.value()
        s.safe_height_mm = self.safe.value()
        s.soft_limits = self.limits.isChecked()
        s.auto_reconnect = self.reconnect.isChecked()
        self.accept()
