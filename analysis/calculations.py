from __future__ import annotations

import math
import os
from typing import List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from scipy.optimize import curve_fit
from scipy.signal import find_peaks

from .models import (
    CharacterizationComputation,
    CharacterizationConfig,
    CharacterizationMetrics,
    CorrectedSpectrum,
    HgArPeakMatch,
    LsfSample,
    OffsetCurve,
    ParameterSample,
    ReferenceOverlayData,
    SlitParameterSample,
)


def _pixel_columns(df: pd.DataFrame) -> List[str]:
    return [column for column in df.columns if str(column).startswith("Pixel_")]


def get_normalized_lsf(
    df: pd.DataFrame,
    wavelength: str,
    sat_thresh: float,
    use_latest: bool = True,
) -> Optional[np.ndarray]:
    pixel_cols = _pixel_columns(df)
    if not pixel_cols:
        return None

    sig_rows = df[df["Wavelength"] == wavelength]
    dark_rows = df[df["Wavelength"] == f"{wavelength}_dark"]
    if sig_rows.empty or dark_rows.empty:
        return None

    sig_row = sig_rows.iloc[-1] if use_latest else sig_rows.iloc[0]
    dark_row = dark_rows.iloc[-1] if use_latest else dark_rows.iloc[0]

    try:
        signal = sig_row[pixel_cols].astype(float).to_numpy()
        dark = dark_row[pixel_cols].astype(float).to_numpy()
    except Exception:
        return None

    if signal.shape != dark.shape or signal.size == 0:
        return None
    if not np.all(np.isfinite(signal)) or not np.all(np.isfinite(dark)):
        return None
    if np.any(signal >= sat_thresh):
        return None

    corrected = signal - dark
    corrected -= np.nanmin(corrected)
    denom = float(np.nanmax(corrected))
    if not np.isfinite(denom) or denom <= 0:
        return None

    normalized = corrected / denom
    if not np.all(np.isfinite(normalized)):
        return None
    return normalized


def get_corrected_signal(df: pd.DataFrame, base: str) -> Optional[np.ndarray]:
    pixel_cols = _pixel_columns(df)
    if not pixel_cols:
        return None

    sig_rows = df[df["Wavelength"] == base]
    dark_rows = df[df["Wavelength"] == f"{base}_dark"]
    if sig_rows.empty or dark_rows.empty:
        return None

    signal = sig_rows.iloc[-1][pixel_cols].astype(float).to_numpy()
    dark = dark_rows.iloc[-1][pixel_cols].astype(float).to_numpy()
    corrected = np.clip(signal - dark, 1e-5, None)
    if not np.all(np.isfinite(corrected)):
        return None
    return corrected


def best_ordered_linear_match(
    peaks_pix: Sequence[int],
    candidate_wls: Sequence[float],
    min_points: int = 5,
) -> Optional[Tuple[float, float, float, np.ndarray, np.ndarray]]:
    peaks_pix = np.asarray(peaks_pix, dtype=float)
    candidate_wls = np.asarray(candidate_wls, dtype=float)
    peak_count, line_count = len(peaks_pix), len(candidate_wls)
    best: Optional[Tuple[float, float, float, np.ndarray, np.ndarray]] = None

    def score(pix_sel: np.ndarray, wl_sel: np.ndarray) -> Tuple[float, float, float]:
        design = np.vstack([pix_sel, np.ones_like(pix_sel)]).T
        a, b = np.linalg.lstsq(design, wl_sel, rcond=None)[0]
        predicted = a * pix_sel + b
        rmse = float(np.sqrt(np.mean((wl_sel - predicted) ** 2)))
        return rmse, float(a), float(b)

    if peak_count >= line_count:
        for start in range(peak_count - line_count + 1):
            pix_sel = peaks_pix[start : start + line_count]
            wl_sel = candidate_wls.copy()
            rmse, a, b = score(pix_sel, wl_sel)
            if best is None or rmse < best[0]:
                best = (rmse, a, b, pix_sel.copy(), wl_sel.copy())
    else:
        for start in range(line_count - peak_count + 1):
            pix_sel = peaks_pix.copy()
            wl_sel = candidate_wls[start : start + peak_count]
            rmse, a, b = score(pix_sel, wl_sel)
            if best is None or rmse < best[0]:
                best = (rmse, a, b, pix_sel.copy(), wl_sel.copy())

    if best and len(best[3]) >= min_points:
        return best
    return None


