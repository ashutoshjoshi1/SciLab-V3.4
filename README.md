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
it is `u-ApsSpecEvaluation.exe` with **`MICRO_USB2_CLR.dll`**, a .NET assembly;
SciLab loads that assembly through pythonnet. Hamamatsu's "Minispectrometer
H-micro edition" download (`Minispectrometer_H-micro edition_ver1.1.0.0`) also
holds the WinUSB driver (`winusb2.0MICRO.inf`, `VID_0661&PID_2936`, shown as
"C13016 USB2(HPK)" in Device Manager), the DLL function specification
`K29-B61584B C13016_DotNet_DLLFunctionSpecifications.pdf` and console samples
in VB.NET and C++/CLI under `DeveloperTools/`; the driver follows those samples.

1. Install Hamamatsu's evaluation software for the C13016 (it installs the USB
   driver; keep that driver, do not use Zadig here), confirm
   `u-ApsSpecEvaluation.exe` sees the board, then close it.
2. `pip install pythonnet`. The DLL is looked for in `spectrometers dll files/`
   and, failing that, in the installer's folder
   `C:\Program Files\HamamatsuMinispectrometer for C13016\EvaluationSoftware\`;
   copy it next to the other DLLs or point the Setup tab's DLL path at it.
3. Setup → type **Hama6** → Connect.

The head on the bench is a **C16767MA-20** (UV, 190 to 440 nm, 288 pixels).
The board reports the head's serial and sensor, not its model, and it keeps
the wavelength polynomial of the head it was calibrated with: the stored
polynomial spans about 320 to 888 nm, the range of a C12880MA, not of a 190 to
440 nm head. Check the head's label and inspection sheet and put that sheet's
polynomial into `hama6_wavelength_coefficients.json` next to the DLL, keyed by
the head serial; a matching entry overrides the board's polynomial (an entry
may also name the model, which then appears in the device name):

```json
{"26C00021": {"model": "C16767MA-20", "coefficients": [A0, B1, B2, B3, B4, B5]}}
```

How SciLab drives the board (verified against the hardware on 2026-09-18,
C13016 serial 326VA021, firmware 1.08, with Hamamatsu's sample sequence):

* `initialize`, `getModuleConnectionList`, `open`, then `captureStop` to
  clear a capture a killed process may have left running, then identity,
  limits and the wavelength polynomial. Calls go through .NET reflection with
  every scalar boxed inside the CLR, so `ref` parameters and `Int64` handles
  work from Python.
* `setCaptureMode(0)` unless the board is already in mode 0, the documented
  "specified number of measurement" mode: one frame per `captureStart`. The
  mode persists in the board. Mode 2 waits for an external trigger (no frame
  without one). Mode 1 exists only in the assembly's enum: it delivers one
  frame and the next `setCaptureMode` blocks inside the DLL until the process
  is killed, so SciLab never selects it (the board recovers by itself in a new
  process).
* `setExposureTime` in **microseconds** (limits 11 to 1,000,000; codes 31
  underTime / 30 overTime outside). The DLL derives the exposure *cycle* (frame
  period) itself as exposure + 2,000 µs. Earlier SciLab versions set the cycle
  to exposure + 1,032 µs through the key-protected `setExposureCycle`; that
  starved the board of frames in every mode and is gone.
* Per frame: `captureStart`, poll `getCaptureStatus` until it answers 0 (1
  while no frame waits, 4 on data loss, after which the capture is restarted),
  `getImageData` (288 counts and a timestamp; the buffer is consumed and the
  next call answers 6 notImage), `captureStop`. A frame is ready about one
  cycle after the start; the round trip costs about 26 ms, mostly inside
  `captureStart`, so 10 ms exposures run at about 35 frames per second and
  100 ms exposures at about 7.5. A ready frame waits unread without loss.
  Timestamps repeat between frames and are not used to tell frames apart.
* Every DLL call runs on a worker thread with a 15 s watchdog, so a blocked
  call ends in an error message ("restart SciLab and re-plug the C13016")
  instead of a frozen window. A `captureStart` that throws did not start: the
  driver stops, retries once, re-opens the module, retries once more and then
  asks you to unplug and re-plug the board. Status codes are named in the log
  after the specification's "Return Code" table.

A failed Connect writes the assembly's public API to
`spectrometers/micro_usb2_api_dump.txt`; send that file with the error text
when reporting a problem. If Connect works but Live View reports "No frame
within ...", run the diagnostic and send its output:

```powershell
python tools/diagnose_c13016.py > c13016-diagnostics.txt
```

It takes under a minute (`--skip-assembly` skips the assembly dump). It lists
the assembly's API and internals, identity and settings, selects capture mode
0, sweeps the exposure to show the cycle the DLL derives, takes 20 frames with
the vendor loop and reports the time to ready and the pixel statistics, checks
what the DLL answers once a frame is consumed, and with `--trigger` tries
capture mode 2 for 3 s. `--free-run` reproduces the mode-1 hang (the script
exits if `setCaptureMode` does not return; the board is fine for the next
process).

## Files

| Path | Purpose |
| --- | --- |
| `main.py` / `app.py` | entry point, Tk shell, shared state |
| `tabs/` | Setup, Live View, Measurements, Check Resolution, Analysis, EEPROM |
| `services/` | measurement orchestrator, analysis service, acquisition guard, check-spectrometer service |
| `analysis/` | calculations, plotting, data models |
| `spectrometers/` | vendor backends (`ava1`, `hama2`, `hama3`, `hama4`, `hama5`, `hama6`, `minispec`) and the simulator |
| `hardware/`, `stage/` | laser / filter-wheel serial controllers, Modbus stage |
| `results/` | created at runtime: `<serial>_<timestamp>/Measurements_*.csv` + `plots/` (not versioned) |
| `spectro_gui_settings.json` | operator settings (ports, powers, advanced flags) |
