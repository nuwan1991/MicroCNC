"""Job side file / job list (docs/split-design.md, phase 1)."""
import json

import pytest

from micropcb import jobfile
from micropcb.jobfile import JobFeature, JobInfo
from micropcb.pcb import cam

from test_cam import DR, EM, VB, board


def _ops(pb):
    return [cam.isolation(pb, "top", VB, depth=0.05, passes=1),
            cam.drilling(pb.holes[:2], DR, 1.8),
            cam.cutout(pb, EM, 1.6, 0.1, 0, 2.0, 0.5)]


def test_roundtrip_and_unknown_keys(tmp_path):
    info = JobInfo(operation="isolation", side="bottom", mirror_x_mm=10.0,
                   board_size=(20.0, 10.0), outline=[[(0, 0), (20, 0), (20, 10)]],
                   tools=[{"name": "V30"}], features=[JobFeature("hole", 5, 5, 0.8)],
                   copper=[[(1, 1), (2, 1), (2, 2)]], time_s=60, cut_area=(0, 0, 20, 10))
    nc = tmp_path / "a.nc"
    nc.write_text("G0 X1\n")
    p = jobfile.write(str(nc), "G0 X1\n", info)
    d = json.loads(open(p).read())
    d["future_key"] = {"x": 1}
    open(p, "w").write(json.dumps(d))
    back, msg = jobfile.read(str(nc), "G0 X1\r\n")     # CRLF does not count as a change
    assert msg == "with board data"
    assert back.side == "bottom" and back.mirror_x_mm == 10.0
    assert back.features == info.features and back.cut_area == (0, 0, 20, 10)
    assert back.copper[0][1] == (2, 1)


def test_missing_stale_and_newer(tmp_path):
    nc = str(tmp_path / "a.nc")
    assert jobfile.read(nc, "x") == (None, "")
    jobfile.write(nc, "G0 X1\n", JobInfo())
    info, msg = jobfile.read(nc, "G0 X2\n")
    assert info is None and "out of date" in msg
    d = json.loads(open(nc + ".json").read())
    d["version"] = 99
    open(nc + ".json", "w").write(json.dumps(d))
    info, msg = jobfile.read(nc, "G0 X1\n")
    assert info is None and "newer" in msg


def test_copper_point_limit():
    ring = [(0.0, 0.0)] * 1000
    d = JobInfo(copper=[ring] * 30).to_dict()
    assert sum(len(r) for r in d["copper"]) <= jobfile.MAX_COPPER_POINTS


def test_job_info_from_cam_board():
    pb = board()
    ops = _ops(pb)
    info = cam.job_info(pb, ops, "top", ["b.gbr"], "MicroPCB test")
    assert info.operation == "combined" and info.side == "top" and info.mirror_x_mm is None
    assert [t["name"] for t in info.tools] == ["V30", "D0.8", "EM1"]
    assert len(info.holes) == 3
    pads = [f for f in info.features if f.kind == "pad"]
    assert pads == []            # both pads have a hole in the middle -> holes only
    assert info.outline and info.copper
    x0, y0, x1, y1 = info.cut_area
    assert x0 < 1 and x1 > 19          # cutout goes around the board
    one = cam.job_info(pb, ops[:1], "top")
    assert one.operation == "isolation"


def test_per_tool_groups_and_export_files(tmp_path):
    pb = board()
    a, b, c = _ops(pb)
    a2 = cam.isolation(pb, "top", VB, depth=0.05, passes=1)
    groups = cam.per_tool([a, a2, b, c])
    assert [[o.tool.name for o in g] for g in groups] == [["V30", "V30"], ["D0.8"], ["EM1"]]
    files = []
    for n, g in enumerate(groups, 1):
        text = cam.combine(g, "t")
        assert "M6" not in text                      # one tool per file
        p = str(tmp_path / f"{n}.nc")
        open(p, "w").write(text)
        jobfile.write(p, text, cam.job_info(pb, g, "top"))
        files.append({"file": f"{n}.nc", "operation": cam.op_kind(g[0]), "tool": g[0].tool.label})
    lst = jobfile.write_list(str(tmp_path / "b-jobs.json"), "b", files)
    d = jobfile.read_list(lst)
    assert [f["operation"] for f in d["files"]] == ["isolation", "drilling", "cutout"]
    info, msg = jobfile.read(str(tmp_path / "2.nc"), open(tmp_path / "2.nc").read())
    assert info.operation == "drilling" and msg == "with board data"
    with pytest.raises(ValueError):
        jobfile.read_list(str(tmp_path / "2.nc.json"))


def test_jobfile_does_not_import_pcb():
    src = open(jobfile.__file__, encoding="utf-8").read()
    assert "from .pcb" not in src and "micropcb.pcb" not in src.replace("micropcb.pcb (", "")
