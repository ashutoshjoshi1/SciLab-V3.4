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
    # Auto-IT controller: back off by this factor while the detector is saturated
    # (a clipped frame carries no amplitude information), and stop iterating once a
    # proposed change is smaller than this relative amount.
    saturation_backoff: float = 0.7
    it_convergence_rel: float = 0.005
    max_nonfinite_frames: int = 3
    # --- Bench-validated speed options (default = v3.3 behaviour) ---------------
    # Turn off only the source that is lit instead of sending all_off() before
    # every laser (saves ~1-2 s of serial sleeps per source).
    smart_source_off: bool = False
    # Arm the CUBE (EXT/CW/P, no emission) while the previous source's dark is
    # being acquired, so only L=1 + settle remains when 377 nm starts.
    cube_prearm: bool = False


@dataclass
class MeasurementOrchestratorCallbacks:
    prepare_devices: Callable[[], None]
    power_lookup: Callable[[str], float]
    auto_it_update: Callable[[str, np.ndarray, float, float], None] = lambda *_args, **_kwargs: None
    measurement_completed: Callable[[str], None] = lambda *_args, **_kwargs: None
    countdown: Callable[[int, str, str], None] = lambda *_args, **_kwargs: None
    error_handler: Callable[[str, Exception], None] = lambda *_args, **_kwargs: None
    # (tag, index, total) just before a source is measured: drives the progress bar.
    source_started: Callable[[str, int, int], None] = lambda *_args, **_kwargs: None


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
        self._lit_source: Optional[str] = None
        self._cube_armed = False

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
        self._lit_source = None
        self._cube_armed = False

        try:
            main_tags = [tag for tag in laser_tags if tag != "640"]
            if "640" in laser_tags:
                trailing_640 = ["640"]
            else:
                trailing_640 = []

            sequence = [str(tag) for tag in [*main_tags, *trailing_640]]
            for index, tag in enumerate(sequence):
                if not continue_run():
                    result.stopped_early = True
                    break

                next_tag = sequence[index + 1] if index + 1 < len(sequence) else None
                self.callbacks.source_started(tag, index + 1, len(sequence))
                try:
                    completed = (
                        self._run_640_measurement(continue_run)
                        if tag == "640"
                        else self._run_single_measurement(tag, start_it_override, continue_run, next_tag=next_tag)
                    )
                    if completed:
                        result.completed_tags.append(str(tag))
                except Exception as exc:
                    if not continue_run():
                        # The operator pressed Stop mid-capture: not an error worth a dialog.
                        result.stopped_early = True
                        break
                    result.errors.append(f"{tag}: {exc}")
                    self.callbacks.error_handler(f"Measurement {tag}", exc)
        finally:
            self._turn_all_sources_off()
            result.rows_written = len(self.data.rows)

        return result

    def _check_backend_call(self, result: object, action: str) -> None:
        if isinstance(result, str) and result and result != "OK":
            raise RuntimeError(f"{action} failed: {result}")

    def _turn_off_before(self, tag: str) -> None:
        """Make sure nothing else is lit before ``tag`` is switched on."""
        if self.config.smart_source_off:
            lit = self._lit_source
            if lit is not None and lit != tag:
                self._ensure_source_state(lit, False)
            return
        self._turn_all_sources_off()

    def _turn_all_sources_off(self) -> None:
        self._lit_source = None
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

    def _cube_power_mw(self) -> float:
        value = float(self.callbacks.power_lookup("377"))
        return value * 1000.0 if value <= 0.3 else value

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
        elif tag == "377":
            if turn_on:
                if self.config.cube_prearm and self._cube_armed and hasattr(self.lasers, "cube_emit"):
                    self.lasers.cube_emit()
                else:
                    self.lasers.cube_on(power_mw=self._cube_power_mw())
                    self._cube_armed = True
            else:
                self.lasers.cube_off()
        elif tag in self.config.relay_channel_map:
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
        else:
            raise RuntimeError(f"Unsupported measurement tag '{tag}'.")

        if turn_on:
            self._lit_source = tag
        elif self._lit_source == tag:
            self._lit_source = None

    def _prearm_cube_if_next(self, next_tag: Optional[str]) -> None:
        """Bench option: send the CUBE's EXT/CW/P setup (no emission) during a dark capture."""
        if not self.config.cube_prearm or next_tag != "377" or self._cube_armed:
            return
        if not hasattr(self.lasers, "cube_arm"):
            return
        try:
            self.lasers.ensure_open_for_tag("377")
            self.lasers.cube_arm(power_mw=self._cube_power_mw())
            self._cube_armed = True
        except Exception as exc:
            self.callbacks.error_handler("CUBE pre-arm", exc)

    def _apply_it(self, it_ms: float) -> None:
        """Send set_it only when the value actually changes.

        Signal and dark captures reuse the settled auto-IT value, so this removes
        two DLL round trips per source (on Hama4 each set_it also stops and
        restarts the stream with a fixed 0.2 s pause).
        """
        current = getattr(self.spec, "it_ms", None)
        try:
            unchanged = current is not None and abs(float(current) - float(it_ms)) <= 1e-9
        except (TypeError, ValueError):
            unchanged = False
        if unchanged:
            return
        self._check_backend_call(self.spec.set_it(it_ms), "set_it")

    def _capture_counts(self, it_ms: float, num_cycles: int) -> np.ndarray:
        self._apply_it(it_ms)
        self._check_backend_call(self.spec.measure(ncy=num_cycles), "measure")
        self._check_backend_call(self.spec.wait_for_measurement(), "wait_for_measurement")
        raw = getattr(self.spec, "rcm", None)
        if raw is None:
            raise RuntimeError("Spectrometer did not expose latest counts in 'rcm'.")
        counts = np.asarray(raw, dtype=float)
        if counts.size == 0 or not np.any(np.isfinite(counts)):
            raise RuntimeError("Spectrometer returned an empty or invalid measurement frame.")
        return counts

    def _clamp_it(self, it_ms: float) -> float:
        return max(self.config.it_min, min(self.config.it_max, float(it_ms)))

    def _auto_adjust_it(self, start_it: float, tag: str, should_continue: Callable[[], bool]) -> tuple[float, float]:
        """Find the integration time that puts the peak inside the target band.

        Detector counts scale linearly with integration time, so each unsaturated
        frame lets us jump straight to ``it * target / peak``; a saturated frame
        (which carries no amplitude information) backs off geometrically. This
        converges in a handful of frames where the previous additive stepper
        (+0.3 ms / -0.1 ms per frame) needed hundreds.
        """
        cfg = self.config
        it_ms = self._clamp_it(start_it)
        peak = float("nan")
        self.it_history.clear()
        nonfinite_frames = 0

        for _ in range(max(1, cfg.max_it_adjust_iters) + 1):
            if not should_continue():
                break
            counts = self._capture_counts(it_ms, 1)
            peak = float(np.nanmax(counts))
            if not np.isfinite(peak):
                nonfinite_frames += 1
                if nonfinite_frames >= cfg.max_nonfinite_frames:
                    raise RuntimeError(f"{tag}: spectrometer returned {nonfinite_frames} non-finite frames during auto-IT")
                continue
            nonfinite_frames = 0

            self.it_history.append((it_ms, peak))
            self.callbacks.auto_it_update(tag, counts, it_ms, peak)

            if cfg.target_low <= peak <= cfg.target_high:
                return it_ms, peak

            if peak >= cfg.sat_thresh:
                if it_ms <= cfg.it_min * (1.0 + 1e-9):
                    raise RuntimeError(
                        f"{tag}: detector saturates even at the minimum integration time ({cfg.it_min} ms); reduce the source power"
                    )
                proposed = it_ms * cfg.saturation_backoff
            else:
                proposed = it_ms * (cfg.target_mid / max(peak, 1.0))

            proposed = self._clamp_it(proposed)
            if abs(proposed - it_ms) <= cfg.it_convergence_rel * max(it_ms, 1e-9):
                # Pinned against it_min/it_max: no further progress is possible.
                return proposed, peak
            it_ms = proposed

        return it_ms, peak

    def _run_single_measurement(
        self,
        tag: str,
        start_it_override: Optional[float],
        should_continue: Callable[[], bool],
        next_tag: Optional[str] = None,
    ) -> bool:
        self._turn_off_before(tag)
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
        self._prearm_cube_if_next(next_tag)
        dark = self._capture_counts(it_ms, self.config.n_dark)

        self.data.append_measurement(tag, it_ms, self.config.n_sig, signal)
        self.data.append_measurement(f"{tag}_dark", it_ms, self.config.n_dark, dark)
        self.callbacks.measurement_completed(tag)
        return True

    def _run_640_measurement(self, should_continue: Callable[[], bool]) -> bool:
        if not should_continue():
            return False

        measurements_recorded = False
        self._turn_off_before("640")
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
