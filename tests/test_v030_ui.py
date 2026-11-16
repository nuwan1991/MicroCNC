"""0.3.0: MicroCNC name, update check, layers / project, Go to, jog %, auto-level preview."""
import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from micropcb import APP_NAME, DOWNLOAD_URL, __version__
from micropcb import updates
from micropcb.layers import Layer, Project, guess_kind

GCODE = "G21 G90\nG0 Z1\nG0 X10 Y5\nG1 Z-0.1 F60\nG1 X20 Y5 F200\nG1 X20 Y15\nG0 Z1\n"


def test_name_and_version():
    assert APP_NAME == "MicroCNC" and __version__ == "0.3.0"


def test_parse_version_and_info():
    assert updates.parse_version("0.3.10") > updates.parse_version("0.3.9")
    assert updates.parse_version("v1.2") == (1, 2, 0)
    assert updates.parse_version("0.3.1-beta") == (0, 3, 1)
    with pytest.raises(ValueError):
        updates.parse_version("abc")
    i = updates.parse_info(json.dumps({"version": "9.0.0", "notes": "x"}))
    assert i.newer and i.url == DOWNLOAD_URL
    assert not updates.parse_info('{"version": "0.3.0"}').newer
    # a non-web link in the file is replaced by the download page
    assert updates.parse_info('{"version": "1.0", "url": "file:///c:/x.exe"}').url == DOWNLOAD_URL
    with pytest.raises(ValueError):
        updates.parse_info('{"date": "x"}')


@pytest.mark.parametrize("name,kind", [
    ("Board-F_Cu_iso.nc", "top"), ("board_bottom.nc", "bottom"), ("drill.nc", "drill"),
    ("align_pins.nc", "align"), ("board_cutout.nc", "cutout"), ("PCB.txt", "top"),
    ("hello.nc", "other")])
def test_guess_kind(name, kind):
    assert guess_kind(name) == kind


def test_layer_defaults_and_project_roundtrip(tmp_path):
    top = tmp_path / "top.nc"
    drill = tmp_path / "drill.nc"
    for p in (top, drill):
        p.write_text(GCODE, encoding="utf-8")
    a = Layer.new(str(top), GCODE, default_depth=0.08)
    b = Layer.new(str(drill), GCODE, default_depth=0.08)
    assert (a.kind, a.max_depth) == ("top", 0.08)
    assert (b.kind, b.max_depth) == ("drill", 0.0)      # no depth limit for drilling
    b.visible = False
    pp = tmp_path / "p.mcnc.json"
    pr = Project([a, b], 1, {"top": None, "bottom": None})
    pp.write_text(pr.to_json(str(pp)), encoding="utf-8")
    d = json.loads(pp.read_text(encoding="utf-8"))
    assert d["layers"][0]["path"] == "top.nc"           # relative to the project
    q = Project.load(str(pp))
    assert [ly.kind for ly in q.layers] == ["top", "drill"]
    assert q.selected == 1 and not q.layers[1].visible and q.layers[0].text == GCODE
    drill.unlink()
    assert Project.load(str(pp)).layers[1].missing


def _win():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from micropcb.ui import main_window as mw
    return mw, mw.MainWindow()


def test_window_layout_names_and_jog():
    mw, w = _win()
    try:
        assert w.windowTitle() == f"MicroCNC {__version__}"
        assert mw.STEP_SIZES[0] == 0.004 and 5.0 in mw.STEP_SIZES
        assert w.step_group.checkedId() == mw.DEFAULT_STEP
        menus = [a.text().replace("&", "") for a in w.menu_bar.actions()]
        assert menus == ["File", "View", "Settings", "Help"]
        w.speed.setValue(0)
        assert w._jog_spt() == 1
        w.speed.setValue(100)
        assert w._jog_spt() == w.settings.max_steps_per_tick
        w.speed.setValue(50)
        assert w._jog_spt() == round(w.settings.max_steps_per_tick / 2)
        assert w.settings.jog_speed_pct == 50
    finally:
        w.close()


