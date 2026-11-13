"""Jog move generation: a single-axis move with accel/decel ramp.

The full G-code planner comes in milestone 1.3; this is enough for jogging.
"""
from __future__ import annotations

from .protocol import HEADER_JOG, TICKS_PER_PACKET, ZERO_TICK, Tick, encode_packet

# Same ramp the original software uses for jogging (steps per tick).
DEFAULT_RAMP = (3, 6, 9, 10, 11, 12, 13, 14)


def ramp_profile(total_steps: int, max_steps: int = 14,
                 ramp: tuple[int, ...] = DEFAULT_RAMP) -> list[int]:
    """Steps per tick (all positive) that sum exactly to total_steps.

    Accelerates up the ramp, cruises at max_steps, decelerates down the ramp.
    """
    if total_steps < 0:
        raise ValueError("total_steps must be >= 0")
    levels = [r for r in ramp if r < max_steps] + [max_steps]

    def brake_dist(level_idx: int) -> int:
        return sum(levels[:level_idx])

    out: list[int] = []
    remaining = total_steps
    idx = -1  # current speed level index (-1 = stopped)
    while remaining > 0:
        chosen = None
        for cand in range(min(idx + 1, len(levels) - 1), -1, -1):
            v = levels[cand]
            if v <= remaining and remaining - v >= brake_dist(cand):
                chosen = cand
                break
        if chosen is None:
            v = min(remaining, levels[0])
            out.append(v)
            remaining -= v
            idx = 0
            continue
        idx = chosen
        out.append(levels[idx])
        remaining -= levels[idx]
    return out


def jog_ticks(axis: str, steps: int, max_steps: int = 14, spindle: bool = False) -> list[Tick]:
    axis = axis.lower()
    if axis not in "xyz" or len(axis) != 1:
        raise ValueError("axis must be x, y or z")
    sign = -1 if steps < 0 else 1
    ticks = []
    for n in ramp_profile(abs(steps), max_steps):
        kw = {axis: sign * n}
        ticks.append(Tick(spindle=spindle, **kw))
    return ticks


def pack(ticks: list[Tick], header: int = HEADER_JOG, spindle: bool = False) -> list[bytes]:
    """Group ticks into 3-tick packets, padding the last one with zero ticks."""
    pad = Tick(spindle=spindle) if spindle else ZERO_TICK
    packets = []
    for i in range(0, len(ticks), TICKS_PER_PACKET):
        chunk = list(ticks[i:i + TICKS_PER_PACKET])
        chunk += [pad] * (TICKS_PER_PACKET - len(chunk))
        packets.append(encode_packet(chunk, header))
    return packets


def mm_to_steps(mm: float, mm_per_step: float) -> int:
    return round(mm / mm_per_step)
