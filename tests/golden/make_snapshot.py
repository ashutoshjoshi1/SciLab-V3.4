"""Generate a numeric golden snapshot of compute_characterization outputs.

Usage:
    python tests/golden/make_snapshot.py [output.json]

The snapshot pins behaviour of the analysis layer so refactors can be verified.
Regenerate ONLY when a change is intended to alter the numbers, and say so in
the commit message.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from analysis.calculations import compute_characterization  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
DEFAULT_OUT = ROOT / "tests" / "golden" / "snapshot.json"


def _f(value):
    if value is None:
        return None
    return float(value)


def summarize(comp) -> dict:
    m = comp.metrics
    sdf = np.ascontiguousarray(comp.sdf_matrix)
    return {
        "pixel_count": int(comp.pixel_count),
        "laser_peak_pixels": {k: int(v) for k, v in m.laser_peak_pixels.items()},
        "dispersion_coefficients": [_f(c) for c in m.dispersion_coefficients],
        "hg_ar_match_rmse_nm": _f(m.hg_ar_match_rmse_nm),
        "matched_hg_line_count": int(m.matched_hg_line_count),
        "slit_parameter_samples": [
            {"wl": _f(s.wavelength_nm), "peak_pixel": int(s.peak_pixel),
             "a2": _f(s.a2), "a3": _f(s.a3), "c1": _f(s.c1)}
            for s in m.slit_parameter_samples
        ],
        "spectral_resolution_wavelengths_nm": np.asarray(m.spectral_resolution_wavelengths_nm, float).tolist(),
        "spectral_resolution_fwhm_nm": np.asarray(m.spectral_resolution_fwhm_nm, float).tolist(),
        "laser_lsfs": [
            {"label": s.label, "wl": _f(s.wavelength_nm), "pixel_index": int(s.pixel_index),
             "sum": _f(np.nansum(s.values)), "max": _f(np.nanmax(s.values)), "n": int(s.values.size)}
            for s in comp.laser_lsfs
        ],
        "measured_laser_curves": [
            {"label": c.label, "wl": _f(c.wavelength_nm), "fwhm_nm": _f(c.fwhm_nm),
             "w20_nm": _f(c.width_20_percent_nm), "n": int(c.x_values_nm.size),
             "x_min": _f(np.nanmin(c.x_values_nm)) if c.x_values_nm.size else None,
             "x_max": _f(np.nanmax(c.x_values_nm)) if c.x_values_nm.size else None}
            for c in comp.measured_laser_curves
        ],
        "slit_examples": [
            {"label": c.label, "wl": _f(c.wavelength_nm), "fwhm_nm": _f(c.fwhm_nm), "n": int(c.x_values_nm.size)}
            for c in comp.slit_examples
        ],
        "a2_poly": np.asarray(comp.a2_poly_coefficients, float).tolist(),
        "a3_poly": np.asarray(comp.a3_poly_coefficients, float).tolist(),
        "dispersion_fit_pixels": np.asarray(comp.dispersion_fit_pixels, float).tolist(),
        "dispersion_fit_wavelengths_nm": np.asarray(comp.dispersion_fit_wavelengths_nm, float).tolist(),
        "sdf": {
            "shape": list(sdf.shape),
            "nonzero": int(np.count_nonzero(sdf)),
            "sum": _f(sdf.sum()),
            "sha1": hashlib.sha1(sdf.tobytes()).hexdigest(),
        },
        "corrected_640_count": len(comp.corrected_640_spectra),
        "hg_ar_lamp_lsfs_count": len(comp.hg_ar_lamp_lsfs),
        "summary_lines": list(comp.summary_lines),
    }


def main(out_path: Path) -> None:
    snapshot = {}
    for csv_path in sorted(FIXTURES.glob("*.csv")):
        df = pd.read_csv(csv_path)
        t0 = time.perf_counter()
        comp = compute_characterization(df=df, sn=csv_path.stem, config=None, reference_csv_paths=None)
        elapsed = time.perf_counter() - t0
        snapshot[csv_path.name] = summarize(comp)
        print(f"{csv_path.name}: {elapsed*1000:.0f} ms, peaks={snapshot[csv_path.name]['laser_peak_pixels']}")
    out_path.write_text(json.dumps(snapshot, indent=1, sort_keys=True), encoding="utf-8")
    print(f"wrote {out_path} ({out_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT)
