"""MicroCNC dark theme: low glare next to a machine.

One blue accent for normal actions, green only for "running / on", amber for
pause, red only for Stop and E-STOP. Buttons get a role with
`set_role(button, "primary" | "start" | "pause" | "stop" | "estop")`.
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget

BG = "#15191e"
PANEL = "#1d232a"
PANEL2 = "#242b33"
FIELD = "#11161b"
LINE = "#323b45"
TEXT = "#dfe5ec"
MUTE = "#8b97a5"
ACCENT = "#2f81f7"
OK = "#2ea043"
WARN = "#d29922"
BAD = "#da3633"
AXIS = {"x": "#e5534b", "y": "#57ab5a", "z": "#539bf5"}

QSS = f"""
QMainWindow, QDialog {{ background:{BG}; }}
QWidget {{ color:{TEXT}; font-family:"Segoe UI"; font-size:9pt; }}
QToolTip {{ background:{PANEL2}; color:{TEXT}; border:1px solid {LINE}; padding:4px; }}
QMenuBar {{ background:{PANEL}; border:none; padding:1px 2px; }}
QMenuBar::item {{ padding:4px 10px; background:transparent; border-radius:3px; }}
QMenuBar::item:selected {{ background:{PANEL2}; }}
QMenu {{ background:{PANEL}; border:1px solid {LINE}; padding:4px 0; }}
QMenu::item {{ padding:5px 26px 5px 16px; }}
QMenu::item:selected {{ background:#1f3a5f; }}
QMenu::item:disabled {{ color:{MUTE}; }}
QMenu::separator {{ height:1px; background:{LINE}; margin:4px 0; }}
QGroupBox {{ border:1px solid {LINE}; border-radius:5px; margin-top:14px; padding-top:4px; }}
QGroupBox::title {{ subcontrol-origin:margin; left:8px; padding:0 4px; color:{MUTE}; }}
QFrame#section {{ background:{PANEL}; border-bottom:1px solid {LINE}; }}
QLabel#sectionTitle {{ color:{MUTE}; font-size:8pt; font-weight:600; letter-spacing:1px; }}
QLabel#muted {{ color:{MUTE}; }}
QLabel#dro {{ font-family:Consolas; font-size:15pt; font-weight:bold; background:{FIELD};
             border-radius:3px; padding:0 6px; }}
QLabel#droMachine {{ font-family:Consolas; font-size:9pt; color:{MUTE}; }}
QLabel#summary {{ background:{FIELD}; border:1px solid {LINE}; border-radius:4px; padding:4px 8px; }}
QPushButton {{ background:{PANEL2}; border:1px solid {LINE}; border-radius:4px; padding:4px 10px; }}
QPushButton:hover {{ border-color:#4a5764; }}
QPushButton:pressed {{ background:#1a2027; }}
QPushButton:checked {{ background:#1f3a5f; border-color:{ACCENT}; color:#fff; }}
QPushButton:disabled {{ color:#5b6672; background:#1b2026; border-color:#2a3138; }}
QPushButton[role="primary"] {{ background:{ACCENT}; border-color:{ACCENT}; color:#fff; }}
QPushButton[role="start"] {{ background:{OK}; border-color:{OK}; color:#fff; font-weight:600; }}
QPushButton[role="pause"] {{ background:#9e6a03; border-color:#9e6a03; color:#fff; font-weight:600; }}
QPushButton[role="stop"] {{ background:{BAD}; border-color:{BAD}; color:#fff; font-weight:600; }}
QPushButton[role="estop"] {{ background:{BAD}; color:#fff; font-weight:bold; font-size:10pt;
                             border:2px solid #ff7b72; border-radius:5px; padding:3px 22px; }}
QPushButton[role="estop"]:pressed {{ background:#8e1519; }}
QPushButton[role="start"]:disabled, QPushButton[role="pause"]:disabled,
QPushButton[role="stop"]:disabled, QPushButton[role="primary"]:disabled
    {{ background:#1b2026; border-color:#2a3138; color:#5b6672; }}
QPushButton[role="view"] {{ padding:3px 12px; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit, QTextBrowser,
QListWidget, QTreeWidget {{ background:{FIELD}; border:1px solid {LINE}; border-radius:4px;
    selection-background-color:#1f3a5f; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ padding:2px 5px; min-height:18px; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color:{ACCENT}; }}
QComboBox QAbstractItemView {{ background:{PANEL}; border:1px solid {LINE}; }}
QTreeWidget::item, QListWidget::item {{ padding:2px; }}
QTabWidget::pane {{ border:none; border-top:1px solid {LINE}; }}
QTabBar::tab {{ background:transparent; color:{MUTE}; padding:5px 14px; border-right:1px solid {LINE}; }}
QTabBar::tab:selected {{ color:#fff; background:{PANEL2}; border-top:2px solid {ACCENT}; }}
QTabBar::tab:hover {{ color:{TEXT}; }}
QSlider::groove:horizontal {{ height:6px; background:{FIELD}; border-radius:3px; }}
QSlider::sub-page:horizontal {{ background:{ACCENT}; border-radius:3px; }}
QSlider::handle:horizontal {{ background:#fff; width:14px; height:14px; margin:-4px 0; border-radius:7px; }}
QSlider::handle:horizontal:disabled {{ background:#5b6672; }}
QProgressBar {{ background:{FIELD}; border:none; border-radius:3px; }}
QProgressBar::chunk {{ background:{OK}; border-radius:3px; }}
QScrollArea {{ border:none; background:transparent; }}
QScrollBar:vertical {{ background:{BG}; width:10px; }}
QScrollBar::handle:vertical {{ background:{LINE}; border-radius:4px; min-height:24px; }}
QScrollBar:horizontal {{ background:{BG}; height:10px; }}
QScrollBar::handle:horizontal {{ background:{LINE}; border-radius:4px; min-width:24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width:0; height:0; }}
QSplitter::handle {{ background:{LINE}; }}
QStatusBar {{ background:#0f1317; color:{MUTE}; }}
QCheckBox::indicator {{ width:14px; height:14px; }}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    for role, col in ((QPalette.Window, BG), (QPalette.WindowText, TEXT), (QPalette.Base, FIELD),
                      (QPalette.AlternateBase, PANEL), (QPalette.Text, TEXT),
                      (QPalette.Button, PANEL2), (QPalette.ButtonText, TEXT),
                      (QPalette.Highlight, ACCENT), (QPalette.HighlightedText, "#ffffff"),
                      (QPalette.ToolTipBase, PANEL2), (QPalette.ToolTipText, TEXT),
                      (QPalette.PlaceholderText, MUTE), (QPalette.Link, "#58a6ff")):
        pal.setColor(role, QColor(col))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor("#5b6672"))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#5b6672"))
    pal.setColor(QPalette.Disabled, QPalette.WindowText, QColor("#5b6672"))
    app.setPalette(pal)
    app.setStyleSheet(QSS)


def set_role(w: QWidget, role: str) -> None:
    """Colour role for a button (see QSS); re-polishes so it shows at once."""
    w.setProperty("role", role)
    w.style().unpolish(w)
    w.style().polish(w)
