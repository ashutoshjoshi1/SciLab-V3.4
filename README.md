# Scilab-V3.3

This folder contains the trimmed Windows-ready app bundle.

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
- Hardware control for these DLL-backed spectrometers is Windows-only.