def normalize_lsf_stray_light(lsf: np.ndarray, pixel_number: int, ib_size: int) -> np.ndarray:
    ib_start = max(0, pixel_number - ib_size // 2)
    ib_end = min(len(lsf), pixel_number + ib_size // 2 + 1)
    ib_region = np.arange(ib_start, ib_end)
    ib_sum = float(np.sum(lsf[ib_region]))
    normalized = lsf.copy()
    normalized[ib_region] = 0.0
    if not np.isfinite(ib_sum) or ib_sum <= 0:
        return np.zeros_like(lsf)
    return normalized / ib_sum


def slit_func(x: np.ndarray, a2: float, a3: float, c1: float) -> np.ndarray:
    with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
        safe_a2 = a2 if abs(a2) > 1e-12 else 1e-12
        base = np.abs(x / safe_a2) ** a3
        return np.exp(-np.clip(base, 0.0, 700.0)) + c1


def compute_fwhm(x: np.ndarray, y: np.ndarray) -> float:
    y = np.asarray(y, dtype=float)
    x = np.asarray(x, dtype=float)
    if y.size == 0:
        return 0.0
    y = y - np.min(y)
    if np.max(y) <= 0:
        return 0.0
    y = y / np.max(y)
    half = 0.5
    above = np.where(y >= half)[0]
    if len(above) < 2:
        return 0.0
    left, right = above[0], above[-1]

    def interp(idx1: int, idx2: int) -> float:
        if idx1 < 0 or idx2 >= len(x):
            return float(x[min(max(idx1, 0), len(x) - 1)])
        x1, x2 = x[idx1], x[idx2]
        y1, y2 = y[idx1], y[idx2]
        if y2 == y1:
            return float(x1)
        return float(x1 + (x2 - x1) * (half - y1) / (y2 - y1))

    x_left = interp(left - 1, left)
    x_right = interp(right, right + 1)
    return abs(x_right - x_left)


def compute_width_at_percent_max(x: np.ndarray, y: np.ndarray, percent: float = 0.2) -> float:
    y = np.asarray(y, dtype=float)
    x = np.asarray(x, dtype=float)
    if y.size == 0:
        return 0.0
    y = y - np.min(y)
    if np.max(y) <= 0:
        return 0.0
    y = y / np.max(y)
    threshold = percent
    above = np.where(y >= threshold)[0]
    if len(above) < 2:
        return 0.0
    left, right = above[0], above[-1]

    def interp(idx1: int, idx2: int) -> float:
        if idx1 < 0 or idx2 >= len(x):
            return float(x[min(max(idx1, 0), len(x) - 1)])
        x1, x2 = x[idx1], x[idx2]
        y1, y2 = y[idx1], y[idx2]
        if y2 == y1:
            return float(x1)
        return float(x1 + (x2 - x1) * (threshold - y1) / (y2 - y1))

    x_left = interp(left - 1, left)
    x_right = interp(right, right + 1)
    return abs(x_right - x_left)


def generate_adaptive_x(a2: float, spacing: float = 0.01) -> np.ndarray:
    half_width = 3 * a2
    num_points = int(2 * half_width / spacing) + 1
    return np.linspace(-half_width, half_width, max(num_points, 3))


def _safe_polyfit(x: np.ndarray, y: np.ndarray, deg: int) -> np.ndarray:
    if len(x) < deg + 1:
        deg = len(x) - 1
    if deg < 0:
        return np.array([0.0])
    return np.polyfit(x, y, deg=deg)


def _build_offset_curve(
    wavelength_nm: float,
    values: np.ndarray,
    peak_pixel: int,
    dispersion_derivative,
    label: Optional[str] = None,
) -> OffsetCurve:
    if values.size == 0:
        return OffsetCurve(
            label=label or f"{wavelength_nm:.0f} nm",
            wavelength_nm=float(wavelength_nm),
            x_values_nm=np.array([]),
            y_values=np.array([]),
            fwhm_nm=0.0,
            width_20_percent_nm=0.0,
        )
    disp_nm_per_pixel = float(dispersion_derivative(peak_pixel)) if dispersion_derivative.order >= 0 else 0.0
    center = int(np.nanargmax(values))
    x_values = (np.arange(len(values)) - center) * (disp_nm_per_pixel if disp_nm_per_pixel else 1.0)
    normalized = (values - np.min(values)) / max(1e-12, np.max(values) - np.min(values))
    fwhm = compute_fwhm(x_values, normalized)
    width_20 = compute_width_at_percent_max(x_values, normalized, percent=0.2)
    return OffsetCurve(
        label=label or f"{wavelength_nm:.0f} nm",
        wavelength_nm=float(wavelength_nm),
        x_values_nm=x_values,
        y_values=normalized,
        fwhm_nm=fwhm,
        width_20_percent_nm=width_20,
    )


def compute_characterization(
    df: pd.DataFrame,
    sn: str,
    config: Optional[CharacterizationConfig] = None,
    reference_csv_paths: Optional[List[str]] = None,
) -> CharacterizationComputation:
    config = config or CharacterizationConfig()

    summary_lines: List[str] = []
    df = df.copy()
    df["Wavelength"] = df["Wavelength"].astype(str)
    pixel_cols = _pixel_columns(df)
    if not pixel_cols:
        metrics = CharacterizationMetrics(
            hg_ar_match_rmse_nm=None,
            matched_hg_line_count=0,
            dispersion_coefficients=[],
            laser_peak_pixels={},
            slit_parameter_samples=[],
            spectral_resolution_wavelengths_nm=np.array([]),
            spectral_resolution_fwhm_nm=np.array([]),
        )
        return CharacterizationComputation(
            serial_number=sn,
            pixel_count=0,
            summary_lines=["No pixel columns detected in dataframe."],
            metrics=metrics,
            laser_lsfs=[],
            corrected_640_spectra=[],
            hg_ar_peak_match=HgArPeakMatch(None, np.array([]), np.array([]), np.array([]), None),
            hg_ar_lamp_lsfs=[],
            sdf_matrix=np.zeros((0, 0)),
            sdf_reference_pixels=np.array([]),
            dispersion_fit_pixels=np.array([]),
            dispersion_fit_wavelengths_nm=np.array([]),
            dispersion_curve_pixels=np.array([]),
            dispersion_curve_wavelengths_nm=np.array([]),
            a2_samples=[],
            a3_samples=[],
            a2_poly_coefficients=np.array([0.0]),
            a3_poly_coefficients=np.array([0.0]),
            measured_laser_curves=[],
            reference_overlays=[],
            hg_ar_lamp_curves=[],
            slit_examples=[],
        )

    npix = len(pixel_cols)

    laser_lsfs: List[LsfSample] = []
    pixel_locations: List[int] = []
    laser_wavelengths: List[float] = []

    for tag in config.laser_sequence:
        lsf = get_normalized_lsf(df, tag, config.sat_threshold)
        if lsf is None:
            summary_lines.append(f"Missing/invalid LSF for {tag} nm; skipped.")
            continue
        wavelength_nm = float(config.laser_reference_map.get(tag, float(tag)))
        peak_pixel = int(np.nanargmax(lsf))
        laser_lsfs.append(LsfSample(label=tag, wavelength_nm=wavelength_nm, pixel_index=peak_pixel, values=lsf))
        pixel_locations.append(peak_pixel)
        laser_wavelengths.append(wavelength_nm)

    if not laser_lsfs:
        metrics = CharacterizationMetrics(
            hg_ar_match_rmse_nm=None,
            matched_hg_line_count=0,
            dispersion_coefficients=[],
            laser_peak_pixels={},
            slit_parameter_samples=[],
            spectral_resolution_wavelengths_nm=np.array([]),
            spectral_resolution_fwhm_nm=np.array([]),
        )
        return CharacterizationComputation(
            serial_number=sn,
            pixel_count=npix,
            summary_lines=["No valid LSFs were computed; skipping plots."],
            metrics=metrics,
            laser_lsfs=[],
            corrected_640_spectra=[],
            hg_ar_peak_match=HgArPeakMatch(None, np.array([]), np.array([]), np.array([]), None),
            hg_ar_lamp_lsfs=[],
            sdf_matrix=np.zeros((npix, npix)),
            sdf_reference_pixels=np.array([]),
            dispersion_fit_pixels=np.array([]),
            dispersion_fit_wavelengths_nm=np.array([]),
            dispersion_curve_pixels=np.array([]),
            dispersion_curve_wavelengths_nm=np.array([]),
            a2_samples=[],
            a3_samples=[],
            a2_poly_coefficients=np.array([0.0]),
            a3_poly_coefficients=np.array([0.0]),
            measured_laser_curves=[],
            reference_overlays=[],
            hg_ar_lamp_curves=[],
            slit_examples=[],
        )

    pixel_locations_arr = np.array(pixel_locations, dtype=int)
    laser_wavelengths_arr = np.array(laser_wavelengths, dtype=float)

    corrected_640_spectra: List[CorrectedSpectrum] = []
    sig_entries = df[df["Wavelength"].str.startswith("640") & ~df["Wavelength"].str.contains("dark")]
    if sig_entries.empty:
        summary_lines.append("No 640 nm signal entries found.")
    else:
        for _, row in sig_entries.iterrows():
            tag = str(row["Wavelength"])
            dark_tag = f"{tag}_dark"
            dark_rows = df[df["Wavelength"] == dark_tag]
            if dark_rows.empty:
                continue
            signal = row[pixel_cols].astype(float).to_numpy()
            dark = dark_rows.iloc[0][pixel_cols].astype(float).to_numpy()
            corrected = np.clip(signal - dark, 1e-5, None)
            it_ms = float(row["IntegrationTime"]) if "IntegrationTime" in row else float(row.iloc[2])
            corrected_640_spectra.append(CorrectedSpectrum(label=tag, integration_time_ms=it_ms, values=corrected))

    signal_corr = get_corrected_signal(df, "Hg_Ar")
    peaks = np.array([], dtype=int)
    matched_pixels = np.array([], dtype=float)
    matched_wavelengths = np.array([], dtype=float)
    rmse = None
    a_lin = b_lin = math.nan

    if signal_corr is None:
        summary_lines.append("Unable to compute Hg-Ar corrected signal.")
    else:
        prominence = 0.014 * np.max(signal_corr) if np.max(signal_corr) > 0 else 0
        peaks, _ = find_peaks(signal_corr, prominence=prominence, distance=20)
        peaks = np.sort(peaks)
        candidates = [config.known_lines_nm, config.known_lines_nm[:-1]]
        solutions = [best_ordered_linear_match(peaks, candidate) for candidate in candidates]
        solutions = [solution for solution in solutions if solution is not None]
        if not solutions:
            summary_lines.append("No valid match between Hg-Ar peaks and known lines.")
        else:
            solutions.sort(key=lambda item: item[0])
            rmse, a_lin, b_lin, matched_pixels, matched_wavelengths = solutions[0]
            matched_pixels = np.array(matched_pixels, dtype=float)
            matched_wavelengths = np.array(matched_wavelengths, dtype=float)
            summary_lines.append(f"Matched {len(matched_pixels)} Hg-Ar lines (RMSE={rmse:.2f} nm)")

    hg_match = HgArPeakMatch(
        signal=signal_corr,
        detected_peaks=np.asarray(peaks, dtype=int),
        matched_pixels=np.asarray(matched_pixels, dtype=float),
        matched_wavelengths_nm=np.asarray(matched_wavelengths, dtype=float),
        rmse_nm=float(rmse) if rmse is not None else None,
    )

    hg_ar_lamp_lsfs: List[LsfSample] = []
    if signal_corr is not None and len(peaks) > 0:
        for pixel_value, wavelength_nm in zip(matched_pixels, matched_wavelengths):
            start = max(int(pixel_value - config.win_hg), 0)
            end = min(int(pixel_value + config.win_hg + 1), npix)
            segment = signal_corr[start:end]
            segment = segment - segment.min()
            denom = max(1e-12, segment.max())
            segment = segment / denom
            if np.all(np.isfinite(segment)):
                hg_ar_lamp_lsfs.append(
                    LsfSample(
                        label=f"{wavelength_nm:.1f} nm",
                        wavelength_nm=float(wavelength_nm),
                        pixel_index=int(pixel_value),
                        values=segment,
                    )
                )

    sdf_matrix = np.zeros((npix, npix))
    for sample in laser_lsfs:
        normalized = normalize_lsf_stray_light(np.asarray(sample.values, dtype=float), sample.pixel_index, config.ib_region_size)
        sdf_matrix[:, sample.pixel_index] = normalized

    for idx in range(len(pixel_locations_arr) - 1, 0, -1):
        current_pixel = int(pixel_locations_arr[idx])
        previous_pixel = int(pixel_locations_arr[idx - 1])
        for column in range(current_pixel - 1, previous_pixel, -1):
            shift_amount = current_pixel - column
            sdf_matrix[:-shift_amount, column] = sdf_matrix[shift_amount:, current_pixel]
            sdf_matrix[-shift_amount:, column] = 0
    first_pixel = int(pixel_locations_arr[0])
    for column in range(first_pixel - 1, -1, -1):
        shift_amount = first_pixel - column
        sdf_matrix[:-shift_amount, column] = sdf_matrix[shift_amount:, first_pixel]
        sdf_matrix[-shift_amount:, column] = 0
    last_lsf_pixel = int(pixel_locations_arr[-1])
    for column in range(last_lsf_pixel + 1, npix):
        shift_amount = column - last_lsf_pixel
        sdf_matrix[shift_amount:, column] = sdf_matrix[:-shift_amount, last_lsf_pixel]
        sdf_matrix[:shift_amount, column] = 0
    for idx in range(len(pixel_locations_arr) - 1, -1, -1):
        current_pixel = int(pixel_locations_arr[idx])
        stop_column = int(pixel_locations_arr[idx - 1]) + 1 if idx > 0 else 0
        last_value = sdf_matrix[-1, current_pixel]
        for column in range(current_pixel - 1, stop_column - 1, -1):
            ib_end = min(npix, column + config.ib_region_size // 2 + 1)
            for row in range(ib_end, npix):
                if sdf_matrix[row, column] == 0:
                    sdf_matrix[row, column] = last_value
    first_value = sdf_matrix[0, last_lsf_pixel]
    for column in range(last_lsf_pixel + 1, npix):
        ib_start = max(0, column - config.ib_region_size // 2)
        for row in range(0, ib_start):
            if sdf_matrix[row, column] == 0:
                sdf_matrix[row, column] = first_value

    comb_peak_pixels = (
        np.concatenate((pixel_locations_arr, np.asarray([sample.pixel_index for sample in hg_ar_lamp_lsfs], dtype=int)))
        if hg_ar_lamp_lsfs
        else pixel_locations_arr
    )
    comb_wavelengths = (
        np.concatenate((laser_wavelengths_arr, np.asarray([sample.wavelength_nm for sample in hg_ar_lamp_lsfs], dtype=float)))
        if hg_ar_lamp_lsfs
        else laser_wavelengths_arr
    )
    order = np.argsort(comb_peak_pixels)
    comb_peak_pixels_sorted = comb_peak_pixels[order]
    comb_wavelengths_sorted = comb_wavelengths[order]
    degree = 2 if len(comb_peak_pixels_sorted) >= 3 else 1
    dispersion_coeffs = _safe_polyfit(comb_peak_pixels_sorted, comb_wavelengths_sorted, deg=degree)
    terms = []
    for index, coefficient in enumerate(dispersion_coeffs):
        power = degree - index
        if power == 0:
            terms.append(f"{coefficient:.6e}")
        elif power == 1:
            terms.append(f"{coefficient:.6e}·p")
        else:
            terms.append(f"{coefficient:.6e}·p^{power}")
    summary_lines.append("Dispersion Polynomial: λ(p) = " + " + ".join(terms))
    dispersion_poly = np.poly1d(dispersion_coeffs)
    dispersion_derivative = dispersion_poly.deriv()
    dispersion_curve_pixels = np.arange(npix)
    dispersion_curve_wavelengths = np.polyval(dispersion_coeffs, dispersion_curve_pixels)

    def fit_slit_parameters(lsf: np.ndarray, peak_pixel: int, dispersion_nm_per_pixel: float):
        if lsf.size == 0:
            return None
        center = len(lsf) // 2
        x_values = (np.arange(len(lsf)) - center) * dispersion_nm_per_pixel
        y_values = lsf - np.min(lsf)
        if np.max(y_values) <= 0:
            return None
        y_values /= np.max(y_values)
        try:
            params, _ = curve_fit(slit_func, x_values, y_values, p0=(0.5, 2.0, 0.0), maxfev=2000)
            return tuple(map(float, params))
        except Exception:
            return None

    a2_pairs: List[ParameterSample] = []
    a3_pairs: List[ParameterSample] = []
    c1_pairs: List[ParameterSample] = []
    slit_parameter_samples: List[SlitParameterSample] = []

    all_lsfs: List[np.ndarray] = []
    all_peak_pixels: List[int] = []
    all_wavelengths: List[float] = []
    win = 25
    for sample in laser_lsfs:
        start = max(0, sample.pixel_index - win)
        end = min(len(sample.values), sample.pixel_index + win + 1)
        cropped = np.array(sample.values[start:end], dtype=float)
        if len(cropped) < (2 * win + 1):
            pad_left = max(0, win - sample.pixel_index)
            pad_right = max(0, win - (len(sample.values) - sample.pixel_index - 1))
            cropped = np.pad(cropped, (pad_left, pad_right), mode="constant")
        all_lsfs.append(cropped)
        all_peak_pixels.append(sample.pixel_index)
        all_wavelengths.append(sample.wavelength_nm)
    for sample in hg_ar_lamp_lsfs:
        center = len(sample.values) // 2
        start = max(0, center - win)
        end = min(len(sample.values), center + win + 1)
        cropped = np.array(sample.values[start:end], dtype=float)
        if len(cropped) < (2 * win + 1):
            pad_left = max(0, win - center)
            pad_right = max(0, win - (len(sample.values) - center - 1))
            cropped = np.pad(cropped, (pad_left, pad_right), mode="constant")
        all_lsfs.append(cropped)
        all_peak_pixels.append(sample.pixel_index)
        all_wavelengths.append(sample.wavelength_nm)

    order = np.argsort(np.asarray(all_peak_pixels, dtype=int))
    ordered_lsfs = [all_lsfs[index] for index in order]
    ordered_peak_pixels = np.asarray(all_peak_pixels, dtype=int)[order]
    ordered_wavelengths = np.asarray(all_wavelengths, dtype=float)[order]

    for lsf_values, peak_pixel, wavelength_nm in zip(ordered_lsfs, ordered_peak_pixels, ordered_wavelengths):
        dispersion_nm_per_pixel = float(dispersion_derivative(peak_pixel)) if dispersion_derivative.order >= 0 else 0.0
        params = fit_slit_parameters(
            np.asarray(lsf_values, dtype=float),
            int(peak_pixel),
            dispersion_nm_per_pixel if dispersion_nm_per_pixel else 1.0,
        )
        if params is None:
            continue
        a2, a3, c1 = params
        a2_pairs.append(ParameterSample(wavelength_nm=float(wavelength_nm), value=float(a2)))
        a3_pairs.append(ParameterSample(wavelength_nm=float(wavelength_nm), value=float(a3)))
        c1_pairs.append(ParameterSample(wavelength_nm=float(wavelength_nm), value=float(c1)))
        slit_parameter_samples.append(
            SlitParameterSample(
                wavelength_nm=float(wavelength_nm),
                peak_pixel=int(peak_pixel),
                a2=float(a2),
                a3=float(a3),
                c1=float(c1),
            )
        )

    a2_poly = np.array([0.0])
    a3_poly = np.array([0.0])
    if len(a2_pairs) >= 2:
        a2_poly = _safe_polyfit(
            np.asarray([sample.wavelength_nm for sample in a2_pairs]) / 1000.0,
            np.asarray([sample.value for sample in a2_pairs]),
            deg=min(2, len(a2_pairs) - 1),
        )
    if len(a3_pairs) >= 2:
        a3_poly = _safe_polyfit(
            np.asarray([sample.wavelength_nm for sample in a3_pairs]) / 1000.0,
            np.asarray([sample.value for sample in a3_pairs]),
            deg=min(2, len(a3_pairs) - 1),
        )

    resolution_wavelengths = np.linspace(min(ordered_wavelengths, default=300), max(ordered_wavelengths, default=800), 200)
    a2_values = np.polyval(a2_poly, resolution_wavelengths / 1000.0)
    a3_values = np.polyval(a3_poly, resolution_wavelengths / 1000.0)
    safe_a3 = np.where(np.isfinite(a3_values) & (np.abs(a3_values) > 1e-6), a3_values, 1.0)
    resolution_fwhm = 2 * np.abs(a2_values) * (np.log(2)) ** (1.0 / safe_a3)

    measured_laser_curves = [
        _build_offset_curve(sample.wavelength_nm, np.asarray(sample.values, dtype=float), sample.pixel_index, dispersion_derivative)
        for sample in laser_lsfs
    ]
    reference_overlays: List[ReferenceOverlayData] = []
    if reference_csv_paths:
        for reference_csv_path in reference_csv_paths:
            if not isinstance(reference_csv_path, str) or not os.path.exists(reference_csv_path):
                continue
            try:
                reference_df = pd.read_csv(reference_csv_path)
            except Exception:
                continue
            if "Wavelength_nm" not in reference_df.columns or "WavelengthOffset_nm" not in reference_df.columns or "LSF_Normalized" not in reference_df.columns:
                continue
            reference_name = os.path.splitext(os.path.basename(reference_csv_path))[0]
            reference_curves: List[OffsetCurve] = []
            for wavelength_nm in sorted(reference_df["Wavelength_nm"].unique()):
                rows = reference_df[reference_df["Wavelength_nm"] == wavelength_nm]
                if rows.empty:
                    continue
                x_values = rows["WavelengthOffset_nm"].astype(float).to_numpy()
                y_values = rows["LSF_Normalized"].astype(float).to_numpy()
                reference_curves.append(
                    OffsetCurve(
                        label=f"{float(wavelength_nm):.0f} nm (Reference)",
                        wavelength_nm=float(wavelength_nm),
                        x_values_nm=x_values,
                        y_values=y_values,
                        fwhm_nm=compute_fwhm(x_values, y_values),
                        width_20_percent_nm=compute_width_at_percent_max(x_values, y_values, percent=0.2),
                    )
                )
            if reference_curves:
                reference_overlays.append(
                    ReferenceOverlayData(
                        reference_name=reference_name,
                        measured_curves=measured_laser_curves,
                        reference_curves=reference_curves,
                    )
                )

    hg_ar_lamp_curves = [
        _build_offset_curve(sample.wavelength_nm, np.asarray(sample.values, dtype=float), sample.pixel_index, dispersion_derivative)
        for sample in hg_ar_lamp_lsfs
    ]

    slit_examples: List[OffsetCurve] = []
    c1_default = c1_pairs[0].value if c1_pairs else 0.0
    for center_nm in (350, 400, 480):
        lam_um = center_nm / 1000.0
        a2_value = np.clip(np.polyval(a2_poly, lam_um), 0.2, 5.0)
        a3_value = np.polyval(a3_poly, lam_um)
        x_values = generate_adaptive_x(a2_value)
        y_values = slit_func(x_values, a2_value, a3_value, c1_default)
        slit_examples.append(
            OffsetCurve(
                label=f"λ0 = {center_nm} nm",
                wavelength_nm=float(center_nm),
                x_values_nm=x_values,
                y_values=y_values,
                fwhm_nm=compute_fwhm(x_values, y_values),
                width_20_percent_nm=compute_width_at_percent_max(x_values, y_values, percent=0.2),
            )
        )

    metrics = CharacterizationMetrics(
        hg_ar_match_rmse_nm=float(rmse) if rmse is not None else None,
        matched_hg_line_count=len(matched_pixels),
        dispersion_coefficients=[float(value) for value in np.asarray(dispersion_coeffs, dtype=float)],
        laser_peak_pixels={sample.label: int(sample.pixel_index) for sample in laser_lsfs},
        slit_parameter_samples=slit_parameter_samples,
        spectral_resolution_wavelengths_nm=resolution_wavelengths,
        spectral_resolution_fwhm_nm=resolution_fwhm,
    )

    return CharacterizationComputation(
        serial_number=sn,
        pixel_count=npix,
        summary_lines=summary_lines,
        metrics=metrics,
        laser_lsfs=laser_lsfs,
        corrected_640_spectra=corrected_640_spectra,
        hg_ar_peak_match=hg_match,
        hg_ar_lamp_lsfs=hg_ar_lamp_lsfs,
        sdf_matrix=sdf_matrix,
        sdf_reference_pixels=pixel_locations_arr,
        dispersion_fit_pixels=comb_peak_pixels_sorted,
        dispersion_fit_wavelengths_nm=comb_wavelengths_sorted,
        dispersion_curve_pixels=dispersion_curve_pixels,
        dispersion_curve_wavelengths_nm=dispersion_curve_wavelengths,
        a2_samples=a2_pairs,
        a3_samples=a3_pairs,
        a2_poly_coefficients=a2_poly,
        a3_poly_coefficients=a3_poly,
        measured_laser_curves=measured_laser_curves,
        reference_overlays=reference_overlays,
        hg_ar_lamp_curves=hg_ar_lamp_curves,
        slit_examples=slit_examples,
    )

