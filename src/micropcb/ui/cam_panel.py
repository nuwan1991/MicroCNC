"""CAM panel (Stage 3.2–3.5): board -> isolation / clearing / drilling /
cutout / alignment pins -> one job with tool-change pauses.

Single-sided: one job for the side chosen in the PCB tab.
Double-sided: "Side 1" (top up: pins, top isolation, drills) and "Side 2"
(flipped on the pins: bottom isolation, clearing, cutout).
"""
from __future__ import annotations

import os
import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
                               QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QSpinBox, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ..pcb import cam
from ..pcb.board import Placement
from ..pcb.tools import DRILL, ENDMILL, VBIT, Tool, ToolLibrary


def _d(lo, hi, val, dec=2, step=0.05, suffix=" mm"):
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(dec)
    s.setSingleStep(step)
    s.setValue(val)
    s.setSuffix(suffix)
    return s


class ToolDialog(QDialog):
    COLS = ["name", "kind", "diameter", "angle", "tip", "feed", "plunge", "stepdown"]

    def __init__(self, lib: ToolLibrary, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Tool library")
        self.lib = lib
        self.resize(820, 420)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("kind: vbit / endmill / drill · sizes in mm · feeds in mm/min "
                           "(the machine moves at most ~170 mm/min)"))
        self.t = QTableWidget(0, len(self.COLS))
        self.t.setHorizontalHeaderLabels(["Name", "Kind", "Ø", "V angle°", "V tip",
                                          "Feed", "Plunge", "Step-down"])
        self.t.setSelectionBehavior(QAbstractItemView.SelectRows)
        v.addWidget(self.t, 1)
        for tool in lib.tools:
            self._add_row(tool)
        h = QHBoxLayout()
        for text, fn in (("Add", lambda: self._add_row(Tool("New tool", ENDMILL, 1.0))),
                         ("Remove", self._remove)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            h.addWidget(b)
        h.addStretch(1)
        v.addLayout(h)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _add_row(self, tool: Tool) -> None:
        r = self.t.rowCount()
        self.t.insertRow(r)
        for c, key in enumerate(self.COLS):
            val = getattr(tool, key)
            self.t.setItem(r, c, QTableWidgetItem(f"{val:g}" if isinstance(val, float) else str(val)))

    def _remove(self) -> None:
        for r in sorted({i.row() for i in self.t.selectedIndexes()}, reverse=True):
            self.t.removeRow(r)

    def _save(self) -> None:
        tools = []
        try:
            for r in range(self.t.rowCount()):
                vals = [self.t.item(r, c).text().strip() for c in range(len(self.COLS))]
                kind = vals[1].lower()
                if kind not in (VBIT, ENDMILL, DRILL):
                    raise ValueError(f"row {r + 1}: kind must be vbit, endmill or drill")
                tools.append(Tool(vals[0], kind, *[float(x) for x in vals[2:]]))
        except (ValueError, AttributeError) as e:
            QMessageBox.warning(self, "Tool library", str(e))
            return
        self.lib.tools = tools
        self.lib.save()
        self.accept()


class CamPanel(QWidget):
    def __init__(self, win) -> None:
        super().__init__()
        self.win = win
        self.lib = ToolLibrary.load()
        self.text: str | None = None
        root = QVBoxLayout(self)

        top = QHBoxLayout()
        self.side_lbl = QLabel()
        top.addWidget(self.side_lbl, 1)
        top.addWidget(QLabel("Safe Z"))
        self.safe = _d(0.5, 20, 2.0, 1, 0.5)
        top.addWidget(self.safe)
        b = QPushButton("Tools…")
        b.clicked.connect(self.edit_tools)
        top.addWidget(b)
        root.addLayout(top)

        row = QHBoxLayout()
        root.addLayout(row)

        # ---- isolation
        self.iso_on = QGroupBox("Isolation")
        self.iso_on.setCheckable(True)
        f = QFormLayout(self.iso_on)
        self.iso_tool = QComboBox()
        self.iso_depth = _d(0.01, 1, 0.08, 3, 0.01)
        self.iso_passes = QSpinBox()
        self.iso_passes.setRange(1, 8)
        self.iso_passes.setValue(2)
        self.iso_overlap = QSpinBox()
        self.iso_overlap.setRange(0, 90)
        self.iso_overlap.setValue(30)
        self.iso_overlap.setSuffix(" %")
        self.iso_extra = _d(0, 2, 0.0, 3, 0.01)
        self.iso_climb = QCheckBox("Climb milling")
        self.iso_climb.setChecked(True)
        self.iso_width = QLabel()
        f.addRow("Tool", self.iso_tool)
        f.addRow("Depth", self.iso_depth)
        f.addRow("Passes", self.iso_passes)
        f.addRow("Overlap", self.iso_overlap)
        f.addRow("Extra gap", self.iso_extra)
        f.addRow(self.iso_climb)
        f.addRow(self.iso_width)
        self.iso_tool.currentIndexChanged.connect(self._width)
        self.iso_depth.valueChanged.connect(self._width)
        row.addWidget(self.iso_on)

        # ---- clearing
        self.clr_on = QGroupBox("Copper clearing")
        self.clr_on.setCheckable(True)
        self.clr_on.setChecked(False)
        f = QFormLayout(self.clr_on)
        self.clr_tool = QComboBox()
        self.clr_rest = QComboBox()
        self.clr_depth = _d(0.01, 1, 0.1, 3, 0.01)
        self.clr_overlap = QSpinBox()
        self.clr_overlap.setRange(5, 90)
        self.clr_overlap.setValue(40)
        self.clr_overlap.setSuffix(" %")
        self.clr_keep = _d(0, 5, 0.3, 2, 0.05)
        f.addRow("Tool", self.clr_tool)
        f.addRow("Rest tool", self.clr_rest)
        f.addRow("Depth", self.clr_depth)
        f.addRow("Overlap", self.clr_overlap)
        f.addRow("Keep around copper", self.clr_keep)
        row.addWidget(self.clr_on)

        # ---- drilling
        self.drl_on = QGroupBox("Drilling")
        self.drl_on.setCheckable(True)
        f = QFormLayout(self.drl_on)
        self.drl_bits = QLineEdit()
        self.drl_bits.setToolTip("Drill bits you have (mm). Each hole uses the nearest bit.")
        self.drl_tol = _d(0, 1, 0.15, 2, 0.05)
        self.drl_mill = _d(0, 10, 2.0, 2, 0.1)
        self.drl_mill.setToolTip("Holes bigger than this are milled with the end mill (0 = never)")
        self.drl_mill_tool = QComboBox()
        self.drl_depth = _d(0.2, 5, 1.8, 2, 0.1)
        self.drl_peck = QCheckBox("Peck drilling (tool step-down)")
        f.addRow("Bits", self.drl_bits)
        f.addRow("Size tolerance", self.drl_tol)
        f.addRow("Mill holes above", self.drl_mill)
        f.addRow("Mill tool", self.drl_mill_tool)
        f.addRow("Depth", self.drl_depth)
        f.addRow(self.drl_peck)
        row.addWidget(self.drl_on)
        row = QHBoxLayout()          # second row: cutout + double-sided
        root.addLayout(row)

        # ---- cutout
        self.cut_on = QGroupBox("Board cutout")
        self.cut_on.setCheckable(True)
        f = QFormLayout(self.cut_on)
        self.cut_tool = QComboBox()
        self.cut_thick = _d(0.2, 5, 1.6, 2, 0.1)
        self.cut_tabs = QSpinBox()
        self.cut_tabs.setRange(0, 12)
        self.cut_tabs.setValue(4)
        self.cut_tabw = _d(0.5, 10, 3.0, 1, 0.5)
        self.cut_tabh = _d(0.1, 2, 0.6, 2, 0.1)
        f.addRow("Tool", self.cut_tool)
        f.addRow("Board thickness", self.cut_thick)
        f.addRow("Tabs", self.cut_tabs)
        f.addRow("Tab width", self.cut_tabw)
        f.addRow("Tab height", self.cut_tabh)
        row.addWidget(self.cut_on)

        # ---- double-sided
        self.ds_on = QGroupBox("Double-sided (alignment pins)")
        self.ds_on.setCheckable(True)
        self.ds_on.setChecked(False)
        f = QFormLayout(self.ds_on)
        self.pin_tool = QComboBox()
        self.pin_d = _d(1, 6, 3.0, 2, 0.1)
        self.pin_off = _d(2, 30, 5.0, 1, 0.5)
        self.pin_depth = _d(1, 10, 4.0, 1, 0.5)
        f.addRow("Pin tool", self.pin_tool)
        f.addRow("Pin diameter", self.pin_d)
        f.addRow("Distance from board", self.pin_off)
        f.addRow("Depth (into plate)", self.pin_depth)
        hint = QLabel("Side 1 = top copper up: pins, top isolation, drills.\n"
                      "Flip on the pins (keep X/Y zero!).\n"
                      "Side 2 = bottom up: bottom isolation, clearing, cutout.")
        hint.setWordWrap(True)
        f.addRow(hint)
        row.addWidget(self.ds_on)

        # ---- actions
        act = QHBoxLayout()
        self.gen_btn = QPushButton("▶ Generate job")
        self.gen_btn.setStyleSheet("font-weight:bold;")
        self.gen_btn.clicked.connect(lambda: self.generate(None))
        self.s1_btn = QPushButton("Side 1 job")
        self.s1_btn.clicked.connect(lambda: self.generate(1))
        self.s2_btn = QPushButton("Side 2 job")
        self.s2_btn.clicked.connect(lambda: self.generate(2))
        self.save_btn = QPushButton("Save G-code…")
        self.save_btn.clicked.connect(self.save)
        for b in (self.gen_btn, self.s1_btn, self.s2_btn, self.save_btn):
            act.addWidget(b)
        act.addStretch(1)
        root.addLayout(act)
        self.notes = QLabel("Load a board in the PCB tab first.")
        self.notes.setWordWrap(True)
        self.notes.setTextFormat(Qt.RichText)
        root.addWidget(self.notes)
        self.ds_on.toggled.connect(lambda _: self.refresh())
        self._fill_tools()
        self.refresh()

    # ------------------------------------------------------------ tools
    def _fill_tools(self) -> None:
        def fill(combo, kinds, none=False, prefer=None):
            keep = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            if none:
                combo.addItem("(none)", None)
            for t in self.lib.of_kind(*kinds):
                combo.addItem(t.label, t.name)
            i = combo.findText(keep)
            if i < 0 and prefer:
                i = next((k for k in range(combo.count()) if prefer in combo.itemText(k)), 0)
            combo.setCurrentIndex(max(i, 0))
            combo.blockSignals(False)

        fill(self.iso_tool, (VBIT, ENDMILL), prefer="30°")
        fill(self.clr_tool, (ENDMILL, VBIT), prefer="1.0")
        fill(self.clr_rest, (VBIT, ENDMILL), none=True)
        fill(self.drl_mill_tool, (ENDMILL,), prefer="1.0")
        fill(self.cut_tool, (ENDMILL,), prefer="1.0")
        fill(self.pin_tool, (DRILL, ENDMILL), prefer="Drill 3")
        if not self.drl_bits.text():
            self.drl_bits.setText(", ".join(f"{d:g}" for d in self.lib.drill_sizes()))
        self._width()

    def edit_tools(self) -> None:
        if ToolDialog(self.lib, self).exec():
            self._fill_tools()
            self.win.log("Tool library saved")

    def _tool(self, combo) -> Tool | None:
        name = combo.currentData()
        return self.lib.get(name) if name else None

    def _width(self) -> None:
        t = self._tool(self.iso_tool)
        if t:
            self.iso_width.setText(f"cut width {t.cut_width(self.iso_depth.value()):.3f} mm")

    # ------------------------------------------------------------ generate
    def refresh(self) -> None:
        pcb = self.win.pcb
        has = pcb.board is not None
        ds = self.ds_on.isChecked()
        for b in (self.gen_btn,):
            b.setEnabled(has and not ds)
        for b in (self.s1_btn, self.s2_btn):
            b.setEnabled(has and ds)
        self.save_btn.setEnabled(self.text is not None)
        if has:
            pl = pcb.placement()
            side = "bottom copper (mirrored)" if pl.mirror else "top copper"
            self.side_lbl.setText(f"<b>Single-sided job uses: {side}</b>, rotation "
                                  f"{pl.rotation}°, offset {pl.margin:g} mm - set in the PCB tab")
        else:
            self.side_lbl.setText("No board loaded - PCB tab → Open")

    def _bits(self) -> list[float]:
        out = []
        for part in self.drl_bits.text().replace(";", ",").split(","):
            part = part.strip()
            if part:
                out.append(float(part))
        return out

    def _ops(self, pb, side: str, iso: bool, clear: bool, drill: bool, cut: bool,
             pins: bool) -> list[cam.OpResult]:
        safe = self.safe.value()
        ops: list[cam.OpResult] = []
        if pins:
            ops.append(cam.pins(pb, self._tool(self.pin_tool), self.pin_d.value(),
                                self.pin_depth.value(), self.pin_off.value(), safe,
                                tuple(self.win.settings.work_area_mm[:2])))
        if iso:
            ops.append(cam.isolation(pb, side, self._tool(self.iso_tool), self.iso_depth.value(),
                                     self.iso_passes.value(), self.iso_overlap.value() / 100,
                                     self.iso_extra.value(), self.iso_climb.isChecked(), safe))
        if clear:
            ops += cam.clearing(pb, side, self._tool(self.clr_tool), self.clr_depth.value(),
                                self.clr_overlap.value() / 100, self.clr_keep.value(),
                                self._tool(self.clr_rest), safe_z=safe)
        if drill and pb.holes:
            mill_above = self.drl_mill.value() or None
            groups, mill = cam.assign_drills(pb.holes, self._bits(), self.drl_tol.value(),
                                             mill_above)
            for d, holes in groups.items():
                bit = next((t for t in self.lib.of_kind(DRILL) if abs(t.diameter - d) < 1e-6),
                           Tool(f"Drill {d:g}", DRILL, d, feed=60, plunge=40, stepdown=0.8))
                ops.append(cam.drilling(holes, bit, self.drl_depth.value(),
                                        bit.stepdown if self.drl_peck.isChecked() else None, safe))
            if mill:
                ops.append(cam.mill_holes(mill, self._tool(self.drl_mill_tool),
                                          self.drl_depth.value(), safe))
        if cut:
            ops.append(cam.cutout(pb, self._tool(self.cut_tool), self.cut_thick.value(),
                                  0.1, self.cut_tabs.value(), self.cut_tabw.value(),
                                  self.cut_tabh.value(), safe))
        return ops

    def generate(self, side_no: int | None) -> None:
        pcb = self.win.pcb
        if pcb.board is None:
            return
        base = pcb.placement()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            if side_no is None:
                pb = pcb.placed
                side = "bottom" if base.mirror else "top"
                ops = self._ops(pb, side, self.iso_on.isChecked(), self.clr_on.isChecked(),
                                self.drl_on.isChecked(), self.cut_on.isChecked(), False)
                title = f"{side} side"
            else:
                mirror = side_no == 2
                pl = Placement(base.rotation, mirror, base.flip_axis, base.margin)
                pb = pcb.board.placed(pl)
                if side_no == 1:
                    ops = self._ops(pb, "top", self.iso_on.isChecked(), False,
                                    self.drl_on.isChecked(), False, True)
                else:
                    ops = self._ops(pb, "bottom", self.iso_on.isChecked(),
                                    self.clr_on.isChecked(), False, self.cut_on.isChecked(), False)
                title = f"side {side_no} ({'bottom up, flipped' if mirror else 'top up'})"
                pcb.side.setCurrentIndex(0 if mirror else 1)   # show what is being cut
            self.text = cam.combine(ops, title, self.safe.value())
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Generate", f"Could not generate:\n{e}")
            return
        QApplication.restoreOverrideCursor()
        problem = None
        html = [f"<b>{title}</b>: {sum(1 for o in ops if not o.empty)} operation(s)"]
        tools = []
        for o in ops:
            if o.empty:
                html.append(f"• {o.name}: nothing to do " + " ".join(o.notes))
                continue
            if not tools or tools[-1] != o.tool.name:
                tools.append(o.tool.name)
            notes = "; ".join(o.notes)
            color = "#c62828" if "⚠" in notes else "#424242"
            html.append(f"• {o.name} - {o.tool.label}"
                        + (f" <span style='color:{color}'>{notes}</span>" if notes else ""))
            if o.problem_area is not None:
                problem = o.problem_area
        html.append(f"Tools in order: {' → '.join(tools)} ({max(len(tools) - 1, 0)} tool change(s))")
        self.notes.setText("<br>".join(html))
        self.win.pcb_view.set_problem(problem)
        name = os.path.splitext(os.path.basename(pcb.source or "board"))[0]
        name += f"_{title.split()[0]}{side_no or ''}.nc".replace(" ", "_")
        self.win.load_job_text(self.text, name)
        for line in html:
            self.win.log("  " + re.sub(r"<[^>]+>", "", line))
        self.refresh()

    def save(self) -> None:
        if self.text is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save G-code",
                                              os.path.join(self.win.settings.last_dir,
                                                           self.win.job_name()),
                                              "G-code (*.nc *.gcode)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.text)
            self.win.log(f"G-code saved: {path}")
