# All Spec Laser Control System Design

Status: Revision 1.1  
Date: 2026-04-06  
Author: Ashutosh Joshi

## Overview

This document describes the architecture that is now in the repository after the April 2026 refactor. It is meant to be practical. The goal is not to describe an idealized future system. The goal is to help the next engineer understand how the application is put together today, why a few structural changes were made, and where the remaining pressure points still are.

All Spec Laser Control is still a desktop lab application built with Python and Tkinter. That has not changed, and it should not be treated as a problem in itself. The software needs to live on a workstation next to the hardware, work offline, and stay usable for operators who are running setup, acquisition, analysis, and maintenance in one place.

What did change is the shape of the code underneath the UI:

- The spectrometer backends now have a formal contract.
- The measurement sequence is no longer buried inside the Measurements tab.
- Analysis calculations are now separated from plot rendering and from UI presentation.
- The GUI does less work per frame, especially in Live View, Auto-IT, and the Analysis tab.

## What The Application Does

At a high level, the application handles a fairly specific lab workflow:

- Discover and connect to one of several supported spectrometer families
- Control external light sources over serial
- Capture live spectra
- Run automated characterization measurements across selected sources
- Save raw measurement data to disk
- Generate analysis outputs and plots for the run
- Provide EEPROM and stage-control utilities where supported

That workflow is still exposed through the same operator-facing tabs:

- Setup
- Live View
- Measurements
- Analysis
- EEPROM

## What Changed In This Revision

The refactor in this revision was aimed at reducing coupling without disturbing the operator workflow.

### 1. Spectrometer backend interface was formalized

The codebase used to rely on a shared, unwritten convention across spectrometer backends. That was workable, but brittle. We now have a concrete runtime contract in `domain/spectrometer.py` built around `SpectrometerBackend`, along with validation helpers used at connection time.

This does two useful things:

- it makes backend expectations obvious
- it fails early if a new backend drifts from the required shape

### 2. Measurement workflow moved into an orchestrator

The measurement sequence used to live directly inside `tabs/measurements_tab.py`. That meant the UI layer was also responsible for source control, Auto-IT, dark capture, special-case handling, and error sequencing.

That logic now lives in `services/measurement_orchestrator.py`. The tab is back to doing UI work: reading operator inputs, starting the run, forwarding progress updates, and showing results.

### 3. Analysis was split into calculation and rendering layers

The old `characterization_analysis.py` module mixed together three different concerns:

- scientific calculations
- plot generation
- the result object handed back to the UI

That has now been separated into:

- `analysis/calculations.py` for metrics and computed analysis structures
- `analysis/plotting.py` for writing plot images
- `services/analysis_service.py` as the orchestration layer used by the app

`characterization_analysis.py` still exists as a small compatibility facade so existing imports do not break.

### 4. GUI responsiveness was improved in a few hot spots

There was no single dramatic performance bug. The sluggishness came from a handful of small things stacking up:

- too many redraw requests during live acquisition
- too many redraw requests during Auto-IT
- the Measurements tab updating its plot aggressively
- the Analysis tab embedding live Matplotlib canvases for every saved plot

Those hotspots were addressed by batching redraws and by switching the Analysis tab to lightweight PNG previews instead of full embedded plot canvases.

## Current Architecture

This is still a single-process desktop application. The difference now is that the internal boundaries are clearer.

```mermaid
flowchart TB
    Main["main.py<br/>Startup and splash screen"] --> App["app.py<br/>Tk shell, shared state, results management"]

    App --> UI["tabs/*<br/>Tkinter views and widget binding"]
    App --> Services["services/*<br/>Application orchestration"]
    App --> Domain["domain/*<br/>Contracts and shared models"]
    App --> Infra["loaders, backends, serial and stage adapters"]

    Services --> MeasureSvc["MeasurementOrchestrator"]
    Services --> AnalysisSvc["AnalysisService"]

    Domain --> SpecPort["SpectrometerBackend"]
    Domain --> MeasureModel["MeasurementData / run models"]
    Domain --> AnalysisModel["Analysis result models"]

    Infra --> Loader["spectrometer_loader.py"]
    Infra --> Backends["spectrometers/*"]
    Infra --> Lasers["LaserController / serial devices"]
    Infra --> Stage["stage/*"]

    AnalysisSvc --> Calc["analysis/calculations.py"]
    AnalysisSvc --> Plot["analysis/plotting.py"]
```

