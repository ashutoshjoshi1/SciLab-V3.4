# Changelog

All notable changes to SciLab are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Versions are four-field Windows-style numbers (`major.minor.patch.build`) so
that one number serves the application, the executable's version resource and
the installer. The single source of truth is
[`scilab_version.py`](scilab_version.py); bump it there and rebuild.

## [Unreleased]

Nothing yet.

## [3.4.2.0] — 2026-10-05

### Added

- **Jeti1 backend** — JETI spectrometer electronics, written for and verified
  on a PE60_2 line-array readout board (board 2360093, firmware SPECFIRM_PE60
  0.66.0, 2048 px, 16 bit, USB `0C6C:0CDC` as a virtual COM port), through
  the JETI SDK 4.8.10 (`jeti_spectro_ex64.dll` + `jeti_core64.dll`, bound with
  ctypes). Both DLLs are bundled in `spectrometers dll files/`, which the SDK
  licence allows. One firmware scan per cycle with float averaging in SciLab,
  about 6–7 ms per frame on top of the exposure (126 spectra/s at the 1.88 ms
  minimum). Stop returns in under a second; the firmware ignores the break and
  finishes the exposure, so the next measurement waits for it. Auto probes
  Jeti1 only when a JETI USB device is attached, because the SDK's search
  opens every COM port, including the laser controllers'. Study and measured
  numbers in `docs/jeti-pe60-2-integration-guide.md`, a live check in
  `tools/jeti_smoke_test.py`, and `--self-test` imports the backend.
  Checked in the app on the bench: Setup → Jeti1 → Connect, Live View, and
  Auto detection.
- **JETI SDK DLLs in the installer** — `jeti_spectro_ex64.dll` and
  `jeti_core64.dll` (SDK 4.8.10) with `JETI_SDK_license.txt`, in
  `spectrometers dll files/`. They need no Visual C++ runtime, and Windows
  10/11 needs no extra driver for the PE60_2.

### Fixed

- **OcOpt1 backend did not import**: the last commit left unresolved merge
  markers in `oceandirect_spectrometer.py` and its tests (a stash pop), a
  SyntaxError that also failed `--self-test`. Both sides are kept: the wait is
  bounded by `measurement_timeout_s`, a user stop is reported as "Measurement
  stopped by user", and one `request_stop` does both.

## [3.4.1.0] — 2026-09-22

### Added

- **OcOpt1 backend** — Ocean Insight / Ocean Optics spectrometers through the
  OceanDirect SDK (`OceanDirect.dll`, bound directly with ctypes like the other
  vendor DLLs). Verified on a Flame-S (FLMS17243, 2048 px, 339.7–1019.7 nm):
  discovery by serial, raw counts with the SDK's electric-dark and nonlinearity
  corrections switched off, 388 spectra/s at 1 ms, reconnect after close.
  The DLL is not bundled (its licence forbids redistribution); the app finds the
  installed SDK, `OCEANDIRECT_HOME`, or a path typed in Setup. The
  hands-on study is in `docs/flame-s-oceandirect-integration-guide.md`, a live
  check in `tools/oceandirect_smoke_test.py`, and `--self-test` now imports
  this backend too.

### Changed

- **Startup is ~1.4 s faster.** pandas and scipy were imported before the main
  window appeared, although nothing on screen uses them until a run is saved
  or analysed. They are now imported on a worker thread two seconds after the
  window is up (`main.preload_analysis_stack_later`), and the measurement
  model no longer imports pandas at module load. Source tree: ~3.0 s → ~1.6 s
  from launch to a usable window; a test guards the startup import set.

### Fixed

- **MiniSpec lifecycle test failed on Windows**: the acquisition thread's Win32
  message pump reached `user32` through the module-level DLL-loader name that
  the tests replace with a fake. It now uses `ctypes.windll` directly.
- Removed the duplicate `packaging/SciLab.iss`; the build uses
  `packaging/installer.iss`.

## [3.4.0.6] — 2026-09-21

First packaged release: SciLab now ships as a Windows installer rather than a
source checkout, with a rebuilt interface.

### Added

- **Windows installer** — `SciLab-Setup-3.4.0.6.exe`, built with Inno Setup
  around a one-folder PyInstaller bundle. Per-machine by default, per-user
  with `/CURRENTUSER`, unattended with `/VERYSILENT`. Upgrades replace in
  place; uninstall leaves measurement data alone. See
  [`packaging/README.md`](packaging/README.md).
- **`--self-test`** — checks every dependency, all eight spectrometer
  backends, the bundled vendor DLLs, the writable data directory and the
  bundled assets without opening a window; writes
  `%LOCALAPPDATA%\SciLab\self-test.log`. Run by the build, by the installer
  after copying files, and from a Start-menu shortcut for support. Also
  `--version`.
- **Single-instance guard** — a second copy hands focus to the running one
  instead of competing for the same USB instrument. The installer refuses to
  overwrite a running installation.
- **Multi-resolution application icon** (16–256 px) generated from the SciGlob
  mark; the previous icon existed only at 32 px.
- **Wavelength-coded sources** — every laser is tinted towards its own
  wavelength across Live View, Check Resolution, Measurements and Setup, each
  colour darkened to stay readable on white. The wavelength is always written
  out as well, so colour is never the only cue.
- **Controller-type badges** — OBIS / CUBE / RELAY as filled pills instead of
  bracketed grey text.
