from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence

import numpy as np

from domain.measurement import MeasurementData, MeasurementRunResult
from domain.spectrometer import SpectrometerBackend, assert_spectrometer_backend


@dataclass(frozen=True)
class MeasurementOrchestratorConfig:
    default_start_it: Mapping[str, float]
    target_low: float
    target_high: float
    target_mid: float
    it_min: float
    it_max: float
    it_step_up: float
    it_step_down: float
    max_it_adjust_iters: int
    sat_thresh: float
    n_sig: int
    n_dark: int
    n_sig_640: int
    n_dark_640: int
    relay_channel_map: Mapping[str, int] = field(
        default_factory=lambda: {"517": 2, "532": 1, "Hg_Ar": 4}
    )
    nm640_integrations: Sequence[float] = (100.0, 500.0, 1000.0)
    source_delay_s: float = 0.2
    cube_stabilize_delay_s: float = 3.0
    dark_delay_s: float = 0.3
    nm640_warmup_delay_s: float = 1.0


@dataclass
class MeasurementOrchestratorCallbacks:
    prepare_devices: Callable[[], None]
    power_lookup: Callable[[str], float]
    auto_it_update: Callable[[str, np.ndarray, float, float], None] = lambda *_args, **_kwargs: None
    measurement_completed: Callable[[str], None] = lambda *_args, **_kwargs: None
    countdown: Callable[[int, str, str], None] = lambda *_args, **_kwargs: None
    error_handler: Callable[[str, Exception], None] = lambda *_args, **_kwargs: None