The important point here is simple: the UI still drives the application, but the business logic is no longer expected to live inside the tabs.

## Main Modules And Responsibilities

| Module | Responsibility | Notes |
| --- | --- | --- |
| `main.py` | Application startup and splash screen | Small entry point |
| `app.py` | Tk shell, shared state, results directory management, analysis view refresh | Still central, but slimmer than before |
| `tabs/setup_tab.py` | Spectrometer setup, connection flow, configuration UI | Still one of the larger UI modules |
| `tabs/live_view_tab.py` | Live acquisition controls and display | Uses a background loop plus throttled UI redraw |
| `tabs/measurements_tab.py` | Measurement UI, operator input collection, run start/stop | Delegates sequence logic to `MeasurementOrchestrator` |
| `tabs/analysis_tab.py` | Analysis notebook and result presentation | Displays saved image previews instead of live figure canvases |
| `services/measurement_orchestrator.py` | Auto-IT, source sequencing, signal/dark capture, special-case handling | Core measurement workflow service |
| `services/analysis_service.py` | Bridges computed analysis and rendered artifacts | Primary analysis entry point for the app |
| `analysis/calculations.py` | Metric computation and structured analysis data | Pure analysis layer |
| `analysis/plotting.py` | Plot generation and PNG output | Rendering layer only |
| `domain/spectrometer.py` | Backend protocol and runtime validation | Contract boundary for spectrometer adapters |
| `domain/measurement.py` | Measurement row and run data models | Shared run-state model |
| `spectrometer_loader.py` | Discovery, backend selection, connection validation | First stop for spectrometer integration |
| `spectrometers/*.py` | Vendor-specific spectrometer implementations | Still vendor-heavy, still hardware-facing |
| `stage/*` | Stage configuration, communication, safe moves | Separate subsystem |

## Device Boundaries

### Spectrometers

Supported spectrometer families:

- Ava1 (Avantes, avaspecx64.dll)
- Hama2 (Hamamatsu DCAM-API, dcamapi.dll)
- Hama3 (Hamamatsu DcIcUSB.dll)
- Hama4 (Hamamatsu HiasApi.dll)
- MiniSpec (Hamamatsu Micro Series Plus, DCamUSB.dll)
- OcOpt1 (Ocean Insight / Ocean Optics via the OceanDirect SDK, OceanDirect.dll; verified on the Flame-S FLMS17243, see `docs/flame-s-oceandirect-integration-guide.md`)
- Jeti1 (JETI PE60_2 and other JETI spectrometer electronics via the JETI SDK, jeti_spectro_ex64.dll + jeti_core64.dll; verified on PE60_2 board 2360093, see `docs/jeti-pe60-2-integration-guide.md`). Auto probes it only when a JETI USB device (vendor 0C6C) is attached, because the SDK's search opens every COM port.

The shared contract is now explicit. A backend is expected to expose:

- `connect()`
- `disconnect()`
- `set_it(it_ms)`
- `measure(ncy=...)`
- `wait_for_measurement()`
- `rcm`
- `sn`
- `npix_active`

That contract is enforced at runtime through the loader before the backend is handed to the rest of the app.

### Laser and serial-controlled devices

`LaserController` still manages three serial paths:

- OBIS
- CUBE
- RELAY

The orchestrator treats source control as an external dependency and does not know about Tk widgets or COM port entries directly. That separation matters because it keeps the workflow logic testable.

### Stage subsystem

The stage stack is unchanged structurally:

- `StageConfig`
- `ModbusManager`
- `StageController`

The safe move sequence remains encoded in the application layer:

1. move Y to home
2. move X to target
3. move Y to target

That is still the right choice for this system because the safety rule belongs to workflow policy, not just to raw motion control.

