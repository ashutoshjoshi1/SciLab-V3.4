from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np


@dataclass
class CharacterizationConfig:
    laser_sequence: Sequence[str] = ("377", "405", "445", "488", "532", "640", "685", "Hg_Ar")
    laser_reference_map: Dict[str, float] = field(
        default_factory=lambda: {
            "377": 375.0,
            "405": 403.46,
            "445": 445.0,
            "488": 488.0,
            "517": 517.0,
            "532": 532.0,
            "640": 640.0,
            "685": 685.0,
        }
    )
    known_lines_nm: Sequence[float] = (
        289.36,
        296.73,
        302.15,
        313.16,
        334.19,
        365.01,
        404.66,
        407.78,
        435.84,
        507.30,
        546.08,
    )
    ib_region_size: int = 20
    sat_threshold: float = 65400.0
    win_hg: int = 30


@dataclass(frozen=True)
class AnalysisArtifact:
    name: str
    path: str


@dataclass(frozen=True)
class LsfSample:
    label: str
    wavelength_nm: float
    pixel_index: int
    values: np.ndarray


@dataclass(frozen=True)
class CorrectedSpectrum:
    label: str
    integration_time_ms: float
    values: np.ndarray


@dataclass(frozen=True)
class HgArPeakMatch:
    signal: Optional[np.ndarray]
    detected_peaks: np.ndarray
    matched_pixels: np.ndarray
    matched_wavelengths_nm: np.ndarray
    rmse_nm: Optional[float]


@dataclass(frozen=True)
class OffsetCurve:
    label: str
    wavelength_nm: float
    x_values_nm: np.ndarray
    y_values: np.ndarray
    fwhm_nm: float
    width_20_percent_nm: float


@dataclass(frozen=True)
class ReferenceOverlayData:
    reference_name: str
    measured_curves: List[OffsetCurve]
    reference_curves: List[OffsetCurve]


@dataclass(frozen=True)
class ParameterSample:
    wavelength_nm: float
    value: float


@dataclass(frozen=True)
class SlitParameterSample:
    wavelength_nm: float
    peak_pixel: int
    a2: float
    a3: float
    c1: float


@dataclass
class CharacterizationMetrics:
    hg_ar_match_rmse_nm: Optional[float]
    matched_hg_line_count: int
    dispersion_coefficients: List[float]
    laser_peak_pixels: Dict[str, int]
    slit_parameter_samples: List[SlitParameterSample]
    spectral_resolution_wavelengths_nm: np.ndarray
    spectral_resolution_fwhm_nm: np.ndarray


@dataclass
class CharacterizationComputation:
    serial_number: str
    pixel_count: int
    summary_lines: List[str]
    metrics: CharacterizationMetrics
    laser_lsfs: List[LsfSample]
    corrected_640_spectra: List[CorrectedSpectrum]
    hg_ar_peak_match: HgArPeakMatch
    hg_ar_lamp_lsfs: List[LsfSample]
    sdf_matrix: np.ndarray
    sdf_reference_pixels: np.ndarray
    dispersion_fit_pixels: np.ndarray
    dispersion_fit_wavelengths_nm: np.ndarray
    dispersion_curve_pixels: np.ndarray
    dispersion_curve_wavelengths_nm: np.ndarray
    a2_samples: List[ParameterSample]
    a3_samples: List[ParameterSample]
    a2_poly_coefficients: np.ndarray
    a3_poly_coefficients: np.ndarray
    measured_laser_curves: List[OffsetCurve]
    reference_overlays: List[ReferenceOverlayData]
    hg_ar_lamp_curves: List[OffsetCurve]
    slit_examples: List[OffsetCurve]


@dataclass
class CharacterizationResult:
    metrics: CharacterizationMetrics
    artifacts: List[AnalysisArtifact]
    summary_lines: List[str]

    @property
    def summary_text(self) -> str:
        return "\n".join(self.summary_lines)

