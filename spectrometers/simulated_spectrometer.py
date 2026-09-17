"""Simulated spectrometer and light-source bench.

Lets the whole application (Live View, Measurements, Check Resolution,
analysis) run with zero hardware, and gives the test-suite a deterministic
backend that honours the same duck-typed contract as the vendor drivers
(see ``domain/spectrometer.py``).

The optical model is deliberately simple but realistic enough for the
analysis pipeline: super-Gaussian emission lines at fixed wavelengths on a
linear dispersion, counts that scale linearly with integration time and
clip at the 16-bit ADC limit, a dark level with fixed-pattern noise, and
read + shot noise. ``SimulatedLaserController`` mirrors the method surface
of ``hardware.controllers.LaserController`` and simply tells the shared
``SimulatedBench`` which source is lit.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

LOGGER = logging.getLogger(__name__)

ADC_SATURATION = 2 ** 16 - 1


# ---------------------------------------------------------------------------
# Bench: shared "which source is on" state
# ---------------------------------------------------------------------------


class SimulatedBench:
    """Which light sources are on, and at what relative power. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._on: Dict[str, float] = {}

    def set_source(self, tag: str, on: bool, power_factor: float = 1.0) -> None:
        with self._lock:
            if on:
                self._on[str(tag)] = float(power_factor)
            else:
                self._on.pop(str(tag), None)

    def all_off(self) -> None:
        with self._lock:
            self._on.clear()

    def active(self) -> Dict[str, float]:
        """Snapshot copy of ``{tag: power_factor}`` for the lit sources."""
        with self._lock:
            return dict(self._on)


DEFAULT_BENCH = SimulatedBench()


# ---------------------------------------------------------------------------
# Optical model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmissionLine:
    tag: str                  # source that lights it: "377".."685" or "Hg_Ar"
    wavelength_nm: float      # emission wavelength (matches CharacterizationConfig.laser_reference_map)
    counts_per_ms: float      # peak counts per ms of integration at power factor 1.0
    fwhm_px: float = 6.0
    shape_n: float = 2.2      # super-Gaussian exponent; 2.0 is a Gaussian


DEFAULT_LINES: Tuple[EmissionLine, ...] = (
    EmissionLine("377", 375.00, 2400.0),
    EmissionLine("405", 403.46, 520.0),
    EmissionLine("445", 445.00, 930.0),
    EmissionLine("488", 488.00, 4300.0),
    EmissionLine("517", 517.00, 800.0),
    EmissionLine("532", 532.00, 1500.0),
    EmissionLine("640", 640.00, 500.0),   # 100 ms unsaturated, 500/1000 ms saturated: like the real 640 stray-light run
    EmissionLine("685", 685.00, 900.0),
    # Hg-Ar lamp lines that land on-sensor with the default dispersion.
    EmissionLine("Hg_Ar", 404.66, 400.0, 5.0, 2.0),
    EmissionLine("Hg_Ar", 407.78, 60.0, 5.0, 2.0),
    EmissionLine("Hg_Ar", 435.84, 300.0, 5.0, 2.0),
    EmissionLine("Hg_Ar", 507.30, 40.0, 5.0, 2.0),
    EmissionLine("Hg_Ar", 546.08, 250.0, 5.0, 2.0),
)


# ---------------------------------------------------------------------------
# Laser controller stand-in
# ---------------------------------------------------------------------------


