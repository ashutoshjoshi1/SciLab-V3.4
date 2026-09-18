# Integrating the Hamamatsu C13016 / C13985 micro-spectrometer (MICRO_USB2_CLR.dll) into another application

Everything in this guide was verified against the actual unit on 2026-09-18
(SciLab development PC, Windows 11, 64-bit Python 3.12 + pythonnet, and a C#
build with the .NET Framework 4 compiler). It is written so that a team
integrating the same spectrometer into a different program (C#, VB.NET, C++,
LabVIEW, Python or a non-.NET application) can do it without repeating the
two days of bench work that produced these findings. SciLab's own driver,
`spectrometers/hama6_spectrometer.py`, is the reference implementation of
everything below.

---

## 1. Hardware, driver and vendor kit

### 1.1 The unit

| Item | Value (our unit) | Source |
| --- | --- | --- |
| Circuit | Hamamatsu **C13016** evaluation circuit (the **C13985** module is the same circuit with a head built in and uses the same DLL) | KACC9003E table 2-9 |
| Circuit serial / firmware / boot | `326VA021` / `1.08` / `1.1.3` | `getModuleInformationForLabVIEW`, `GetBootVersion` |
| Head ("unit") serial / sensor | `26C00021` / `S11639` | `getSpectroInformationForLabVIEW` |
| Head model | **C16767MA-20** (UV, 190 to 440 nm) per the operator; the board does not report the model | label / inspection sheet |
| Pixels / A/D | 288 pixels, 16 bit (saturation 65535) | `getModulePropertyForLabVIEW` = 288, 16, 32 |
| Exposure range | 11 to 1,000,000 µs, step 1 µs | `getExposurePropertyForLabVIEW` |
| Exposure cycle (frame period) range | 1,043 to 1,100,000 µs, step 1 µs | same call |
| Wavelength conversion | 6 coefficients A0, B1..B5: λ[nm] = A0 + B1·p + B2·p² + B3·p³ + B4·p⁴ + B5·p⁵ | `getCalibrationCoefficient(id, 0, double[6])` |
| Dark level at 1 to 100 ms | about 5,250 to 5,300 counts, about 9 counts rms noise | measured |

### 1.2 USB identity and driver

| Item | Value |
| --- | --- |
| VID / PID | `0x0661` / `0x2936` |
| Device Manager name | **C13016 USB2(HPK)**, class "Hamamatsu USB Device" |
| Driver | WinUSB, installed by Hamamatsu's INF `winusb2.0MICRO.inf` (service `WinUSB`, interface GUID `{B2B3F265-88E7-40ba-9FB3-C6E8E95DAE6A}`) |
| Do not | replace the driver with Zadig/libusb; the DLL opens the device through SetupAPI + WinUSB using that INF's interface GUID |

Only **one process** can use the board at a time. Close `u-ApsSpecEvaluation.exe`
(and any other program using the DLL) before your application opens it.

### 1.3 Vendor kit ("Minispectrometer H-micro edition" ver 1.1.0.0)

The download (folder `Minispectrometer_H-micro edition_ver1.1.0.0`) contains
everything an integrator needs:

| Path in the kit | Content |
| --- | --- |
| `EvaluationSoftware\setup.exe` | Installs `u-ApsSpecEvaluation.exe` + `MICRO_USB2_CLR.dll` to `C:\Program Files\HamamatsuMinispectrometer for C13016\EvaluationSoftware\` and the USB driver |
| `DeveloperTools\DLL&Driver\` | `MICRO_USB2_CLR.dll`, `winusb2.0MICRO.inf`, `winusb2.0micro.cat`, WinUSB co-installers |
| `DeveloperTools\ForVisualBasic\SampleAPL\SampleAPL\Module1.vb` | VB.NET console sample: the reference call sequence |
| `DeveloperTools\ForVisualC++\SampleAPL\SampleAPL\SampleAPL.cpp` | The same sample in C++/CLI |
| `Document\K29-B61584B C13016_DotNet_DLLFunctionSpecifications.pdf` | The DLL function specification (English): signatures, return codes, capture-status semantics |
| `Document\K29-B61582-F-C13016InstructionManual.pdf` | Instruction manual |

The DLL is identical in every copy of the kit (86,088 bytes, MD5
`44c891945a6103ea8d55b747aeaf990b`, assembly version 1.0.0.2).

### 1.4 What the DLL is

* A **managed .NET Framework assembly** (`MICRO_USB2_CLR, Version=1.0.0.2`),
  namespace `MICRO_USB2_CLR_DLL`. It has **no native export table**: it cannot
  be loaded with `LoadLibrary`/`GetProcAddress`, ctypes, or any C FFI. You need
  a CLR in the process (or a bridge process, see section 8).
* Public classes: `MICRO_USB2_CLR` (the device API; instance class with a
  parameterless constructor), `Usb2Struct` (nested enums `Cusb2Err`,
  `Usb2CaptureMode`, `Usb2TriggerMode`, ... and the property classes
  `ModuleInformation`, `SpectroInformation`, `ModuleProperty`,
  `ExposureProperty`, `WaveLengthProperty`, `AdOffsetProperty`),
  `USBRemovableCheck`, `FirmVerUp`.
* Every `MICRO_USB2_*` method returns an **`Int64` status** (`Cusb2Err`, 0 =
  success). Values come back through `ref` parameters or arrays. Most methods
  exist twice: a `...` version taking a property object and a `...ForLabVIEW`
  version taking scalar `ref` parameters. Both work; the `ForLabVIEW` ones are
  easier from reflection and from LabVIEW.
* Internally it P/Invokes `setupapi.dll`, `kernel32.dll`, `user32.dll`,
  `shell32.dll` and WinUSB. It ran under the .NET Framework 4 CLR
  (`4.0.30319`) in a **64-bit** process. The vendor samples target .NET
  Framework 3.5. A 32-bit process was not tested.
* Several methods take a `String strCheck` argument and answer
  `12 invalidParameter` unless it equals the literal **`"ichino"`** (the
  assembly compares against that constant). Use it only for the *read*
  methods (`getExposureCycle`, `getWaveLengthProperty*`, `getImageSize`,
  `getADMode`, `getAdOffset*`, `GetBootVersion`). Do not use the keyed
  *setters* (`setExposureCycle`, `setADMode`, `setAdOffset`, EEPROM writes):
  see section 6.

---

## 2. The API subset you need

Exact signatures as reflected from the assembly (C#-style; `long` = Int64):

| Method | Purpose | Notes |
| --- | --- | --- |
| `long MICRO_USB2_initialize()` | Load the library | Once per process. 37 = already initialized |
| `long MICRO_USB2_getModuleConnectionList(short[] list, ref ushort number)` | Enumerate boards | Pass an 8-slot array (max 8 units per PC) and `number = 0`; `number` is an **output** (count). Undetected slots read -1 |
| `long MICRO_USB2_open(long id)` | Open a board | `id` from the list (0 for the first) |
| `long MICRO_USB2_close(long id)` | Close it | Before `uninitialize` |
| `long MICRO_USB2_uninitialize()` | Unload | 39 = a device is still open |
| `long MICRO_USB2_getModuleInformationForLabVIEW(long id, ref string vender, ref string product, ref string serial, ref string firmware, ref string driver, ref string library)` | Identity | Strings are **NUL-padded**: trim `'\0'` |
| `long MICRO_USB2_getSpectroInformationForLabVIEW(long id, ref string unit, ref string sensor)` | Head serial and sensor | Works before and after `open` |
| `long MICRO_USB2_getModulePropertyForLabVIEW(long id, ref long pixel, ref long resolution, ref long capturemode)` | 288, 16, 32 | `capturemode` is a capability mask (reads 32), not the current mode |
| `long MICRO_USB2_getExposurePropertyForLabVIEW(long id, ref double lowertime, ref double uppertime, ref double timestep, ref double lowercycle, ref double uppercycle, ref double cyclestep)` | Limits in **µs** | 11 / 1e6 / 1 / 1043 / 1.1e6 / 1 |
| `long MICRO_USB2_getCaptureMode(long id, ref long mode)` | Current mode | Persists in the board |
| `long MICRO_USB2_setCaptureMode(long id, long mode)` | Set mode | **Use 0 only** (section 5.3) |
| `long MICRO_USB2_setExposureTime(long id, double sec)` | Exposure in **µs** despite the name | 31 underTime below 11, 30 overTime above the limit. Sets the cycle too |
| `long MICRO_USB2_getExposureTime(long id, ref double sec)` | Read back (µs) | |
| `long MICRO_USB2_getCalibrationCoefficient(long id, long index, double[] coefficient)` | Wavelength polynomial | `index 0`, 6 doubles. `index 1` (sensitivity) is unused: NaN |
| `long MICRO_USB2_captureStart(long id)` | Start one frame (mode 0) | About 13 ms inside the DLL |
| `long MICRO_USB2_getCaptureStatus(long id)` | **0 = a frame is ready**, 1 = not yet, 4 = data loss | Poll this, not `getImageData` |
| `long MICRO_USB2_getImageData(long id, ref ushort[] imageData, ref ulong[] time)` | Read the frame | 288-element `ushort[]`, 1-element `ulong[]`. Consumes the frame: the next call answers 6 |
| `long MICRO_USB2_captureStop(long id)` | Stop | Also right after `open`, to clear leftovers |

Optional, read-only, informational: `getTriggerOutput` (reads 1),
`getTriggerPolarity` (reads 3), `getExposureCycle(id, ref double, "ichino")`,
`getWaveLengthPropertyForLabVIEW(id, ref long lower, ref long upper, "ichino")`
(200, 800), `getImageSize(id, ref long x, ref long y, "ichino")` (288, 1),
`GetBootVersion(id, ref string, "ichino")`.

The complete 45-method list is in Appendix A.

---

## 3. Return codes (`Usb2Struct.Cusb2Err`)

| Code | Name | Where you will meet it |
| --- | --- | --- |
| 0 | usb2Success | everywhere; for `getCaptureStatus` it means "a captured frame exists" |
| 1 | usb2Err_unsuccess | `getCaptureStatus`: no frame yet (normal while waiting) |
| 2 | usb2Err_notList | `getModuleConnectionList` when the library failed to initialize (no board) |
| 3 | usb2Err_notAccess | `initialize` with **no board attached**; also "failed to access device" |
| 4 | usb2Err_notFrame | `getCaptureStatus`: data lost. Stop and restart the capture |
| 5 | usb2Err_notAcPower | external power missing (not this model) |
| 6 | usb2Err_notImage | `getImageData`: buffer empty or already consumed |
| 10 | usb2Err_invalidDeviceIndex | wrong `id` |
| 11 | usb2Err_invalidMode | `setCaptureMode` with 3 or above |
| 12 | usb2Err_invalidParameter | wrong `strCheck`; also `setExposureTime` above the cycle |
| 25 | usb2Err_invalidData | `getImageData`: frame flagged invalid by its header. Discard, restart |
| 26 | usb2Err_invalidIndex | `getCalibrationCoefficient` with index outside 0..1 |
| 30 / 31 | usb2Err_overTime / underTime | `setExposureTime` outside 11..1,000,000 µs |
| 35 | usb2Err_nowCapturing | a setter called while a capture runs |
| 37 | usb2Err_initialized | `initialize` twice |
| 38 | usb2Err_opened | `open` twice |
| 39 | usb2Err_existDevice | `uninitialize` with a board still open |
| 40 | usb2Err_FirmCorrupted | firmware not written properly |

Full table (0..42) in Appendix B. Map these to your application's error
strings: the log is far more useful with names than with numbers.

---

## 4. The working sequence (verified)

This is the sequence of Hamamatsu's `SampleAPL`, with the values the unit
returned. Follow it literally; every deviation we tried is listed in
section 6 with what it broke.

### 4.1 Session start

```
initialize()                                        -> 0
getModuleConnectionList(short[8], ref number=0)     -> 0; number = 1; list = [0, 0, 0, ...]  => id = 0
getSpectroInformationForLabVIEW(0, ...)             -> 0; unit "26C00021", sensor "S11639"
open(0)                                             -> 0
captureStop(0)                                      -> 0   (clears a capture a killed process may have left)
getModuleInformationForLabVIEW(0, ...)              -> 0; "Hamamatsu", "C13016", "326VA021", "1.08", "1.00", "1.00"
getModulePropertyForLabVIEW(0, ...)                 -> 0; pixel 288, resolution 16, capturemode 32
getExposurePropertyForLabVIEW(0, ...)               -> 0; 11, 1000000, 1, 1043, 1100000, 1   (µs)
getCaptureMode(0, ref mode)                         -> 0; mode 0     (if not 0: setCaptureMode(0, 0) -> 0)
getCalibrationCoefficient(0, 0, double[6])          -> 0; [317.769, 2.70069, -1.1445e-3, -8.5309e-6, 1.4963e-8, -5.7063e-12]
setExposureTime(0, 10000.0)                         -> 0   (10 ms)
getExposureTime(0, ref sec)                         -> 0; 10000.0
getExposureCycle(0, ref sec, "ichino")              -> 0; 12000.0   (set by the DLL: exposure + 2000)
```

### 4.2 One frame (repeat for every frame)

```
captureStart(0)                                     -> 0            [about 13 ms]
loop:  getCaptureStatus(0)                          -> 1, 1, ..., 0 [0 after about one cycle: 11 to 22 ms at 10 ms]
getImageData(0, ref ushort[288], ref ulong[1])      -> 0; 288 counts (about 5250..5350 dark), time[0] = 22
captureStop(0)                                      -> 0
```

After the read, `getImageData` answers `6` and `getCaptureStatus` answers `1`
until the next `captureStart`.

### 4.3 Session end

```
captureStop(0) ; close(0) -> 0 ; uninitialize() -> 0
```

### 4.4 Changing the integration time

Only between frames (never while a `captureStart` is pending):
`captureStop`, `setExposureTime(us)`, optionally `getExposureTime` to confirm,
then the next `captureStart`. Do not touch the exposure cycle.

---

## 5. Behaviours and measurements to design around

### 5.1 Exposure and the automatic cycle

`setExposureTime` takes **microseconds** (the parameter is named `sec` and the
vendor samples' comment says "msec"; both are wrong). On every call the DLL
sets the exposure *cycle* (frame period) to **exposure + 2,000 µs**:

| setExposureTime (µs) | Return | Cycle read back (µs) |
| --- | --- | --- |
| 5 | 31 underTime | unchanged |
| 11 | 0 | 2,011 |
| 1,000 | 0 | 3,000 |
| 10,000 | 0 | 12,000 |
| 50,000 | 0 | 52,000 |
| 1,000,000 | 0 | 1,002,000 |
| 2,000,000 | 0 (accepted although above the advertised limit) | 2,002,000 |

Clamp your integration time to the advertised 11 µs .. 1,000,000 µs and let
the DLL handle the cycle.

### 5.2 Frame timing (mode 0, this PC)

| Exposure | Time to `getCaptureStatus == 0` after start | Start/status/read/stop round trip | Frames per second |
| --- | --- | --- | --- |
| 11 µs | 0.2 .. 0.6 ms | about 26 ms | about 38 |
| 1 ms | 0.2 .. 1.9 ms | about 25 ms | about 40 |
| 10 ms | 0.2 .. 10 ms (about one cycle) | 26 .. 33 ms | about 35 |
| 100 ms | about 100 ms | about 132 ms | about 7.5 |

The round trip is dominated by `captureStart` (about 13 ms) and the USB calls
(each getter takes 7 to 9 ms; `getCaptureStatus`, `getImageData` and
`captureStop` under 1 ms). Poll `getCaptureStatus` every 1 ms (short
exposures) to about 20 ms (long exposures).

### 5.3 Capture modes

| Mode | Name (assembly enum) | Documented | Behaviour observed |
| --- | --- | --- | --- |
| 0 | SINGLE_SHOT ("specified number of measurement mode") | yes | one frame per `captureStart`; the frame waits in the DLL buffer until read (waited 2 s unread, intact) |
| 1 | FREE_RUN | **no** (only in the enum) | delivered exactly one frame in 2 s, then `setCaptureMode` **never returned** and the process had to be killed. The board itself came back in mode 0 for the next process |
| 2 | TRIGGER | yes | waits for an external trigger; without one `getCaptureStatus` stays 1 for ever and `getImageData` returns **0 with an all-zero buffer** (an "echo", not a frame) |
| 3+ | EDGE / UNKNOWN | no | `setCaptureMode` answers 11 invalidMode |

The mode **persists in the board** across processes and power cycles. Read it
after `open` and set 0 if it is anything else (safe when nothing is being
captured; send `captureStop` first).

### 5.4 Frame semantics

* `getCaptureStatus == 0` is the only reliable "frame ready" signal.
* `getImageData` **consumes** the frame: a second call answers 6 notImage
  with an untouched (zero) buffer. Never treat code 0 + all zeros as data.
* The `time` value (22, 18, 24, 24, 24 ...) **repeats** between frames; it is
  not a frame counter. Identify a new frame by having consumed the previous
  one, not by the timestamp.
* In mode 0 a second `captureStart` after the frame was read works without a
  `captureStop` in between, but Hamamatsu's samples always stop; do the same.
* A `captureStart` while a capture is running in mode 1 or 2 threw a
  `SynchronizationLockException` inside the DLL on the bench and in one case
  blocked the DLL. Never issue overlapping starts.

### 5.5 Data

* 288 `ushort` values, 16 bit, saturation at 65535.
* Dark level about 5,250 .. 5,300 counts at 1 .. 100 ms, about 9 counts rms
  frame-to-frame noise. Subtract a dark frame in your application.
* Wavelength: λ[nm] = A0 + B1·p + ... + B5·p⁵. The specification calls `p` an
  "arbitrary pixel number"; SciLab uses p = 1..288 (Hamamatsu's numbering).
  Verify the offset against a known emission line once.
* **The board's stored polynomial may belong to a different head.** Our board
  reports 320.5 .. 888.5 nm (a C12880MA range) while the head is a UV
  C16767MA-20 (190 .. 440 nm). Take the coefficients from the head's final
  inspection sheet and let them override the board. SciLab reads them from
  `hama6_wavelength_coefficients.json` keyed by head serial.
* Settings persist in the board: after our tests the exposure read 5,000 µs
  and the cycle 7,000 µs in the next process. Always set what you need after
  `open`; never assume defaults.

---

## 6. Things that break it (all findings, with what happened)

1. **Never call `MICRO_USB2_setExposureCycle`.** SciLab's earlier driver set
   the cycle to exposure + 1,032 µs (the difference between the cycle and
   exposure lower limits). With that cycle the board **never delivered a
   frame in any mode** (two bench days of "no frame"). The same board, with
   the DLL's own exposure + 2,000 µs, delivers a frame 11 ms after each
   start. The specification deleted `setExposureCycle` in 2015; the DLL still
   exports it behind the `"ichino"` key.
2. **Never select capture mode 1.** It hangs the DLL on the following
   `setCaptureMode` (reproduced 2026-09-18 in an isolated process). Only a
   process kill gets out; the board recovers on its own.
3. **Do not expect frames in mode 2** without an external trigger on the
   board's trigger input, and do not accept its all-zero code-0 answer as a
   frame.
4. **Poll `getCaptureStatus`, not `getImageData`.** The old driver polled
   `getImageData`, saw 6 for ever and concluded the board was dead.
5. **Never overlap `captureStart` calls** (see 5.4).
6. **A killed process leaves the board capturing.** In the next process
   `captureStart` may throw a `SynchronizationLockException`; the DLL cannot
   stop a capture it did not start. Recovery that worked: `captureStop`, retry
   the start once; if it throws again, `close`, `open`, `captureStop`, retry
   once more; then ask the operator to unplug and re-plug the board.
   Prevention: send `captureStop` right after `open`, and `captureStop`,
   `close`, `uninitialize` on every shutdown path.
7. **Run every DLL call on one dedicated thread with a watchdog.** The DLL
   can block for ever (items 2 and 5). Report "blocked inside `<call>`,
   restart the application and re-plug the board" instead of freezing the UI.
   SciLab uses 15 s.
8. **Only one process at a time.** With `u-ApsSpecEvaluation.exe` open,
   `open` fails. With no board attached, `initialize` answers 3 notAccess and
   `getModuleConnectionList` answers 2 notList; do not mistake that for a
   stuck DLL: check Device Manager for "C13016 USB2(HPK)".
9. **Identity strings are NUL-padded** (`"326VA021\0\0\0..."`): trim before
   comparing or displaying.
10. **`number` in `getModuleConnectionList` is an output.** Pass 0 (the
    samples do). Passing the array length also worked; passing anything and
    reading the array beyond `number` does not: slots are 0 or -1.
11. **Exposure is in microseconds** despite `sec`. `setExposureTime(10)` is
    10 µs, refused as 31 underTime below 11.
12. **`capturemode` from `getModuleProperty` (32) is a mask, not the mode.**
    Use `getCaptureMode`.
13. **Trigger polarity reads 3**, which is not one of the specification's
    values (1 falling, 0x11 rising). Leave it alone unless you use the
    trigger input.
14. **pythonnet traps** (Python only): pythonnet 3's overload resolution
    rejects the `ref` parameters and typed scalars ("No method matches given
    arguments"), and a Python `int` written into a .NET `object[]` becomes a
    `Python.Runtime.PyInt` that `MethodInfo.Invoke` cannot convert to
    `Int64`. Call through reflection and box every scalar inside the CLR
    (one-element typed array copied into the `object[]`), as in section 8.2.
15. **Console encoding** (any language): the assembly's string literals
    include characters outside cp1252 (e.g. `★`); if you ever print
    reflection output to a Windows console or a redirected file, use UTF-8.

---

## 7. Recommended driver design for the operational software

1. **Lifecycle**: `initialize` once per process (reference-count it if
   several device objects share the DLL); `open`; `captureStop`; read
   identity, property, limits, mode, coefficients; `setCaptureMode(0)` if
   needed; `setExposureTime`; on shutdown `captureStop`, `close`,
   `uninitialize` in every exit path (including exceptions and UI close).
2. **One DLL thread + watchdog**: marshal every call onto one worker thread;
   wait with a timeout (10 to 15 s); on timeout mark the device blocked and
   surface a clear message (restart + re-plug). Never call the DLL from the
   UI thread.
3. **Frame loop** exactly as in 4.2. Timeout per frame: at least 2 s, or
   3 × exposure + 1 s. On `getCaptureStatus == 4` or `getImageData == 25`,
   `captureStop` and restart (limit to about 3 retries, then error). Reject a
   code-0 frame whose buffer is all zeros.
4. **Integration-time changes** only between frames: `captureStop` first,
   then `setExposureTime`, read back and compare, then continue. Clamp to
   11 µs .. 1,000,000 µs. Do not write the cycle.
5. **Averaging / noise**: accumulate frames in your application (mean and
   standard deviation per pixel); the board itself averages nothing.
   Saturation is 65535.
6. **Wavelength axis**: prefer the inspection-sheet coefficients keyed by
   head serial; fall back to the board's polynomial; fall back to the pixel
   index. Verify the pixel-numbering offset once against a known line.
7. **Discovery**: `initialize`, `getModuleConnectionList`, `uninitialize`
   gives you the count without opening; `open` by index for multiple units
   (up to 8).
8. **Diagnostics**: log every status code by name (Appendix B); on failure
   include the last codes of `initialize`, `getModuleConnectionList`, `open`,
   `setCaptureMode`, `setExposureTime`, `captureStart`, `getCaptureStatus`
   and `getImageData`, the capture mode and the exposure cycle read back.
9. **Recovery**: the sequence in section 6 item 6 for a throwing start; a
   plain "unplug and re-plug" instruction for anything the watchdog catches.
10. **Expectations to communicate**: about 35 frames per second at 10 ms, one
    frame per exposure at long exposures, no hardware averaging, no internal
    trigger-free continuous mode.

---

## 8. Integration by language

### 8.1 C# / VB.NET / any .NET application (recommended)

Reference `MICRO_USB2_CLR.dll` directly (Add Reference, or `<Reference
Include="MICRO_USB2_CLR"><HintPath>...</HintPath></Reference>`). Target .NET
Framework 4.x (the vendor samples target 3.5; the CLR 4 loaded it fine; .NET
Core / .NET 5+ was not tested and may need the Windows compatibility pack).
Ship the DLL next to your executable; the operator still needs Hamamatsu's
USB driver installed once.

The following program compiled with the .NET Framework 4 compiler
(`csc -platform:anycpu -r:MICRO_USB2_CLR.dll C13016Example.cs`) and runs
the verified sequence:

```csharp
// Minimal C13016 acquisition with MICRO_USB2_CLR.dll (Hamamatsu's SampleAPL sequence).
using System;
using System.Threading;
using MICRO_USB2_CLR_DLL;