def test_go_to_moves_at_safe_height():
    from micropcb.sim_machine import SimMachine
    mw, w = _win()
    try:
        w.ctl.connect(transport=SimMachine(realtime=False))
        w.ctl.zero_axes()
        w.ctl.go_to(3.0, 2.0, None)
        w.ctl.wait_idle(10)
        x, y, z = w.ctl.work_mm()
        assert abs(x - 3) < 0.01 and abs(y - 2) < 0.01
        assert abs(z - w.settings.safe_height_mm) < 0.01    # lifted first, Z not given
        w.ctl.go_to(None, None, 1.0)
        w.ctl.wait_idle(10)
        assert abs(w.ctl.work_mm()[2] - 1.0) < 0.01
        for e, v in zip(w.goto_edits, ("", "4", "")):
            e.setText(v)
        w.go_to_position()
        w.ctl.wait_idle(10)
        assert abs(w.ctl.work_mm()[1] - 4) < 0.01 and abs(w.ctl.work_mm()[0] - 3) < 0.01
    finally:
        w.ctl.disconnect()
        w.close()


def test_layers_select_options_and_overlays(tmp_path):
    top = tmp_path / "top_iso.nc"
    drill = tmp_path / "drill.nc"
    top.write_text(GCODE, encoding="utf-8")
    drill.write_text(GCODE.replace("Z-0.1", "Z-1.6"), encoding="utf-8")
    mw, w = _win()
    try:
        w.settings.job_max_depth = 0.05
        assert w.add_layer_file(str(top)) == 0
        assert w._active_layer is w.layers[0] and w.job_path == str(top)
        assert w.ctl.job_max_depth == 0.05                 # isolation: depth limit on
        assert w.add_layer_file(str(drill)) == 1
        assert w._active_layer.kind == "drill" and w.ctl.job_max_depth == 0.0
        assert len(w.preview._layers) == 1                 # the other layer drawn faint
        w.layers_box.tree.setCurrentItem(w.layers_box.tree.topLevelItem(0))
        assert w._active_layer is w.layers[0]
        w.layers_box.side_combo.setCurrentIndex(1)        # bottom side
        assert w.layers[0].side == "bottom" and w.al.side == "bottom"
        assert w.current_side() == "bottom"
        assert w.add_layer_file(str(top)) == 0             # same file: no duplicate
        assert len(w.layers) == 2
        # a CAM / camera job is not one of the layers
        w.load_job_text(GCODE, "camera-marks.nc")
        assert w._active_layer is None and w.layers_box.current() == -1
        # project save / open
        pth = str(tmp_path / "x.mcnc.json")
        w.project_path = pth
        w.save_project()
        w.new_project.__func__  # exists
        w.layers = []
        w.open_project(pth)
        assert len(w.layers) == 2 and w.layers[0].side == "bottom"
        w.remove_layer(1)
        assert len(w.layers) == 1
    finally:
        w.close()


def test_autolevel_preview_points_follow_the_fields():
    mw, w = _win()
    try:
        w.set_bottom_visible(True)
        w.bottom_tabs.setCurrentWidget(w.al_scroll)
        al = w.al
        for f, v in zip(al.area, (0, 20, 0, 10)):
            f.setValue(v)
        al.margin.setValue(0)
        al.spacing.setValue(10)
        plan = w.preview.plan
        assert plan is not None and len(plan[4]) == 3 * 2   # 3 × 2 grid, before measuring
        al.spacing.setValue(5)
        assert len(w.preview.plan[4]) == 5 * 3             # updates while typing
        w.bottom_tabs.setCurrentIndex(0)                   # console: no points drawn
        assert w.preview.plan is None
        assert al.side == "top" and al.hm is None
        al.side_combo.setCurrentIndex(1)
        assert al.side == "bottom"
    finally:
        w.close()


def test_about_and_docs():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from micropcb.ui.help_dialogs import AboutDialog, DocViewer, docs_dir, system_info
    assert "MicroCNC" in system_info()
    d = AboutDialog()
    d.close()
    assert docs_dir() is not None and (docs_dir() / "user-guide.md").is_file()
    DocViewer("User guide", "user-guide.md").close()
