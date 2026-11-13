import math
import os
import zipfile

import pytest

from micropcb.pcb.board import Board, Placement, role_from_name
from micropcb.pcb.excellon import parse_excellon
from micropcb.pcb.gerber import parse_gerber

HEAD = "%FSLAX46Y46*%\n%MOMM*%\n%LPD*%\nG01*\n"


def mm(v):  # 4.6 format integer
    return str(round(v * 1_000_000))


def g(body, head=HEAD):
    return parse_gerber(head + body + "M02*\n")


# ------------------------------------------------------------------ gerber
def test_flashes_and_units():
    L = g("%ADD10C,1.000000*%\n%ADD11R,2.000000X1.000000*%\nD10*\n"
          f"X{mm(0)}Y{mm(0)}D03*\nD11*\nX{mm(10)}Y{mm(0)}D03*\n")
    assert L.flashes == 2
    assert L.geometry.area == pytest.approx(math.pi * 0.25 + 2.0, rel=1e-3)
    inch = parse_gerber("%FSLAX25Y25*%\n%MOIN*%\n%ADD10C,0.10000*%\nD10*\nX100000Y0D03*\nM02*\n")
    c = inch.geometry.centroid
    assert c.x == pytest.approx(25.4) and inch.geometry.area == pytest.approx(
        math.pi * (2.54 / 2) ** 2, rel=3e-3)


def test_draw_is_round_stroke():
    L = g(f"%ADD10C,0.500000*%\nD10*\nX{mm(0)}Y{mm(0)}D02*\nX{mm(10)}Y{mm(0)}D01*\n")
    assert L.draws == 1
    assert L.geometry.area == pytest.approx(10 * 0.5 + math.pi * 0.25 ** 2, rel=1e-3)
    assert L.paths == [[(0.0, 0.0), (10.0, 0.0)]]


def test_arc_draw_quarter_circle():
    L = g(f"%ADD10C,0.100000*%\nG75*\nD10*\nX{mm(5)}Y{mm(0)}D02*\n"
          f"G03X{mm(0)}Y{mm(5)}I{mm(-5)}J{mm(0)}D01*\n")
    pts = L.paths[0]
    assert pts[-1] == pytest.approx((0, 5))
    for x, y in pts:
        assert math.hypot(x, y) == pytest.approx(5, abs=1e-6)
    length = sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
    assert length == pytest.approx(math.pi * 5 / 2, rel=1e-3)


def test_region_and_clear_polarity():
    sq = (f"G36*\nX{mm(0)}Y{mm(0)}D02*\nX{mm(10)}Y{mm(0)}D01*\nX{mm(10)}Y{mm(10)}D01*\n"
          f"X{mm(0)}Y{mm(10)}D01*\nX{mm(0)}Y{mm(0)}D01*\nG37*\n")
    hole = "%LPC*%\n%ADD10C,2.000000*%\nD10*\n" + f"X{mm(5)}Y{mm(5)}D03*\n"
    L = g(sq + hole)
    assert L.regions == 1
    assert L.geometry.area == pytest.approx(100 - math.pi, rel=1e-3)


def test_kicad_roundrect_macro():
    macro = ("%AMRoundRect*\n0 Rectangle with rounded corners*\n"
             "4,1,4,$2,$3,$4,$5,$6,$7,$8,$9,$2,$3,0*\n"
             "1,1,$1+$1,$2,$3*\n1,1,$1+$1,$4,$5*\n1,1,$1+$1,$6,$7*\n1,1,$1+$1,$8,$9*\n"
             "20,1,$1+$1,$2,$3,$4,$5,0*\n20,1,$1+$1,$4,$5,$6,$7,0*\n"
             "20,1,$1+$1,$6,$7,$8,$9,0*\n20,1,$1+$1,$8,$9,$2,$3,0*%\n")
    # 2 x 1 mm pad, corner radius 0.25 -> inner box 1.5 x 0.5
    ad = "%ADD20RoundRect,0.250000X-0.750000X-0.250000X0.750000X-0.250000X0.750000X0.250000X-0.750000X0.250000X0*%\n"
    L = g(macro + ad + f"D20*\nX{mm(0)}Y{mm(0)}D03*\n")
    expect = 2 * 1 - (4 - math.pi) * 0.25 ** 2
    assert L.geometry.area == pytest.approx(expect, rel=2e-3)
    assert L.geometry.bounds == pytest.approx((-1, -0.5, 1, 0.5), abs=1e-6)


def test_x2_file_function_from_comment():
    L = g("G04 #@! TF.FileFunction,Copper,L2,Bot*\n%ADD10C,1*%\nD10*\nX0Y0D03*\n")
    assert L.file_function == "Copper,L2,Bot"


