from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

import numpy as np

try:
    import pandas as pd
except Exception:  # pragma: no cover - runtime dependency on target machine
    pd = None


@dataclass(frozen=True)
class MeasurementCapture:
    timestamp: str
    wavelength: str
    integration_time_ms: float
    num_cycles: int
    counts: np.ndarray

    def to_row(self, npix: int) -> List[float]:
        values = np.asarray(self.counts, dtype=float).tolist()
        if len(values) < int(npix):
            values.extend([np.nan] * (int(npix) - len(values)))
        elif len(values) > int(npix):
            values = values[: int(npix)]
        return [self.timestamp, self.wavelength, float(self.integration_time_ms), int(self.num_cycles), *values]


@dataclass
class MeasurementRunResult:
    requested_tags: List[str]
    completed_tags: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    stopped_early: bool = False
    rows_written: int = 0


class MeasurementData:
    def __init__(self, npix: int = 2048, serial_number: str = "Unknown"):
        self.npix = int(npix)
        self.serial_number = serial_number
        self.rows: List[List[float]] = []

    def clear(self) -> None:
        self.rows.clear()

    def append_capture(self, capture: MeasurementCapture) -> None:
        self.rows.append(capture.to_row(self.npix))

    def append_measurement(
        self,
        wavelength: str,
        integration_time_ms: float,
        num_cycles: int,
        counts,
        timestamp: Optional[str] = None,
    ) -> MeasurementCapture:
        capture = MeasurementCapture(
            timestamp=timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            wavelength=str(wavelength),
            integration_time_ms=float(integration_time_ms),
            num_cycles=int(num_cycles),
            counts=np.asarray(counts, dtype=float),
        )
        self.append_capture(capture)
        return capture

    def to_dataframe(self):
        if pd is None:
            raise RuntimeError("pandas is required to save measurement data.")

        columns = ["Timestamp", "Wavelength", "IntegrationTime", "NumCycles"] + [
            f"Pixel_{idx}" for idx in range(int(self.npix))
        ]

        normalized_rows = []
        for row in self.rows:
            row_values = list(row)
            if len(row_values) < len(columns):
                row_values.extend([np.nan] * (len(columns) - len(row_values)))
            elif len(row_values) > len(columns):
                row_values = row_values[: len(columns)]
            normalized_rows.append(row_values)

        return pd.DataFrame(normalized_rows, columns=columns)

    def last_vectors_for(self, tag: str):
        signal = None
        dark = None
        dark_tag = f"{tag}_dark"

        for row in reversed(self.rows):
            wavelength = str(row[1])
            values = np.asarray(row[4:], dtype=float)
            if signal is None and wavelength == str(tag):
                signal = values
            elif dark is None and wavelength == dark_tag:
                dark = values
            if signal is not None and dark is not None:
                break

        return signal, dark

