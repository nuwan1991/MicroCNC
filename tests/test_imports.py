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


def test_low_z_xy_jog_warning(monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import micropcb.ui.main_window as mw
    from micropcb.sim_machine import SimMachine
    from micropcb.ui.settings_dialog import SettingsDialog
    answer = {"go": False, "asked": 0}

    class FakeBox:
        Warning, RejectRole, AcceptRole = 0, 0, 1

        def __init__(self, *a, **k):
            self._b = []

        def addButton(self, text, role):
            self._b.append(text)
            return text

        def setDefaultButton(self, b):
            pass

        def exec(self):
            answer["asked"] += 1

        def clickedButton(self):
            return self._b[1] if answer["go"] else self._b[0]

    w = mw.MainWindow()
    try:
        w.ctl.connect(transport=SimMachine(realtime=False))
        w.ctl.zero_axes()
        monkeypatch.setattr(mw, "QMessageBox", FakeBox)
        assert w._xy_jog_allowed("z")                      # Z is never asked
        assert not w._xy_jog_allowed("x") and answer["asked"] == 1   # Z 0: cancel
        answer["go"] = True
        assert w._xy_jog_allowed("y") and w._xy_jog_allowed("x")
        assert answer["asked"] == 2                        # accepted: not asked again
        w.ctl.jog("z", 1)
        w.ctl.wait_idle(5)
        assert w._xy_jog_allowed("x") and not w._low_z_ok   # high: no question, reset
        w.settings.jog_low_z_warning = False
        w.ctl.jog("z", -1)
        w.ctl.wait_idle(5)
        assert w._xy_jog_allowed("x") and answer["asked"] == 2   # switched off
        d = SettingsDialog(w.settings, w)
        assert not d.low_warn.isChecked() and d.res_safe.isChecked()
    finally:
        w.settings.jog_low_z_warning = True
        w.ctl.disconnect()
        w.close()


def test_run_part_buttons_from_simulation_bar():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import micropcb.ui.main_window as mw
    from test_gcode_planner import JOB
    w = mw.MainWindow()
    keep = (w.settings.job_origin, w.settings.job_max_depth)
    w.origin_combo.setCurrentIndex(w.origin_combo.findData("file"))
    w.depth_chk.setChecked(False)
    try:
        w.load_job_text(JOB, "job.nc")
        tl = w.timeline
        w._set_play_idx(tl.index_for_line(7))
        w._set_range_end(0)
        w._set_play_idx(w.timeline.index_for_line(9) + 1)   # timeline of the part now
        w._set_range_end(1)
        assert w.ctl.job_range == (7, 9)
        cut_x = [m.end[0] for m in w.job_prog_raw.lines() if m.end[2] < 0]
        assert min(cut_x) >= 20                       # preview shows only the part
        assert "7" in w.range_lbl.text() and "9" in w.range_lbl.text()
        w.depth_chk.setChecked(True)                  # other job options keep the range
        assert w.ctl.job_range == (7, 9)
        w.depth_chk.setChecked(False)
        w._clear_range()
        assert w.ctl.job_range is None
        assert min(m.end[0] for m in w.job_prog_raw.lines() if m.end[2] < 0) < 10
        w.ctl.job_range = (7, 9)
        w.load_job_text("G0 X1\nG1 Z-0.1 F50\nG1 X2 F100\n", "other.nc")   # new file
        assert w.ctl.job_range is None
    finally:
        w.settings.job_origin, w.settings.job_max_depth = keep
        w.close()


def test_heightmap_autosave(tmp_path):
    import os
    from pathlib import Path
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import micropcb.ui.main_window as mw
    from micropcb.autolevel import HeightMap
    w = mw.MainWindow()
    try:
        hm = HeightMap.empty([0, 10], [0, 10])
        hm.z[:] = 0.01
        hm.meta.update(counter_fixed=True, drift_mm=0.002)
        w.al.hm = hm
        p = w.al.autosave()
        assert p and Path(p).exists() and Path(p).parent.name == "MicroPCB height maps"
        assert str(tmp_path) in p                        # not the user's Documents
        assert HeightMap.from_json(Path(p).read_text(encoding="utf-8")).complete
        hm.meta["drift_mm"] = 0.2
        assert w.al.autosave().endswith("-DRIFT.hmap.json")
        w.settings.autosave_heightmaps = False
        assert w.al.autosave() is None
    finally:
        w.close()
