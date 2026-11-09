"""Key-results card data: what the Analysis run tab shows above the plots."""
from __future__ import annotations

import json

from tabs.analysis_results import key_results_headline, key_results_rows, load_metrics

METRICS = {
    "schema": "scilab.characterization.metrics/1",
    "serial_number": "SIM-0001",
    "pixel_count": 2048,
    "dispersion_coefficients_high_to_low": [1.2e-9, -3.1e-7, 0.15558, 369.0],
    "laser_peak_pixels": {"405": 222, "445": 489},
    "hg_ar": {"matched_line_count": 11, "match_rmse_nm": 0.0834, "matched_pixels": [], "matched_wavelengths_nm": []},
    "slit_parameters": [
        {"wavelength_nm": 403.5, "peak_pixel": 222, "a2": 0.5562, "a3": 2.1718, "c1": 1.0},
        {"wavelength_nm": 445.0, "peak_pixel": 489, "a2": 0.5538, "a3": 2.1927, "c1": 1.0},
    ],
    "measured_lines": [
        {"label": "403 nm", "wavelength_nm": 403.5, "fwhm_nm": 0.9418, "width_20_percent_nm": 1.3778},
        {"label": "445 nm", "wavelength_nm": 445.0, "fwhm_nm": 0.9396, "width_20_percent_nm": float("nan")},
    ],
}


def test_rows_join_lines_peaks_and_slit_parameters():
    rows = key_results_rows(METRICS)
    assert rows[0] == ("403 nm", "222", "0.9418", "1.3778", "0.5562", "2.1718")
    # A peak pixel the laser table does not have comes from the slit sample; NaN prints as a dash.
    assert rows[1] == ("445 nm", "489", "0.9396", "—", "0.5538", "2.1927")


def test_headline_summarises_lamp_dispersion_and_lasers():
    items = dict(key_results_headline(METRICS))
    assert items["Hg-Ar"] == "11 lines matched, RMSE 0.083 nm"
    assert items["Dispersion"] == "0.15558 nm/px (linear term)"
    assert items["Lasers"] == "403 nm, 445 nm"
    assert items["Detector"] == "2048 pixels"


def test_headline_without_lamp_or_lasers():
    metrics = {"hg_ar": {"matched_line_count": 0}, "dispersion_coefficients_high_to_low": [0.1]}
    items = dict(key_results_headline(metrics))
    assert items["Hg-Ar"] == "not measured"
    assert "Dispersion" not in items and "Lasers" not in items
    assert key_results_rows(metrics) == []


def test_load_metrics_tolerates_missing_or_broken_files(tmp_path):
    assert load_metrics(None) is None
    assert load_metrics(tmp_path / "missing.json") is None
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert load_metrics(broken) is None
    good = tmp_path / "metrics.json"
    good.write_text(json.dumps(METRICS), encoding="utf-8")
    assert load_metrics(good)["pixel_count"] == 2048
    not_an_object = tmp_path / "list.json"
    not_an_object.write_text("[1, 2]", encoding="utf-8")
    assert load_metrics(not_an_object) is None
