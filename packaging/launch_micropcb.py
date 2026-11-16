"""MicroCNC.exe - the machine program (PyInstaller entry point).

MicroCNC.exe --selftest <file>: import every library the program can need and
write "ok" or the error to <file> (checks a build; no window opens).
"""
import sys


def _selftest(out: str) -> int:
    try:
        import cv2  # noqa: F401  camera
        import numpy  # noqa: F401
        import OpenGL.GL  # noqa: F401  3D view
        import pyqtgraph.opengl  # noqa: F401
        import serial.tools.list_ports  # noqa: F401  machine port
        import shapely  # noqa: F401  CAM
        from PySide6.QtWidgets import QApplication

        import micropcb
        from micropcb.apps.cam import CamWindow  # noqa: F401
        from micropcb.pcb.board import Board  # noqa: F401
        from micropcb.ui.main_window import MainWindow
        app = QApplication.instance() or QApplication(sys.argv[:1])
        w = MainWindow()
        w.close()
        del app
        msg = f"ok {micropcb.__version__} cv2 {cv2.__version__} shapely {shapely.__version__}"
    except Exception as e:  # noqa: BLE001
        msg = f"FAILED {type(e).__name__}: {e}"
    with open(out, "w", encoding="utf-8") as f:
        f.write(msg)
    return 0 if msg.startswith("ok") else 1


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--selftest":
        sys.exit(_selftest(sys.argv[2]))
    from micropcb.__main__ import main
    sys.exit(main())
