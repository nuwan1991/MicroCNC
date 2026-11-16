"""Help menu: About, user guide / safety viewer, keyboard shortcuts, update check."""
from __future__ import annotations

import platform
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QTextBrowser, QVBoxLayout)

from .. import APP_NAME, COMPANY, DOWNLOAD_URL, TAGLINE, WEBSITE, __version__
from . import theme

SHORTCUTS = (
    "Esc  —  E-STOP (always)\n\n"
    "Hold to jog (when keyboard jog is on, ⌨ in the Jog section):\n"
    "  ← / →     X− / X+\n  ↓ / ↑     Y− / Y+\n  PgDn / PgUp   Z− / Z+\n"
    "Releasing the key stops the motion.\n\n"
    "Ctrl+O  open project · Ctrl+S  save project · Ctrl+L  add G-code layer\n"
    "F5  reload layers · F6  show / hide bottom panel · F11  full-screen view\n"
    "F1  user guide")


def docs_dir() -> Path | None:
    """docs\\ next to the program (installer) or in the source tree."""
    here = Path(sys.executable).parent if getattr(sys, "frozen", False) else \
        Path(__file__).resolve().parents[3]
    for d in (here / "docs", Path(__file__).resolve().parents[3] / "docs"):
        if d.is_dir():
            return d
    return None


def system_info() -> str:
    import PySide6
    lines = [f"{APP_NAME} {__version__}",
             f"{platform.system()} {platform.release()} ({platform.machine()})",
             f"Python {platform.python_version()} · Qt (PySide6) {PySide6.__version__}"]
    for mod in ("numpy", "shapely", "cv2", "serial"):
        try:
            m = __import__(mod)
            lines.append(f"{mod} {getattr(m, '__version__', '?')}")
        except Exception:   # noqa: BLE001 - optional parts
            lines.append(f"{mod} not available")
    return "\n".join(lines)


class AboutDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        logo = QLabel("μC")
        logo.setAlignment(Qt.AlignCenter)
        logo.setFixedSize(56, 56)
        logo.setStyleSheet("background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #2f81f7,"
                           " stop:1 #1a4f99); border-radius:10px; color:white;"
                           " font-size:18pt; font-weight:800;")
        top.addWidget(logo, 0, Qt.AlignTop)
        txt = QLabel(f"<div style='font-size:14pt; font-weight:700'>{APP_NAME} {__version__}</div>"
                     f"<div style='color:{theme.MUTE}'>{TAGLINE}</div><br>"
                     f"© 2026 {COMPANY}<br>"
                     f"<a href='{WEBSITE}' style='color:#58a6ff'>{WEBSITE.split('//')[1]}</a>")
        txt.setOpenExternalLinks(True)
        txt.setTextInteractionFlags(Qt.TextBrowserInteraction)
        top.addWidget(txt, 1)
        v.addLayout(top)
        self.info = system_info()
        box = QLabel(self.info.replace("\n", "<br>"))
        box.setObjectName("summary")
        box.setStyleSheet("font-family:Consolas; font-size:8pt;")
        box.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(box)
        row = QHBoxLayout()
        row.addStretch(1)
        copy = QPushButton("Copy info")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.info))
        lic = QPushButton("Licenses")
        lic.clicked.connect(self.licenses)
        close = QPushButton("Close")
        theme.set_role(close, "primary")
        close.clicked.connect(self.accept)
        for b in (copy, lic, close):
            row.addWidget(b)
        v.addLayout(row)
        self.setMinimumWidth(440)

    def licenses(self) -> None:
        QMessageBox.information(
            self, "Licenses",
            f"{APP_NAME} © 2026 {COMPANY}.\n\nIt uses these open-source parts, each under its "
            "own licence:\n• Qt 6 / PySide6 (LGPL v3)\n• Python (PSF licence)\n"
            "• NumPy, shapely, pyqtgraph, PyOpenGL (BSD / MIT style)\n• OpenCV (Apache 2.0)\n"
            "• pySerial (BSD)\n\nThe licence texts are in the program folder of each library.")