class _NullSerial:
    """Stands in for ``SerialDevice`` so shutdown/configuration code can run unchanged."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.port = ""
        self.is_open = False

    def configure(self, port: Optional[str] = None, baudrate: Optional[int] = None, **_kwargs) -> None:
        if port is not None:
            self.port = port

    def open(self) -> bool:
        self.is_open = True
        return True

    def close(self) -> None:
        self.is_open = False


class SimulatedLaserController:
    """Same method surface as ``hardware.controllers.LaserController``; drives a ``SimulatedBench``."""

    OBIS_MAP = {"405": 5, "445": 4, "488": 3, "640": 2, "685": 6}
    RELAY_MAP = {2: "517", 1: "532", 4: "Hg_Ar"}

    def __init__(self, bench: Optional[SimulatedBench] = None) -> None:
        self.bench = bench or DEFAULT_BENCH
        self.obis = _NullSerial("OBIS")
        self.cube = _NullSerial("CUBE")
        self.relay = _NullSerial("RELAY")
        self.log: List[Tuple[str, ...]] = []   # every command issued, for assertions in tests
        self._tag_by_channel = {channel: tag for tag, channel in self.OBIS_MAP.items()}

    # -- port management (no-ops) ------------------------------------------------

    def configure_ports(self, com_ports: Mapping[str, str]) -> None:
        self.obis.configure(port=com_ports.get("OBIS", ""))
        self.cube.configure(port=com_ports.get("CUBE", ""))
        self.relay.configure(port=com_ports.get("RELAY", ""))

    def open_all(self) -> None:
        for device in (self.obis, self.cube, self.relay):
            device.open()

    def ensure_open_for_tag(self, tag: str) -> None:
        return None

    # -- OBIS ---------------------------------------------------------------------

    def obis_set_power(self, channel: int, power_watts: float) -> None:
        self.log.append(("obis_set_power", str(channel), f"{float(power_watts):.4f}"))

    def obis_on(self, channel: int) -> None:
        tag = self._tag_by_channel.get(int(channel))
        self.log.append(("obis_on", str(channel)))
        if tag is not None:
            self.bench.set_source(tag, True)

    def obis_off(self, channel: int) -> None:
        tag = self._tag_by_channel.get(int(channel))
        self.log.append(("obis_off", str(channel)))
        if tag is not None:
            self.bench.set_source(tag, False)

    # -- CUBE (377 nm) -------------------------------------------------------------

    def cube_on(self, power_mw: float = 12.0) -> None:
        self.log.append(("cube_on", f"{float(power_mw):.3f}"))
        self.bench.set_source("377", True)

    def cube_off(self) -> None:
        self.log.append(("cube_off",))
        self.bench.set_source("377", False)

    # -- Relays ------------------------------------------------------------------

    def relay_on(self, relay_number: int) -> None:
        tag = self.RELAY_MAP.get(int(relay_number))
        self.log.append(("relay_on", str(relay_number)))
        if tag is not None:
            self.bench.set_source(tag, True)

    def relay_off(self, relay_number: int) -> None:
        tag = self.RELAY_MAP.get(int(relay_number))
        self.log.append(("relay_off", str(relay_number)))
        if tag is not None:
            self.bench.set_source(tag, False)

    def all_off(self) -> List[str]:
        self.log.append(("all_off",))
        self.bench.all_off()
        return []


# ---------------------------------------------------------------------------
# Spectrometer
# ---------------------------------------------------------------------------


class SimulatedSpectrometer:
    """Hardware-free backend implementing the full spectrometer contract."""

    spec_type = "Simulated"

    def __init__(
        self,
        *,
        bench: Optional[SimulatedBench] = None,
        npix: int = 2048,
        dispersion_nm_per_px: float = 0.1555,
        lambda0_nm: float = 369.0,
        lines: Sequence[EmissionLine] = DEFAULT_LINES,
        dark_offset: float = 600.0,
        dark_rate_per_ms: float = 0.4,
        read_noise: float = 12.0,
        fixed_pattern_sigma: float = 20.0,
        time_scale: float = 1.0,
        seed: Optional[int] = None,
        sn: str = "SIM-0001",
    ) -> None:
        # --- contract attributes (same names as the vendor drivers) ---
        self.sn = sn
        self.name = "Simulated spectrometer"
        self.alias = "1"
        self.debug_mode = 0
        self.npix_active = int(npix)
        self.npix_blind_left = 0
        self.npix_blind_right = 0
        self.nbits = 16
        self.eff_saturation_limit = float(ADC_SATURATION)
        self.abort_on_saturation = True
        self.simulation_mode = True
        self.measuring = False
        self.it_ms: Optional[float] = None
        self.error = "OK"
        self.rcm = np.array([], dtype=float)
        self.rcs = np.array([], dtype=float)
        self.ncy_requested = 0
        self.ncy_saturated = 0

        # --- test instrumentation ---
        self.measure_calls = 0
        self.set_it_calls = 0
        self.frame_log: List[Tuple[float, int, Tuple[str, ...]]] = []   # (it_ms, ncy, lit tags)
        self.inject_error: Dict[str, str] = {}                          # {"set_it": "boom"} -> next call returns it

        # --- model ---
        self._bench = bench or DEFAULT_BENCH
        self.dispersion_nm_per_px = float(dispersion_nm_per_px)
        self.lambda0_nm = float(lambda0_nm)
        self.lines = tuple(lines)
        self.dark_offset = float(dark_offset)
        self.dark_rate_per_ms = float(dark_rate_per_ms)
        self.read_noise = float(read_noise)
        self.time_scale = float(time_scale)
        self._rng = np.random.default_rng(seed)
        self._px = np.arange(self.npix_active, dtype=float)
        self._fixed_pattern = self._rng.normal(0.0, float(fixed_pattern_sigma), self.npix_active)

        # --- state ---
        self._connected = False
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._done.set()
        self._aborted = False
        self._stop_requested = False
        self._timer: Optional[threading.Timer] = None
        self.logger = LOGGER

    # -- helpers ------------------------------------------------------------------

    @property
    def bench(self) -> SimulatedBench:
        return self._bench

    def set_active_source(self, tag: Optional[str]) -> None:
        """Convenience for manual runs: light exactly one source (or none)."""
        self._bench.all_off()
        if tag:
            self._bench.set_source(tag, True)

    def pixel_of(self, wavelength_nm: float) -> float:
        return (float(wavelength_nm) - self.lambda0_nm) / self.dispersion_nm_per_px

    def initialize_spec_logger(self) -> None:
        self.logger = logging.getLogger(f"spec{self.alias}.simulated")

    def _take_injected_error(self, key: str) -> Optional[str]:
        return self.inject_error.pop(key, None)

    # -- contract: connection ------------------------------------------------------

    def connect(self) -> str:
        injected = self._take_injected_error("connect")
        if injected:
            self.error = injected
            return injected
        self._connected = True
        if self.it_ms is None:
            self.it_ms = 2.4
        self.error = "OK"
        return "OK"

    def disconnect(self, *args, **kwargs) -> str:
        self.abort(ignore_errors=True)
        self._connected = False
        return "OK"

    # -- contract: acquisition -----------------------------------------------------

    def set_it(self, it_ms: float) -> str:
        self.set_it_calls += 1
        injected = self._take_injected_error("set_it")
        if injected:
            self.error = injected
            return injected
        if not self._connected:
            self.error = "Spectrometer not connected"
            return self.error
        try:
            value = float(it_ms)
        except (TypeError, ValueError):
            self.error = f"Invalid integration time '{it_ms}'"
            return self.error
        self.it_ms = min(60000.0, max(0.001, value))
        self.error = "OK"
        return "OK"

    def measure(self, ncy: int = 1) -> str:
        injected = self._take_injected_error("measure")
        if injected:
            self.error = injected
            return injected
        if not self._connected:
            self.error = "Spectrometer not connected"
            return self.error
        if self.it_ms is None:
            self.error = "Integration time not set"
            return self.error
        with self._lock:
            if self.measuring:
                return "Measurement already in progress"
            self.measuring = True
            self._aborted = False
            self._stop_requested = False
            self.error = "OK"
            self.ncy_requested = max(1, int(ncy))
            self.measure_calls += 1
            self._done.clear()

        sources = self._bench.active()   # light is integrated from exposure start
        delay_s = self.ncy_requested * float(self.it_ms) / 1000.0 * self.time_scale
        if delay_s <= 0.0:
            self._complete(sources)
        else:
            self._timer = threading.Timer(delay_s, self._complete, args=(sources,))
            self._timer.daemon = True
            self._timer.start()
        return "OK"

    def wait_for_measurement(self, timeout: Optional[float] = None) -> str:
        if timeout is None:
            expected = self.ncy_requested * float(self.it_ms or 0.0) / 1000.0 * self.time_scale
            timeout = 2.0 * expected + 10.0
        if not self._done.wait(timeout):
            with self._lock:
                self.error = f"Timeout after {timeout:.1f} s waiting for simulated measurement"
                self.measuring = False
                self._done.set()
        return self.error

    def abort(self, ignore_errors: bool = False) -> str:
        with self._lock:
            timer, self._timer = self._timer, None
            was_measuring = self.measuring
            self._aborted = True
            self.measuring = False
            if was_measuring:
                self.error = "Measurement aborted"
            self._done.set()
        if timer is not None:
            timer.cancel()
        return "OK"

    def request_stop(self) -> str:
        self._stop_requested = True
        return self.abort(ignore_errors=True)

    # -- model ---------------------------------------------------------------------

    def expected_frame(self, it_ms: float, sources: Mapping[str, float]) -> Tuple[np.ndarray, np.ndarray]:
        """Return (mean counts, per-pixel sigma) for one frame at ``it_ms``."""
        signal = np.zeros(self.npix_active)
        for line in self.lines:
            factor = sources.get(line.tag)
            if factor is None:
                continue
            center = self.pixel_of(line.wavelength_nm)
            if not (-60.0 < center < self.npix_active + 60.0):
                continue
            # FWHM = 2 w (ln 2)^(1/n): the same relation analysis.calculations.slit_func uses.
            width = line.fwhm_px / (2.0 * np.log(2.0) ** (1.0 / line.shape_n))
            signal += line.counts_per_ms * factor * it_ms * np.exp(-np.abs((self._px - center) / width) ** line.shape_n)
        dark = self.dark_offset + self.dark_rate_per_ms * it_ms + self._fixed_pattern
        sigma = np.sqrt(self.read_noise ** 2 + np.maximum(signal, 0.0))   # read noise + shot noise
        return dark + signal, sigma

    def _complete(self, sources: Mapping[str, float]) -> None:
        it_ms = float(self.it_ms)
        ncy = self.ncy_requested
        mean, sigma = self.expected_frame(it_ms, sources)
        frames = mean[None, :] + self._rng.standard_normal((ncy, self.npix_active)) * sigma[None, :]
        frames = np.clip(np.rint(frames), 0.0, self.eff_saturation_limit)
        saturated = frames.max(axis=1) >= self.eff_saturation_limit
        if self.abort_on_saturation and saturated.any():
            frames = frames[: int(np.argmax(saturated)) + 1]   # stop after the first saturated cycle
        with self._lock:
            if self._aborted:
                return
            self.rcm = frames.mean(axis=0)
            self.rcs = frames.std(axis=0, ddof=1) if frames.shape[0] > 1 else np.zeros(self.npix_active)
            self.ncy_saturated = int(saturated.sum())
            self.frame_log.append((it_ms, ncy, tuple(sorted(sources))))
            self.measuring = False
            self.error = "OK"
            self._done.set()
