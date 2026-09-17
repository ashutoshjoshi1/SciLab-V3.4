# SciLab v3.4 — Spectrometer Characterization System

Desktop application (Python + Tkinter) that drives lab spectrometers and light
sources to characterize an instrument: live spectra, automated multi-source
measurements with auto-exposure, and LSF / dispersion / resolution analysis.

## What changed in v3.4

| Area | v3.3 | v3.4 |
| --- | --- | --- |
| Startup | ~2.5 s of imports with a blank screen, then a fixed 2.5 s splash, then the build | Logo within ~100 ms; heavy imports load behind it; window follows in ~1.5 s |
| Auto-exposure | Additive stepping, up to 1000 single-frame captures per source (typically 80–450) | Proportional controller, settles in ≤ 6 frames |
| Analysis | ~4.7 s render, 12 PNGs (2 blank) at 300 dpi, numbers never saved | ~1.4 s, 8 informative PNGs at 150 dpi, plus `metrics_<sn>_<ts>.json` |
| Analysis maths | ~0.3 s (Python loops over a 2048² matrix) | ~0.04 s (vectorized) |
| Live plot | Full redraw every frame, layout solver on each draw, unbounded frame rate | Blitted trace over a cached background, 25 fps cap, hysteresis on autoscale |
| Thread safety | Tk called from worker threads (crashes on some builds); no ownership of the spectrometer handle | One main-thread UI pump; single-owner acquisition guard; Stop aborts the device immediately |
| Bugs fixed | EEPROM tab non-functional (`NameError`); Hg-Ar analysis crashed on every run; Check Resolution blank if opened before Live View; countdown ran 0.2 s per second; lasers not switched off on exit; settings not saved on exit; power values lost on resize | all fixed, with regression tests |
| Hardware | Ava1, Hama2, Hama3, Hama4, MiniSpec | + **Hama5** (supported Hamamatsu TM/TG/RC mini-spectrometers over USB), **Hama6** (Hamamatsu C13016 / C13985 micro-spectrometer circuit via its .NET DLL) and a **Simulated** backend |
| Tests | none shipped | 58 unit/golden/orchestrator tests + 5 GUI smoke tests |

## Windows setup (operators)

1. Install Python 3.11 or newer (the standard installer includes Tk).
2. `pip install -r requirements.txt`
3. `python main.py`

Default vendor DLLs live in `spectrometers dll files/`. Hama2 needs the Hamamatsu
DCAM runtime; Hama4 uses `HiasApi.dll` with `hias.conf` next to it.

### Hamamatsu C10785: use MiniSpec

The bundled `DCamUSB.dll` explicitly supports the C10785 model. Select
**MiniSpec** in Setup for this device, including the reported USB ID
`0661:3400`. Hama5 excludes that product ID and uses a different USB protocol.
This mapping is supported by inspection of the bundled DLL; physical Windows
connection and acquisition still need bench validation.

1. Keep the Hamamatsu driver that works with the vendor application installed.
   A healthy Device Manager entry does not guarantee that a particular SDK DLL
   can access the device interface registered by that driver.
2. Select **MiniSpec** and set its DLL path to the matching `DCamUSB.dll` supplied
   with the working Hamamatsu application/SDK. Python and the DLL must have the
   same bitness. Close the vendor application before connecting in SciLab.
3. Connect, confirm the serial number and pixel count, then check a known lamp
   line in Live View.

If connection still fails, run the read-only diagnostic on the Windows bench PC:

```powershell
python tools/diagnose_hamamatsu.py --dll "C:\path\to\DCamUSB.dll" > hamamatsu-diagnostics.json
```

The report includes DLL architecture and Windows device/driver interface details;
it does not initialize the DLL, upload firmware, or change drivers. MiniSpec's
native error 4 can mean that no matching interface was found or that all attempts
to open an interface failed; it does not by itself prove the driver is missing.

### Hama5: supported TM / TG / RC mini-spectrometers

