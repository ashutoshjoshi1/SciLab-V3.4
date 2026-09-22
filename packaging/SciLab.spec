# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for SciLab.  Run through packaging/build.py, which first
regenerates packaging/file_version_info.txt from scilab_version.py."""

import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821  (SPECPATH is injected by PyInstaller)
sys.path.insert(0, str(ROOT))

from scilab_version import APP_NAME  # noqa: E402


def _datas():
    out = []
    for csv in (ROOT / "Pandora Reference LSF").glob("*.csv"):
        out.append((str(csv), "Pandora Reference LSF"))
    out.append((str(ROOT / "sciglob_logoRGB.png"), "."))
    out.append((str(ROOT / "sciglob_symbol.ico"), "."))
    out.append((str(ROOT / "packaging" / "scilab.ico"), "packaging"))
    # Vendor DLLs SciLab is allowed to ship (Avantes, Hamamatsu).  OceanDirect.dll
    # is deliberately NOT bundled: its EULA forbids redistribution, the app uses
    # the SDK installed on the target PC.
    for f in (ROOT / "spectrometers dll files").iterdir():
        if f.suffix.lower() in (".dll", ".conf"):
            out.append((str(f), "spectrometers dll files"))
    return out


hiddenimports = [
    "spectrometers",
    "spectrometers.ava1_spectrometer",
    "spectrometers.hama2_spectrometer",
    "spectrometers.hama3_spectrometer",
    "spectrometers.hama4_spectrometer",
    "spectrometers.minispec_spectrometer",
    "spectrometers.oceandirect_spectrometer",
    "spectrometers.spec_xfus",
    "spectrometers.spec_hama2",
    "spectrometers.spec_hama2.Hamamatsu_DCAMSDK4_v25056964.dcam",
    "spectrometers.spec_hama2.Hamamatsu_DCAMSDK4_v25056964.dcamapi4",
    "spec_xfus",
    "dcamapi4",
    "matplotlib.backends.backend_tkagg",
    "PIL._tkinter_finder",
    "serial.tools.list_ports",
    "scipy.optimize",
    "scipy.signal",
    "scipy.special",
    "scipy.interpolate",
    "pymodbus",
    "pymodbus.client",
]

excludes = [
    "pytest",
    "_pytest",
    "PyInstaller",
    "pyinstaller",
    "tkinter.test",
    "test",
    "unittest.test",
    "IPython",
    "notebook",
    "sphinx",
]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=_datas(),
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(ROOT / "packaging" / "scilab.ico")],
    version=str(ROOT / "packaging" / "file_version_info.txt"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
