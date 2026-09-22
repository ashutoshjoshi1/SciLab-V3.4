# SciLab V3.4

SciGlob Spectrometer Characterization System. The version lives in `scilab_version.py`.

## Windows setup

1. Install Python 3.11 or newer on Windows.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Start the GUI:

```bash
python main.py
```

## Notes

- `tkinter` is usually included with the standard Windows Python installer, so it is not listed in `requirements.txt`.
- The app will create its own `spectro_gui_settings.json` and `results` folder when you run it. Those were intentionally not bundled.
- Default DLLs are expected in `spectrometers dll files`.
- `Hama2` still needs the Hamamatsu DCAM runtime on the Windows machine if `dcamapi.dll` is not already available through the vendor install. You can also point the app to that DLL manually.
- `Hama4` uses `HiasApi.dll`, and this bundle now includes `spectrometers dll files/hias.conf` next to it.
- `OcOpt1` drives Ocean Insight / Ocean Optics spectrometers (Flame-S, USB2000+ family and other OceanDirect-supported units) through the **OceanDirect SDK**. The SDK's licence does not allow us to redistribute `OceanDirect.dll`, so it is *not* bundled: install the OceanDirect SDK (default `C:\Program Files\Ocean Optics\OceanDirect SDK`, which also installs the Ocean Optics WinUSB driver) on the PC, or set `OCEANDIRECT_HOME`, or point the Setup tab's DLL path at `OceanDirect.dll`. The SDK is 64-bit only, so SciLab must run in a 64-bit Python. Raw counts are recorded (the SDK's electric-dark and nonlinearity corrections are switched off). Details: `docs/flame-s-oceandirect-integration-guide.md`; live check: `python tools\oceandirect_smoke_test.py`.
- Hardware control for these DLL-backed spectrometers is Windows-only.

## Building the Windows app and installer

```bash
python -m venv .venv-build
.venv-build\Scripts\pip install -r requirements.txt pyinstaller
.venv-build\Scripts\python packaging\build.py
```

`packaging/build.py` stamps the version from `scilab_version.py`, runs PyInstaller with `packaging/SciLab.spec` (output `dist/SciLab/SciLab.exe`) and then Inno Setup 6 with `packaging/SciLab.iss` (output `dist/installer/SciLab-Setup-<version>.exe`). Use `--skip-installer` or `--skip-exe` to run one half only.
