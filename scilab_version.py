"""Single source of truth for the application's identity and version.

Everything that needs to state a version reads it from here: the title strip,
the splash, the frozen executable's Windows version resource, and the Inno
Setup installer. Bump ``VERSION`` and rebuild -- nothing else needs editing.

The name is deliberately not ``version.py``: the application directory sits
first on ``sys.path``, so a module called ``version`` would shadow any
same-named module a dependency imports.
"""

from __future__ import annotations

# Windows file/product versions are four numeric fields, so the app uses the
# same shape everywhere rather than carrying two different version strings.
VERSION_TUPLE = (3, 4, 1, 0)
VERSION = ".".join(str(part) for part in VERSION_TUPLE)
DISPLAY_VERSION = f"v{VERSION}"

APP_NAME = "SciLab"
PRODUCT_NAME = "SciLab Spectrometer Characterization System"
PUBLISHER = "SciGlob"
PUBLISHER_URL = "https://sciglob.com"
COPYRIGHT = "Copyright (C) SciGlob"

# Stable across releases: Inno Setup uses it to recognise an installed copy and
# upgrade it in place instead of stacking a second entry in Apps & features.
# Never change this for an upgrade of the same product.
APP_ID = "{{8F3B6A21-5C4D-4E7A-9B0F-3D6E1A7C24B5}"

# Windows executable name (also the Start-menu target and the AppMutex base).
EXE_NAME = "SciLab"
