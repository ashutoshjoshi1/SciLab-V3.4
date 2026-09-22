"""Synthetic-data helpers shared by the analysis tests."""
from __future__ import annotations

from typing import Dict, Tuple

import numpy as np
import pandas as pd

PIXEL_PREFIX = "Pixel_"


def gaussian(x: np.ndarray, center: float, sigma: float, amplitude: float = 1.0) -> np.ndarray:
    return amplitude * np.exp(-0.5 * ((np.asarray(x, dtype=float) - center) / sigma) ** 2)


def gaussian_fwhm(sigma: float) -> float:
    return 2.0 * np.sqrt(2.0 * np.log(2.0)) * sigma


def make_measurement_df(
    lines: Dict[str, Tuple[float, float, float]],
    *,
    npix: int = 2048,
    dark_level: float = 600.0,
    it_ms: float = 10.0,
    ncy: int = 50,
    noise: float = 0.0,
    seed: int = 0,
) -> pd.DataFrame:
    """Build a DataFrame shaped like ``MeasurementData.to_dataframe()``.

    ``lines`` maps a wavelength tag ("377", "405", ...) to
    ``(center_px, sigma_px, peak_counts)``. A signal row and a matching
    ``<tag>_dark`` row are produced for each entry.
    """
    rng = np.random.default_rng(seed)
    x = np.arange(npix)
    columns = ["Timestamp", "Wavelength", "IntegrationTime", "NumCycles"] + [f"{PIXEL_PREFIX}{i}" for i in range(npix)]
    rows = []
    for tag, (center, sigma, peak) in lines.items():
        signal = dark_level + gaussian(x, center, sigma, peak) + rng.normal(0.0, noise, npix)
        dark = dark_level + rng.normal(0.0, noise, npix)
        rows.append(["2026-01-01 00:00:00", str(tag), float(it_ms), int(ncy), *signal])
        rows.append(["2026-01-01 00:00:00", f"{tag}_dark", float(it_ms), int(ncy), *dark])
    return pd.DataFrame(rows, columns=columns)
