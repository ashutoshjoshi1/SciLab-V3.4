# Third-party notices

SciLab is licensed by SciGlob Research LLC under the terms in [LICENSE](LICENSE).
The installer also carries software that belongs to others. Each of those
components is licensed by its owner under its own terms, listed below, and not
under the SciLab licence. Nothing in the SciLab licence limits the rights these
licences give you (see section 5 of LICENSE).

Versions are those of the v3.4.4.0 build environment (`.venv-build`, Python
3.12.10, PyInstaller 6.22.3). Update this file whenever `requirements.txt`, the
build environment or the files in `spectrometers dll files/` change.

## Instrument vendor libraries

These ship in `spectrometers dll files/` and are loaded only to operate the
vendor's own instruments.

| File | Owner | Used by | Terms |
|---|---|---|---|
| `avaspecx64.dll` | Avantes | Ava1 | Proprietary. Redistribution terms are not recorded in this repository. |
| `DcIcUSB.dll` | Hamamatsu Photonics | Hama3 | Proprietary. Redistribution terms are not recorded in this repository. |
| `HiasApi.dll`, `hias.conf` | Hamamatsu Photonics | Hama4 | Proprietary. Redistribution terms are not recorded in this repository. |
| `DCamUSB.dll` | Hamamatsu Photonics | MiniSpec | Proprietary. Redistribution terms are not recorded in this repository. |
| `MICRO_USB2_CLR.dll` | Hamamatsu Photonics | Hama6 | Proprietary. Redistribution terms are not recorded in this repository. |
| `jeti_spectro_ex64.dll`, `jeti_core64.dll` (JETI SDK 4.8.10) | JETI Technische Instrumente GmbH | Jeti1 | JETI SDK end-user licence, shipped as `spectrometers dll files/JETI_SDK_license.txt`. It permits integration and distribution with SciLab and forbids reverse engineering, modification, sale and sublicensing of the SDK. |

### Not distributed with SciLab

Install these from the vendor. SciLab finds them in place.

| Library | Owner | Used by | Why it is not bundled |
|---|---|---|---|
| OceanDirect SDK (`OceanDirect.dll`) | Ocean Insight | OcOpt1 | Its EULA allows internal use only and forbids redistribution. |
| DCAM-API runtime (`dcamapi.dll`) | Hamamatsu Photonics | Hama2 | Installed by Hamamatsu's DCAM-API installer. |

## Source code with outside authorship

| Files | Origin |
|---|---|
| `spectrometers/spec_hama2/Hamamatsu_DCAMSDK4_v25056964/` (`dcamapi4.py`, `dcam.py`) | Python bindings from the Hamamatsu DCAM-SDK4 samples (dated 2025-04-16), adapted for SciLab. Subject to the DCAM-SDK licence terms. |
| `spectrometers/ava1_spectrometer.py`, `spectrometers/hama2_spectrometer.py` | Headers state they were written by Daniel Santana as spectrometer control libraries for BlickO. |
| `spectrometers/hama3_spectrometer.py`, `spectrometers/spec_xfus.py` | Same BlickO driver family and its reduced `xfus` helper library. |

## Python runtime and packages

| Component | Version | Licence | Project |
|---|---|---|---|
| CPython | 3.12.10 | PSF-2.0 | <https://www.python.org> |
| Tcl/Tk (Tkinter) | 8.6 | Tcl/Tk licence (BSD-style) | <https://www.tcl-lang.org> |
| NumPy | 2.5.3 | BSD-3-Clause, with bundled parts under 0BSD, MIT, Zlib and CC0-1.0 | <https://numpy.org> |
| SciPy | 1.18.1 | BSD-3-Clause, with bundled parts under their own licences | <https://scipy.org> |
| pandas | 3.0.6 | BSD-3-Clause | <https://pandas.pydata.org> |
| Matplotlib | 3.11.2 | Matplotlib licence (PSF-based) | <https://matplotlib.org> |
| Pillow | 12.3.0 | MIT-CMU | <https://python-pillow.github.io> |
| contourpy | 1.4.0 | BSD-3-Clause | <https://github.com/contourpy/contourpy> |
| kiwisolver | 1.5.1 | BSD-3-Clause | <https://github.com/nucleic/kiwi> |
| cycler | 0.12.1 | BSD-3-Clause | <https://matplotlib.org/cycler/> |
| fontTools | 4.65.0 | MIT | <https://github.com/fonttools/fonttools> |
| pyparsing | 3.3.3 | MIT | <https://github.com/pyparsing/pyparsing> |
| python-dateutil | 2.9.0.post0 | Apache-2.0 and BSD-3-Clause (dual) | <https://github.com/dateutil/dateutil> |
| six | 1.17.0 | MIT | <https://github.com/benjaminp/six> |
| tzdata | 2026.4 | Apache-2.0 | <https://github.com/python/tzdata> |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | <https://github.com/pypa/packaging> |
| setuptools | 84.0.0 | MIT | <https://github.com/pypa/setuptools> |
| pySerial | 3.5 | BSD-3-Clause | <https://github.com/pyserial/pyserial> |
| PyModbus | 3.15.0 | BSD-3-Clause | <https://github.com/pymodbus-dev/pymodbus> |
| PyUSB | 1.3.1 | BSD-3-Clause | <https://github.com/pyusb/pyusb> |
| libusb-package | 1.0.30.0 | Apache-2.0 | <https://github.com/pyocd/libusb-package> |
| libusb (`libusb-1.0.dll`) | as shipped in libusb-package 1.0.30.0 | LGPL-2.1-or-later (see below) | <https://libusb.info> |
| Python.NET (pythonnet) | 3.1.0 | MIT | <https://github.com/pythonnet/pythonnet> |
| clr-loader | 0.3.1 | MIT | <https://github.com/pythonnet/clr-loader> |
| cffi | 2.1.1 | MIT-0 | <https://github.com/python-cffi/cffi> |
| pycparser | 3.0 | BSD-3-Clause | <https://github.com/eliben/pycparser> |

## Native runtime libraries

| Component | Files | Licence |
|---|---|---|
| OpenBLAS, inside NumPy and SciPy | `numpy.libs/libscipy_openblas64_*.dll`, `scipy.libs/libscipy_openblas*.dll` | BSD-3-Clause |
| GCC runtime library, inside the OpenBLAS builds | as above | GPL-3.0-or-later WITH GCC-exception-3.1 |
| OpenSSL 3 | `libssl-3.dll`, `libcrypto-3.dll` | Apache-2.0 |
| libffi | `libffi-8.dll` | MIT |
| SQLite | `sqlite3.dll` | Public domain |
| zlib | `zlib1.dll` | Zlib |
| Microsoft Visual C++ runtime | `VCRUNTIME140.dll`, `VCRUNTIME140_1.dll`, `MSVCP140*.dll` | Microsoft Visual C++ Redistributable licence terms |
| PyInstaller bootloader | `SciLab.exe` launcher | GPL-2.0-or-later with the PyInstaller bootloader exception, which allows the bundled application to be distributed under its own licence |

## libusb and the LGPL

`libusb-1.0.dll` is shipped unmodified as a separate, dynamically loaded file.
It is licensed under the GNU Lesser General Public License, version 2.1 or
later. You may replace it with your own compatible build of libusb. Its source
code is available at <https://github.com/libusb/libusb>. Section 5.2 of the
SciLab licence preserves the rights the LGPL gives you.

## Full licence texts

Each Python package carries its full licence text in its `*.dist-info` folder
in the build environment, and each project publishes it at the address above.