## Runtime Workflows

### Startup and setup

Startup is still straightforward:

1. `main.py` shows the splash screen
2. `SpectroApp` initializes the Tk shell
3. tabs are built
4. persisted settings are loaded

Setup then handles device selection and connection. The main difference now is that spectrometer instances are validated against the formal backend contract before the app accepts them.

### Live View

Live View still uses a background acquisition loop. The shape is the same as before, but redraw behavior is calmer.

```mermaid
sequenceDiagram
    participant User
    participant LiveTab
    participant App
    participant Spec
    participant UI

    User->>LiveTab: Start Live
    LiveTab->>App: set live_running
    App->>App: start live thread

    loop while running
        App->>Spec: measure(1)
        App->>Spec: wait_for_measurement()
        App->>Spec: read rcm
        App->>UI: queue throttled plot update
    end

    User->>LiveTab: Stop Live
    LiveTab->>App: clear live_running
```

Two small but useful improvements were made here:

- integration time changes are still deferred safely between frames
- live plot updates are coalesced so the UI thread is not asked to redraw on every possible update

### Measurement and analysis

This is where the architecture changed the most.

```mermaid
sequenceDiagram
    participant User
    participant MeasureTab
    participant Orchestrator
    participant Lasers
    participant Spec
    participant Data
    participant AnalysisSvc
    participant Files
    participant AnalysisTab

    User->>MeasureTab: Run Selected
    MeasureTab->>Orchestrator: start(tags, settings snapshot)

    loop for each requested source
        Orchestrator->>Lasers: enable source
        Orchestrator->>Spec: auto-adjust IT
        Orchestrator->>Spec: capture signal
        Orchestrator->>Lasers: disable source
        Orchestrator->>Spec: capture dark
        Orchestrator->>Data: append rows
    end

    MeasureTab->>Files: save CSV
    MeasureTab->>AnalysisSvc: analyze(saved CSV)
    AnalysisSvc->>Files: write plot PNGs
    MeasureTab->>AnalysisTab: refresh with saved previews
```

The special cases from the old flow are still supported:

- 640 nm uses a multi-integration capture sequence
- Hg-Ar still requires operator-mediated fiber switching
- the application still runs analysis automatically after a successful measurement run

The difference is that those rules now live in one place instead of being spread across the tab implementation.

## Analysis Design

The analysis stack is now deliberately split in two.

### Calculation layer

`analysis/calculations.py` is responsible for:

- normalized LSF extraction
- dark-corrected 640 nm data preparation
- Hg-Ar peak detection and matching
- dispersion fitting
- slit-parameter fitting
- spectral resolution estimation
- structured metric output

This layer returns typed analysis data, not UI widgets and not Tk-specific objects.

### Plotting layer

`analysis/plotting.py` takes the computed structures and writes PNG artifacts. It has no say in how those plots are presented in the GUI.

### Service layer

`services/analysis_service.py` is the small coordinator between those two layers. That keeps `app.py` from having to know how analysis internals are assembled.

### Presentation layer

The Analysis tab now works with saved image files rather than holding full embedded Matplotlib canvases for every result. In practice that makes the tab feel much lighter, especially after several runs.

## Data Model And File Outputs

### In-memory measurement model

`MeasurementData` still holds a denormalized row-oriented capture table. That is a good fit for this application because the CSV output is easy to inspect and easy to move around.

Each row contains:

- `Timestamp`
- `Wavelength`
- `IntegrationTime`
- `NumCycles`
- `Pixel_0 ... Pixel_N`

Signal and dark rows still follow the same naming convention:

- signal: `405`
- dark: `405_dark`

### Persisted outputs

The current layout is:

| Artifact | Location |
| --- | --- |
| Settings | `spectro_gui_settings.json` |
| Run folder | `results/<serial>_<timestamp>/` |
| Raw measurement CSV | run folder root |
| Plot PNGs | `results/<serial>_<timestamp>/plots/` |

Reference CSV overlays are still supported. Expected columns are:

- `Wavelength_nm`
- `WavelengthOffset_nm`
- `LSF_Normalized`

## Concurrency And GUI Responsiveness

This application still uses a simple threaded model, and that is fine for the job it needs to do.

### Threads in play

- UI thread for Tkinter
- live acquisition thread
- measurement thread
- stage thread where applicable
- backend-internal vendor threads where the SDK uses them

### What was improved

The practical GUI improvements in this revision are worth calling out because they affect the day-to-day feel of the app.

- Live plot redraws are throttled instead of being pushed immediately for every frame.
- Auto-IT plot redraws are throttled the same way.
- Measurement plot updates are also batched.
- The measurement worker now snapshots power settings and ports before the run instead of reaching back into Tk widgets throughout the sequence.
- The Hg-Ar countdown is marshaled onto the UI thread instead of trying to create modal UI from the worker thread.
- The Analysis tab displays image previews rather than full embedded figure canvases.

None of those changes are flashy on their own. Together, though, they make the interface feel steadier and less heavy.

## Validation Done For This Refactor

This refactor was verified at code level, not with live hardware attached.

Checks run:

- `python3 -m compileall analysis services app.py tabs`
- `python3 -m unittest discover -s tests`

Basic smoke tests were added for:

- backend contract validation
- measurement orchestrator flow
- analysis service behavior on sparse input

That gives us a decent safety net for the structural refactor. It does not replace real hardware validation.

## Current Strengths

There is still a lot to like about this application.

- The operator workflow is clear and stable.
- The code now has better internal seams without changing the deployment model.
- Spectrometer integrations are easier to reason about because the contract is explicit.
- The measurement workflow is easier to test because it is no longer trapped inside Tk code.
- Analysis now has a cleaner path from computed metrics to rendered artifacts.
- The GUI is more responsive under continuous acquisition and after multi-run analysis sessions.

## Remaining Rough Edges

This revision improves the architecture, but it does not magically finish it.

- `app.py` is still carrying a lot of shared state.
- `tabs/setup_tab.py` is still broader than it should be.
- Results persistence is still handled directly in the app shell rather than in a dedicated repository layer.
- Device capability reporting is still fairly simple.
- Logging is useful for operators, but still not rich enough for serious diagnostics.
- Backend modules under `spectrometers/` are still large and hardware-specific by nature.

That said, the code is in a much better place than it was before this refactor. The biggest maintenance risks were not in the hardware SDK wrappers. They were in the hidden coupling between UI code, workflow logic, and analysis code. That coupling is now reduced.

## Near-Term Next Steps

If work continues in the same direction, the next sensible moves are:

1. pull results persistence into a small repository-style module
2. introduce a dedicated hardware session service for connect/disconnect and capability reporting
3. continue shrinking `app.py` and `tabs/setup_tab.py`
4. add a few more smoke tests around connection setup and analysis edge cases
5. run hardware validation for the refactored measurement path on each supported spectrometer family

## Conclusion

The important thing about this revision is that it keeps the right parts intact. This is still a single desktop application. It still suits the lab environment. Operators do not need to learn a new workflow.

What changed is the internal shape of the code. The backend interface is explicit now. The measurement sequence has a real home. Analysis has been split into calculation and rendering layers. And the UI is doing less unnecessary work while the application is busy.

That is the right kind of progress for this project: lower risk, better structure, no drama.

---

## Revision 1.2 — v3.4 (2026-09-17)

This revision is a performance and robustness pass over the v3.3 code, plus
two new backends. The operator workflow and the file layout are unchanged.

### Measured effect

| Metric | v3.3 | v3.4 |
| --- | --- | --- |
| Time to first pixel on screen at startup | ~2.5 s (imports, blank) | ~0.1 s (splash), window ~1.5 s later |
| Auto-exposure frames per source (simulator, 7 sources) | 80–450 | ≤ 6 |
| `compute_characterization` on a real 4-laser run | ~290 ms | ~40 ms |
| Full analysis (compute + PNGs) | ~4.7 s | ~1.4 s |
| Plot output per run | 12 PNGs, ~3 MB, 2 of them blank | 8 PNGs, ~1.1 MB, + metrics JSON |
| `import app` | ~2.5 s (pandas, scipy, matplotlib) | ~0.2 s (loaded behind the splash instead) |

