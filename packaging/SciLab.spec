# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for SciLab.

One-*folder* build on purpose. A one-file build unpacks the whole bundle to a
temp directory on every launch, which costs seconds here (numpy, scipy,
pandas, matplotlib and Tk), and -- worse for this app -- it hands the vendor
DLLs a new path each run, which some USB drivers cache badly. A folder also
lets a support engineer look at, or replace, a single vendor DLL in place.

Build with ``python packaging/build.py`` so the version resource is generated
from ``scilab_version.py`` first; ``pyinstaller packaging/SciLab.spec`` also
works if that file is already present.
"""

import sys
from pathlib import Path

# __file__ is not defined while PyInstaller execs a spec, so derive the root
# from the spec path it puts in sys.argv, falling back to the CWD.
SPEC_PATH = Path(globals().get("SPECPATH", Path.cwd()))
ROOT = SPEC_PATH.parent if SPEC_PATH.name == "packaging" else SPEC_PATH
if not (ROOT / "main.py").is_file():          # invoked from the repo root
    ROOT = Path.cwd()

sys.path.insert(0, str(ROOT))
from scilab_version import EXE_NAME  # noqa: E402

VERSION_FILE = ROOT / "packaging" / "file_version_info.txt"

# --- Data files -------------------------------------------------------------
# Paths are kept identical to the source tree so the same relative lookups work
# frozen and unfrozen (get_resource_path / spectrometer_loader.DLLS_DIR).
datas = [
    (str(ROOT / "sciglob_logoRGB.png"), "."),
    (str(ROOT / "sciglob_symbol.ico"), "."),
    (str(ROOT / "packaging" / "scilab.ico"), "packaging"),
    # Vendor DLLs: Avantes, Hamamatsu DcIcUSB / HiasApi / DCamUSB, the C13016
    # .NET assembly and the JETI SDK (jeti_spectro_ex64 + jeti_core64).
    # suggest_default_dll_path() looks here by name.
    (str(ROOT / "spectrometers dll files"), "spectrometers dll files"),
    # Reference line-shape files the Measurements tab can load for comparison.
    (str(ROOT / "Pandora Reference LSF"), "Pandora Reference LSF"),
]

# --- Hidden imports ---------------------------------------------------------
# Every backend is loaded through importlib.import_module(), so the analysis
# cannot see them. Missing one means "No spectrometer detected" on a bench that
# has the instrument plugged in -- the worst possible failure for this app.
hiddenimports = [
    "spectrometers.ava1_spectrometer",
    "spectrometers.hama2_spectrometer",
    "spectrometers.hama3_spectrometer",
    "spectrometers.hama4_spectrometer",
    "spectrometers.hama5_spectrometer",
    "spectrometers.hama6_spectrometer",
    "spectrometers.minispec_spectrometer",
    "spectrometers.oceandirect_spectrometer",
    "spectrometers.jeti_spectrometer",
    "spectrometers.simulated_spectrometer",
    "spectrometers.spec_hama2",
    "spectrometers.spec_hama2.Hamamatsu_DCAMSDK4_v25056964.dcam",
    "spectrometers.spec_hama2.Hamamatsu_DCAMSDK4_v25056964.dcamapi4",
    "spec_xfus",
    # Tk + plotting backends, resolved by name at runtime.
    "matplotlib.backends.backend_tkagg",
    "PIL._tkinter_finder",
    # Serial port enumeration (Refresh Ports) is a submodule import.
    "serial.tools.list_ports",
    # Analysis stack: scipy submodules are imported lazily inside functions.
    "scipy.optimize",
    "scipy.signal",
    "scipy.special",
    "scipy.interpolate",
]

# Optional hardware stacks: include them when the build environment has them,
# but never fail the build over a backend this bench does not use.
for optional in ("clr", "pythonnet", "usb", "usb.core", "usb.backend.libusb1",
                 "libusb_package", "pymodbus", "pymodbus.client"):
    try:
        __import__(optional)
    except Exception:
        continue
    hiddenimports.append(optional)

binaries = []
try:  # libusb-1.0.dll lives inside libusb_package; PyInstaller needs it copied.
    import libusb_package

    for dll in Path(libusb_package.__file__).parent.glob("*.dll"):
        binaries.append((str(dll), "libusb_package"))
except Exception:
    pass

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Test/build-only machinery has no business in a shipped bundle.
    excludes=["pytest", "_pytest", "pyinstaller", "PyInstaller", "tkinter.test",
              "test", "unittest.test", "IPython", "notebook", "sphinx"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX-packed scientific DLLs trip antivirus heuristics
    console=False,      # GUI app: no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "packaging" / "scilab.ico"),
    version=str(VERSION_FILE) if VERSION_FILE.is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=EXE_NAME,
)