Hama5 implements the USB protocol from the open-source
[usbspecdrivers project](https://sourceforge.net/projects/usbspecdrivers/), whose
authors verified C10082/C10083/C9407 hardware. Its known product table includes
C9407MA (`0661:2906`), C10082CA/C10083CA (`0661:2909`), and C11007MA (`0661:1300`).
SciLab's implementation still needs physical bench validation.

This backend uses **pyusb** and libusb, installed by `requirements.txt`; Windows
requires a compatible WinUSB binding. Use Hama5 only for a device confirmed to
use this protocol. Do not replace the working C10785 vendor driver to try Hama5.

## Developer setup (macOS / Linux, no hardware)

The vendor drivers only load their DLLs on first use, so the whole application,
including a **Simulated** spectrometer + light bench, runs anywhere:

```bash
# A Python with Tk (python.org builds have it; pyenv builds often do not):
/usr/local/bin/python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python main.py           # Setup -> type "Simulated" -> Connect
```

### Tests

```bash
python3 -m pytest tests -q --ignore=tests/test_smoke_gui.py   # any Python with numpy/scipy/pandas
.venv/bin/python -m pytest tests/test_smoke_gui.py -q         # needs Tk: builds the GUI, streams simulated frames
.venv/bin/ruff check .
```

`tests/golden/snapshot.json` pins the numeric analysis outputs for four real
measurement CSVs in `tests/fixtures/`. Regenerate it only when a change is meant
to alter the numbers: `python3 tests/golden/make_snapshot.py`.

## Bench-validated speed options (Setup → Advanced)

Three further speedups change laser/serial timing and are **off by default**
until validated on the bench with a reference spectrum: turn off only the lit
source between lasers, pre-arm the 377 nm CUBE during the previous dark, and
framed serial replies instead of fixed waits. They are saved with Settings.

### Hama6: Hamamatsu C13016 evaluation circuit / C13985 module

The C13016 drives the fingertip micro-spectrometer heads (C12880MA, C16767MA;
288 pixels, 16-bit A/D, integration 11 µs to 1 s). Hamamatsu's PC software for
it is `u-ApsSpecEvaluation.exe` with **`MICRO_USB2_CLR.dll`**, a .NET assembly
(source: Hamamatsu technical note KACC9003E, table 2-9). SciLab loads that
assembly through pythonnet.

1. Install Hamamatsu's evaluation software for the C13016 (it installs the USB
   driver; keep that driver, do not use Zadig here) and confirm
   `u-ApsSpecEvaluation.exe` sees the board.
2. Copy `MICRO_USB2_CLR.dll` from that installation into `spectrometers dll files/`
   (or point the Setup tab's DLL path at it) and run `pip install pythonnet`.
3. Setup → type **Hama6** → Connect.

SciLab drives the assembly's own workflow (`MICRO_USB2_initialize`, module
connection list, `open`, module properties, `setExposureTime` in seconds,
one continuous `captureStart` / `getImageData` capture) through .NET
reflection, so its `ref` parameters and `Int64` handles work from Python. The
exposure API works in microseconds (the driver reads the unit from the device's
exposure limits), status code 0 means success, and the driver checks each step
by its effect and prints all codes in the connect log line and in any error.
Frames arrive once per exposure *cycle* stored in the board (up to 1.1 s); the
cycle is key-protected in the DLL, so ask Hamamatsu for the `strCheck` value or
set a short cycle in u-ApsSpecEvaluation before using SciLab. A failed
Connect also writes the assembly's public API to
`spectrometers/micro_usb2_api_dump.txt`; send that file with the error text
when reporting a problem. If Connect works but Live View reports "No frame
within ...", run the read-out experiment script and send its output:

```powershell
python tools/diagnose_c13016.py > c13016-diagnostics.txt
```

It takes about a minute and records how `getImageData` answers before any
start, during one continuous capture (frame period from the timestamps), after
an exposure change, with each buffer size, after `captureStop`, and after a
restart; add `--modes` to try the candidate capture modes as well. The board's
mode is restored at the end.

## Files

| Path | Purpose |
| --- | --- |
| `main.py` / `app.py` | entry point, Tk shell, shared state |
| `tabs/` | Setup, Live View, Measurements, Check Resolution, Analysis, EEPROM |
| `services/` | measurement orchestrator, analysis service, acquisition guard, check-spectrometer service |
| `analysis/` | calculations, plotting, data models |
| `spectrometers/` | vendor backends (`ava1`, `hama2`, `hama3`, `hama4`, `hama5`, `minispec`) and the simulator |
| `hardware/`, `stage/` | laser / filter-wheel serial controllers, Modbus stage |
| `results/` | created at runtime: `<serial>_<timestamp>/Measurements_*.csv` + `plots/` (not versioned) |
| `spectro_gui_settings.json` | operator settings (ports, powers, advanced flags) |
