"""The analysis must give the same answers for a 4096-pixel detector as for its 2048-pixel twin.

A detector with twice the pixels over the same wavelength range has twice the
pixels per nanometre. Every window the analysis cuts in pixels (the slit-fit
window, the SDF in-band region, the lamp-line window, the peak spacing) was
tuned at 2048 pixels; scaled with the pixel count, the slit function, the
resolution and the SDF normalisation come out the same in nanometres.
"""
from __future__ import annotations

import numpy as np
import pytest

from analysis.calculations import compute_characterization
from analysis.models import CharacterizationConfig
from tests.helpers import make_measurement_df

# Three lasers on a 2048-pixel detector at ~0.154 nm/px, with lines whose FWHM
# (about 2.2 nm) is wide enough that a window of +/-25 pixels on a 4096-pixel
# head would not reach the baseline on either side.
LINES_2048 = {"405": (222.0, 6.0, 60000.0), "445": (489.0, 6.2, 60000.0), "532": (1048.0, 6.4, 60000.0)}


def _twin_4096(lines):
    """The same instrument read out with 4096 pixels: centres and widths in pixels double."""
    return {tag: (2.0 * center, 2.0 * sigma, peak) for tag, (center, sigma, peak) in lines.items()}


@pytest.fixture(scope="module")
def pair():
    df_2048 = make_measurement_df(LINES_2048, npix=2048, noise=0.0)
    df_4096 = make_measurement_df(_twin_4096(LINES_2048), npix=4096, noise=0.0)
    return compute_characterization(df_2048, "A"), compute_characterization(df_4096, "B")


def test_pixel_scale_and_windows():
    config = CharacterizationConfig()
    assert config.pixel_scale(2048) == 1.0 and config.pixel_scale(4096) == 2.0
    assert config.scaled_px(25, 2048) == 25 and config.scaled_px(25, 4096) == 50
    assert config.scaled_px(20, 1024) == 10 and config.scaled_px(1, 256) == 1      # never below one pixel
    assert CharacterizationConfig(reference_pixel_count=0).scaled_px(25, 4096) == 25  # scaling switched off


def test_slit_parameters_agree_between_2048_and_4096(pair):
    comp_2048, comp_4096 = pair
    assert comp_2048.pixel_count == 2048 and comp_4096.pixel_count == 4096
    samples_2048 = {round(s.wavelength_nm): s for s in comp_2048.metrics.slit_parameter_samples}
    samples_4096 = {round(s.wavelength_nm): s for s in comp_4096.metrics.slit_parameter_samples}
    assert samples_2048.keys() == samples_4096.keys() and len(samples_2048) == 3
    for wavelength, sample in samples_2048.items():
        twin = samples_4096[wavelength]
        assert twin.peak_pixel == 2 * sample.peak_pixel
        # Width and shape are properties of the optics, in nanometres: the same on both detectors.
        assert twin.a2 == pytest.approx(sample.a2, rel=0.03), (wavelength, sample.a2, twin.a2)
        assert twin.a3 == pytest.approx(sample.a3, rel=0.05), (wavelength, sample.a3, twin.a3)
    # The linear dispersion term halves with the pixel pitch.
    disp_2048 = np.poly1d(comp_2048.metrics.dispersion_coefficients).deriv()(600)
    disp_4096 = np.poly1d(comp_4096.metrics.dispersion_coefficients).deriv()(1200)
    assert disp_4096 == pytest.approx(disp_2048 / 2.0, rel=1e-3)


def test_resolution_and_measured_widths_agree(pair):
    comp_2048, comp_4096 = pair
    fwhm_2048 = np.asarray(comp_2048.metrics.spectral_resolution_fwhm_nm)
    fwhm_4096 = np.asarray(comp_4096.metrics.spectral_resolution_fwhm_nm)
    assert fwhm_2048.shape == fwhm_4096.shape
    assert np.allclose(fwhm_2048, fwhm_4096, rtol=0.03), (fwhm_2048[:3], fwhm_4096[:3])
    for curve_2048, curve_4096 in zip(comp_2048.measured_laser_curves, comp_4096.measured_laser_curves):
        assert curve_4096.fwhm_nm == pytest.approx(curve_2048.fwhm_nm, rel=0.03)
    for example_2048, example_4096 in zip(comp_2048.slit_examples, comp_4096.slit_examples):
        assert example_4096.fwhm_nm == pytest.approx(example_2048.fwhm_nm, rel=0.03)


def test_sdf_in_band_region_scales_with_pixels():
    # A little noise keeps the stray-light floor above zero, so the zeroed
    # in-band region stands out: +/-10 px at 2048 pixels, +/-20 px at 4096.
    comp_2048 = compute_characterization(make_measurement_df(LINES_2048, npix=2048, noise=5.0, seed=3), "A")
    comp_4096 = compute_characterization(make_measurement_df(_twin_4096(LINES_2048), npix=4096, noise=5.0, seed=3), "B")
    for comp, half in ((comp_2048, 10), (comp_4096, 20)):
        for sample in comp.laser_lsfs:
            column = comp.sdf_matrix[:, sample.pixel_index]
            peak = sample.pixel_index
            assert np.all(column[peak - half:peak + half + 1] == 0.0)
            assert column[peak - half - 1] != 0.0 and column[peak + half + 1] != 0.0


def test_window_unscaled_is_too_narrow_on_4096():
    """What the fix changes: at a fixed +/-25 px window the 4096-pixel fit does not match its twin."""
    config = CharacterizationConfig(reference_pixel_count=0)
    comp_2048 = compute_characterization(make_measurement_df(LINES_2048, npix=2048, noise=0.0), "A", config=config)
    comp_4096 = compute_characterization(make_measurement_df(_twin_4096(LINES_2048), npix=4096, noise=0.0), "B",
                                         config=config)
    a2_2048 = [s.a2 for s in comp_2048.metrics.slit_parameter_samples]
    a2_4096 = [s.a2 for s in comp_4096.metrics.slit_parameter_samples]
    assert len(a2_2048) == len(a2_4096) == 3
    assert max(abs(a - b) / a for a, b in zip(a2_2048, a2_4096)) > 0.03, (a2_2048, a2_4096)
