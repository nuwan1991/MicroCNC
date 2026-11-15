"""Every module must at least compile/import (catches broken UI files the
other tests never load)."""
import compileall
from pathlib import Path


def test_all_sources_compile():
    src = Path(__file__).resolve().parents[1] / "src" / "micropcb"
    assert compileall.compile_dir(str(src), quiet=1, force=True)


def test_cam_app_does_not_load_machine_side():
    """MicroPCB CAM must run without the machine code (docs/split-design.md)."""
    import subprocess
    import sys
    code = ("import sys; import micropcb.apps.cam; "
            "bad = [m for m in ('micropcb.serial_link', 'micropcb.controller', "
            "'micropcb.camera', 'micropcb.autolevel', 'micropcb.sim_machine', "
            "'micropcb.ui.main_window', 'serial') if m in sys.modules]; print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(__import__("pathlib").Path(__file__).parent.parent / "src"))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", out.stdout


def test_cam_window_generates_and_shows_job(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from micropcb.apps.cam import CamWindow
    from micropcb.pcb import cam
    from test_cam import board
    from test_jobfile import _ops
    w = CamWindow()
    try:
        pb = board()
        ops = _ops(pb)
        w.cam.text = cam.combine(ops, "t")
        w.cam._last = (pb, ops, "top", "top side", None)
        w.load_job_text(w.cam.text, "board_top.nc")
        app.processEvents()
        assert w.tp is not None and w.checks_list.count() >= 1
        assert "moves" in w.info_lbl.text()
    finally:
        w.close()
