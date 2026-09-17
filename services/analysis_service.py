from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from analysis.calculations import compute_characterization, load_reference_frame
from analysis.models import CharacterizationComputation, CharacterizationConfig, CharacterizationResult
from analysis.plotting import render_characterization_artifacts

LOGGER = logging.getLogger(__name__)


class AnalysisService:
    """Coordinates the analysis layers: compute metrics, render plots, persist the numbers."""

    def __init__(self, config: Optional[CharacterizationConfig] = None):
        self.config = config or CharacterizationConfig()
        # Reference CSVs are re-used across runs; cache them by (path, mtime).
        self._reference_cache: Dict[str, Tuple[float, pd.DataFrame]] = {}

    # ------------------------------------------------------------------

    def _reference_frames(self, paths: Optional[List[str]]) -> Dict[str, pd.DataFrame]:
        frames: Dict[str, pd.DataFrame] = {}
        for path in paths or []:
            if not isinstance(path, str):
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                LOGGER.warning("Reference CSV not found: %s", path)
                continue
            cached = self._reference_cache.get(path)
            if cached is None or cached[0] != mtime:
                frame = load_reference_frame(path)
                if frame is None:
                    continue
                self._reference_cache[path] = (mtime, frame)
                cached = self._reference_cache[path]
            frames[os.path.splitext(os.path.basename(path))[0]] = cached[1]
        return frames

    @staticmethod
    def metrics_to_dict(computation: CharacterizationComputation, timestamp: str) -> dict:
        """JSON-friendly view of the quantitative characterization result."""
        metrics = computation.metrics

        def _list(values) -> list:
            array = np.asarray(values, dtype=float)
            return [None if not np.isfinite(value) else float(value) for value in array.tolist()]

        return {
            "schema": "scilab.characterization.metrics/1",
            "serial_number": computation.serial_number,
            "timestamp": timestamp,
            "pixel_count": int(computation.pixel_count),
            "dispersion_coefficients_high_to_low": [float(value) for value in metrics.dispersion_coefficients],
            "laser_peak_pixels": {tag: int(pixel) for tag, pixel in metrics.laser_peak_pixels.items()},
            "hg_ar": {
                "matched_line_count": int(metrics.matched_hg_line_count),
                "match_rmse_nm": None if metrics.hg_ar_match_rmse_nm is None else float(metrics.hg_ar_match_rmse_nm),
                "matched_pixels": _list(computation.hg_ar_peak_match.matched_pixels),
                "matched_wavelengths_nm": _list(computation.hg_ar_peak_match.matched_wavelengths_nm),
            },
            "slit_parameters": [asdict(sample) for sample in metrics.slit_parameter_samples],
            "a2_poly_coefficients_um": _list(computation.a2_poly_coefficients),
            "a3_poly_coefficients_um": _list(computation.a3_poly_coefficients),
            "measured_lines": [
                {
                    "label": curve.label,
                    "wavelength_nm": float(curve.wavelength_nm),
                    "fwhm_nm": float(curve.fwhm_nm),
                    "width_20_percent_nm": float(curve.width_20_percent_nm),
                }
                for curve in computation.measured_laser_curves
            ],
            "spectral_resolution": {
                "wavelengths_nm": _list(metrics.spectral_resolution_wavelengths_nm),
                "fwhm_nm": _list(metrics.spectral_resolution_fwhm_nm),
            },
            "summary_lines": list(computation.summary_lines),
        }

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        os.replace(tmp_path, path)

    # ------------------------------------------------------------------

    def analyze(
        self,
        df: pd.DataFrame,
        serial_number: str,
        output_dir: str,
        timestamp: Optional[str] = None,
        reference_csv_paths: Optional[List[str]] = None,
    ) -> CharacterizationResult:
        run_timestamp = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
        computation = compute_characterization(
            df=df,
            sn=serial_number,
            config=self.config,
            reference_frames=self._reference_frames(reference_csv_paths),
        )

        artifacts = []
        metrics_path: Optional[str] = None
        if computation.pixel_count > 0 and computation.laser_lsfs:
            artifacts = render_characterization_artifacts(computation, output_dir, run_timestamp)
            # Persist the numbers next to the plots so runs can be compared and trended
            # without re-reading pictures.
            path = Path(output_dir) / f"metrics_{serial_number}_{run_timestamp}.json"
            try:
                self._write_json_atomic(path, self.metrics_to_dict(computation, run_timestamp))
                metrics_path = str(path)
                computation.summary_lines.append(f"Metrics saved: {path.name}")
            except Exception:
                LOGGER.exception("Could not write metrics JSON to %s", path)

        return CharacterizationResult(
            metrics=computation.metrics,
            artifacts=artifacts,
            summary_lines=computation.summary_lines,
            metrics_path=metrics_path,
        )