- **Analysis empty state** — a card explaining what to do, with a button
  through to Measurements, in place of a blank grey panel.
- **Auto-hiding scrollbars** — each scrollbar appears only while it has
  something to scroll.

### Changed

- **Interface rebuilt** around one accent colour and a slate neutral ramp:
  hairline-bordered cards, visible borders and hover states on every control,
  slim scrollbars, a branded title strip carrying a live instrument chip
  (backend and serial number), and a status strip with a state-coloured
  indicator. Plots share one typography and legend treatment.
- **Typography** — Segoe UI Variable's three optical cuts (Small for captions,
  Text for body, Display for headings) with Cascadia Mono for data, falling
  back to Segoe UI and then the platform sans. The base size steps down on
  screens shorter than ~820 logical pixels so dense panels still fit. Tk's
  named fonts are set too, so message boxes and file dialogs match.
- **Setup always opens on Auto** whatever was saved; the saved backend becomes
  the probe hint rather than a pin, and a DLL path the app suggested is
  cleared when Auto is selected. A path typed by hand is kept.
- **Auto probing no longer narrows to one backend.** A vendor DLL in the path
  box moves its backend to the front of the probe order instead of replacing
  the order.
- **Installed builds keep data in `%LOCALAPPDATA%\SciLab`** (settings,
  results, self-test log), because Program Files is read-only for an operator.
  Running from a source checkout still writes next to the code.
- **Measurements** — on a wide window the four control cards sit in one row
  with the actions as a 2×2 block, so nothing is clipped or pushed below the
  fold.
- The window title now carries the version.

### Fixed

- **No dropdown could be opened.** The combobox list font was passed as
  `"Segoe UI 10"`, which Tk parses as family `Segoe` with size `UI`; posting
  any dropdown raised `expected integer but got "UI"`. This affected every
  combobox, including the spectrometer type selector.
- **Measurements control panel was two pixels tall** and **Live View's
  controls were squeezed to a sliver**: a Tk-Agg canvas re-requests
  `figsize × dpi` pixels after every resize (~2000×1000 px at 150 % scaling),
  which starved whatever shared the container.
- **Check Resolution's plot was clipped**, losing its x axis: on `<Map>` the
  Tk-Agg backend multiplies figure dpi by the device-pixel ratio and resizes
  the widget past its container.
- **Navigation toolbars were invisible** in Live View and Check Resolution —
  constructed after an expanding canvas, pack had no room left to give them,
  so zoom and pan could not be reached.
- **Plot titles and axis labels were clipped** on short, wide figures; axes
  margins are now held constant in DPI-scaled pixels.
- **Labels and frames inside white cards painted the window tint**, showing as
  grey blocks behind their text.
- **Check Resolution's Fit Readout was cut off** at the bottom of the window;
  that column now scrolls.
- **Permanent horizontal scrollbars** on Setup and EEPROM, and two
  over-wide entries that forced sideways scrolling at 1020 px.
- EEPROM address hints drifted to the far right of the window instead of
  sitting beside their values; fields that write to the device now carry a
  warm tint.
- Setup's laser-power grid read `Hg_Ar nm` for the lamp, its column headers
  did not line up with the values, and its setpoint caption was clipped.

### Notes for operators

- A fresh install starts from built-in defaults, not from another machine's
  configuration. To seed a bench, copy a known-good
  `spectro_gui_settings.json` into `%LOCALAPPDATA%\SciLab\` after installing.
- Uninstalling deliberately leaves `%LOCALAPPDATA%\SciLab` in place; remove it
  by hand to reset a machine completely.

## [3.4.0.0] — 2026-09-18

Summarised from the v3.4 comparison table in [`README.md`](README.md), which
remains the fuller account. Released before this changelog existed.

### Added

- **Hama5** backend (Hamamatsu TM/TG/RC mini-spectrometers over USB),
  **Hama6** (C13016 / C13985 micro-spectrometer circuit via its .NET DLL) and
  a **Simulated** backend for exercising the workflow without hardware.
- A test suite: unit, golden, orchestrator and GUI smoke tests.
- `metrics_<sn>_<ts>.json` alongside the analysis plots, so the numbers are
  saved rather than only drawn.

### Changed

- Startup shows the logo within ~100 ms and loads the heavy imports behind it,
  instead of ~2.5 s of blank screen followed by a fixed 2.5 s splash.
- Auto-exposure replaced with a proportional controller that settles in ≤ 6
  frames, from additive stepping that could take hundreds of captures.
- Analysis renders in ~1.4 s (was ~4.7 s), producing 8 informative plots at
  150 dpi; the analysis maths is vectorised (~0.04 s, was ~0.3 s).
- Live plot blits the trace over a cached background with a 25 fps cap.
- Tk is called only from the main thread through one UI pump, and the
  spectrometer handle has a single-owner acquisition guard; Stop aborts the
  device immediately.

### Fixed

- EEPROM tab was non-functional (`NameError`); Hg-Ar analysis crashed on every
  run; Check Resolution came up blank unless Live View had been opened first;
  the countdown ran 0.2 s per second requested; lasers were not switched off
  on exit; settings were not saved on exit; laser power values were lost on
  resize.

## Earlier releases

v3.3 and earlier predate this changelog. See the git history.

[Unreleased]: https://github.com/ashutoshjoshi1/SciLab-V3.4/compare/main...HEAD
