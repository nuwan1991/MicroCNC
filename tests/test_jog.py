import pytest

from micropcb.jog import DEFAULT_RAMP, jog_ticks, mm_to_steps, pack, ramp_profile
from micropcb.protocol import decode_packet


@pytest.mark.parametrize("total", list(range(0, 200)) + [2500, 16887, 49999])
@pytest.mark.parametrize("vmax", [1, 5, 14])
def test_profile_exact_and_bounded(total, vmax):
    prof = ramp_profile(total, vmax)
    assert sum(prof) == total
    assert all(1 <= v <= vmax for v in prof)


def test_profile_ramps_like_original():
    prof = ramp_profile(2500)
    assert prof[:8] == list(DEFAULT_RAMP)
    assert prof[-7:] == sorted(prof[-7:], reverse=True)
    assert max(prof) == 14


def test_jog_packets_negative_and_padding():
    pkts = pack(jog_ticks("y", -100))
    total = 0
    for p in pkts:
        h, ticks = decode_packet(p)
        assert h == 0x3F
        total += sum(t.y for t in ticks)
        assert all(t.x == 0 and t.z == 0 for t in ticks)
    assert total == -100


def test_mm_to_steps():
    assert mm_to_steps(10, 0.004) == 2500
    assert mm_to_steps(-0.002, 0.004) == 0  # rounds half to even
