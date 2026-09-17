"""Tests for the shared driver helpers in spectrometers/spec_xfus.py."""
from __future__ import annotations

import numpy as np
import pytest

from spectrometers.spec_xfus import calc_msl, measurement_timeout_s


def _sums(frames: np.ndarray, shift: np.ndarray | None = None):
    """Accumulate frames the way the drivers do (optionally around a shift)."""
    data = frames - shift if shift is not None else frames
    x = np.arange(frames.shape[0], dtype=float)
    sy = data.sum(axis=0)
    syy = (data * data).sum(axis=0)
    sxy = (x[:, None] * data).sum(axis=0)
    return x, sxy, sy, syy


def test_calc_msl_mean_and_std_match_numpy():
    rng = np.random.default_rng(0)
    frames = 62000.0 + rng.normal(0.0, 12.0, size=(50, 256))
    x, sxy, sy, syy = _sums(frames)
    res, m, s, line_rms = calc_msl("t", x, sxy, sy, syy)
    assert res == "OK"
    assert np.allclose(m, frames.mean(axis=0))
    assert np.allclose(s, frames.std(axis=0, ddof=1), rtol=1e-3)
    assert len(line_rms) == 256 and np.all(np.isfinite(line_rms))


def test_calc_msl_shifted_accumulation_restores_mean():
    """Accumulating around the first frame avoids cancellation; the mean is shifted back."""
    rng = np.random.default_rng(1)
    frames = 62000.0 + rng.normal(0.0, 3.0, size=(50, 64))
    shift = frames[0].copy()
    x, sxy, sy, syy = _sums(frames, shift)
    res, m, s, _l = calc_msl("t", x, sxy, sy, syy, shift=shift)
    assert res == "OK"
    assert np.allclose(m, frames.mean(axis=0))
    assert np.allclose(s, frames.std(axis=0, ddof=1), rtol=1e-3)


def test_calc_msl_constant_frames_do_not_report_negative_variance():
    frames = np.full((50, 32), 65000.0)
    x, sxy, sy, syy = _sums(frames)
    res, m, s, line_rms = calc_msl("t", x, sxy, sy, syy)
    assert res == "OK"
    assert np.all(s == 0.0)
    assert np.all(np.asarray(line_rms) == 0.0)


def test_calc_msl_accepts_ndarray_nav_argument():
    frames = 100.0 + np.zeros((5, 8))
    x, sxy, sy, syy = _sums(frames)
    # nav given as an ndarray used to raise "truth value of an array is ambiguous".
    res, m, _s, _l = calc_msl("t", x, sxy, sy, syy, nav=np.array([]), syw=np.array([]))
    assert res == "OK" and np.allclose(m, 100.0)


@pytest.mark.parametrize(
    "ncy,it_ms,expected",
    [(1, 0.2, 2 * 1 * 0.0002 + 10.0), (50, 3000.0, 2 * 50 * 3.0 + 10.0), (None, None, 10.0)],
)
def test_measurement_timeout_scales_with_expected_duration(ncy, it_ms, expected):
    assert measurement_timeout_s(ncy, it_ms) == pytest.approx(expected)
    assert measurement_timeout_s(ncy, it_ms) >= 10.0