# ------------------------------------------------------------------ excellon
def test_excellon_kicad_metric_decimal():
    d = parse_excellon("M48\n; #@! TF.FileFunction,Plated,1,2,PTH\nFMAT,2\nMETRIC\nT1C0.800\n"
                       "T2C1.000\n%\nG90\nG05\nT1\nX10.0Y-5.0\nX12.5Y-5.0\nT2\nX20Y-10\n"
                       "T1\nG00X1Y1\nM15\nG01X3Y1\nM16\nG05\nM30\n")
    assert d.plated is True
    assert [(h.x, h.y, h.d) for h in d.holes] == [(10, -5, 0.8), (12.5, -5, 0.8), (20, -10, 1.0)]
    assert len(d.slots) == 1 and (d.slots[0].x1, d.slots[0].x2) == (1, 3)
    assert d.sizes() == {0.8: 3, 1.0: 1}


def test_excellon_inch_implied_and_g85():
    d = parse_excellon("M48\nINCH,LZ\nT01C0.0315\n%\nT01\nX01Y02\nX005Y-01G85X015Y-01\nM30\n")
    h = d.holes[0]
    assert h.x == pytest.approx(25.4) and h.y == pytest.approx(50.8)
    assert h.d == pytest.approx(0.8001, abs=1e-3)
    s = d.slots[0]
    assert (s.x1, s.x2) == (pytest.approx(12.7), pytest.approx(38.1))


# ------------------------------------------------------------------ board
def _write_board(folder):
    os.makedirs(folder, exist_ok=True)
    cu = (HEAD + "%ADD10C,1.000000*%\nD10*\n" + f"X{mm(5)}Y{mm(5)}D03*\n"
          f"X{mm(35)}Y{mm(15)}D03*\nM02*\n")
    edge = (HEAD + "%ADD10C,0.050000*%\nD10*\n"
            f"X{mm(0)}Y{mm(0)}D02*\nX{mm(40)}Y{mm(0)}D01*\nX{mm(40)}Y{mm(20)}D01*\n"
            f"X{mm(0)}Y{mm(20)}D01*\nX{mm(0)}Y{mm(0)}D01*\nM02*\n")
    drl = "M48\nMETRIC\nT1C0.800\n%\nT1\nX5.0Y5.0\nX35.0Y15.0\nM30\n"
    files = {"b-B_Cu.gbl": cu, "b-Edge_Cuts.gm1": edge, "b-PTH.drl": drl,
             "b-F_Silkscreen.gto": cu}
    for n, t in files.items():
        with open(os.path.join(folder, n), "w") as f:
            f.write(t)
    return files


def test_board_roles_folder_and_zip(tmp_path):
    files = _write_board(tmp_path / "plot")
    b = Board.load([str(tmp_path / "plot")])
    roles = {l.name: l.role for l in b.layers}
    assert roles == {"b-B_Cu.gbl": "bottom", "b-Edge_Cuts.gm1": "outline", "b-PTH.drl": "drill",
                     "b-F_Silkscreen.gto": "other"}
    silk = [l for l in b.layers if l.role == "other"][0]
    assert silk.gerber is None          # parsed only when shown
    z = tmp_path / "board.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for n, t in files.items():
            zf.writestr(n, t)
    assert len(Board.load([str(z)]).layers) == 4
    assert b.size(0) == pytest.approx((40, 20))


def test_placement_rotation_and_mirror(tmp_path):
    _write_board(tmp_path / "plot")
    b = Board.load([str(tmp_path / "plot")])
    assert b.suggest_rotation((30, 50)) == 90
    pb = b.placed(Placement())
    assert pb.outline.bounds == pytest.approx((0, 0, 40, 20))
    assert (pb.holes[0].x, pb.holes[0].y) == pytest.approx((5, 5))
    pr = b.placed(Placement(rotation=90))        # CCW: (x, y) -> (H - y, x)
    assert pr.size == pytest.approx((20, 40))
    assert (pr.holes[0].x, pr.holes[0].y) == pytest.approx((15, 5))
    pm = b.placed(Placement(mirror=True, flip_axis="x"))   # bottom up: x -> W - x
    assert (pm.holes[0].x, pm.holes[0].y) == pytest.approx((35, 5))
    assert pm.copper["bottom"].centroid.x == pytest.approx(20)
    assert pm.fits((140, 200))


def test_role_from_name_easyeda():
    assert role_from_name("Gerber_TopLayer.GTL") == "top"
    assert role_from_name("Gerber_BottomLayer.GBL") == "bottom"
    assert role_from_name("Gerber_BoardOutlineLayer.GKO") == "outline"
    assert role_from_name("Drill_PTH_Through.DRL") == "drill"
    assert role_from_name("Gerber_TopSilkscreenLayer.GTO") == "other"


SAMPLE = os.path.join(os.path.dirname(__file__), "..", "samples", "ECB_OLD_V1.0", "production",
                      "ECB_OLD_V1.0.zip")


@pytest.mark.skipif(not os.path.exists(SAMPLE), reason="private sample board not present")
def test_user_sample_board():
    b = Board.load([SAMPLE])
    assert not b.problems
    assert b.size(0) == pytest.approx((190, 126), abs=0.01)
    assert b.suggest_rotation((140, 200)) == 90
    pb = b.placed(Placement(rotation=90, mirror=True))
    assert pb.fits((140, 200)) and len(pb.holes) == 480
