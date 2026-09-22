"""Unit tests for the pure functions in analysis.calculations (synthetic Gaussians)."""
from __future__ import annotations

import numpy as np
import pytest

from analysis import calculations as calc
from analysis.models import CharacterizationConfig
from tests.helpers import gaussian, gaussian_fwhm, make_measurement_df


# --------------------------------------------------------------------------- slit_func

def test_slit_func_peak_and_symmetry():
    x = np.linspace(-5, 5, 1001)
    y = calc.slit_func(x, a2=1.3, a3=2.4, c1=0.02)
    assert y[500] == pytest.approx(1.02)
    assert np.allclose(y, y[::-1])


def test_slit_func_gaussian_when_a3_is_two():
    x = np.linspace(-4, 4, 801)
    assert np.allclose(calc.slit_func(x, 1.5, 2.0, 0.0), np.exp(-((x / 1.5) ** 2)))


def test_slit_func_tolerates_zero_width():
    y = calc.slit_func(np.array([0.0, 1.0]), 0.0, 2.0, 0.0)
    assert np.all(np.isfinite(y))


# --------------------------------------------------------------------------- FWHM

def test_compute_fwhm_of_gaussian():
    sigma = 3.0
    x = np.arange(0, 200, 1.0)
    y = gaussian(x, 100.0, sigma)
    assert calc.compute_fwhm(x, y) == pytest.approx(gaussian_fwhm(sigma), rel=0.01)


def test_compute_fwhm_degenerate_inputs():
    x = np.arange(10.0)
    assert calc.compute_fwhm(x, np.zeros(10)) == 0.0
    assert calc.compute_fwhm(np.array([]), np.array([])) == 0.0
    assert calc.compute_fwhm(x, np.eye(10)[3]) == 0.0  # a single sample above half-max


def test_width_at_half_equals_fwhm_and_20_percent_width():
    sigma = 2.5
    x = np.linspace(0, 100, 2001)
    y = gaussian(x, 50.0, sigma)
    assert calc.compute_width_at_percent_max(x, y, 0.5) == pytest.approx(calc.compute_fwhm(x, y))
    expected_20 = 2.0 * sigma * np.sqrt(2.0 * np.log(5.0))
    assert calc.compute_width_at_percent_max(x, y, 0.2) == pytest.approx(expected_20, rel=0.01)


def test_compute_fwhm_ignores_second_line_in_window():
    """Two Hg lines 20 px apart share the fit window; FWHM must describe the brightest line only."""
    sigma = 2.0
    x = np.arange(0, 60, 1.0)
    y = gaussian(x, 20.0, sigma, 1.0) + gaussian(x, 40.0, sigma, 0.9)
    assert calc.compute_fwhm(x, y) == pytest.approx(gaussian_fwhm(sigma), rel=0.05)


# --------------------------------------------------------------------------- misc pure helpers

def test_generate_adaptive_x_spans_three_widths():
    x = calc.generate_adaptive_x(2.0, spacing=0.5)
    assert x[0] == pytest.approx(-6.0) and x[-1] == pytest.approx(6.0)
    assert len(x) % 2 == 1 and x[len(x) // 2] == pytest.approx(0.0)


def test_normalize_lsf_stray_light_zeroes_in_band_and_scales_by_its_sum():
    lsf = np.ones(50)
    out = calc.normalize_lsf_stray_light(lsf, pixel_number=25, ib_size=10)
    assert np.all(out[20:31] == 0.0)          # in-band window zeroed (11 samples)
    assert out[0] == pytest.approx(1.0 / 11.0)  # outside divided by the in-band sum
    assert np.all(calc.normalize_lsf_stray_light(np.zeros(50), 25, 10) == 0.0)


def test_best_ordered_linear_match_recovers_dispersion():
    a, b = 0.1555, 369.0
    lines = np.array([404.66, 407.78, 435.84, 507.30, 546.08, 576.96, 579.07])
    peaks = np.round((lines - b) / a).astype(int)
    result = calc.best_ordered_linear_match(peaks, lines, min_points=5)
    assert result is not None
    rmse, a_fit, b_fit, pix_sel, wl_sel = result
    assert a_fit == pytest.approx(a, rel=1e-3)
    assert b_fit == pytest.approx(b, abs=0.2)
    assert rmse < 0.1 and len(pix_sel) == len(lines)


def test_best_ordered_linear_match_handles_too_few_peaks():
    lines = np.array([404.66, 407.78, 435.84, 507.30, 546.08])
    assert calc.best_ordered_linear_match(np.array([10, 20]), lines, min_points=5) is None
    assert calc.best_ordered_linear_match(np.array([], dtype=int), lines, min_points=5) is None


def test_safe_polyfit_clamps_degree():
    coeffs = calc._safe_polyfit(np.array([0.0, 1.0]), np.array([1.0, 3.0]), deg=3)
    assert len(coeffs) == 2 and coeffs[0] == pytest.approx(2.0)
    assert calc._safe_polyfit(np.array([]), np.array([]), deg=2).tolist() == [0.0]


# --------------------------------------------------------------------------- DataFrame helpers

def test_get_normalized_lsf_peak_one_min_zero_and_rejections():
    df = make_measurement_df({"405": (900.0, 3.0, 40000.0)})
    lsf = calc.get_normalized_lsf(df, "405", sat_thresh=65400.0)
    assert lsf is not None and lsf.max() == pytest.approx(1.0) and lsf.min() == pytest.approx(0.0)
    assert int(np.argmax(lsf)) == 900

    saturated = make_measurement_df({"405": (900.0, 3.0, 70000.0)})
    assert calc.get_normalized_lsf(saturated, "405", sat_thresh=65400.0) is None
    assert calc.get_normalized_lsf(df, "445", sat_thresh=65400.0) is None  # no such rows


def test_get_corrected_signal_floors_at_small_positive():
    df = make_measurement_df({"Hg_Ar": (500.0, 4.0, 30000.0)})
    corrected = calc.get_corrected_signal(df, "Hg_Ar")
    assert corrected is not None and corrected.min() >= 1e-5 and int(np.argmax(corrected)) == 500
    assert calc.get_corrected_signal(df, "405") is None


# --------------------------------------------------------------------------- end-to-end synthetic

def test_compute_characterization_recovers_synthetic_dispersion():
    dispersion, lambda0 = 0.1555, 369.0
    config = CharacterizationConfig()
    lines = {}
    for tag in ("377", "405", "445", "488"):
        wavelength = config.laser_reference_map[tag]
        center = round((wavelength - lambda0) / dispersion)
        lines[tag] = (float(center), 2.6, 40000.0)
    df = make_measurement_df(lines, noise=5.0)

    comp = calc.compute_characterization(df=df, sn="SYNTH", config=config)

    for tag, (center, _sigma, _peak) in lines.items():
        assert abs(comp.metrics.laser_peak_pixels[tag] - center) <= 1
    coeffs = comp.metrics.dispersion_coefficients
    assert coeffs[-2] == pytest.approx(dispersion, rel=0.01)   # linear term
    assert coeffs[-1] == pytest.approx(lambda0, abs=1.0)       # intercept
    # Each measured curve's FWHM in nm should match sigma_px * 2.355 * dispersion within 10 %.
    expected_fwhm_nm = gaussian_fwhm(2.6) * dispersion
    for curve in comp.measured_laser_curves:
        assert curve.fwhm_nm == pytest.approx(expected_fwhm_nm, rel=0.10)
