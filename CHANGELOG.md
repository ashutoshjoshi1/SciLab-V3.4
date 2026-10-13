# Changelog

All notable changes to SciLab are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Versions are four-field Windows-style numbers (`major.minor.patch.build`) so
that one number serves the application, the executable's version resource and
the installer. The single source of truth is
[`scilab_version.py`](scilab_version.py); bump it there and rebuild.

## [Unreleased]

### Added

- **Software licence.** `LICENSE` is the SciLab licence agreement from
  SciGlob Research LLC (proprietary, all rights reserved), and
  `THIRD-PARTY-NOTICES.md` lists the vendor libraries and open-source
  components shipped with SciLab and their licences. The installer shows
  the licence for acceptance and installs both files beside `SciLab.exe`.

### Changed

- The executable's copyright field now reads "Copyright (C) 2026 SciGlob
  Research LLC".

## [3.4.4.0] — 2026-10-09

### Added

- **Run progress on the Measurements tab and in the title strip.** The
  Actions card lists every source in the run with its state (waiting,
  measuring, done, failed, stopped), the integration time, peak and auto-IT
  frame count while it is being measured, its duration once done, and the
  failure reason as a tooltip; the header names the source in progress, how
  many are done and the elapsed time. A chip in the title strip mirrors the
  run from any tab (click it to open Measurements) and keeps the outcome up
  for a while after the run ends. The orchestrator gained `run_started`,
  `source_failed`, `source_state_changed` and `all_sources_off` callbacks to
  drive it.
- **Lit-source chip with All off.** The title strip shows which lasers or
  the lamp are emitting, whichever path switched them (the Live View and
  Check Resolution checkboxes or a measurement run), and an **All off**
  button beside it switches everything off, stopping a running measurement
  first. The shared checkboxes now follow the real state too, so a source a
  run switched off no longer shows as checked.
- **Live View readout card.** Peak counts, peak pixel, percent of full
  scale, a saturation flag, the applied integration time, display frame rate
  and the stream state beside the plot, refreshed up to ten times a second.
- **Event log drawer.** A **Log** button in the status bar (or F12) opens a
  colour-tagged view of every logger's records above the status bar, with
  Clear and Copy; warnings and errors that arrive while it is closed are
  counted on the button. Backend errors no longer live only in dialogs and
  the console.

### Changed

- **Integration time steps from the keyboard.** The Live View and Check
  Resolution fields are spinboxes: Up/Down and the arrows step 0.1 ms (1 ms
  on Check Resolution) and apply at once while a spectrometer is connected,
  Enter applies a typed value, and Apply IT still works as before.
- **Start/Stop pairs show their state.** Stop is greyed while nothing runs
  and Start while something does, on Live View, Check Resolution and
  Measurements; Analysis's Clear All is disabled until there is a run tab.
- **Setup reads with one primary button per card.** Refresh Ports, Load
  Config and + Add Laser are outlined now; Connect and Save Settings stay
  filled. The COM port, head sensor and stage indicators say "not tested",
  "testing…", "open", "not found" or "no port" beside the dot, with the
  reason in a tooltip. The driver-path row is shown only for a backend that
  may need it (not for Auto or Simulated, unless an override was typed), and
  the bench speed options live in a collapsed Advanced card that opens on
  its own when an option is on.
- **One name per source.** The lamp is "Hg-Ar" on every tab (it was
  "Hg_Ar" on the checkboxes and "Hg-Ar lamp" on Setup); the data tags in
  CSV files are unchanged.
- **Plots say what to do while empty.** Live View, Measurements and Check
  Resolution show a muted hint ("Select sources and press Run Selected",
  …) until the first frame, and Check Resolution starts on sensible axes
  instead of matplotlib's defaults.
- **Clearer actions.** The Measurements button that ran the analysis was
  labelled "Analysis"; it is "Analyse last run". "Check Spectrometer" moved
  from the laser switches to the Live View Control card as "Check
  spectrometer (line fit)". The EEPROM write buttons are red and say
  "Write …" instead of "Save …".

### Fixed

- Bench speed options saved in the settings file loaded into the
  measurement but not into the Setup checkboxes, which stayed unticked.
- Check Resolution's Fit Readout rows painted the canvas grey behind their
  labels on the white card: the `FieldLabel`, `Metric`, `MetricSmall` and
  `Strong` label styles are now re-tinted with their container like the
  other typography-only styles.

## [3.4.3.0] — 2026-10-07

### Changed

- **The window opens maximised and complete.** The main window is now built
  behind the splash and shown once, maximised, when it is finished; closing
  the splash first left a bare desktop for the half second the window took
  to build. Every tab is built, laid out and drawn at its final size before
  the window appears (Live View, Check Resolution and EEPROM used to be built
  on the first click, and the Measurements figure on first show), so the
  first switch to a tab no longer builds it. The splash floor drops from
  800 ms to 250 ms since the window build now runs behind it.
- **Type and layout follow the screen.** The base size is 10 pt on a
  1080p-class screen, 11 pt on 1440p (and 4K at 150 %), 12 pt on 4K at
  125 %, and 9 pt on a 768 px-tall laptop. Paddings, control sizes,
  scrollbars, tree rows, plot typography and every layout breakpoint scale
  with it (`Fonts.SCALE`, `theme.logical_px`), so a form is the same physical
  size on a 100 % 1080p panel and a 125 % 4K panel instead of a sliver of
  small text beside a plot that fills the screen. `SCILAB_UI_SCALE=1.0` pins
  the compact size.
- **Wide-screen layouts.** The Setup form is capped at a reading width
  (1560 logical px) and centred in two columns; the EEPROM register listing
  sits at document width (1240); the Measurements control strip is capped at
  1720 and centred under the plot, with the source row holding as many
  sources as fit (one row of nine on a wide screen); Live View and Check
  Resolution give the controls a quarter of the window (300–400 logical px)
  with the sources in two columns; Analysis previews use as many columns as
  fit and are sized to fill them; dialogs are sized to their content and
  centred over the main window.
- **Plot titles keep the theme face.** `set_title` alone reset the Live View
  title to matplotlib's default face and weight on the first frame; titles
  now go through `theme.plot_title_font`. The canvas device-pixel ratio is
  pinned to what the Tk backend would compute, so a tab's first appearance
  costs one draw instead of three and the toolbar icons come out at the
  right size.

### Fixed

- **Auto-IT jumped instead of stepping.** On a Hamamatsu head the first
  auto-IT frame at the 2.4 ms start is mostly dark offset (about 750 counts),
  and the proportional controller introduced in 3.4.0.0 (`it × target ÷ peak`,
  which assumes counts scale through zero) sent the integration time straight
  to about 200 ms. The v3.3 additive stepping is back: each unsaturated frame
  moves the integration time by at most `IT_STEP_UP` (0.3 ms) up or
  `IT_STEP_DOWN` (0.1 ms) down, shrinking to 0.05 ms near the band, for up to
  `MAX_IT_ADJUST_ITERS` (1000) frames per source; a saturated frame still backs
  off by 30 %. A source that does not reach the band within that budget is now
  reported in the run summary instead of being skipped silently.

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
