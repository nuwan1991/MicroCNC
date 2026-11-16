# PyInstaller spec: two programs (MicroPCB, MicroPCB-CAM) in one folder.
# Build with packaging\build_installer.ps1 (runs this, then Inno Setup).
# -*- mode: python -*-
import os

HERE = os.path.abspath(SPECPATH)
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")

EXCLUDES = ["tkinter", "pytest", "IPython", "matplotlib", "PyQt5", "PyQt6", "PySide2"]


def analysis(script):
    return Analysis([os.path.join(HERE, script)], pathex=[SRC],
                    hiddenimports=["micropcb.ui.view3d", "OpenGL.platform.win32",
                                   "pyqtgraph.opengl"],
                    excludes=EXCLUDES, noarchive=False)


a_main = analysis("launch_micropcb.py")
a_cam = analysis("launch_cam.py")


def exe(a, name):
    return EXE(PYZ(a.pure), a.scripts, [], exclude_binaries=True, name=name,
               console=False, upx=False)


e_main = exe(a_main, "MicroPCB")
e_cam = exe(a_cam, "MicroPCB-CAM")

coll = COLLECT(e_main, a_main.binaries, a_main.datas,
               e_cam, a_cam.binaries, a_cam.datas,
               name="MicroPCB", upx=False)
