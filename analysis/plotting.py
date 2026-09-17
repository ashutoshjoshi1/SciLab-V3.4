from __future__ import annotations

import os
from pathlib import Path
from typing import List

import numpy as np
from matplotlib.figure import Figure

from .models import AnalysisArtifact, CharacterizationComputation


def _save_figure(fig: Figure, path: Path, artifact_name: str) -> AnalysisArtifact:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=300, bbox_inches="tight")
    return AnalysisArtifact(name=artifact_name, path=str(path))


def render_characterization_artifacts(
    computation: CharacterizationComputation,
    folder: str,
    timestamp: str,
) -> List[AnalysisArtifact]:
    output_dir = Path(folder)
    output_dir.mkdir(parents=True, exist_ok=True)
    serial_number = computation.serial_number
    artifacts: List[AnalysisArtifact] = []
    npix = computation.pixel_count

    if computation.laser_lsfs:
        fig_norm = Figure(figsize=(12, 6))
        ax_norm = fig_norm.add_subplot(111)
        ax_norm.set_yscale("log")
        ax_norm.set_xticks(np.arange(0, npix, 100))
        ax_norm.set_ylim(1e-5, 1.4)
        for sample in computation.laser_lsfs:
            ax_norm.plot(sample.values, label=f"{sample.wavelength_nm:.1f} nm")
        ax_norm.set_title(f"Spectrometer= {serial_number}: Normalized LSFs")
        ax_norm.set_xlabel("Pixel Index")
        ax_norm.set_ylabel("Normalized Intensity")
        ax_norm.grid(True)
        ax_norm.legend()
        artifacts.append(
            _save_figure(
                fig_norm,
                output_dir / f"Normalized_Laser_Plot_{serial_number}_{timestamp}.png",
                "Normalized LSFs",
            )
        )

    fig_640 = Figure(figsize=(12, 6))
    ax_640 = fig_640.add_subplot(111)
    ax_640.set_xticks(np.arange(0, max(npix, 1), 100))
    for spectrum in computation.corrected_640_spectra:
        ax_640.plot(spectrum.values, label=f"{spectrum.label} @ {spectrum.integration_time_ms:.1f} ms")
    ax_640.set_title(f"Spectrometer= {serial_number}: Dark-Corrected 640 nm Measurements")
    ax_640.set_xlabel("Pixel Index")
    ax_640.set_ylabel("Corrected Intensity")
    ax_640.grid(True)
    if computation.corrected_640_spectra:
        ax_640.legend()
    artifacts.append(
        _save_figure(
            fig_640,
            output_dir / f"OOR_640nm_Plot_{serial_number}_{timestamp}.png",
            "640 nm Dark-Corrected",
        )
    )

    fig_hg = Figure(figsize=(14, 6))
    ax_hg = fig_hg.add_subplot(111)
    if computation.hg_ar_peak_match.signal is not None:
        signal = computation.hg_ar_peak_match.signal
        pixels = np.arange(len(signal))
        ax_hg.set_yscale("log")
        ax_hg.plot(pixels, signal, label="Dark-Corrected Hg-Ar", color="blue")
        if computation.hg_ar_peak_match.detected_peaks.size:
            detected = computation.hg_ar_peak_match.detected_peaks
            ax_hg.plot(detected, signal[detected], "ro", label="Detected Peaks")
        for pixel_value, wavelength_nm in zip(
            computation.hg_ar_peak_match.matched_pixels,
            computation.hg_ar_peak_match.matched_wavelengths_nm,
        ):
            ax_hg.text(
                pixel_value,
                signal[int(pixel_value)] + 2500,
                f"{wavelength_nm:.1f} nm",
                rotation=0,
                color="brown",
                fontsize=10,
                ha="center",
                va="bottom",
            )
    ax_hg.set_xlabel("Pixel")
    ax_hg.set_ylabel("Signal (Counts)")
    ax_hg.set_title(f"Spectrometer= {serial_number}: Hg-Ar Lamp Spectrum with Detected Peaks")
    if ax_hg.lines:
        ax_hg.legend()
    ax_hg.grid(True)
    artifacts.append(
        _save_figure(
            fig_hg,
            output_dir / f"HgAr_Peaks_Plot_{serial_number}_{timestamp}.png",
            "Hg-Ar Peaks",
        )
    )

    fig_sdf = Figure(figsize=(12, 6))
    ax_sdf = fig_sdf.add_subplot(111)
    ax_sdf.set_xlim(0, max(npix, 1))
    ax_sdf.set_xticks(np.arange(0, max(npix, 1), 100))
    for column in computation.sdf_reference_pixels:
        ax_sdf.plot(computation.sdf_matrix[:, int(column)], label=f"{int(column)} pixel")
    ax_sdf.set_xlabel("Pixels")
    ax_sdf.set_ylabel("SDF Value")
    ax_sdf.set_title(f"Spectrometer= {serial_number}: Spectral Distribution Function (SDF)")
    ax_sdf.grid(True)
    if computation.sdf_reference_pixels.size:
        ax_sdf.legend()
    artifacts.append(
        _save_figure(
            fig_sdf,
            output_dir / f"SDF_Plot_{serial_number}_{timestamp}.png",
            "SDF",
        )
    )

    fig_sdf_heat = Figure(figsize=(10, 6))
    ax_heat = fig_sdf_heat.add_subplot(111)
    image = ax_heat.imshow(computation.sdf_matrix, aspect="auto", cmap="coolwarm", origin="lower")
    fig_sdf_heat.colorbar(image, ax=ax_heat, label="SDF Value")
    ax_heat.set_xlabel("Pixels")
    ax_heat.set_ylabel("Spectral Pixel Index")
    ax_heat.set_title(f"Spectrometer= {serial_number}: SDF Matrix Heatmap")
    artifacts.append(
        _save_figure(
            fig_sdf_heat,
            output_dir / f"SDF_Heatmap_{serial_number}_{timestamp}.png",
            "SDF Heatmap",
        )
    )

    if len(computation.dispersion_fit_pixels) >= 2:
        fig_disp = Figure(figsize=(14, 6))
        ax_disp = fig_disp.add_subplot(111)
        ax_disp.plot(
            computation.dispersion_fit_pixels,
            computation.dispersion_fit_wavelengths_nm,
            "ro",
            label="Laser + Lamp Peaks",
            markersize=8,
        )
        ax_disp.plot(
            computation.dispersion_curve_pixels,
            computation.dispersion_curve_wavelengths_nm,
            "b-",
            label="Dispersion Fit",
            linewidth=2,
        )
        ax_disp.set_xlabel("Pixel", fontsize=18)
        ax_disp.set_ylabel("Wavelength (nm)", fontsize=18)
        ax_disp.set_xticks(np.arange(0, npix + 50, 100))
        ax_disp.tick_params(axis="both", labelsize=16)
        ax_disp.set_title(f"Spectrometer= {serial_number}: Dispersion Fit")
        ax_disp.grid(True)
        ax_disp.legend(fontsize=14)
        fig_disp.tight_layout()
        artifacts.append(
            _save_figure(
                fig_disp,
                output_dir / f"Dispersion_Fit_{serial_number}_{timestamp}.png",
                "Dispersion Fit",
            )
        )

    fig_params = Figure(figsize=(14, 6))
    ax_a2 = fig_params.add_subplot(1, 2, 1)
    ax_a3 = fig_params.add_subplot(1, 2, 2)
    if computation.a2_samples:
        wavelengths = np.asarray([sample.wavelength_nm for sample in computation.a2_samples], dtype=float)
        values = np.asarray([sample.value for sample in computation.a2_samples], dtype=float)
        ax_a2.plot(wavelengths, values, "ro", label="Measured A2", markersize=8)
        if len(computation.a2_samples) >= 2:
            fit_x = np.linspace(wavelengths.min(), wavelengths.max(), 100)
            fit_y = np.polyval(computation.a2_poly_coefficients, fit_x / 1000.0)
            ax_a2.plot(fit_x, fit_y, "b-", label="Fitted A2", linewidth=2)
    ax_a2.set_xlabel("Wavelength (nm)", fontsize=14)
    ax_a2.set_ylabel("A2 (Width)", fontsize=14)
    ax_a2.set_title(f"Spectrometer={serial_number}: A2 vs Wavelength")
    ax_a2.grid(True)
    if ax_a2.lines:
        ax_a2.legend(fontsize=12)
    if computation.a3_samples:
        wavelengths = np.asarray([sample.wavelength_nm for sample in computation.a3_samples], dtype=float)
        values = np.asarray([sample.value for sample in computation.a3_samples], dtype=float)
        ax_a3.plot(wavelengths, values, "ro", label="Measured A3", markersize=8)
        if len(computation.a3_samples) >= 2:
            fit_x = np.linspace(wavelengths.min(), wavelengths.max(), 100)
            fit_y = np.polyval(computation.a3_poly_coefficients, fit_x / 1000.0)
            ax_a3.plot(fit_x, fit_y, "b-", label="Fitted A3", linewidth=2)
    ax_a3.set_xlabel("Wavelength (nm)", fontsize=14)
    ax_a3.set_ylabel("A3 (Shape)", fontsize=14)
    ax_a3.set_title(f"Spectrometer={serial_number}: A3 vs Wavelength")
    ax_a3.grid(True)
    if ax_a3.lines:
        ax_a3.legend(fontsize=12)
    fig_params.tight_layout()
    artifacts.append(
        _save_figure(
            fig_params,
            output_dir / f"A2_A3_vs_Wavelength_{serial_number}_{timestamp}.png",
            "A2_A3_vs_Wavelength",
        )
    )

    fig_resolution = Figure(figsize=(10, 6))
    ax_res = fig_resolution.add_subplot(111)
    ax_res.plot(
        computation.metrics.spectral_resolution_wavelengths_nm,
        computation.metrics.spectral_resolution_fwhm_nm,
        label=f"Spectrometer = {serial_number}",
    )
    ax_res.set_xlabel("Wavelength (nm)")
    ax_res.set_ylabel("FWHM (nm)")
    ax_res.set_title(f"Spectrometer= {serial_number}: Spectral Resolution vs Wavelength")
    ax_res.grid(True)
    ax_res.legend()
    artifacts.append(
        _save_figure(
            fig_resolution,
            output_dir / f"Spectral_Resolution_with_wavelength_{serial_number}_{timestamp}.png",
            "Spectral Resolution",
        )
    )

    fig_slit = Figure(figsize=(10, 6))
    ax_slit = fig_slit.add_subplot(111)
    for curve in computation.slit_examples:
        ax_slit.plot(
            curve.x_values_nm,
            curve.y_values,
            label=f"{curve.label}, FWHM = {curve.fwhm_nm:.3f} nm",
        )
    ax_slit.set_title(f"Spectrometer= {serial_number}: Slit Function with FWHM")
    ax_slit.set_xlabel("Wavelength Offset from Center (nm)")
    ax_slit.set_ylabel("Normalized Intensity")
    ax_slit.grid(True)
    if ax_slit.lines:
        ax_slit.legend()
    artifacts.append(
        _save_figure(
            fig_slit,
            output_dir / f"Slit_Functions_{serial_number}_{timestamp}.png",
            "Slit Functions",
        )
    )

    fig_lasers = Figure(figsize=(10, 6))
    ax_lasers = fig_lasers.add_subplot(111)
    for curve in computation.measured_laser_curves:
        ax_lasers.plot(
            curve.x_values_nm,
            curve.y_values,
            linewidth=2,
            label=f"{curve.wavelength_nm:.0f} nm (FWHM={curve.fwhm_nm:.2f})",
        )
    ax_lasers.set_yscale("log")
    ax_lasers.set_title(f"Spectrometer = {serial_number}: Normalized LSFs of Measured Lasers", fontsize=12, fontweight="bold")
    ax_lasers.set_xlabel("Wavelength Offset from Peak (nm)", fontsize=10)
    ax_lasers.set_ylabel("Normalized Intensity", fontsize=10)
    ax_lasers.set_xlim(-7, 7)
    ax_lasers.set_ylim(1e-4, 1.5)
    ax_lasers.grid(True, alpha=0.3)
    if ax_lasers.lines:
        ax_lasers.legend(loc="upper right", fontsize=9, ncol=2)
    fig_lasers.tight_layout()
    artifacts.append(
        _save_figure(
            fig_lasers,
            output_dir / f"Normalized_LSFs_Measured_Lasers_{serial_number}_{timestamp}.png",
            "Measured Lasers LSFs",
        )
    )

    wavelength_colors = [
        "blue",
        "orange",
        "green",
        "red",
        "purple",
        "brown",
        "pink",
        "olive",
        "cyan",
        "magenta",
        "gold",
        "teal",
        "navy",
        "coral",
        "lime",
        "crimson",
        "indigo",
        "chocolate",
    ]
    for overlay in computation.reference_overlays:
        fig_overlay = Figure(figsize=(12, 7))
        ax_overlay = fig_overlay.add_subplot(111)
        color_map: dict[str, str] = {}
        color_index = 0

        def color_for(wavelength_nm: float) -> str:
            nonlocal color_index
            key = f"{wavelength_nm:.0f}"
            if key not in color_map:
                color_map[key] = wavelength_colors[color_index % len(wavelength_colors)]
                color_index += 1
            return color_map[key]

        for curve in overlay.measured_curves:
            ax_overlay.plot(
                curve.x_values_nm,
                curve.y_values,
                "-",
                linewidth=2.0,
                color=color_for(curve.wavelength_nm),
                label=f"{curve.wavelength_nm:.0f} nm (Measured)",
                alpha=0.8,
            )
        for curve in overlay.reference_curves:
            ax_overlay.plot(
                curve.x_values_nm,
                curve.y_values,
                "--",
                linewidth=2.0,
                color=color_for(curve.wavelength_nm),
                label=f"{curve.wavelength_nm:.0f} nm (Reference)",
                alpha=0.7,
            )

        ax_overlay.set_yscale("log")
        ax_overlay.set_title(f"{serial_number} Reference ({overlay.reference_name}) Normalised LSF", fontsize=13, fontweight="bold")
        ax_overlay.set_xlabel("Wavelength Offset from Peak (nm)", fontsize=11)
        ax_overlay.set_ylabel("Normalized Intensity", fontsize=11)
        ax_overlay.set_xlim(-7, 7)
        ax_overlay.set_ylim(1e-4, 1.5)
        ax_overlay.grid(True, alpha=0.3)
        ax_overlay.legend(loc="upper right", fontsize=8, ncol=2, framealpha=0.9, edgecolor="gray")
        fig_overlay.tight_layout()

        safe_name = overlay.reference_name.replace(" ", "_").replace("/", "_").replace("{", "").replace("}", "")[:50]
        artifact_name = f"{serial_number} Reference ({overlay.reference_name}) Normalised LSF"
        if len(artifact_name) > 50:
            artifact_name = artifact_name[:47] + "..."
        artifacts.append(
            _save_figure(
                fig_overlay,
                output_dir / f"{serial_number}_Reference_{safe_name}_Normalised_LSF_{timestamp}.png",
                artifact_name,
            )
        )

    fig_lamp = Figure(figsize=(10, 6))
    ax_lamp = fig_lamp.add_subplot(111)
    for curve in computation.hg_ar_lamp_curves:
        ax_lamp.plot(
            curve.x_values_nm,
            curve.y_values,
            linewidth=2,
            label=f"{curve.wavelength_nm:.1f} nm (FWHM={curve.fwhm_nm:.2f})",
        )
    ax_lamp.set_yscale("log")
    ax_lamp.set_title(f"Spectrometer = {serial_number}: Normalized LSFs of Hg-Ar Lamp", fontsize=12, fontweight="bold")
    ax_lamp.set_xlabel("Wavelength Offset from Peak (nm)", fontsize=10)
    ax_lamp.set_ylabel("Normalized Intensity", fontsize=10)
    ax_lamp.set_xlim(-7, 7)
    ax_lamp.set_ylim(1e-4, 1.5)
    ax_lamp.grid(True, alpha=0.3)
    if computation.hg_ar_lamp_curves:
        ax_lamp.legend(loc="upper right", fontsize=9, ncol=2)
    fig_lamp.tight_layout()
    artifacts.append(
        _save_figure(
            fig_lamp,
            output_dir / f"Normalized_LSFs_HgAr_Lamp_{serial_number}_{timestamp}.png",
            "Hg-Ar Lamp LSFs",
        )
    )

    return artifacts
