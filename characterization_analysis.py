"""Backward-compatible facade for the refactored characterization pipeline."""

from __future__ import annotations

from typing import List, Optional

import pandas as pd

from analysis.models import AnalysisArtifact, CharacterizationConfig, CharacterizationResult
from services.analysis_service import AnalysisService


def perform_characterization(
    df: pd.DataFrame,
    sn: str,
    folder: str,
    timestamp: Optional[str] = None,
    config: Optional[CharacterizationConfig] = None,
    reference_csv_paths: Optional[List[str]] = None,
) -> CharacterizationResult:
    service = AnalysisService(config=config)
    return service.analyze(
        df=df,
        serial_number=sn,
        output_dir=folder,
        timestamp=timestamp,
        reference_csv_paths=reference_csv_paths,
    )