### Structural additions

- `tabs/ui_pump.py` — `UiPump` (one queue drained by a single main-thread `after` loop; every worker→UI hand-off uses `pump.post`) and `ThrottledRedraw` (latest-frame-wins rendering, one redraw per tick, skipped while the tab is hidden). Replaces four copy-pasted throttles and all cross-thread Tk calls.
- `services/acquisition_guard.py` — single-owner token for the spectrometer handle. Live View, Measurements, Check Resolution, Check Spectrometer, Apply-IT and the EEPROM tools all claim it; the operator gets one consistent "X is running" message.
- `spectrometers/simulated_spectrometer.py` — `SimulatedSpectrometer` + `SimulatedLaserController` implementing the full backend contract with a realistic optical model. Selected as spectrometer type "Simulated"; Auto never falls back to it.
- `spectrometers/hama5_spectrometer.py` — Hamamatsu mini-spectrometer USB backend for supported TM/TG/RC models on pyusb; C10785 uses MiniSpec/DCamUSB. See below.
- `spectrometers/_winapi.py` — `ctypes.windll` shim so every driver imports on macOS/Linux and fails on first use instead.
- `domain/spectrometer.py` — the backend contract now covers what callers really use (`rcs`, `it_ms`, `eff_saturation_limit`, `abort`, `request_stop`, `wait_for_measurement(timeout)`).
- `tests/` — pure-function tests with synthetic Gaussians, golden regression against four real CSVs, orchestrator end-to-end on the simulator, Hama5 protocol tests against a fake USB device, UI-pump/guard tests, and a Tk smoke test that builds every tab and streams simulated frames.

### Driver changes (targeted, no consolidation)

- `wait_for_measurement(timeout=None)` on all backends: the timeout defaults to twice the expected duration plus a margin, so a dead watchdog thread can no longer hang the app. New `request_stop()` wakes a waiting thread immediately when the operator presses Stop.
- Hama2/Hama4 `handle_cycle_data` now returns `(issat, data_ok)` like Ava1/Hama3 (Hama4 treats negative counts as a warning only, since HiasApi frames may be background-corrected).
- Hot paths: in-place accumulation in all four drivers, zero-copy ctypes→numpy in Ava1, `np.frombuffer(...).copy()` instead of `deepcopy` and a reused pack buffer in Hama3, poll sleep clamped to 2–50 ms in Hama3, unused X array skipped in Hama4.
- `spec_xfus.calc_msl` accepts an optional `shift` (accumulate around the first frame to avoid the negative-variance cancellation) and no longer uses mutable default arguments.
- The two divergent `dcamapi4.py` copies were merged into the `spec_hama2` package copy; the root shims `dcamapi4.py`, `avantes_spectrometer.py` and the dead `characterization_analysis.py` were removed. Hama2 no longer loads DCAM-API twice.

### Analysis changes

- Hg-Ar branch fixed (`float("Hg_Ar")` was evaluated eagerly as a dict default and raised on every run that had a lamp measurement).
- SDF fill vectorized; pixel matrix hoisted once per run; reference CSVs cached by path+mtime in `AnalysisService`.
- `compute_fwhm` walks outward from the peak (a second line inside the window inflated widths by up to 5×); slit-function fits are bounded with a data-derived starting width; the resolution curve leaves gaps instead of substituting an arbitrary exponent.
- Magic numbers moved onto `CharacterizationConfig` with identical defaults. Plots at 150 dpi; blank figures are no longer written. `metrics_<sn>_<ts>.json` persists every number next to the plots.
- Golden snapshot drift from these changes: ≤ 1.2e-4 relative on the slit polynomials, none on peak pixels or dispersion.

### Setup → "Auto" spectrometer detection