class MeasurementOrchestrator:
    def __init__(
        self,
        spectrometer: SpectrometerBackend,
        laser_controller,
        measurement_data: MeasurementData,
        config: MeasurementOrchestratorConfig,
        callbacks: MeasurementOrchestratorCallbacks,
        *,
        it_history: Optional[list[tuple[float, float]]] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self.spec = assert_spectrometer_backend(spectrometer)
        self.lasers = laser_controller
        self.data = measurement_data
        self.config = config
        self.callbacks = callbacks
        self.it_history = it_history if it_history is not None else []
        self.sleep = sleep_fn

    def run(
        self,
        laser_tags: Sequence[str],
        start_it_override: Optional[float] = None,
        *,
        should_continue: Optional[Callable[[], bool]] = None,
    ) -> MeasurementRunResult:
        continue_run = should_continue or (lambda: True)
        result = MeasurementRunResult(requested_tags=[str(tag) for tag in laser_tags])

        self.callbacks.prepare_devices()
        self.lasers.open_all()
        self._turn_all_sources_off()

        try:
            main_tags = [tag for tag in laser_tags if tag != "640"]
            if "640" in laser_tags:
                trailing_640 = ["640"]
            else:
                trailing_640 = []

            for tag in [*main_tags, *trailing_640]:
                if not continue_run():
                    result.stopped_early = True
                    break

                try:
                    completed = (
                        self._run_640_measurement(continue_run)
                        if tag == "640"
                        else self._run_single_measurement(str(tag), start_it_override, continue_run)
                    )
                    if completed:
                        result.completed_tags.append(str(tag))
                except Exception as exc:
                    result.errors.append(f"{tag}: {exc}")
                    self.callbacks.error_handler(f"Measurement {tag}", exc)
        finally:
            self._turn_all_sources_off()
            result.rows_written = len(self.data.rows)

        return result

    def _check_backend_call(self, result: object, action: str) -> None:
        if isinstance(result, str) and result and result != "OK":
            raise RuntimeError(f"{action} failed: {result}")

    def _turn_all_sources_off(self) -> None:
        if hasattr(self.lasers, "all_off"):
            self.lasers.all_off()
            return

        obis_map = getattr(self.lasers, "OBIS_MAP", {})
        for channel in obis_map.values():
            try:
                self.lasers.obis_off(channel)
            except Exception:
                pass
        for relay_number in self.config.relay_channel_map.values():
            try:
                self.lasers.relay_off(relay_number)
            except Exception:
                pass
        try:
            self.lasers.cube_off()
        except Exception:
            pass

    def _ensure_source_state(self, tag: str, turn_on: bool) -> None:
        self.lasers.ensure_open_for_tag(tag)
        obis_map = getattr(self.lasers, "OBIS_MAP", {})

        if tag in obis_map:
            channel = obis_map[tag]
            if turn_on:
                self.lasers.obis_set_power(channel, float(self.callbacks.power_lookup(tag)))
                self.lasers.obis_on(channel)
            else:
                self.lasers.obis_off(channel)
            return

        if tag == "377":
            if turn_on:
                value = float(self.callbacks.power_lookup(tag))
                mw = value * 1000.0 if value <= 0.3 else value
                self.lasers.cube_on(power_mw=mw)
            else:
                self.lasers.cube_off()
            return

        if tag in self.config.relay_channel_map:
            relay_number = int(self.config.relay_channel_map[tag])
            if turn_on:
                if tag == "Hg_Ar":
                    self.callbacks.countdown(
                        45,
                        "Fiber Switch",
                        "Switch the fiber to Hg-Ar and press Enter to skip.",
                    )
                self.lasers.relay_on(relay_number)
            else:
                self.lasers.relay_off(relay_number)
            return

        raise RuntimeError(f"Unsupported measurement tag '{tag}'.")

    def _capture_counts(self, it_ms: float, num_cycles: int) -> np.ndarray:
        self._check_backend_call(self.spec.set_it(it_ms), "set_it")
        self._check_backend_call(self.spec.measure(ncy=num_cycles), "measure")
        self._check_backend_call(self.spec.wait_for_measurement(), "wait_for_measurement")
        raw = getattr(self.spec, "rcm", None)
        if raw is None:
            raise RuntimeError("Spectrometer did not expose latest counts in 'rcm'.")
        counts = np.asarray(raw, dtype=float)
        if counts.size == 0 or not np.any(np.isfinite(counts)):
            raise RuntimeError("Spectrometer returned an empty or invalid measurement frame.")
        return counts

    def _auto_adjust_it(self, start_it: float, tag: str, should_continue: Callable[[], bool]) -> tuple[float, float]:
        it_ms = max(self.config.it_min, min(self.config.it_max, float(start_it)))
        peak = float("nan")
        self.it_history.clear()

        iterations = 0
        while iterations <= self.config.max_it_adjust_iters and should_continue():
            counts = self._capture_counts(it_ms, 1)
            peak = float(np.nanmax(counts))
            if not np.isfinite(peak):
                iterations += 1
                continue

            self.it_history.append((it_ms, peak))
            self.callbacks.auto_it_update(tag, counts, it_ms, peak)

            if peak >= self.config.sat_thresh:
                it_ms = max(self.config.it_min, it_ms * 0.7)
                iterations += 1
                continue

            if self.config.target_low <= peak <= self.config.target_high:
                return it_ms, peak

            error = self.config.target_mid - peak
            if error > 0:
                delta = min(self.config.it_step_up, max(0.05, abs(error) / 5000.0))
                it_ms = min(self.config.it_max, it_ms + delta)
            else:
                delta = min(self.config.it_step_down, max(0.05, abs(error) / 5000.0))
                it_ms = max(self.config.it_min, it_ms - delta)

            iterations += 1

        return it_ms, peak

    def _run_single_measurement(
        self,
        tag: str,
        start_it_override: Optional[float],
        should_continue: Callable[[], bool],
    ) -> bool:
        self._turn_all_sources_off()
        self._ensure_source_state(tag, True)
        self.sleep(self.config.cube_stabilize_delay_s if tag == "377" else self.config.source_delay_s)

        start_it = (
            float(start_it_override)
            if start_it_override is not None
            else float(self.config.default_start_it.get(tag, self.config.default_start_it["default"]))
        )
        it_ms, peak = self._auto_adjust_it(start_it, tag, should_continue)

        if not (self.config.target_low <= peak <= self.config.target_high):
            self._ensure_source_state(tag, False)
            return False

        signal = self._capture_counts(it_ms, self.config.n_sig)
        self._ensure_source_state(tag, False)

        if not should_continue():
            return False

        self.sleep(self.config.dark_delay_s)
        dark = self._capture_counts(it_ms, self.config.n_dark)

        self.data.append_measurement(tag, it_ms, self.config.n_sig, signal)
        self.data.append_measurement(f"{tag}_dark", it_ms, self.config.n_dark, dark)
        self.callbacks.measurement_completed(tag)
        return True

    def _run_640_measurement(self, should_continue: Callable[[], bool]) -> bool:
        if not should_continue():
            return False

        measurements_recorded = False
        self._ensure_source_state("640", True)
        self.sleep(self.config.nm640_warmup_delay_s)

        try:
            for it_ms in self.config.nm640_integrations:
                if not should_continue():
                    return measurements_recorded
                signal = self._capture_counts(float(it_ms), self.config.n_sig_640)
                peak = float(np.nanmax(signal)) if signal.size else 0.0
                self.data.append_measurement("640", float(it_ms), self.config.n_sig_640, signal)
                self.callbacks.auto_it_update("640", signal, float(it_ms), peak)
                measurements_recorded = True

            self._ensure_source_state("640", False)
            self.sleep(self.config.dark_delay_s)

            for it_ms in self.config.nm640_integrations:
                if not should_continue():
                    return measurements_recorded
                dark = self._capture_counts(float(it_ms), self.config.n_dark_640)
                self.data.append_measurement("640_dark", float(it_ms), self.config.n_dark_640, dark)
        finally:
            self._ensure_source_state("640", False)

        self.callbacks.measurement_completed("640")
        return measurements_recorded
