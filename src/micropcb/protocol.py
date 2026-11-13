"""Wegstr serial protocol: packet encoding and reply parsing.

Pure functions only (no I/O). See docs/protocol.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

BAUDRATE = 9600

HEADER_MOTION = 0xFF  # job execution
HEADER_JOG = 0x3F  # jog / handshake / probing (stops on surface touch)
HEADER_SETTINGS = 0x7F  # keep-alive + performance setting

TICKS_PER_PACKET = 3
MAX_STEPS_PER_TICK = 15  # hard limit of the encoding (4 bits)
TICK_SECONDS = 0.0196  # measured firmware time per tick at full load


@dataclass(frozen=True)
class Tick:
    """Steps to execute in one firmware tick (signed, |value| <= 15)."""

    x: int = 0
    y: int = 0
    z: int = 0
    spindle: bool = False

    def is_zero(self) -> bool:
        return self.x == 0 and self.y == 0 and self.z == 0


ZERO_TICK = Tick()


def encode_tick(t: Tick) -> bytes:
    for name, v in (("x", t.x), ("y", t.y), ("z", t.z)):
        if abs(v) > MAX_STEPS_PER_TICK:
            raise ValueError(f"{name}={v} exceeds {MAX_STEPS_PER_TICK} steps/tick")
    ax, ay, az = abs(t.x), abs(t.y), abs(t.z)
    lo = ax | (0x10 if t.x < 0 else 0) | ((az & 0x07) << 5)
    hi = ay | (0x10 if t.y < 0 else 0)
    if az >= 8:
        hi |= 0x20
    if t.z < 0:
        hi |= 0x40
    if t.spindle:
        hi |= 0x80
    return bytes((lo, hi))


def decode_tick(lo: int, hi: int) -> Tick:
    x = lo & 0x0F
    if lo & 0x10:
        x = -x
    y = hi & 0x0F
    if hi & 0x10:
        y = -y
    z = ((lo >> 5) & 0x07) | (0x08 if hi & 0x20 else 0)
    if hi & 0x40:
        z = -z
    return Tick(x, y, z, bool(hi & 0x80))


def encode_packet(ticks: list[Tick] | tuple[Tick, ...], header: int = HEADER_MOTION) -> bytes:
    if len(ticks) != TICKS_PER_PACKET:
        raise ValueError("a packet needs exactly 3 ticks")
    if header not in (HEADER_MOTION, HEADER_JOG):
        raise ValueError(f"bad motion header 0x{header:02X}")
    return bytes((header,)) + b"".join(encode_tick(t) for t in ticks)


def decode_packet(data: bytes) -> tuple[int, list[Tick]]:
    if len(data) != 7:
        raise ValueError("motion packet must be 7 bytes")
    return data[0], [decode_tick(data[i], data[i + 1]) for i in (1, 3, 5)]


def encode_settings(performance: int = 4) -> bytes:
    if not 0 <= performance <= 255:
        raise ValueError("performance must fit in a byte")
    return bytes((HEADER_SETTINGS, performance))


HANDSHAKE = encode_packet([ZERO_TICK] * 3, HEADER_JOG)


# ---------------------------------------------------------------- replies

class ReplyCode(IntEnum):
    READY = 0x01
    OVERLOAD = 0x02
    RELEASED = 0x03
    PROBE = 0x04


@dataclass(frozen=True)
class Reply:
    code: int
    value: int | None = None  # signed 16-bit for PROBE

    @property
    def known(self) -> bool:
        return self.code in ReplyCode._value2member_map_


class ReplyParser:
    """Turns a raw byte stream into Reply objects.

    PROBE replies (04 hi lo) may arrive split over several reads, so bytes
    are buffered until a reply is complete.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[Reply]:
        self._buf.extend(data)
        out: list[Reply] = []
        while self._buf:
            code = self._buf[0]
            if code == ReplyCode.PROBE:
                if len(self._buf) < 3:
                    break
                raw = (self._buf[1] << 8) | self._buf[2]
                value = raw - 0x10000 if raw & 0x8000 else raw
                out.append(Reply(code, value))
                del self._buf[:3]
            else:
                out.append(Reply(code))
                del self._buf[:1]
        return out

    def pending(self) -> int:
        return len(self._buf)