v3.3 probed all backends in a fixed order on the Tk thread, loading and
initialising each DLL (seconds each), never releasing the probe's DLL session,
enumerating the hardware a second time on connect, and reporting failures as
one `" | "`-joined string. In v3.4 the probe runs on a worker with per-backend
progress in the status bar and:

- probes the backend that connected last time first (`last_connected_type`,
  saved with Settings);
- pre-scans the USB bus once (libusb, ~40 ms) and skips backends whose vendor
  (Avantes 1992, Hamamatsu 0661) is not attached, so no DLL is loaded for
  absent hardware. An empty scan is treated as "unknown" and probes everything;
- releases every probe's DLL session (`close_spec_dll` / `_finalize_dll` /
  `DcIc_Terminate`), and connects only the backend type of the device that was
  actually discovered;
- reports a readable failure: which backends were probed and why they failed,
  which were skipped, and which USB vendors are present.

### Hamamatsu C10785: MiniSpec/DCamUSB compatibility

Inspection of the bundled `spectrometers dll files/DCamUSB.dll` found an explicit
C10785 model-name comparison in its device initialization path. This is evidence
for the **MiniSpec** backend, replacing the earlier unsupported assumption that
C10785 used Hama5's TM/TG/RC protocol. The reported `0661:3400` product ID is
excluded from Hama5. Binary inspection does not validate connection or acquisition
on a physical Windows C10785; that bench check remains required.

The bundled DLL enumerates a specific Windows device interface GUID,
`{2ae95266-dd2b-4dc4-ae37-972b9c2f02f3}`. A device can appear healthy in Device
Manager while the installed driver exposes an interface incompatible with the
selected SDK DLL. Native error 4 covers both no matching interface
and failure to open every matching interface; treating it simply as "driver not
installed" loses the distinction needed to diagnose this case.

Keep the vendor driver that works with the existing Hamamatsu application. In
Setup select **MiniSpec** and the matching `DCamUSB.dll` from that application/SDK,
using Python of the same bitness. Close the vendor application before connecting.
Do not replace the C10785 driver with a generic WinUSB binding to try Hama5.
If the connection still fails, run `python tools/diagnose_hamamatsu.py --dll
"C:\path\to\DCamUSB.dll"` on the Windows bench PC and retain the JSON report.
This reads DLL architecture and Windows device/driver interface properties without
initializing the DLL, uploading firmware, or changing drivers.

Hama5 implements the protocol documented by the open-source `usbspecdrivers`
project, whose authors verified C10082/C10083/C9407 hardware. Its known product
table covers C9407MA (`0661:2906`), C10082CA/C10083CA (`0661:2909`), and C11007MA
(`0661:1300`). An unknown Hamamatsu product ID is not evidence of compatibility.
SciLab's Hama5 implementation still requires physical bench validation for a
confirmed supported device.

### Hamamatsu C13016 (Hama6)

Hamamatsu's "Mini-spectrometers Technical Information" (KACC9003E, table 2-9)
assigns the C13016 evaluation circuit and the C13985 module to
`MICRO_USB2_CLR.dll` + `u-ApsSpecEvaluation.exe`, a .NET assembly rather than
a C export library. The board (C12880MA/C16767MA datasheet KACC1226E): USB 2.0,
16-bit A/D, 288 pixels, 5 MHz clock, integration 11 µs to 1,000,000 µs, six
wavelength conversion factors (λ = A0 + B1·p + … + B5·p⁵, p = 1..288).
Hamamatsu's "Minispectrometer H-micro edition" download (ver 1.1.0.0) ships
the assembly, the WinUSB driver (`VID_0661&PID_2936`, "C13016 USB2(HPK)"), the
DLL function specification K29-B61584B (status codes, capture modes, capture
status semantics) and console samples in VB.NET and C++/CLI; the samples are
the reference sequence the driver follows.

