"""Regression test: compute_characterization vs. the pinned numeric snapshot.

The snapshot is produced by tests/golden/make_snapshot.py. Numeric fields are
compared with tolerances; summary text is reported but not asserted.
"""
from __future__ import annotations

import math
import time

import numpy as np
import pandas as pd
import pytest

from analysis.calculations import compute_characterization
from tests.golden.make_snapshot import summarize

REL_TOL = 1e-6
ABS_TOL = 1e-9
# Fields whose exact values are allowed to drift within tolerance.
NUMERIC_LISTS = (
    "dispersion_coefficients",
    "spectral_resolution_wavelengths_nm",
    "spectral_resolution_fwhm_nm",
    "a2_poly",
    "a3_poly",
    "dispersion_fit_pixels",
    "dispersion_fit_wavelengths_nm",
)


def _close(a, b) -> bool:
    if a is None or b is None:
        return a is b
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return math.isclose(float(a), float(b), rel_tol=REL_TOL, abs_tol=ABS_TOL)


def _assert_list_close(name, got, want):
    assert len(got) == len(want), f"{name}: length {len(got)} != {len(want)}"
    for i, (g, w) in enumerate(zip(got, want)):
        assert _close(g, w), f"{name}[{i}]: {g} != {w}"


@pytest.fixture(params=sorted(p.name for p in (__import__("tests.conftest", fromlist=["FIXTURES"]).FIXTURES).glob("*.csv")))
def fixture_name(request):
    return request.param


def test_matches_golden_snapshot(fixture_name, fixtures_dir, golden_snapshot):
    want = golden_snapshot[fixture_name]
    df = pd.read_csv(fixtures_dir / fixture_name)

    t0 = time.perf_counter()
    comp = compute_characterization(df=df, sn=fixture_name, config=None, reference_csv_paths=None)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    got = summarize(comp)

    assert got["pixel_count"] == want["pixel_count"]
    assert got["laser_peak_pixels"] == want["laser_peak_pixels"]
    assert got["matched_hg_line_count"] == want["matched_hg_line_count"]
    assert _close(got["hg_ar_match_rmse_nm"], want["hg_ar_match_rmse_nm"])

    for key in NUMERIC_LISTS:
        _assert_list_close(key, got[key], want[key])

    assert len(got["slit_parameter_samples"]) == len(want["slit_parameter_samples"])
    for g, w in zip(got["slit_parameter_samples"], want["slit_parameter_samples"]):
        for k in ("wl", "peak_pixel", "a2", "a3", "c1"):
            assert _close(g[k], w[k]), f"slit {g['wl']} {k}: {g[k]} != {w[k]}"

    assert len(got["measured_laser_curves"]) == len(want["measured_laser_curves"])
    for g, w in zip(got["measured_laser_curves"], want["measured_laser_curves"]):
        assert g["label"] == w["label"]
        for k in ("wl", "fwhm_nm", "x_min", "x_max"):
            assert _close(g[k], w[k]), f"curve {g['label']} {k}: {g[k]} != {w[k]}"
        assert g["n"] == w["n"]

    assert len(got["laser_lsfs"]) == len(want["laser_lsfs"])
    for g, w in zip(got["laser_lsfs"], want["laser_lsfs"]):
        assert g["label"] == w["label"] and g["pixel_index"] == w["pixel_index"] and g["n"] == w["n"]
        assert _close(g["sum"], w["sum"]) and _close(g["max"], w["max"])

    # SDF matrix: shape and fill pattern must be identical; values within tolerance.
    assert got["sdf"]["shape"] == want["sdf"]["shape"]
    assert got["sdf"]["nonzero"] == want["sdf"]["nonzero"]
    assert _close(got["sdf"]["sum"], want["sdf"]["sum"])

    # Informational only: summary text and timing.
    if got["summary_lines"] != want["summary_lines"]:
        print(f"\n[{fixture_name}] summary_lines differ:\n  got : {got['summary_lines']}\n  want: {want['summary_lines']}")
    print(f"\n[{fixture_name}] compute_characterization: {elapsed_ms:.0f} ms")
    assert np.isfinite(elapsed_ms)
