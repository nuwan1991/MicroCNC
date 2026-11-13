import itertools
import re
from pathlib import Path

import pytest

from micropcb.protocol import (HANDSHAKE, Reply, ReplyCode, ReplyParser, Tick,
                               decode_packet, decode_tick, encode_packet,
                               encode_settings, encode_tick)

CAPTURES = Path(__file__).resolve().parent.parent / "Request view"


def test_handshake_bytes():
    assert HANDSHAKE == bytes.fromhex("3f 00 00 00 00 00 00")


def test_settings_bytes():
    assert encode_settings(4) == bytes.fromhex("7f 04")


@pytest.mark.parametrize("x,y,z,s", [
    (v[0], v[1], v[2], s)
    for v in itertools.product(range(-15, 16), repeat=3)
    for s in (False, True)
][::37])
def test_tick_roundtrip(x, y, z, s):
    t = Tick(x, y, z, s)
    assert decode_tick(*encode_tick(t)) == t


def test_tick_known_values_from_capture():
    # Z jog ramp seen in capture 1: 3f 60 00 ... = z+3, 3f c0 00 = z+6, 3f 20 20 = z+9
    assert decode_tick(0x60, 0x00) == Tick(z=3)
    assert decode_tick(0xC0, 0x00) == Tick(z=6)
    assert decode_tick(0x20, 0x20) == Tick(z=9)
    assert decode_tick(0xC0, 0x60) == Tick(z=-14)


def test_tick_limit():
    with pytest.raises(ValueError):
        encode_tick(Tick(x=16))


def test_parser_simple_and_split_probe():
    p = ReplyParser()
    assert p.feed(b"\x01") == [Reply(1)]
    assert p.feed(b"\x04") == []
    assert p.feed(b"\xf1") == []
    assert p.feed(b"\xac\x01") == [Reply(4, 0xF1AC - 0x10000), Reply(1)]
    assert p.feed(b"\x03") == [Reply(ReplyCode.RELEASED)]


def _capture_writes(path: Path) -> list[bytes]:
    text = path.read_text(encoding="utf-16")
    out = []
    for rec in re.split(r"\n\s*(?=\d{6}: )", text):
        if re.match(r"\s*\d{6}: Write ", rec):
            hx = " ".join(re.findall(r"^\s+((?:[0-9a-f]{2} )+)", rec, re.M)).split()
            out.append(bytes.fromhex("".join(hx)))
    return out


@pytest.mark.parametrize("name", ["1.txt", "2.txt"])
def test_every_captured_packet_reencodes_identically(name):
    path = CAPTURES / name
    if not path.exists():
        pytest.skip("capture not available")
    writes = _capture_writes(path)
    motion = [w for w in writes if len(w) == 7]
    assert len(motion) > 100
    for w in motion:
        header, ticks = decode_packet(w)
        assert encode_packet(ticks, header) == w
    others = {w for w in writes if len(w) != 7}
    assert others == {encode_settings(4)}
