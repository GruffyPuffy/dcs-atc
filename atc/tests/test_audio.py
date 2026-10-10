"""Audio level helpers: output volume (--tx-volume) and input gain (--gain)."""

import numpy as np

from atc_bot import scale_pcm


def _pcm(*samples: int) -> bytes:
    return np.array(samples, dtype=np.int16).tobytes()


def _samples(pcm: bytes) -> list[int]:
    return np.frombuffer(pcm, dtype=np.int16).tolist()


def test_scale_pcm_unity_is_unchanged():
    pcm = _pcm(1000, -2000, 30000)
    assert scale_pcm(pcm, 1.0) is pcm  # no copy when the factor is 1.0


def test_scale_pcm_halves_the_level():
    assert _samples(scale_pcm(_pcm(1000, -2000, 30000), 0.5)) == [500, -1000, 15000]


def test_scale_pcm_clips_to_int16():
    # A factor that would overflow must clip, not wrap around.
    assert _samples(scale_pcm(_pcm(30000, -30000), 2.0)) == [32767, -32768]


def test_scale_pcm_boosts_quiet_audio():
    assert _samples(scale_pcm(_pcm(100, -100), 3.0)) == [300, -300]
