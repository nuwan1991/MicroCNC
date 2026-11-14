"""Engraving resolution test: finds the smallest gap / trace / SMD pitch your
bit + depth can make.  python examples/make_resolution_test.py [--tip 0.1 --angle 30 --depth 0.08]

Writes examples/resolution-test.nc.  Work zero = front-left corner of the test
area, Z 0 = copper (use Probe + set Z 0, auto-level recommended).  Size ~42 x 25 mm.

Rows (front to back):
  A  Isolation gaps: pairs of cuts whose COPPER gap between them shrinks
     1.0 .. 0.10 mm.  Smallest pair that still shows a copper line = your limit.
  B  Traces: single copper traces 0.6 .. 0.15 mm wide, isolated on both sides.
  C  SMD pads: rows of 6 pads at IC pitches 1.27 (SOIC), 1.0, 0.8, 0.65 (TSSOP),
     0.5 mm (QFP/TQFP), pad width = pitch / 2, each pad isolated by its outline.
Every group starts with a short "tick" line pointing at it; tick count = group
number, so the groups can be told apart without text.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path


def cut_width(tip: float, angle: float, depth: float) -> float:
    return tip + 2 * depth * math.tan(math.radians(angle / 2))


class G:
    def __init__(self, depth, feed, plunge, safe):
        self.depth, self.feed, self.plunge, self.safe = depth, feed, plunge, safe
        self.out = []

    def c(self, text):
        # G-code comments cannot contain brackets
        self.out.append("(" + text.replace("(", "[").replace(")", "]") + ")")

    def path(self, pts, closed=False):
        if closed:
            pts = list(pts) + [pts[0]]
        x, y = pts[0]
        self.out += [f"G0 Z{self.safe:g}", f"G0 X{x:.3f} Y{y:.3f}",
                     f"G1 Z{-self.depth:.3f} F{self.plunge:g}"]
        f = f" F{self.feed:g}"
        for x, y in pts[1:]:
            self.out.append(f"G1 X{x:.3f} Y{y:.3f}{f}")
            f = ""
        self.out.append(f"G0 Z{self.safe:g}")

    def line(self, x0, y0, x1, y1):
        self.path([(x0, y0), (x1, y1)])

    def rect(self, x0, y0, x1, y1):
        self.path([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], closed=True)

    def ticks(self, x, y, n):
        """n short ticks left of a group (group number)."""
        for k in range(n):
            self.line(x - 1.6, y + k * 0.6, x - 0.9, y + k * 0.6)


def build(tip=0.1, angle=30.0, depth=0.08, feed=100.0, plunge=30.0, safe=1.0) -> str:
    w = cut_width(tip, angle, depth)
    g = G(depth, feed, plunge, safe)
    g.c("MicroPCB engraving resolution test")
    g.c(f"tool: V-bit {angle:g} deg, tip {tip:g} mm, depth {depth:g} mm -> cut width {w:.3f} mm")
    g.c("work zero: front-left corner, Z0 on the copper; size about 42 x 25 mm")
    g.out += ["G21 G90 G17", f"G0 Z{safe:g}", "M3", "G4 P1"]

    # ---- row A: isolation gaps
    gaps = [1.0, 0.6, 0.4, 0.3, 0.25, 0.2, 0.15, 0.1]
    x = 3.0
    g.c("ROW A - copper gap between two cuts (mm): " + ", ".join(f"{v:g}" for v in gaps))
    for n, gap in enumerate(gaps, 1):
        pitch = gap + w                       # centre distance of the two cuts
        g.c(f"A{n}: gap {gap:g} mm at X {x:.2f}")
        g.ticks(x, 1.0, n)
        g.line(x, 1.0, x, 8.0)
        g.line(x + pitch, 8.0, x + pitch, 1.0)
        x += pitch + 2.6

    # ---- row B: traces
    traces = [0.6, 0.4, 0.3, 0.25, 0.2, 0.15]
    x = 3.0
    g.c("ROW B - trace width (mm): " + ", ".join(f"{v:g}" for v in traces))
    for n, tw in enumerate(traces, 1):
        g.c(f"B{n}: trace {tw:g} mm at X {x:.2f}")
        g.ticks(x, 11.0, n)
        half = tw / 2 + w / 2                 # cut centre lines on both sides
        g.rect(x - half, 11.0, x + half, 18.0)
        x += tw + w + 4.0

    # ---- row C: SMD pads (isolate each pad: outline at pad edge + w/2)
    pitches = [1.27, 1.0, 0.8, 0.65, 0.5]
    y = 21.5
    g.c("ROW C - SMD pad pitch (mm): " + ", ".join(f"{v:g}" for v in pitches) +
        " ; pad width = pitch/2, length 1.6 mm")
    x0 = 3.0
    for n, p in enumerate(pitches, 1):
        pw = p / 2
        g.c(f"C{n}: pitch {p:g} mm, pad {pw:g} mm, gap {p - pw:g} mm at X {x0:.2f}")
        g.ticks(x0, y, n)
        for k in range(6):
            cx = x0 + k * p
            o = w / 2
            g.rect(cx - pw / 2 - o, y - o, cx + pw / 2 + o, y + 1.6 + o)
        x0 += 6 * p + 3.0
        if x0 > 38:
            x0, y = 3.0, y + 5.0
    g.out += [f"G0 Z{safe + 4:g}", "M5", "G0 X0 Y0", "M2"]
    return "\n".join(g.out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tip", type=float, default=0.1)
    ap.add_argument("--angle", type=float, default=30.0)
    ap.add_argument("--depth", type=float, default=0.08)
    ap.add_argument("--feed", type=float, default=100.0)
    ap.add_argument("-o", default=str(Path(__file__).with_name("resolution-test.nc")))
    a = ap.parse_args()
    Path(a.o).write_text(build(a.tip, a.angle, a.depth, a.feed), encoding="utf-8")
    print(f"written {a.o}  (cut width {cut_width(a.tip, a.angle, a.depth):.3f} mm)")


if __name__ == "__main__":
    main()
