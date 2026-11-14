"""Analyse a bridge capture CSV: timing report + reconstructed motion G-code.

    python -m micropcb.bridge.analyze capture.csv [--mm-per-step 0.004]

Writes <capture>-report.txt and <capture>-motion.nc and prints the report.
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np

MM_PER_STEP = 0.004
NOMINAL_TICK = 0.0196


@dataclass
class Row:
    t: float
    dir: str
    hex: str
    kind: str
    ticks: list[tuple[int, int, int]]
    spindle: str
    pos: tuple[int, int, int]
    reply_ms: float | None
    note: str


def load(path: str | Path) -> tuple[list[str], list[Row]]:
    meta, rows = [], []
    with open(path, encoding="utf-8", newline="") as f:
        lines = [ln for ln in f if not (ln.startswith("#") and meta.append(ln[1:].strip()) is None)]
    for d in csv.DictReader(lines):
        ticks = []
        if d["ticks"] and not d["ticks"].startswith("perf"):
            ticks = [tuple(int(v) for v in t.split(",")) for t in d["ticks"].split()]
        rows.append(Row(float(d["t_s"]), d["dir"], d["hex"], d["kind"], ticks, d["spindle"],
                        (int(d["x"] or 0), int(d["y"] or 0), int(d["z"] or 0)),
                        float(d["reply_ms"]) if d["reply_ms"] else None, d["note"]))
    return meta, rows


def _stats(v) -> str:
    v = np.asarray(v, float)
    if not len(v):
        return "-"
    return (f"n={len(v)}  min {v.min():.2f}  median {np.median(v):.2f}  mean {v.mean():.2f}  "
            f"p95 {np.percentile(v, 95):.2f}  max {v.max():.2f} ms")


def report(meta: list[str], rows: list[Row], mmps: float = MM_PER_STEP) -> str:
    out = ["MicroPCB bridge capture report", "=" * 30]
    out += [f"  {m}" for m in meta]
    if not rows:
        return "\n".join(out + ["(empty capture)"])
    tx = [r for r in rows if r.dir == "TX"]
    rx = [r for r in rows if r.dir == "RX"]
    dur = rows[-1].t - rows[0].t
    out.append(f"Duration {dur:.3f} s   rows {len(rows)}   TX {len(tx)}   RX {len(rx)}")
    out.append("")
    out.append("Packets sent (PC -> machine): " +
               ", ".join(f"{k} {v}" for k, v in Counter(r.kind for r in tx).most_common()))
    out.append("Replies (machine -> PC):      " +
               ", ".join(f"{k} {v}" for k, v in Counter(r.kind for r in rx).most_common()))

    # pairing: each TX packet is answered by the next RX
    out.append("")
    out.append("Reply time after each packet (machine handshake):")
    by_kind: dict[str, list[float]] = {}
    last_tx_kind = None
    unanswered = 0
    for r in rows:
        if r.dir == "TX":
            if last_tx_kind is not None:
                unanswered += 1
            last_tx_kind = r.kind
        elif r.dir == "RX" and r.reply_ms is not None and last_tx_kind is not None:
            by_kind.setdefault(last_tx_kind, []).append(r.reply_ms)
            last_tx_kind = None
    for k, v in sorted(by_kind.items()):
        out.append(f"  {k:10s} {_stats(v)}")
    if unanswered:
        out.append(f"  {unanswered} packets were followed by another packet before any reply "
                   "(pipelined / not answered)")

    # motion
    motion = [r for r in tx if r.kind in ("job", "jog")]
    if motion:
        ticks = [t for r in motion for t in r.ticks]
        arr = np.array(ticks)
        moving = np.any(arr != 0, axis=1)
        out.append("")
        out.append(f"Motion: {len(motion)} packets, {len(ticks)} ticks "
                   f"({moving.sum()} moving, {(~moving).sum()} empty)")
        mx = np.abs(arr).max(axis=0)
        out.append(f"  max steps per tick  X {mx[0]}  Y {mx[1]}  Z {mx[2]}")
        travel = np.abs(arr).sum(axis=0) * mmps
        out.append(f"  travel  X {travel[0]:.3f}  Y {travel[1]:.3f}  Z {travel[2]:.3f} mm")
        lo = np.array([r.pos for r in motion]).min(axis=0) * mmps
        hi = np.array([r.pos for r in motion]).max(axis=0) * mmps
        out.append(f"  range   X {lo[0]:.3f}…{hi[0]:.3f}  Y {lo[1]:.3f}…{hi[1]:.3f}  "
                   f"Z {lo[2]:.3f}…{hi[2]:.3f} mm (from capture start)")
        fp = np.array(motion[-1].pos) * mmps
        out.append(f"  end position  X {fp[0]:.3f}  Y {fp[1]:.3f}  Z {fp[2]:.3f} mm")
        # real time per tick while streaming: packet spacing / 3
        for kind in ("job", "jog"):
            ts = np.array([r.t for r in motion if r.kind == kind])
            if len(ts) > 2:
                gaps = np.diff(ts) * 1000
                stream = gaps[gaps < 200]
                if len(stream):
                    out.append(f"  {kind} packet spacing while streaming: {_stats(stream)}"
                               f"  -> {np.median(stream) / 3:.2f} ms per tick")
        # speeds
        v = np.hypot(arr[:, 0], arr[:, 1]) * mmps / NOMINAL_TICK * 60
        out.append(f"  XY speed at nominal {NOMINAL_TICK * 1000:.1f} ms/tick: max {v.max():.0f} "
                   f"mm/min")
    # spindle
    sp = [(r.t, r.spindle[-1] == "1") for r in tx if r.spindle]
    changes = [(t, on) for k, (t, on) in enumerate(sp) if k == 0 or on != sp[k - 1][1]]
    if changes:
        out.append("")
        out.append("Spindle: " + ", ".join(f"{'ON' if on else 'off'} at {t:.3f} s"
                                           for t, on in changes[:20]))
    # keep-alive
    ka = [r for r in tx if r.kind == "keepalive"]
    if len(ka) > 1:
        out.append("")
        out.append(f"Keep-alive 7F: {len(ka)} sent, interval {_stats(np.diff([r.t for r in ka]) * 1000)}")
        perf = Counter(r.hex.split()[1] for r in ka if len(r.hex.split()) > 1)
        out.append("  performance bytes: " + ", ".join(f"0x{k} ×{v}" for k, v in perf.items()))
    # probes
    pr = [r for r in rx if r.kind == "probe"]
    if pr:
        out.append("")
        out.append("Probe touches (machine Z counter, Δ to first):")
        first = None
        for r in pr[:50]:
            val = int(r.note.split("counter")[-1].split()[0]) if "counter" in r.note else None
            first = val if first is None else first
            z = r.pos[2] * mmps
            d = f"{(val - first) * mmps:+.3f} mm" if val is not None else ""
            out.append(f"  {r.t:10.3f} s  counter {val}  {d}  (sent Z {z:.3f} mm) {r.note}")
    # anomalies
    out.append("")
    out.append("Things to look at:")
    found = False
    for r in rows:
        if r.kind in ("overload", "released", "raw") or r.dir == "EV":
            out.append(f"  {r.t:10.3f} s  {r.dir} {r.kind} {r.hex} {r.note}")
            found = True
    gaps = [(rows[k].t - rows[k - 1].t, rows[k - 1].t) for k in range(1, len(rows))]
    long = sorted([g for g in gaps if g[0] > 0.5], reverse=True)[:10]
    for g, t in long:
        out.append(f"  {t:10.3f} s  no traffic for {g * 1000:.0f} ms")
        found = True
    slow = [r for r in rx if r.reply_ms is not None and r.reply_ms > 100]
    for r in slow[:10]:
        out.append(f"  {r.t:10.3f} s  slow reply {r.reply_ms:.1f} ms ({r.kind})")
        found = True
    if not found:
        out.append("  nothing unusual")
    return "\n".join(out) + "\n"


def motion_gcode(rows: list[Row], mmps: float = MM_PER_STEP) -> str:
    """Straight segments from the tick stream (consecutive ticks in one direction).

    F is from the real packet timing (3 ticks per packet). Positions are from
    the capture start (0,0,0). Comments mark jog vs job and spindle changes.
    """
    out = ["(motion reconstructed from a MicroPCB bridge capture)",
           "(positions relative to the machine position at capture start)", "G21 G90"]
    pos = np.zeros(3)
    seg_dir = None
    seg_end = None
    seg_time = 0.0
    seg_kind = None
    spindle = False
    motion = [r for r in rows if r.dir == "TX" and r.kind in ("job", "jog")]

    def flush():
        nonlocal seg_dir, seg_time
        if seg_dir is None:
            return
        dist = math.dist(seg_start, seg_end)
        f = dist / seg_time * 60 if seg_time > 0 else 0
        out.append(f"G1 X{seg_end[0]:.3f} Y{seg_end[1]:.3f} Z{seg_end[2]:.3f} F{f:.0f}")
        seg_dir = None
        seg_time = 0.0

    seg_start = pos.copy()
    for k, r in enumerate(motion):
        dt = (motion[k + 1].t - r.t) if k + 1 < len(motion) else NOMINAL_TICK * 3
        dt_tick = min(dt, 0.2) / 3
        if r.kind != seg_kind:
            flush()
            out.append(f"( {r.kind} at {r.t:.3f} s )")
            seg_kind = r.kind
        for i, t in enumerate(r.ticks):
            on = r.spindle[i] == "1" if r.spindle else spindle
            if on != spindle:
                flush()
                out.append("M3" if on else "M5")
                spindle = on
            v = np.array(t, float)
            if not v.any():
                continue
            d = tuple(np.sign(v) * (np.abs(v) / np.abs(v).max()).round(2))
            if d != seg_dir:
                flush()
                seg_start = pos.copy()
                seg_dir = d
            pos = pos + v * mmps
            seg_end = pos.copy()
            seg_time += dt_tick
    flush()
    out.append("M2")
    return "\n".join(out) + "\n"


def analyze_file(path: str | Path, mmps: float = MM_PER_STEP) -> tuple[Path, Path, str]:
    path = Path(path)
    meta, rows = load(path)
    rep = report(meta, rows, mmps)
    rp = path.with_name(path.stem + "-report.txt")
    mp = path.with_name(path.stem + "-motion.nc")
    rp.write_text(rep, encoding="utf-8")
    mp.write_text(motion_gcode(rows, mmps), encoding="utf-8")
    return rp, mp, rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("capture")
    ap.add_argument("--mm-per-step", type=float, default=MM_PER_STEP)
    a = ap.parse_args(argv)
    rp, mp, rep = analyze_file(a.capture, a.mm_per_step)
    sys.stdout.reconfigure(encoding="utf-8")
    print(rep)
    print(f"written: {rp}\n         {mp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