static class C13016Example
{
    const long OK = 0;                       // Usb2Struct.Cusb2Err.usb2Success
    const long STATUS_NO_FRAME = 1;          // getCaptureStatus: no captured frame yet
    const long STATUS_DATA_LOSS = 4;         // getCaptureStatus: stop and restart the capture

    static void Check(long code, string what)
    {
        if (code != OK) throw new Exception(what + " returned " + code + " (" + (Usb2Struct.Cusb2Err)code + ")");
    }

    static int Main()
    {
        var dll = new MICRO_USB2_CLR();
        Check(dll.MICRO_USB2_initialize(), "initialize");          // 3 notAccess: no board attached
        try
        {
            short[] list = new short[8];         // at most 8 units per PC
            ushort number = 0;                   // output: how many were detected
            Check(dll.MICRO_USB2_getModuleConnectionList(list, ref number), "getModuleConnectionList");
            if (number == 0) { Console.WriteLine("no C13016 found"); return 1; }
            long id = list[0];

            Check(dll.MICRO_USB2_open(id), "open");
            try
            {
                dll.MICRO_USB2_captureStop(id);                  // clear a capture a killed process left running

                string vendor = null, product = null, serial = null, firmware = null, driver = null, library = null;
                Check(dll.MICRO_USB2_getModuleInformationForLabVIEW(id, ref vendor, ref product, ref serial,
                                                                    ref firmware, ref driver, ref library), "getModuleInformation");
                string unit = null, sensor = null;
                dll.MICRO_USB2_getSpectroInformationForLabVIEW(id, ref unit, ref sensor);
                Console.WriteLine("{0} {1} serial {2} firmware {3}, head {4} sensor {5}",
                    vendor.TrimEnd('\0'), product.TrimEnd('\0'), serial.TrimEnd('\0'), firmware.TrimEnd('\0'),
                    (unit ?? "").TrimEnd('\0'), (sensor ?? "").TrimEnd('\0'));

                long pixels = 0, bits = 0, modeMask = 0;
                Check(dll.MICRO_USB2_getModulePropertyForLabVIEW(id, ref pixels, ref bits, ref modeMask), "getModuleProperty");

                long mode = -1;
                Check(dll.MICRO_USB2_getCaptureMode(id, ref mode), "getCaptureMode");
                if (mode != 0)                                     // mode 0: one frame per captureStart. Never use 1.
                    Check(dll.MICRO_USB2_setCaptureMode(id, 0), "setCaptureMode(0)");

                double exposureUs = 10000.0;                      // microseconds although the parameter is named "sec"
                Check(dll.MICRO_USB2_setExposureTime(id, exposureUs), "setExposureTime");
                // Do NOT call MICRO_USB2_setExposureCycle: the DLL sets the frame period itself (exposure + 2000 us).

                double[] coefficients = new double[6];             // A0, B1..B5: nm = A0 + B1*p + ... + B5*p^5
                Check(dll.MICRO_USB2_getCalibrationCoefficient(id, 0, coefficients), "getCalibrationCoefficient");

                ushort[] image = new ushort[(int)pixels];
                ulong[] time = new ulong[1];
                for (int frame = 0; frame < 5; frame++)
                {
                    Check(dll.MICRO_USB2_captureStart(id), "captureStart");
                    long status;
                    var deadline = DateTime.UtcNow.AddSeconds(2 + 3 * exposureUs / 1e6);
                    while ((status = dll.MICRO_USB2_getCaptureStatus(id)) == STATUS_NO_FRAME)
                    {
                        if (DateTime.UtcNow > deadline) throw new TimeoutException("no frame");
                        Thread.Sleep(1);
                    }
                    if (status == STATUS_DATA_LOSS) { dll.MICRO_USB2_captureStop(id); frame--; continue; }   // restart
                    Check(status, "getCaptureStatus");
                    Array.Clear(image, 0, image.Length);
                    Check(dll.MICRO_USB2_getImageData(id, ref image, ref time), "getImageData");
                    Check(dll.MICRO_USB2_captureStop(id), "captureStop");
                    long sum = 0; int max = 0;
                    foreach (var v in image) { sum += v; if (v > max) max = v; }
                    Console.WriteLine("frame {0}: mean {1:F1} max {2} time {3}", frame + 1, (double)sum / image.Length, max, time[0]);
                }
            }
            finally { dll.MICRO_USB2_close(id); }
        }
        finally { dll.MICRO_USB2_uninitialize(); }
        return 0;
    }
}
```

In VB.NET the vendor's `Module1.vb` is already this program (with the `msec`
comment corrected to microseconds). In production wrap the calls in the
worker-thread/watchdog of section 7.

### 8.2 Python (pythonnet)

`pip install pythonnet`. Because of the two pythonnet traps (section 6 item
14) do not call the methods as attributes; use reflection with CLR-boxed
scalars. This is the pattern SciLab's driver uses (`MicroUsb2Api` in
`spectrometers/hama6_spectrometer.py`), condensed:

```python
import time
import clr                                   # pip install pythonnet (loads the .NET Framework CLR)
import System
from System.Reflection import Assembly

