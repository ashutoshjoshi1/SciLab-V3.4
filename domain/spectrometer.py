from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence, runtime_checkable


@runtime_checkable
class SpectrometerBackend(Protocol):
    sn: str
    npix_active: int
    rcm: Sequence[float]
    abort_on_saturation: bool

    def connect(self) -> str: ...

    def disconnect(self, *args: Any, **kwargs: Any) -> Any: ...

    def set_it(self, it_ms: float) -> Any: ...

    def measure(self, ncy: int = 1) -> Any: ...

    def wait_for_measurement(self) -> Any: ...


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
)
REQUIRED_BACKEND_ATTRIBUTES = ("sn", "npix_active", "rcm")


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

