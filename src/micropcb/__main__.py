"""Start the MicroCNC GUI:  python -m micropcb"""
import sys


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from .ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("MicroCNC")
    from .ui.theme import apply_theme
    apply_theme(app)
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