asm = Assembly.LoadFrom(r"C:\path\to\MICRO_USB2_CLR.dll")
cls = asm.GetType("MICRO_USB2_CLR_DLL.MICRO_USB2_CLR")
dev = System.Activator.CreateInstance(cls)

def call(name, *args):
    """Invoke a MICRO_USB2_CLR method; returns (status code, argument values after the call)."""
    method = cls.GetMethod(name)                              # no overloads in this assembly
    params = method.GetParameters()
    boxed = System.Array.CreateInstance(System.Object, len(args))
    for i, (p, v) in enumerate(zip(params, args)):
        ptype = p.ParameterType.GetElementType() if p.ParameterType.IsByRef else p.ParameterType
        if isinstance(v, (bool, int, float, str)):
            holder = System.Array.CreateInstance(ptype, 1)    # box the scalar inside the CLR ...
            holder[0] = v
            System.Array.Copy(holder, 0, boxed, i, 1)         # ... not as a Python.Runtime.PyInt
        elif v is not None:
            boxed[i] = v                                      # a .NET array
    code = int(method.Invoke(dev, boxed))
    return code, [boxed[i] for i in range(len(args))]        # ref/out values are read back from the object[]

assert call("MICRO_USB2_initialize")[0] == 0                  # 3 notAccess: no board attached
ids = System.Array.CreateInstance(System.Int16, 8)
code, (ids, number) = call("MICRO_USB2_getModuleConnectionList", ids, 0)
dev_id = int(ids[0])
assert call("MICRO_USB2_open", dev_id)[0] == 0
call("MICRO_USB2_captureStop", dev_id)                        # clear a capture left by a killed process
code, (_, mode) = call("MICRO_USB2_getCaptureMode", dev_id, 0)
if int(mode) != 0:
    assert call("MICRO_USB2_setCaptureMode", dev_id, 0)[0] == 0   # one frame per captureStart; never mode 1
