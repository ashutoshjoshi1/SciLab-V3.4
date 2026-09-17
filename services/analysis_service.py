from __future__ import annotations

from datetime import datetime
from typing import List, Optional

import pandas as pd

from analysis.calculations import compute_characterization
from analysis.models import CharacterizationConfig, CharacterizationResult
from analysis.plotting import render_characterization_artifacts


class AnalysisService:
    def __init__(self, config: Optional[CharacterizationConfig] = None):
        self.config = config or CharacterizationConfig()

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
            reference_csv_paths=reference_csv_paths,
        )
        artifacts = []
        if computation.pixel_count > 0 and computation.laser_lsfs:
            artifacts = render_characterization_artifacts(computation, output_dir, run_timestamp)
        return CharacterizationResult(
            metrics=computation.metrics,
            artifacts=artifacts,
            summary_lines=computation.summary_lines,
        )
