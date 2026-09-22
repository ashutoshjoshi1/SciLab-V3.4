"""Single source of truth for the SciLab version.

Bump ``__version__`` here; ``packaging/build.py`` reads it to stamp SciLab.exe
(Windows version resource) and the installer (Inno Setup AppVersion / file name).
The splash screen shows it too.
"""

__version__ = "3.4.1.0"

APP_NAME = "SciLab"
COMPANY = "SciGlob"
DESCRIPTION = "SciLab Spectrometer Characterization System"


def version_tuple() -> tuple:
    parts = [int(p) for p in __version__.split(".")]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])
