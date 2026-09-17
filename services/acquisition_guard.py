"""Single-owner token for the spectrometer handle.

Live View, Measurements, Check Resolution, Check Spectrometer, Apply-IT and
the EEPROM tools all drive the same device. Before this guard existed each
Start button checked one of the other tabs' flags at best, so two threads
could issue ``measure()`` on one handle. Every entry point now asks the guard
first and gets a consistent "X is running" message when refused.

The owner is a *label*, not a thread: Start acquires on the Tk thread and the
worker releases when its last frame has completed, which is why a plain lock
around the label is used rather than an ``RLock`` (whose ``release`` must be
called from the acquiring thread).
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator, Optional


class AcquisitionGuard:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owner: Optional[str] = None

    @property
    def owner(self) -> Optional[str]:
        with self._lock:
            return self._owner

    def try_acquire(self, owner: str) -> bool:
        """Claim the spectrometer for ``owner``. False if anyone holds it."""
        with self._lock:
            if self._owner is not None:
                return False
            self._owner = str(owner)
            return True

    def release(self, owner: Optional[str] = None) -> None:
        """Release if ``owner`` matches the current holder (``None`` forces)."""
        with self._lock:
            if owner is None or self._owner == owner:
                self._owner = None

    def busy_reason(self) -> Optional[str]:
        with self._lock:
            if self._owner is None:
                return None
            return f"{self._owner} is running. Stop it first."

    @contextmanager
    def hold(self, owner: str) -> Iterator[bool]:
        """Short critical section for Tk-thread operations (Apply IT, EEPROM).

        Yields True when the guard was acquired (and releases on exit), False
        when something else owns the spectrometer.
        """
        acquired = self.try_acquire(owner)
        try:
            yield acquired
        finally:
            if acquired:
                self.release(owner)
