"""PCB panel (Stage 3.1): open a KiCad / EasyEDA export, check layer roles,
place the board on the machine (rotation, side, margin)."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ..pcb.board import ROLE_NAMES, ROLES, Board, Placement


class PcbPanel(QWidget):
    def __init__(self, win, view) -> None:
        super().__init__()
        self.win = win
        self.view = view
        self.board: Board | None = None
        self.placed = None
        self.source: str | None = None
        self._building = False
        root = QHBoxLayout(self)

        # ---- files / layers
        g1 = QGroupBox("Board files")
        v1 = QVBoxLayout(g1)
        h = QHBoxLayout()
        b = QPushButton("Open folder…")
        b.setToolTip("Folder with the Gerber + drill files (KiCad 'production' / plot folder)")
        b.clicked.connect(self.open_folder)
        h.addWidget(b)
        b = QPushButton("Open zip / files…")
        b.clicked.connect(self.open_files)
        h.addWidget(b)
        h.addStretch(1)
        v1.addLayout(h)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Show", "File", "Used as"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        self.table.setMinimumHeight(140)
        v1.addWidget(self.table, 1)
        root.addWidget(g1, 3)

        # ---- placement
        g2 = QGroupBox("Placement on the machine")
        f = QFormLayout(g2)
        self.side = QComboBox()
        self.side.addItem("Bottom copper up (mirrored)", True)
        self.side.addItem("Top copper up", False)
        self.side.setToolTip("Which side faces the spindle. Your single-sided boards: bottom "
                             "copper up.")
        self.flip = QComboBox()
        self.flip.addItem("Left ↔ right", "x")
        self.flip.addItem("Top ↔ bottom", "y")
        self.rot = QComboBox()
        for r in (0, 90, 180, 270):
            self.rot.addItem(f"{r}°", r)
        self.margin = QDoubleSpinBox()
        self.margin.setRange(0, 50)
        self.margin.setDecimals(1)
        self.margin.setSuffix(" mm")
        self.margin.setToolTip("Board corner offset from work 0,0")
        f.addRow("Side facing up", self.side)
        f.addRow("Flip direction", self.flip)
        f.addRow("Rotation", self.rot)
        f.addRow("Offset from work zero", self.margin)
        for w in (self.side, self.flip, self.rot):
            w.currentIndexChanged.connect(self.rebuild)
        self.margin.valueChanged.connect(self.rebuild)
        root.addWidget(g2, 2)

        # ---- info
        g3 = QGroupBox("Board")
        v3 = QVBoxLayout(g3)
        self.info = QLabel("No board loaded")
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.RichText)
        self.info.setAlignment(Qt.AlignTop)
        v3.addWidget(self.info, 1)
        root.addWidget(g3, 3)

    # ------------------------------------------------------------ open
    def open_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Board folder (Gerber + drill files)",
                                             self.win.settings.last_dir)
        if d:
            self.load([d])

    def open_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Gerber / drill files or zip", self.win.settings.last_dir,
            "PCB files (*.zip *.gbr *.gtl *.gbl *.gko *.gm1 *.drl *.xln *.txt *.g*);;All (*)")
        if paths:
            self.load(paths)

    def load(self, paths: list[str]) -> None:
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            board = Board.load(paths)
        finally:
            QApplication.restoreOverrideCursor()
        if not any(l.role in ("top", "bottom") for l in board.layers):
            QMessageBox.warning(self, "Open board", "No copper layer found in:\n" +
                                "\n".join(paths) + ("\n\n" + "\n".join(board.problems)
                                                    if board.problems else ""))
            return
        self.board = board
        self.source = paths[0]
        self.win.settings.last_dir = os.path.dirname(paths[0]) if os.path.isfile(paths[0]) \
            else paths[0]
        has_bottom = bool(board.by_role("bottom"))
        self._building = True
        self.side.setCurrentIndex(0 if has_bottom else 1)
        rot = board.suggest_rotation(tuple(self.win.settings.work_area_mm[:2]))
        self.rot.setCurrentIndex((0, 90, 180, 270).index(rot))
        self._building = False
        self._fill_table()
        self.win.log(f"Board loaded: {len(board.layers)} files from {', '.join(paths)}")
        for p in board.problems:
            self.win.log(f"  ⚠ {p}")
        if rot:
            self.win.log(f"  board only fits the machine rotated {rot}° - rotation set")
        self.rebuild()
        self.win.view_tabs.setCurrentWidget(self.view)

    def _fill_table(self) -> None:
        b = self.board
        self.table.setRowCount(len(b.layers))
        mirrored = self.side.currentData()
        for row, lf in enumerate(b.layers):
            chk = QCheckBox()
            default_on = lf.role in ("outline", "drill") or \
                (lf.role == "bottom" and mirrored) or (lf.role == "top" and not mirrored)
            chk.setChecked(default_on)
            chk.toggled.connect(self._visibility_changed)
            cell = QWidget()
            hl = QHBoxLayout(cell)
            hl.setContentsMargins(6, 0, 0, 0)
            hl.addWidget(chk)
            self.table.setCellWidget(row, 0, cell)
            item = QTableWidgetItem(lf.name)
            ff = lf.gerber.file_function if lf.gerber else (
                "plated" if lf.drill and lf.drill.plated else
                "non-plated" if lf.drill and lf.drill.plated is False else "")
            item.setToolTip(f"{lf.name}\nauto: {ROLE_NAMES[lf.auto_role]}  {ff}")
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, 1, item)
            combo = QComboBox()
            roles = ["drill"] if lf.drill else [r for r in ROLES if r != "drill"]
            for r in roles:
                combo.addItem(ROLE_NAMES[r], r)
            combo.setCurrentIndex(roles.index(lf.role))
            combo.currentIndexChanged.connect(lambda _, r=row, c=combo: self._role_changed(r, c))
            self.table.setCellWidget(row, 2, combo)
        self.table.resizeColumnToContents(0)
        self.table.resizeColumnToContents(2)

    def _role_changed(self, row: int, combo: QComboBox) -> None:
        self.board.layers[row].role = combo.currentData()
        self.rebuild()

    def _checks(self) -> list[bool]:
        out = []
        for row in range(self.table.rowCount()):
            chk = self.table.cellWidget(row, 0).findChild(QCheckBox)
            out.append(chk.isChecked())
        return out

    def _visible(self) -> dict[str, bool]:
        vis: dict[str, bool] = {}
        for lf, on in zip(self.board.layers, self._checks()):
            key = lf.role if lf.role != "other" else "other:" + lf.name
            vis[key] = vis.get(key, False) or on
        return vis

    def _visibility_changed(self) -> None:
        if self.board is None:
            return
        need = [lf for lf, on in zip(self.board.layers, self._checks())
                if on and lf.role == "other" and lf.gerber is None]
        if need:   # silk / mask … parsed the first time they are shown
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                for lf in need:
                    lf.ensure_parsed()
            finally:
                QApplication.restoreOverrideCursor()
            self.rebuild()
            return
        self.view.set_visible(self._visible())

    # ------------------------------------------------------------ placement
    def placement(self) -> Placement:
        return Placement(rotation=self.rot.currentData(), mirror=bool(self.side.currentData()),
                         flip_axis=self.flip.currentData(), margin=self.margin.value())

    def rebuild(self) -> None:
        if self.board is None or self._building:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.placed = self.board.placed(self.placement())
        finally:
            QApplication.restoreOverrideCursor()
        self.view.area = tuple(self.win.settings.work_area_mm[:2])
        self.view.set_board(self.placed, self._visible())
        self.view.set_problem(None)
        self._show_info()
        if hasattr(self.win, "cam"):
            self.win.cam.refresh()
        if hasattr(self.win, "camera"):
            self.win.camera._sync_sim()

    def _show_info(self) -> None:
        pb = self.placed
        area = tuple(self.win.settings.work_area_mm[:2])
        w, h = pb.size
        fits = pb.fits(area)
        lines = [f"<b>{w:.2f} × {h:.2f} mm</b> after placement"]
        lines.append("<span style='color:#2e7d32'>✓ fits the machine area "
                     f"({area[0]:g} × {area[1]:g} mm)</span>" if fits else
                     "<span style='color:#c62828'><b>✗ does not fit</b> the machine area "
                     f"({area[0]:g} × {area[1]:g} mm) - try another rotation</span>")
        for side in ("top", "bottom"):
            g = pb.copper.get(side)
            if g is not None and not g.is_empty:
                lines.append(f"{'Top' if side == 'top' else 'Bottom'} copper: "
                             f"{len(getattr(g, 'geoms', [g]))} areas")
        sizes = pb.hole_sizes()
        if sizes:
            n = sum(sizes.values())
            lines.append(f"<b>{n} holes</b>, {len(sizes)} sizes:")
            lines.append(", ".join(f"{d:g} mm ×{c}" for d, c in sizes.items()))
        self.info.setText("<br>".join(lines))
