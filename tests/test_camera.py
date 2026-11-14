import math

import numpy as np
import pytest
from shapely.geometry import box

from micropcb.camera.align import (AlignPoint, Alignment, apply_alignment, fit_alignment,
                                   refine_feature)
from micropcb.camera.calib import CalibPoint, CameraCalib, fit_homography, mark_job, mark_points
from micropcb.camera.device import SimScene
from micropcb.camera.obstacles import Obstacle, board_overlap, check_obstacles, detect_obstacles
from micropcb.gcode import parse
from micropcb.toolpath import Toolpath

cv2 = pytest.importorskip("cv2")


def calibrated(scene, axes="", pos_fn=None):
    """Calibrate from marks seen in the simulated picture."""
    c = CameraCalib(bed_axes=axes, image_size=scene.size)
    marks = [(x + 3, y + 5) for x, y in mark_points(scene.area, 3)]
    scene.marks = marks
    pos = (40.0, 70.0, 0.0)
    scene.get_pos = lambda: pos
    c.ref = pos[:2]
    gray = cv2.cvtColor(scene.render(), cv2.COLOR_BGR2GRAY)
    for m in marks:
        px = scene.to_px([m])[0] + (1.5, -1.0)              # sloppy click
        r = refine_feature(gray, *px, 12)
        assert r is not None
        f = c.frame_of(m, pos)
        c.points.append(CalibPoint(r[0], r[1], *f))
    c.fit()
    return c


def test_homography_roundtrip():
    rng = np.random.default_rng(1)
    h = np.array([[2.0, 0.1, 30], [-0.05, 1.9, 10], [1e-4, -2e-4, 1]])
    src = rng.uniform(0, 500, (8, 2))
    q = np.c_[src, np.ones(8)] @ h.T
    dst = q[:, :2] / q[:, 2:]
    hh = fit_homography(src, dst)
    assert np.allclose(hh, h / h[2, 2], atol=1e-6)
    with pytest.raises(ValueError):
        fit_homography([(0, 0), (1, 1), (2, 2), (3, 3)], [(0, 0), (1, 1), (2, 2), (3, 3)])


@pytest.mark.parametrize("axes", ["", "Y"])
def test_calibration_from_marks(axes):
    scene = SimScene(bed_axes=axes)
    c = calibrated(scene, axes)
    assert c.rms_mm < 0.15
    # another machine position: a bed point still maps back correctly
    pos = (10.0, 150.0, 0.0)
    scene.get_pos = lambda: pos
    p = (77.0, 33.0)
    px = scene.to_px([p])
    assert np.allclose(c.pixel_to_bed(px, pos)[0], p, atol=0.3)   # ~4 px per mm
    assert np.allclose(c.bed_to_pixel([p], pos), px, atol=0.6)
    d = CameraCalib.from_dict(c.to_dict())
    assert np.allclose(d.pixel_to_bed(px, pos), c.pixel_to_bed(px, pos))


def test_parallax_correction():
    c = CameraCalib(H=np.diag([0.1, 0.1, 1.0]), image_size=(1000, 1000), cam_height_mm=300)
    # point under the lens does not move, others shrink towards it
    assert np.allclose(c.pixel_to_bed([(500, 500)], z=1.6), [(50, 50)])
    far = c.pixel_to_bed([(1000, 500)], z=30)[0]
    assert far[0] == pytest.approx(50 + 50 * 270 / 300)
    assert np.allclose(c.bed_to_pixel([far], z=30), [(1000, 500)])


def test_alignment_fit_and_apply():
    true = Alignment(1.5, 2.0, -0.7)
    e = np.array([(5, 5), (60, 8), (55, 40), (10, 35)], float)
    m = true.apply(e) + np.random.default_rng(2).normal(0, 0.01, e.shape)
    al = fit_alignment([AlignPoint(*a, *b) for a, b in zip(e, m)])
    assert al.angle_deg == pytest.approx(1.5, abs=0.05)
    assert (al.tx, al.ty) == pytest.approx((2.0, -0.7), abs=0.05)
    assert al.max_error < 0.05 and al.scale == pytest.approx(1, abs=1e-3)
    assert np.allclose(al.inverse(al.apply(e)), e)
    one = fit_alignment([AlignPoint(1, 1, 1.3, 0.8)])
    assert (one.tx, one.ty) == pytest.approx((0.3, -0.2))
    prog = parse("G0 X10 Y0\nG1 Z-0.1 F50\nG1 X20\n")
    out = apply_alignment(prog, Alignment(90, 0, 0))
    assert out.lines()[-1].end == pytest.approx((0, 20, -0.1), abs=1e-9)


def test_obstacle_checks():
    prog = parse("G0 Z2\nG0 X30 Y30\nG1 Z-0.1 F50\nG1 X50\nG0 Z10\nG0 X80\n")
    tp = Toolpath(prog)
    clamp = Obstacle([(58, 25), (62, 25), (62, 35), (58, 35)], "clamp")   # bed mm
    iss = check_obstacles(tp, [clamp], (10, 0), safe_z=5)     # work X 48..52
    assert [i.line for i in iss] == [4, 5]   # into it, then lift inside it
    assert not check_obstacles(tp, [clamp], (40, 0), safe_z=5)   # only the Z10 move crosses
    assert board_overlap(box(0, 0, 20, 20), [clamp], (45, 10)) == ["clamp"]


def test_detect_obstacles_from_reference():
    scene = SimScene()
    c = calibrated(scene)
    ref = scene.render()
    scene.objects = [[(100, 20), (115, 20), (115, 40), (100, 40)]]
    found = detect_obstacles(ref, scene.render(),
                             lambda px: c.pixel_to_bed(px, scene.get_pos()))
    assert len(found) == 1
    x0, y0, x1, y1 = found[0].polygon().bounds
    assert (x0, y0, x1, y1) == pytest.approx((100, 20, 115, 40), abs=1.0)


def test_mark_job_parses():
    prog = parse(mark_job(mark_points((140, 200), 2)))
    assert prog.bounds()[0][2] == pytest.approx(-0.1)