`spectrometers/hama6_spectrometer.py` loads the assembly with pythonnet. Class
`MICRO_USB2_CLR` mirrors the C-style `MICRO_USB2` library one-to-one and every
method returns an `Int64` `Cusb2Err` status (0 success). Two pythonnet 3 traps
drove the call layer: its overload resolution rejects `ref` parameters and
typed scalars ("No method matches given arguments"), and a Python `int` stored
into an `object[]` becomes a `Python.Runtime.PyInt` that `MethodInfo.Invoke`
cannot convert. Calls therefore go through reflection with each scalar boxed
inside the CLR (one-element typed array copied into the `object[]`). Every DLL
call runs on one worker thread per backend with a 15 s watchdog
(`DLL_CALL_TIMEOUT_S`) that turns a blocked call into an error and marks the
backend blocked until SciLab is restarted, because the DLL can deadlock (below).
All codes are kept in `diagnostics` and appear in every error message; a failed
Connect also writes `spectrometers/micro_usb2_api_dump.txt`. Acquisition,
averaging, timeouts and Stop follow the same shape as Hama5.

Verified workflow (2026-09-18, development PC, C13016 serial 326VA021,
firmware 1.08, head 26C00021 / sensor S11639, Hamamatsu's SampleAPL sequence):

* `initialize`; `getModuleConnectionList(Int16[] list, ref UInt16 number)`
  with `number` as an output (the samples pass 0; 8 slots, the specification's
  maximum per PC); `open(id)`; `captureStop` to clear a capture a killed
  process may have left running; identity (`getModuleInformationForLabVIEW`,
  NUL-padded strings; `getSpectroInformationForLabVIEW` for the head serial
  and sensor), module property (288 pixels, 16 bit, `capturemode` mask 32),
  exposure property (11 .. 1,000,000 µs, step 1; cycle 1,043 .. 1,100,000 µs),
  capture mode, trigger output/polarity (1 / 3), and the key-protected reads
  of the exposure cycle and the wavelength range (200 .. 800 nm) with the
  literal `CHECK_KEY` the assembly compares `strCheck` against.
* `setCaptureMode(0)` unless the board already reports mode 0, the documented
  "specified number of measurement" mode (one frame per `captureStart`; the
  mode persists in the board). Mode 2 is the external-trigger mode: without a
  trigger `getCaptureStatus` stays 1 and `getImageData` echoes an all-zero
  buffer with code 0. Mode 1 ("free run") exists only in the assembly's
  `Usb2CaptureMode` enum: it delivered one frame and the following
  `setCaptureMode(0)` never returned (reproduced 2026-09-18 in an isolated
  process, killed after 60 s); the board itself was in mode 0 for the next
  process. The driver never selects mode 1.
* `setExposureTime(id, us)` in microseconds although the parameter is named
  `sec` (31 underTime below 11 µs, 30 overTime above the limit). The DLL sets
  the exposure cycle itself on every `setExposureTime`: exposure + 2,000 µs
  (11 → 2,011; 10,000 → 12,000; 1,000,000 → 1,002,000). The driver reads the
  cycle back for the log and never writes it.
* Per frame: `captureStart` (about 13 ms inside the DLL); `getCaptureStatus`
  polled until 0 ("captured frames exist"; 1 while none; 4 = data loss, on
  which the driver stops and restarts the capture, `FRAME_RETRIES` times);
  `getImageData(ref UInt16[288], ref UInt64[1])` → code 0 with the counts and
  a timestamp (25 invalidData is discarded and the capture restarted); the
  buffer is consumed, the next call answers 6 notImage and the status returns
  to 1; `captureStop`. The frame is ready about one cycle after the start
  (11 .. 22 ms at 10 ms); the round trip is about 26 ms at short exposures
  and exposure + 30 ms at long ones. A ready frame waited 2 s unread without
  loss. Timestamps repeat between frames (18, 24, 24, ...), so they cannot
  identify a new frame; consumption does. Connect proves the loop with two
  frames. Measurement timeouts add one round trip per requested frame.
* `close`; `uninitialize`. Recovery: a `captureStart` that throws a
  `SynchronizationLockException` (bench, board still capturing for a killed
  process) did not start; the driver stops, retries once, closes and re-opens
  the module, retries once more and then asks for a re-plug.

Why the bench sessions of 2026-09-17/18 never got frames, found once the
hardware and Hamamatsu's developer kit were on the development PC: the driver
set the exposure cycle to exposure + 1,032 µs through the key-protected
`setExposureCycle` (the specification deleted that function in 2015), which
starved the board of frames in every mode; the very same board, with the
DLL's own exposure + 2,000 µs cycle, delivers a frame 11 ms after each start.
The driver also polled `getImageData` instead of `getCaptureStatus` and left
the board's mode untouched, so it kept probing modes 1 and 2, whose failure
modes (DLL deadlock; no external trigger) look like a dead board. Bench
findings that still hold and are modelled by the test fake: microsecond
exposures behind `sec`; NUL-padded identity strings; `capturemode` 32 in the
module property; key-protected getters answering 12 to a wrong `strCheck`;
`getCalibrationCoefficient` index 0 giving the polynomial (320 .. 888 nm on
this board, the range of a C12880MA, although the head is a UV C16767MA-20,
so the operator's `hama6_wavelength_coefficients.json` next to the DLL wins;
index 1, sensitivity, is NaN); the throwing `captureStart`.

`tools/diagnose_c13016.py` runs the same sequence and prints a report:
assembly internals (enums, P/Invoke imports, string literals, user-string
heap), identity and settings, the keyed state, mode-0 selection, an exposure
sweep showing the DLL-derived cycle, 20 frames with round-trip and
time-to-ready statistics, the answers after a frame is consumed, and
optionally mode 2 (`--trigger`) and the mode-1 hang (`--free-run`, exits if
`setCaptureMode` blocks).

### Bench checklist (Windows, real hardware)

1. Baseline: run the default settings once on a known source (e.g. 405 nm) and keep the `metrics_*.json`.
2. C10785: retain the working vendor driver; Setup → MiniSpec, select the matching `DCamUSB.dll` → Connect; confirm serial and pixel count, then compare a known lamp line in Live View. For a separately confirmed Hama5 device, validate its compatible USB binding, EEPROM identity, and wavelength calibration on that hardware.
3. Advanced flags, one at a time, each followed by a full run and a diff of `metrics_*.json` against the baseline (peak pixels identical, FWHM within noise):
   - *Turn off only the lit source*: inspect the dark frames of every source for residual light.
   - *Pre-arm the CUBE*: inspect the dark frame preceding 377 nm for stray 377 nm light.
   - *Framed serial replies*: watch the log for "timeout" on OBIS/CUBE/relay commands.
4. Stop button during a 50-cycle capture: the run must end within one frame.
5. Ava1/Hama2/Hama3/Hama4: one full run each to exercise the shared `wait_for_measurement` timeout and the in-place accumulators.

6. C13016 (Hama6): install Hamamatsu's evaluation software (USB driver), `pip install pythonnet`, leave `MICRO_USB2_CLR.dll` in `spectrometers dll files/` or in the installer's folder; Setup → Hama6 → Connect (about 0.3 s). Verify 288 pixels and 16 bit in the connect log line, the wavelength axis against the head's inspection sheet (`hama6_wavelength_coefficients.json`), a known line, that Live View runs at about 35 frames per second at 10 ms and follows a change to 100 ms, and that Stop returns within one frame.

### Known limitations left in v3.4

- EEPROM read/write DLL calls still run on the Tk thread (they are rare, seconds-long, and guarded against concurrent acquisition).
- Lasers added in Setup appear in Live View / Measurements / Check Resolution after a restart (the lists are configuration-driven at build time, not rebuilt live).
- The four vendor drivers still carry ~2,500 duplicated lines; consolidation into a base class was deliberately deferred until a bench is available.
- Reference-CSV overlays and the Hg-Ar branch have no golden data (no run in `results/` contains a lamp measurement); they are covered by synthetic tests only.

### Verification commands

```
python3 -m pytest tests -q --ignore=tests/test_smoke_gui.py    # 58 passed
.venv/bin/python -m pytest tests/test_smoke_gui.py -q          # 5 passed
.venv/bin/ruff check .
python3 tests/golden/make_snapshot.py                          # regenerate only on intended numeric change
```
