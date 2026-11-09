from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence

import numpy as np

from domain.measurement import MeasurementData, MeasurementRunResult
from domain.spectrometer import SpectrometerBackend, assert_spectrometer_backend

# Additive auto-IT stepping (the v3.3 rule): an unsaturated frame moves the
# integration time by |target_mid - peak| / IT_STEP_COUNTS_PER_MS milliseconds,
# never less than IT_STEP_FLOOR_MS and never more than the configured
# it_step_up / it_step_down, so the exposure creeps toward the band frame by
# frame and slows down as the peak gets close.
IT_STEP_FLOOR_MS = 0.05
IT_STEP_COUNTS_PER_MS = 5000.0
# Auto-IT has no frame budget: it walks until the peak is in the band, the
# integration time is pinned at a limit, or the operator presses Stop. Once the
# band is bracketed (one IT fell short of it, another overshot) every step stays
# strictly inside the bracket, halving it when the additive step would leave it,
# and the walk gives up when the bracket is narrower than IT_RESOLUTION_MS.
IT_RESOLUTION_MS = 0.001
# Ceiling when neither the config nor the spectrometer (max_it_ms) states one.
FALLBACK_IT_MAX_MS = 60000.0


@dataclass(frozen=True)
class MeasurementOrchestratorConfig:
    default_start_it: Mapping[str, float]
    target_low: float
    target_high: float
    target_mid: float
    it_min: float
    # Highest auto-IT integration time in ms; None uses the spectrometer's own
    # max_it_ms. When both are known the lower one applies.
    it_max: Optional[float]
    sat_thresh: float
    n_sig: int
    n_dark: int
    n_sig_640: int
    n_dark_640: int
    # Largest auto-IT change per unsaturated frame, in ms, when raising / lowering
    # the integration time (SpectroApp.IT_STEP_UP / IT_STEP_DOWN).
    it_step_up: float = 0.3
    it_step_down: float = 0.1
    # Optional cap on auto-IT frames per source; None (the application's setting)
    # lets a weak source keep stepping all the way to the IT ceiling.
    max_it_adjust_iters: Optional[int] = None
    relay_channel_map: Mapping[str, int] = field(
        default_factory=lambda: {"517": 2, "532": 1, "Hg_Ar": 4}
    )
    # The application's source table, {tag: {"type", "channel", ...}}: which
    # controller and channel each tag is switched through. Empty = the bench's
    # historical routing (the laser controller's OBIS_MAP, relay_channel_map,
    # 377 on the CUBE).
    sources: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    nm640_integrations: Sequence[float] = (100.0, 500.0, 1000.0)
    source_delay_s: float = 0.2
    cube_stabilize_delay_s: float = 3.0
    dark_delay_s: float = 0.3
    nm640_warmup_delay_s: float = 1.0
    # Auto-IT: back off by this factor while the detector is saturated (a clipped
    # frame carries no amplitude information).
    saturation_backoff: float = 0.7
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
    # The setpoint for a tag in mW (OBIS lasers are sent watts, converted here).
    power_lookup: Callable[[str], float]
    auto_it_update: Callable[[str, np.ndarray, float, float], None] = lambda *_args, **_kwargs: None
    measurement_completed: Callable[[str], None] = lambda *_args, **_kwargs: None
    countdown: Callable[[int, str, str], None] = lambda *_args, **_kwargs: None
    error_handler: Callable[[str, Exception], None] = lambda *_args, **_kwargs: None
    # (tag, index, total) just before a source is measured: drives the progress bar.
    source_started: Callable[[str, int, int], None] = lambda *_args, **_kwargs: None
    # The ordered sequence the run will follow (640 nm last), before the first source.
    run_started: Callable[[Sequence[str]], None] = lambda *_args, **_kwargs: None
    # (tag, message) when a source's measurement raised; the run goes on with the next.
    source_failed: Callable[[str, str], None] = lambda *_args, **_kwargs: None
    # (tag, is_on) after a source was switched, and once everything was switched off:
    # the application mirrors them into its lit-source indicator.
    source_state_changed: Callable[[str, bool], None] = lambda *_args, **_kwargs: None
    all_sources_off: Callable[[], None] = lambda *_args, **_kwargs: None


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
            self.callbacks.run_started(list(sequence))
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
                    self.callbacks.source_failed(str(tag), str(exc))
                    self.callbacks.error_handler(f"Measurement {tag}", exc)
            if not continue_run():
                # Stop pressed inside the last source: it returned quietly, and
                # the run must not be reported as finished.
                result.stopped_early = True
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

    def _route(self, tag: str) -> tuple:
        """(controller kind, channel) for a tag: from the source table, else the historical bench."""
        config = self.config.sources.get(str(tag)) if self.config.sources else None
        if config is not None:
            kind = str(config.get("type", "")).upper()
            channel = config.get("channel")
            return kind, (int(channel) if channel is not None else None)
        obis_map = getattr(self.lasers, "OBIS_MAP", {})
        if tag in obis_map:
            return "OBIS", int(obis_map[tag])
        if tag == "377":
            return "CUBE", None
        if tag in self.config.relay_channel_map:
            return "RELAY", int(self.config.relay_channel_map[tag])
        raise RuntimeError(f"Unsupported measurement tag '{tag}'.")

    def _obis_channels(self) -> list:
        channels = {channel for kind, channel in (self._route(tag) for tag in self.config.sources) if kind == "OBIS"}
        return sorted(channels) or sorted(set(getattr(self.lasers, "OBIS_MAP", {}).values()))

    def _relay_numbers(self) -> list:
        relays = {channel for kind, channel in (self._route(tag) for tag in self.config.sources) if kind == "RELAY"}
        return sorted(relays | set(self.config.relay_channel_map.values()) | {1, 2, 3, 4})

    def _ensure_open(self, kind: str) -> None:
        if hasattr(self.lasers, "ensure_open"):
            self.lasers.ensure_open(kind)

    def _turn_all_sources_off(self) -> None:
        self._lit_source = None
        try:
            if hasattr(self.lasers, "all_off"):
                self.lasers.all_off(self._obis_channels(), self._relay_numbers())
                return

            for channel in self._obis_channels():
                try:
                    self.lasers.obis_off(channel)
                except Exception:
                    pass
            for relay_number in self._relay_numbers():
                try:
                    self.lasers.relay_off(relay_number)
                except Exception:
                    pass
            try:
                self.lasers.cube_off()
            except Exception:
                pass
        finally:
            self.callbacks.all_sources_off()

    def _cube_power_mw(self) -> float:
        return float(self.callbacks.power_lookup("377"))

    def _ensure_source_state(self, tag: str, turn_on: bool) -> None:
        kind, channel = self._route(tag)
        self._ensure_open(kind)

        if kind == "OBIS":
            if turn_on:
                self.lasers.obis_set_power(channel, float(self.callbacks.power_lookup(tag)) / 1000.0)
                self.lasers.obis_on(channel)
            else:
                self.lasers.obis_off(channel)
        elif kind == "CUBE":
            if turn_on:
                if self.config.cube_prearm and self._cube_armed and hasattr(self.lasers, "cube_emit"):
                    self.lasers.cube_emit()
                else:
                    self.lasers.cube_on(power_mw=self._cube_power_mw())
                    self._cube_armed = True
            else:
                self.lasers.cube_off()
        elif kind == "RELAY":
            if turn_on:
                if tag == "Hg_Ar":
                    self.callbacks.countdown(
                        45,
                        "Fiber Switch",
                        "Switch the fiber to Hg-Ar and press Enter to skip.",
                    )
                self.lasers.relay_on(channel)
            else:
                self.lasers.relay_off(channel)
        else:
            raise RuntimeError(f"Unsupported measurement tag '{tag}' (controller {kind}).")

        if turn_on:
            self._lit_source = tag
        elif self._lit_source == tag:
            self._lit_source = None
        self.callbacks.source_state_changed(tag, bool(turn_on))

    def _prearm_cube_if_next(self, next_tag: Optional[str]) -> None:
        """Bench option: send the CUBE's EXT/CW/P setup (no emission) during a dark capture."""
        if not self.config.cube_prearm or next_tag != "377" or self._cube_armed:
            return
        if not hasattr(self.lasers, "cube_arm"):
            return
        try:
            self._ensure_open("CUBE")
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

    def _it_ceiling(self) -> float:
        """Highest auto-IT integration time: config.it_max and the spectrometer's max_it_ms, whichever is lower."""
        limits = []
        for value in (self.config.it_max, getattr(self.spec, "max_it_ms", None)):
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            if np.isfinite(value) and value > 0.0:
                limits.append(value)
        return max(self.config.it_min, min(limits)) if limits else FALLBACK_IT_MAX_MS

    def _clamp_it(self, it_ms: float) -> float:
        return max(self.config.it_min, min(self._it_ceiling(), float(it_ms)))

    def _source_setting(self, tag: str, key: str) -> Optional[float]:
        """A positive per-source auto-IT setting from the source table (start_it_ms, it_step_ms), or None."""
        config = self.config.sources.get(str(tag)) if self.config.sources else None
        if not config:
            return None
        try:
            value = float(config.get(key) or 0.0)
        except (TypeError, ValueError):
            return None
        return value if value > 0.0 else None

    def _it_step_ms(self, peak: float, tag: str = "") -> float:
        """Signed additive step toward the target band for an unsaturated frame (see IT_STEP_*).

        A source with its own ``it_step_ms`` (set by the operator in
        Measurements) uses it as the largest step in both directions.
        """
        cfg = self.config
        step = self._source_setting(tag, "it_step_ms")
        step_up = step if step is not None else cfg.it_step_up
        step_down = step if step is not None else cfg.it_step_down
        error = cfg.target_mid - peak
        magnitude = max(IT_STEP_FLOOR_MS, abs(error) / IT_STEP_COUNTS_PER_MS)
        if error > 0:
            return min(step_up, magnitude)
        return -min(step_down, magnitude)

    def _auto_adjust_it(
        self, start_it: float, tag: str, should_continue: Callable[[], bool]
    ) -> tuple[float, float, str]:
        """Step the integration time until the peak sits inside the target band.

        Additive stepping, as in v3.3: every unsaturated frame moves the
        integration time by at most ``it_step_up`` up or ``it_step_down`` down
        (``_it_step_ms``), so the operator watches the exposure creep toward the
        band one frame at a time. A saturated frame, which carries no amplitude
        information, backs off by ``saturation_backoff``.

        Versions 3.4.0.0 to 3.4.2.0 jumped straight to ``it * target_mid / peak``
        instead. That assumes counts scale through zero; on a detector with a
        dark offset the first frame can be mostly offset (about 750 counts at
        2.4 ms on a Hamamatsu head) and the jump sent the exposure to about
        200 ms in one step.

        Up to 3.4.4.0 the walk also stopped after 1000 frames, which left a dim
        source no more than about 300 ms above its start IT. Now there is no
        frame budget unless ``max_it_adjust_iters`` sets one: the walk ends in
        the band, pinned at ``it_min`` or the IT ceiling (``_it_ceiling``), when
        the band is narrower than IT_RESOLUTION_MS, or on Stop.

        Returns the integration time of the last frame, its peak, and why the
        walk ended outside the band ("" when it reached the band or was stopped).
        """
        cfg = self.config
        it_ms = self._clamp_it(start_it)
        measured_it = it_ms
        peak = float("nan")
        self.it_history.clear()
        nonfinite_frames = 0
        # The band's bracket: the highest IT whose peak fell short of it and the
        # lowest whose peak overshot it (saturated or above target_high).
        short_it: Optional[float] = None
        over_it: Optional[float] = None
        budget = cfg.max_it_adjust_iters
        frames = range(budget + 1) if budget is not None and budget > 0 else itertools.count()

        for _ in frames:
            if not should_continue():
                return measured_it, peak, ""
            measured_it = it_ms
            counts = self._capture_counts(it_ms, 1)
            peak = float(np.nanmax(counts))
            if not np.isfinite(peak):
                nonfinite_frames += 1
                if nonfinite_frames >= cfg.max_nonfinite_frames:
                    raise RuntimeError(f"spectrometer returned {nonfinite_frames} non-finite frames during auto-IT")
                continue
            nonfinite_frames = 0

            self.it_history.append((it_ms, peak))
            self.callbacks.auto_it_update(tag, counts, it_ms, peak)

            if peak >= cfg.sat_thresh:
                if it_ms <= cfg.it_min * (1.0 + 1e-9):
                    raise RuntimeError(
                        f"detector saturates even at the minimum integration time ({cfg.it_min} ms); reduce the source power"
                    )
                proposed = self._clamp_it(it_ms * cfg.saturation_backoff)
            elif cfg.target_low <= peak <= cfg.target_high:
                return it_ms, peak, ""
            else:
                proposed = self._clamp_it(it_ms + self._it_step_ms(peak, tag))

            if peak < cfg.target_low:
                short_it = it_ms if short_it is None else max(short_it, it_ms)
            else:
                over_it = it_ms if over_it is None else min(over_it, it_ms)
            if short_it is not None and over_it is not None:
                if over_it - short_it <= IT_RESOLUTION_MS:
                    return it_ms, peak, (
                        f"the peak jumps from below the band at {short_it:.3f} ms to above it at {over_it:.3f} ms; "
                        "reduce the source power"
                    )
                if not short_it < proposed < over_it:
                    # Without a frame budget, stepping across the band and back could repeat forever.
                    proposed = 0.5 * (short_it + over_it)

            if proposed == it_ms:
                # Pinned against it_min or the ceiling: no further progress is possible.
                if peak < cfg.target_low:
                    return it_ms, peak, (
                        f"at the maximum integration time of {it_ms:g} ms the peak is only {peak:.0f} counts; "
                        "the source is too weak for this band, so raise its power or lower the target peak"
                    )
                return it_ms, peak, (
                    f"even the minimum integration time of {it_ms:g} ms gives {peak:.0f} counts, above the band; "
                    "reduce the source power"
                )
            it_ms = proposed

        return measured_it, peak, (
            f"the limit of {budget} auto-IT frames ran out; the last frame at {measured_it:.2f} ms peaked at {peak:.0f}. "
            "Enter a start integration time closer to the band or adjust the source power"
        )

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

        # The operator's per-source start wins; then a run-wide override; then the bench default.
        start_it = self._source_setting(tag, "start_it_ms")
        if start_it is None:
            start_it = (
                float(start_it_override)
                if start_it_override is not None
                else float(self.config.default_start_it.get(tag, self.config.default_start_it["default"]))
            )
        it_ms, peak, shortfall = self._auto_adjust_it(start_it, tag, should_continue)

        if not (self.config.target_low <= peak <= self.config.target_high):
            self._ensure_source_state(tag, False)
            if not should_continue():
                return False
            raise RuntimeError(
                f"auto-IT did not reach the target band ({self.config.target_low:g}-{self.config.target_high:g} counts) "
                f"after {len(self.it_history)} frames: {shortfall}."
            )

        signal = self._capture_counts(it_ms, self.config.n_sig)
        self._ensure_source_state(tag, False)

        if not should_continue():
            return False

        self.sleep(self.config.dark_delay_s)
        if not should_continue():
            # A Stop during the delay would otherwise be cleared by the next
            # measure() and the whole dark capture would run with Stop greyed out.
            return False
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
            if not should_continue():
                return measurements_recorded

            for it_ms in self.config.nm640_integrations:
                if not should_continue():
                    return measurements_recorded
                dark = self._capture_counts(float(it_ms), self.config.n_dark_640)
                self.data.append_measurement("640_dark", float(it_ms), self.config.n_dark_640, dark)
        finally:
            self._ensure_source_state("640", False)

        self.callbacks.measurement_completed("640")
        return measurements_recorded
