"""Left-column Layers list with the selected layer's job options."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout, QHBoxLayout,
                               QPushButton, QTreeWidget, QTreeWidgetItem, QWidget)

from ..layers import KINDS, Layer
from .widgets import muted


def _swatch(color: str) -> QIcon:
    pm = QPixmap(10, 10)
    pm.fill(QColor(color))
    return QIcon(pm)


class LayersBox(QWidget):
    """Signals: selected(index), visibility(), options() (origin / depth / side / kind),
    add(), remove(index)."""

    selected = Signal(int)
    visibility = Signal()
    options = Signal()
    add = Signal()
    remove = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.layers: list[Layer] = []
        self.map_state = lambda side: None     # side -> True (applied) / False (none) / None
        v = QGridLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setVerticalSpacing(4)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(0)
        self.tree.setFocusPolicy(Qt.NoFocus)
        self.tree.setMinimumHeight(70)
        self.tree.setToolTip("Tick = show the layer in the views. Click = select it:\n"
                             "Start runs the selected layer.")
        self.tree.currentItemChanged.connect(self._current_changed)
        self.tree.itemChanged.connect(self._item_changed)
        v.addWidget(self.tree, 0, 0, 1, 2)

        self.kind_combo = QComboBox()
        for k, (label, color, _, _) in KINDS.items():
            self.kind_combo.addItem(_swatch(color), label, k)
        self.side_combo = QComboBox()
        self.side_combo.addItem("Top side", "top")
        self.side_combo.addItem("Bottom side", "bottom")
        self.side_combo.setToolTip("Board side of this layer: it uses that side's height map")
        self.origin_combo = QComboBox()
        self.origin_combo.addItem("As in the file", "file")
        self.origin_combo.addItem("Front-left of job = 0", "corner")
        self.origin_combo.setToolTip(
            "Front-left: the job is shifted so its lowest X and Y are at work 0 -\n"
            "zero X/Y on the board corner as usual. The file itself is not changed.")
        self.depth_chk = QCheckBox("Max cut depth")
        self.depth_chk.setToolTip(
            "Cuts deeper than this are raised to it; shallower cuts stay as they are.\n"
            "For engraving with a V-bit (a deeper cut is a wider groove that eats thin\n"
            "tracks). Off by default for drilling, alignment and cutout layers.\n"
            "The file itself is not changed; the height map is added after this.")
        self.depth_spin = QDoubleSpinBox()
        self.depth_spin.setRange(0.01, 2.0)
        self.depth_spin.setDecimals(3)
        self.depth_spin.setSingleStep(0.01)
        self.depth_spin.setSuffix(" mm")
        self.depth_spin.setValue(0.05)
        self.depth_spin.setEnabled(False)
        self.depth_chk.toggled.connect(self.depth_spin.setEnabled)

        for w in (self.kind_combo, self.side_combo, self.origin_combo):
            w.setMinimumContentsLength(8)
            w.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        row = 1
        for label, w in (("Type", self.kind_combo), ("Side", self.side_combo),
                         ("Origin", self.origin_combo)):
            v.addWidget(muted(label), row, 0)
            v.addWidget(w, row, 1)
            row += 1
        dh = QHBoxLayout()
        dh.addWidget(self.depth_chk)
        dh.addWidget(self.depth_spin, 1)
        v.addLayout(dh, row, 0, 1, 2)
        v.setColumnStretch(1, 1)
        self.kind_combo.currentIndexChanged.connect(self._kind_changed)
        self.side_combo.currentIndexChanged.connect(self._opts_changed)
        self.origin_combo.currentIndexChanged.connect(self._opts_changed)
        self.depth_chk.toggled.connect(lambda _: self._opts_changed())
        self.depth_spin.editingFinished.connect(self._opts_changed)
        self._loading = False

    # header buttons (placed in the section title by the main window)
    def header_buttons(self) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(3)
        self.add_btn = QPushButton("＋ Add")
        self.add_btn.setToolTip("Add G-code file(s) as layers (Ctrl+L)")
        self.add_btn.clicked.connect(self.add.emit)
        self.remove_btn = QPushButton("−")
        self.remove_btn.setToolTip("Remove the selected layer from the project\n"
                                   "(the file itself is not deleted)")
        self.remove_btn.setFixedWidth(26)
        self.remove_btn.clicked.connect(lambda: self.remove.emit(self.current()))
        for b in (self.add_btn, self.remove_btn):
            b.setFocusPolicy(Qt.NoFocus)
            b.setStyleSheet("padding:1px 7px;")
            h.addWidget(b)
        return w

    # ------------------------------------------------------------ data
    def current(self) -> int:
        it = self.tree.currentItem()
        return self.tree.indexOfTopLevelItem(it) if it is not None else -1

    def current_layer(self) -> Layer | None:
        i = self.current()
        return self.layers[i] if 0 <= i < len(self.layers) else None

    def set_layers(self, layers: list[Layer], select: int) -> None:
        self.layers = layers
        self._loading = True
        self.tree.clear()
        for ly in layers:
            it = QTreeWidgetItem([ly.label, ""])
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(0, Qt.Checked if ly.visible else Qt.Unchecked)
            self.tree.addTopLevelItem(it)
        self._loading = False
        self.refresh()
        if 0 <= select < len(layers):
            self.tree.setCurrentItem(self.tree.topLevelItem(select))
        else:
            self._show_options(None)

    def clear_selection(self) -> None:
        self._loading = True
        self.tree.setCurrentItem(None)
        self.tree.clearSelection()
        self._loading = False
        self._show_options(None)

    def refresh(self) -> None:
        """Names, colours and height map state of every row."""
        self._loading = True
        for k, ly in enumerate(self.layers):
            it = self.tree.topLevelItem(k)
            if it is None:
                continue
            it.setText(0, ly.label + (" (file missing)" if ly.missing else ""))
            it.setIcon(0, _swatch(ly.color))
            it.setToolTip(0, ly.path)
            on = it.checkState(0) == Qt.Checked
            it.setForeground(0, QColor("#dfe5ec" if on else "#8b97a5"))
            st = self.map_state(ly.side) if KINDS[ly.kind][3] else "-"
            side = "T" if ly.side == "top" else "B"
            if st == "-":
                txt, col = "—", "#8b97a5"
            elif st:
                txt, col = f"map {side} ✓", "#7ee2a8"
            elif st is False:
                txt, col = f"map {side} ✗", "#d29922"
            else:
                txt, col = f"map {side} off", "#8b97a5"
            it.setText(1, txt)
            it.setForeground(1, QColor(col))
        self.tree.resizeColumnToContents(1)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, self.tree.header().ResizeMode.Stretch)
        self._loading = False

    # ------------------------------------------------------------ events
    def _current_changed(self, cur, _prev) -> None:
        if self._loading:
            return
        i = self.current()
        self._show_options(self.layers[i] if 0 <= i < len(self.layers) else None)
        self.selected.emit(i)

    def _item_changed(self, it, col) -> None:
        if self._loading or col != 0:
            return
        k = self.tree.indexOfTopLevelItem(it)
        if 0 <= k < len(self.layers):
            self.layers[k].visible = it.checkState(0) == Qt.Checked
            self.refresh()
            self.visibility.emit()

    def _show_options(self, ly: Layer | None) -> None:
        self._loading = True
        on = ly is not None
        for w in (self.kind_combo, self.side_combo):
            w.setEnabled(on)
        if hasattr(self, "remove_btn"):
            self.remove_btn.setEnabled(on)
        if ly is not None:
            self.kind_combo.setCurrentIndex(max(0, self.kind_combo.findData(ly.kind)))
            self.side_combo.setCurrentIndex(max(0, self.side_combo.findData(ly.side)))
            self.origin_combo.setCurrentIndex(max(0, self.origin_combo.findData(ly.origin)))
            self.depth_chk.setChecked(ly.max_depth > 0)
            if ly.max_depth > 0:
                self.depth_spin.setValue(ly.max_depth)
            self.depth_spin.setEnabled(ly.max_depth > 0)
        self._loading = False

    def _kind_changed(self) -> None:
        if self._loading:
            return
        ly = self.current_layer()
        if ly is None:
            return
        old_label = ly.label
        ly.kind = self.kind_combo.currentData()
        if old_label in (v[0] for v in KINDS.values()):
            ly.name = KINDS[ly.kind][0] if ly.kind != "other" else ly.name
        default_side = KINDS[ly.kind][2]
        if ly.kind == "bottom" or (ly.kind == "top" and ly.side == "bottom"):
            self.side_combo.setCurrentIndex(self.side_combo.findData(default_side))
        self.refresh()
        self._opts_changed()

    def _opts_changed(self) -> None:
        if self._loading:
            return
        ly = self.current_layer()
        if ly is not None:
            ly.side = self.side_combo.currentData()
            ly.origin = self.origin_combo.currentData()
            ly.max_depth = self.depth_spin.value() if self.depth_chk.isChecked() else 0.0
            self.refresh()
        self.options.emit()
