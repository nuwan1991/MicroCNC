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


def test_cam_window_show_one_op_and_narrow_checks():
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
        pb = board(gap=0.1)
        ops = _ops(pb)
        w.cam.problems = [(o.name, x, y, s) for o in ops if o.problem_area is not None
                          for x, y, s in cam.problem_spots(o.problem_area)]
        assert w.cam.problems
        full = cam.combine(ops, "t")
        w.load_job_text(full, "b.nc")
        w.set_ops(ops, "t", 2.0, "b.nc")
        texts = [w.checks_list.item(i).text() for i in range(w.checks_list.count())]
        assert any("too narrow" in t for t in texts)
        w._check_clicked(next(w.checks_list.item(i) for i in range(w.checks_list.count())
                              if "too narrow" in w.checks_list.item(i).text()))
        n_all = len(w.tp.program.lines())
        w.show_op.setCurrentIndex(w.show_op.count() - 1)
        app.processEvents()
        assert len(w.tp.program.lines()) < n_all
        w.show_op.setCurrentIndex(0)
        assert len(w.tp.program.lines()) == n_all
    finally:
        w.close()
