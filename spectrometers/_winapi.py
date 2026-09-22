"""``ctypes.windll`` that imports on every OS.

The vendor drivers do ``from ctypes import windll`` at module level, which
raises ``ImportError`` on macOS/Linux and made four of the five backends
un-importable on a development machine. Importing ``windll`` from here keeps
the drivers importable everywhere; off Windows the failure happens at the
first *use* (``LoadLibrary``) with a clear message instead of at import.
"""
from __future__ import annotations

import sys

try:
    from ctypes import windll  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - exercised only off Windows

    class _WindllUnavailable:
        """Stand-in for ``ctypes.windll`` on non-Windows platforms."""

        def LoadLibrary(self, path):  # noqa: N802 - mirrors the ctypes API
            raise OSError(
                f"Cannot load Windows DLL '{path}' on {sys.platform}; this spectrometer backend needs Windows."
            )

        def __getattr__(self, name):
            raise OSError(f"ctypes.windll.{name} is unavailable on {sys.platform}")

    windll = _WindllUnavailable()

__all__ = ["windll"]
