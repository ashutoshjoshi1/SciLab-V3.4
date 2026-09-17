"""Runtime contract for spectrometer backends.

Every backend (vendor driver or the simulator) is duck-typed against this
contract. ``assert_spectrometer_backend`` is called by the loader before an
instance is handed to the rest of the app, so drift in a new backend fails
early with a readable message instead of an ``AttributeError`` mid-run.

Conventions shared by all backends:

* ``connect``/``set_it``/``measure``/``wait_for_measurement``/``abort`` return
  the string ``"OK"`` on success or an error description otherwise.
* ``measure(ncy)`` is non-blocking and ``wait_for_measurement(timeout)`` blocks
  until the cycles are handled; afterwards ``rcm`` holds the mean counts and
  ``rcs`` the per-pixel standard deviation (empty for a single cycle on some
  drivers).
* ``request_stop()`` is what the UI Stop button calls: it aborts the device
  and wakes any thread blocked in ``wait_for_measurement`` promptly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol, Sequence, runtime_checkable


@runtime_checkable
class SpectrometerBackend(Protocol):
    sn: str
    npix_active: int
    rcm: Sequence[float]
    rcs: Sequence[float]
    it_ms: Optional[float]
    eff_saturation_limit: float
    abort_on_saturation: bool

    def connect(self) -> str: ...

    def disconnect(self, *args: Any, **kwargs: Any) -> Any: ...

    def set_it(self, it_ms: float) -> Any: ...

    def measure(self, ncy: int = 1) -> Any: ...

    def wait_for_measurement(self, timeout: Optional[float] = None) -> Any: ...

    def abort(self, ignore_errors: bool = False) -> Any: ...

    def request_stop(self) -> Any: ...


@dataclass(frozen=True)
class SpectrometerInfo:
    backend_type: str
    serial_number: str
    pixel_count: int


REQUIRED_BACKEND_METHODS = (
    "connect",
    "disconnect",
    "set_it",
    "measure",
    "wait_for_measurement",
    "abort",
    "request_stop",
)
REQUIRED_BACKEND_ATTRIBUTES = (
    "sn",
    "npix_active",
    "rcm",
    "rcs",
    "it_ms",
    "eff_saturation_limit",
    "abort_on_saturation",
)
# Read by callers with getattr(..., default); not every driver defines them.
OPTIONAL_BACKEND_ATTRIBUTES = (
    "npix_blind_left",
    "npix_blind_right",
    "measuring",
    "simulation_mode",
)


def validate_spectrometer_backend(instance: object) -> list[str]:
    issues: list[str] = []

    if instance is None:
        return ["backend instance is None"]

    for attr in REQUIRED_BACKEND_ATTRIBUTES:
        if not hasattr(instance, attr):
            issues.append(f"missing attribute '{attr}'")

    for method in REQUIRED_BACKEND_METHODS:
        value = getattr(instance, method, None)
        if value is None or not callable(value):
            issues.append(f"missing callable '{method}()'")

    npix = getattr(instance, "npix_active", None)
    if npix is not None:
        try:
            if int(npix) <= 0:
                issues.append("npix_active must be > 0")
        except Exception:
            issues.append("npix_active must be an integer-like value")

    return issues


def assert_spectrometer_backend(instance: object) -> SpectrometerBackend:
    issues = validate_spectrometer_backend(instance)
    if issues:
        raise TypeError("Invalid spectrometer backend: " + "; ".join(issues))
    return instance  # type: ignore[return-value]


def describe_spectrometer(instance: SpectrometerBackend) -> SpectrometerInfo:
    backend_type = getattr(instance, "spec_type", type(instance).__name__)
    return SpectrometerInfo(
        backend_type=str(backend_type),
        serial_number=str(getattr(instance, "sn", "Unknown")),
        pixel_count=int(getattr(instance, "npix_active", 0)),
    )