class DocViewer(QDialog):
    """Shows a Markdown file from docs\\ (user guide, safety features)."""

    def __init__(self, title: str, filename: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} — {title}")
        v = QVBoxLayout(self)
        view = QTextBrowser()
        view.setOpenExternalLinks(True)
        d = docs_dir()
        p = d / filename if d else None
        if p is not None and p.is_file():
            view.setMarkdown(p.read_text(encoding="utf-8", errors="replace"))
            view.document().setDefaultStyleSheet("a { color:#58a6ff; }")
        else:
            view.setPlainText(f"{filename} was not found next to the program.")
        v.addWidget(view)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        v.addWidget(close, 0, Qt.AlignRight)
        self.resize(820, 680)


class _UpdateBridge(QObject):
    done = Signal(object, object)   # (UpdateInfo | None, error text | None)


class UpdateChecker:
    """Runs the check in a thread; `show(...)` opens the result dialog when asked."""

    def __init__(self, win) -> None:
        self.win = win
        self.bridge = _UpdateBridge()
        self.bridge.done.connect(self._done, Qt.QueuedConnection)
        self.busy = False
        self.manual = False

    def check(self, manual: bool) -> None:
        if self.busy:
            return
        if manual and self.win.ctl.job_active:
            QMessageBox.information(self.win, "Check for updates",
                                    "A job is running - check for updates after it.")
            return
        self.busy, self.manual = True, manual
        if manual:
            self.win.log("Checking for updates…")

        def work():
            from ..updates import fetch
            try:
                self.bridge.done.emit(fetch(), None)
            except Exception as e:   # noqa: BLE001 - network / bad file
                self.bridge.done.emit(None, str(e) or type(e).__name__)

        threading.Thread(target=work, name="microcnc-update", daemon=True).start()

    def _done(self, info, err) -> None:
        self.busy = False
        if info is None:
            self.win.log(f"Update check failed: {err}")
            if self.manual:
                QMessageBox.warning(self.win, "Check for updates",
                                    f"Could not check for updates:\n{err}\n\n"
                                    f"Download page: {DOWNLOAD_URL}")
            return
        if not info.newer:
            self.win.log(f"{APP_NAME} is up to date ({__version__}; latest {info.version})")
            if self.manual:
                QMessageBox.information(self.win, "Check for updates",
                                        f"{APP_NAME} {__version__} is the latest version.")
            return
        self.win.log(f"New version available: {info.version} - {info.url}")
        if self.win.ctl.job_active:
            return                     # never interrupt a job; the console line is enough
        UpdateDialog(info, self.win).exec()


class UpdateDialog(QDialog):
    def __init__(self, info, win) -> None:
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Check for updates")
        v = QVBoxLayout(self)
        head = QLabel("A new version is available")
        head.setStyleSheet("font-size:12pt; font-weight:600; color:#7ee2a8;")
        v.addWidget(head)
        v.addWidget(QLabel(f"Installed: {__version__}<br>Latest: <b>{info.version}</b>"
                           + (f" ({info.date})" if info.date else "")))
        if info.notes:
            notes = QTextBrowser()
            notes.setPlainText(info.notes)
            notes.setMaximumHeight(160)
            v.addWidget(notes)
        link = QLabel(f"Download page: <a href='{info.url}' style='color:#58a6ff'>{info.url}</a>"
                      "<br><span style='color:#8b97a5'>The program never installs by itself. "
                      "Close it before running the new installer.</span>")
        link.setOpenExternalLinks(True)
        link.setWordWrap(True)
        v.addWidget(link)
        self.auto = QCheckBox("Check at startup")
        self.auto.setChecked(bool(win.settings.check_updates))
        self.auto.toggled.connect(self._auto)
        v.addWidget(self.auto)
        row = QHBoxLayout()
        row.addStretch(1)
        later = QPushButton("Later")
        later.clicked.connect(self.reject)
        go = QPushButton("Open download page")
        theme.set_role(go, "primary")
        go.clicked.connect(lambda: (QDesktopServices.openUrl(QUrl(info.url)), self.accept()))
        row.addWidget(later)
        row.addWidget(go)
        v.addLayout(row)
        self.setMinimumWidth(440)

    def _auto(self, on: bool) -> None:
        self.win.settings.check_updates = on
        self.win.settings.save()