assert call("MICRO_USB2_setExposureTime", dev_id, 10000.0)[0] == 0   # microseconds
image = System.Array.CreateInstance(System.UInt16, 288)
stamp = System.Array.CreateInstance(System.UInt64, 1)
for frame in range(5):
    assert call("MICRO_USB2_captureStart", dev_id)[0] == 0
    deadline = time.monotonic() + 3.0
    while (status := call("MICRO_USB2_getCaptureStatus", dev_id)[0]) == 1:   # 1: no frame yet
        if time.monotonic() > deadline:
            raise TimeoutError("no frame")
        time.sleep(0.001)
    if status == 4:                                                          # data loss: restart
        call("MICRO_USB2_captureStop", dev_id)
        continue
    code, (_, image, stamp) = call("MICRO_USB2_getImageData", dev_id, image, stamp)
    call("MICRO_USB2_captureStop", dev_id)
    counts = [int(v) for v in image]
    print(frame + 1, code, sum(counts) / len(counts), max(counts), int(stamp[0]))
call("MICRO_USB2_close", dev_id)
call("MICRO_USB2_uninitialize")
```

For anything beyond a script, reuse SciLab's `MicroUsb2Api`, `Buf`, `OUT`
and `load_clr_assembly` (they add signature reflection, buffer allocation and
the watchdog thread) or the whole `Hama6_Spectrometer` class.

### 8.3 C++ and other native (non-.NET) applications

The assembly has no C exports, so a native program has three options:

1. **C++/CLI wrapper** (mixed-mode DLL): compile with `/clr`, reference the
   assembly, expose plain C functions (`extern "C" __declspec(dllexport)`)
   that run the sequence of section 4. Hamamatsu's `SampleAPL.cpp` is a
   C++/CLI program you can start from. Requires MSVC and the .NET Framework
   on the target PC.
2. **Out-of-process bridge**: a small .NET console/service (the C# program
   above plus a named pipe or TCP socket) that owns the DLL and streams
   frames; your native application talks to the bridge. This also isolates
   your process from a DLL deadlock (kill and restart the bridge, the board
   recovers).
3. **COM-visible wrapper**: a .NET class library with `[ComVisible(true)]`
   registered with `regasm`, used from C++ via COM. Works, but deployment is
   heavier than 1 or 2.

Do **not** try to drive the board with libusb/WinUSB directly from C: the
protocol is undocumented and the board would need the vendor driver replaced.

### 8.4 LabVIEW

The `...ForLabVIEW` methods exist for this: use a .NET Constructor Node on
`MICRO_USB2_CLR_DLL.MICRO_USB2_CLR`, then Invoke Nodes for the sequence in
section 4; `ref` parameters appear as output terminals. Run the calls in one
loop (one thread) and add a timeout around the whole `captureStart` ...
`captureStop` group.

---

## 9. Acceptance checklist for the integration

1. Device Manager shows "C13016 USB2(HPK)" with no warning; no other program
   has the board open.
2. `initialize` → 0, one module listed, `open` → 0, identity strings match
   the label (trimmed).
3. `getCaptureMode` → 0 (or set to 0), `getExposureCycle` = exposure + 2,000 µs
   after `setExposureTime`.
4. Ten consecutive frames at 10 ms in under 0.4 s, none identical, mean dark
   about 5,300 counts, no code other than 0 / 1 in the loop.
5. Exposure change 10 → 100 ms mid-session: frames follow (mean rises,
   round trip about 130 ms).
6. Stop during a long run returns within one frame; the next run starts
   normally.
7. Kill the application during a run, restart it: connect succeeds (the
   safety `captureStop` clears the leftover), or the recovery sequence
   reports "re-plug" clearly. Never a frozen UI.
8. Unplug the board: `initialize` (new session) → 3 and the message names
   the missing device.
9. Wavelength axis checked against one known line with the inspection-sheet
   coefficients.

---

## 10. Reference implementation and tools in this repository

| File | What it gives you |
| --- | --- |
| `spectrometers/hama6_spectrometer.py` | The verified driver: reflection layer (`MicroUsb2Api`), watchdog thread (`_CallThread`), connect sequence, frame loop with retries, recovery, wavelength handling |
| `tests/test_hama6.py` | A fake of the DLL and board with the behaviour above; a good specification of what a driver must tolerate |
| `tools/diagnose_c13016.py` | Runs the vendor sequence against the board and prints everything (API, enums, identity, exposure sweep, frame timings); `--trigger` and `--free-run` reproduce the mode 2 / mode 1 behaviour |
| `README.md` (Hama6 section), `docs/system-design-document.md` (Hamamatsu C13016) | User-facing summary and the design/history record |

---

## Appendix A. Public API of `MICRO_USB2_CLR` (45 methods, as reflected)

```
MICRO_USB2_initialize() -> Int64
MICRO_USB2_uninitialize() -> Int64
MICRO_USB2_getModuleConnectionList(Int16[] list, ref UInt16 number) -> Int64
MICRO_USB2_open(Int64 id) -> Int64
MICRO_USB2_close(Int64 id) -> Int64
MICRO_USB2_getModuleInformation(Int64 id, ref ModuleInformation information) -> Int64
MICRO_USB2_getModuleInformationForLabVIEW(Int64 id, ref String vender, ref String product, ref String serial, ref String firmware, ref String driver, ref String library) -> Int64
MICRO_USB2_getSpectroInformation(Int64 id, ref SpectroInformation information) -> Int64
MICRO_USB2_getSpectroInformationForLabVIEW(Int64 id, ref String unit, ref String sensor) -> Int64
MICRO_USB2_getModuleProperty(Int64 id, ref ModuleProperty ModuleProperty) -> Int64
MICRO_USB2_getModulePropertyForLabVIEW(Int64 id, ref Int64 pixel, ref Int64 resolution, ref Int64 capturemode) -> Int64
MICRO_USB2_getExposureProperty(Int64 id, ref ExposureProperty ExposureProperty) -> Int64
MICRO_USB2_getExposurePropertyForLabVIEW(Int64 id, ref Double lowertime, ref Double uppertime, ref Double timestep, ref Double lowercycle, ref Double uppercycle, ref Double cyclestep) -> Int64
MICRO_USB2_getCaptureMode(Int64 id, ref Int64 mode) -> Int64
MICRO_USB2_setCaptureMode(Int64 id, Int64 mode) -> Int64
MICRO_USB2_getExposureTime(Int64 id, ref Double sec) -> Int64
MICRO_USB2_setExposureTime(Int64 id, Double sec) -> Int64
MICRO_USB2_getExposureCycle(Int64 id, ref Double sec, String strCheck) -> Int64        (key-protected, read only)
MICRO_USB2_setExposureCycle(Int64 id, Double sec, String strCheck) -> Int64            (key-protected; DO NOT USE)
MICRO_USB2_getTriggerPolarity(Int64 id, ref Int64 polarity) -> Int64
MICRO_USB2_setTriggerPolarity(Int64 id, Int64 polarity) -> Int64
MICRO_USB2_getTriggerOutput(Int64 id, ref Int64 output) -> Int64
MICRO_USB2_setTriggerOutput(Int64 id, Int64 output) -> Int64
MICRO_USB2_getCalibrationCoefficient(Int64 id, Int64 index, Double[] coefficient) -> Int64
MICRO_USB2_setCalibrationCoefficient(Int64 id, Int64 index, Double[] coefficient) -> Int64   (writes the board; avoid)
MICRO_USB2_getWaveLengthProperty(Int64 id, ref WaveLengthProperty WaveLengthProperty, String strCheck) -> Int64
MICRO_USB2_getWaveLengthPropertyForLabVIEW(Int64 id, ref Int64 lowerwavelength, ref Int64 upperwavelength, String strCheck) -> Int64
MICRO_USB2_getImageSize(Int64 id, ref Int64 sizeX, ref Int64 sizeY, String strCheck) -> Int64
MICRO_USB2_captureStart(Int64 id) -> Int64
MICRO_USB2_captureStop(Int64 id) -> Int64
MICRO_USB2_getCaptureStatus(Int64 id) -> Int64
MICRO_USB2_getImageData(Int64 id, ref UInt16[] imageData, ref UInt64[] time) -> Int64
MICRO_USB2_getImageHeaderData(Int64 id, ref UInt16[] headerData, ref UInt16[] imageData, ref UInt64[] time, String strCheck) -> Int64
MICRO_USB2_setEventHandle(Int64 id, Int64 eventid, ManualResetEvent hEvent, String strCheck) -> Int64
MICRO_USB2_getADMode(Int64 id, ref Int64 ADMode, String strCheck) -> Int64             (reads 1)
MICRO_USB2_setADMode(Int64 id, Int64 ADMode, String strCheck) -> Int64                 (avoid)
MICRO_USB2_getAdOffset(Int64 id, ref Int64 offset, String strCheck) -> Int64           (reads 0)
MICRO_USB2_setAdOffset(Int64 id, Int64 offset, String strCheck) -> Int64               (avoid)
MICRO_USB2_getAdOffsetProperty(Int64 id, ref AdOffsetProperty AdOffsetProperty, String strCheck) -> Int64
MICRO_USB2_getAdOffsetPropertyForLabVIEW(Int64 id, ref Int64 lower, ref Int64 upper, String strCheck) -> Int64   (-10000, 10000)
MICRO_USB2_GetBootVersion(Int64 id, ref String strVersion, String strCheck) -> Int64   ("1.1.3")
MICRO_USB2_readEeprom(Int64 id, UInt16 address, ref Byte[] data, UInt32 length, String strCheck) -> Int64
MICRO_USB2_writeEeprom(Int64 id, UInt16 address, ref Byte[] data, UInt32 length, String strCheck) -> Int64        (avoid)
MICRO_USB2_readEepromUserArea(Int64 id, UInt16 address, ref Byte[] data, UInt32 length, String strCheck) -> Int64
MICRO_USB2_writeEepromUserArea(Int64 id, UInt16 address, ref Byte[] data, UInt32 length, String strCheck) -> Int64 (avoid)
```

`strCheck` must be `"ichino"` or the method answers 12 invalidParameter.

## Appendix B. Enums of the assembly

`Usb2Struct.Cusb2Err`: 0 usb2Success, 1 unsuccess, 2 notList, 3 notAccess,
4 notFrame, 5 notAcPower, 6 notImage, 7 notBuffer, 8 notData, 9 notDownload,
10 invalidDeviceIndex, 11 invalidMode, 12 invalidParameter,
13 invalidTriggermode, 14 invalidTriggerpolarity, 15 invalidTriggeroutput,
16 invalidBinning, 17 invalidPosition, 18 invalidSize, 19 invalidGain,
20 invalidOffset, 21 invalidCoolingstatus, 22 invalidFrame, 23 invalidEventID,
24 invalidEventhandle, 25 invalidData, 26 invalidIndex, 27 invalidNumber,
28 invalidAddress, 29 invalidLength, 30 overTime, 31 underTime,
32 overTemperature, 33 underTemperature, 34 overArea, 35 nowCapturing,
36 nowFirmUpdate, 37 initialized, 38 opened, 39 existDevice,
40 FirmCorrupted, 41 ReadImageStatus, 42 InvalidEepromParameter.

`Usb2Struct.Usb2CaptureMode`: 0 SINGLE_SHOT, 1 FREE_RUN, 2 TRIGGER, 3 EDGE,
4..9 UNKNOWN1..6, 10 END (the specification documents 0 and 2 only).

`Usb2Struct.Usb2TriggerMode`: 0 SYNCHRO_INTERNAL, 1 ASYNCHRONOUS_SOFTWARE,
2 SYNCHRO_SOFTWARE, 3 ASYNCHRONOUS_EXTERNAL_EDGE_SENSE,
4 ASYNCHRONOUS_EXTERNAL_LEVEL_SENSE, 5 SYNCHRO_EXTERNAL_EDGE_SENSE,
6 SYNCHRO_EXTERNAL_LEVEL_SENSE, ... (no public setter uses it for this board).

## Appendix C. Excerpt of the verified session (2026-09-18, 10 ms, mode 0)

```
initialize() -> 0 (success)
getModuleConnectionList(Buf(8), 0) -> 0 (success) slots=[[0, 0, 0, 0, 0, 0, 0, 0], 1]
getSpectroInformationForLabVIEW(0) -> 0 slots=['26C00021', 'S11639']
open(0) -> 0 ; captureStop(0) -> 0 ; status after safety stop: 1
getModuleInformationForLabVIEW(0) -> 0 slots=['Hamamatsu', 'C13016', '326VA021', '1.08', '1.00', '1.00']
getModulePropertyForLabVIEW(0) -> 0 slots=[288, 16, 32]
getExposurePropertyForLabVIEW(0) -> 0 slots=[11.0, 1000000.0, 1.0, 1043.0, 1100000.0, 1.0]
getCaptureMode(0) -> 0 slots=[0] ; getExposureTime(0) -> 0 slots=[11.0] ; getExposureCycle(0, 'ichino') -> 0 slots=[1043.0]
setCaptureMode(0, 0) -> 0 ; setExposureTime(0, 10000.0) -> 0 ; getExposureCycle -> 12000.0
capture 1: captureStart -> 0 [14.0 ms]; status 1 -> 0 after 21.3 ms
           getImageData -> 0, n=288 min=5247 max=5345 mean=5287.6, time [22]
           getImageData again -> 6 (notImage), all zeros ; status after read: 1 ; captureStop -> 0
capture 2: ready after 11.0 ms, mean 5275.9, time [18]
capture 3: ready after 21.6 ms, mean 5272.5, time [32]
close(0) -> 0 ; uninitialize() -> 0
```
