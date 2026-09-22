# Ocean Insight Flame-S Spectrometer - Driver, SDK, Requirements, Findings and Integration Guide

Unit: Flame-S, serial FLMS17243 (USB2000+ family). Software: OceanDirect SDK 3.1.4, Ocean Optics WinUSB driver 1.2.0. Target: SciGlob operational software (BlickO / BlickO-CPP, Python 3 tools). Evaluation date: 21 September 2026, PC Mini-Pan (Windows 11 Pro x64). Prepared with Claude Code from a hands-on session with the instrument.

## 1. Summary

This guide documents the Ocean Insight **Flame-S** spectrometer (serial **FLMS17243**) connected to this PC, the Windows driver and the **OceanDirect SDK 3.1.4** that drive it, every prerequisite for using it from our own software, the findings from a hands-on evaluation performed on 21 September 2026, and a concrete plan for adding the instrument to the SciGlob operational software (BlickO / BlickO-CPP and the Python 3 tools).

| Item | Value |
| --- | --- |
| Spectrometer | Ocean Insight Flame-S, serial FLMS17243, USB2000+ device family (USB VID 0x2457, PID 0x101E) |
| Detector / range | 2048 pixels, 339.68 to 1019.66 nm, 16-bit counts (full scale 65535) |
| Integration time | 1 ms to 655.35 s in 1 microsecond steps |
| Windows driver | WinUSB, Ocean Optics INF OOI_USB2000Plus.inf v1.2.0 (installed as oem185.inf), device status OK |
| SDK | OceanDirect 3.1.4 (C API, .NET wrapper, Python wrapper), installed 2026-09-21 in C:\Program Files\Ocean Optics\OceanDirect SDK |
| Host requirements | Windows 10/11 x64, VC++ 2015-2022 x64 runtime, a 64-bit host process (Python 3.9+ x64, x64 C++ or .NET) |
| Verified | Python 3.12 x64 reads spectra; 387 spectra/s at 1 ms and 200 spectra/s at 5 ms; a Tkinter viewer with CSV export works |

**Key conclusions**

- The spectrometer, driver and SDK are fully working on this PC. The OceanDirect Python wrapper delivers spectra at the hardware limit (one spectrum per integration time down to about 2.6 ms).
- OceanDirect.dll ships **64-bit only**. The legacy BlickO Python process is 32-bit Python 2.7 and therefore cannot load it in-process; it keeps using OmniDriver (Java) for Ocean Optics units. The natural home for OceanDirect is the **BlickO-CPP hardware abstraction layer**, which already reserves `RoeType::kOcOpt1` for exactly this adapter.
- The SDK enables **electric dark and nonlinearity correction by default**. The existing Blick Ocean Optics backend switches both off to record raw counts; the new adapter must do the same to keep Level-0 data comparable.
- Flame-S has no thermoelectric cooler and OceanDirect 3.1.4 has no generic temperature call, so the board temperature that the OmniDriver backend logged is not available through this SDK.
- The OceanDirect EULA limits the software to internal use on one device and prohibits redistribution. Shipping OceanDirect.dll inside the SciGlobPandora installer needs written confirmation from Ocean Insight first.

## 2. The spectrometer

### 2.1 Identity as reported by the device

| Property | Value | Source |
| --- | --- | --- |
| Model name | Flame-S | odapi_get_device_name / Spectrometer.get_model() |
| Device type (protocol family) | USB2000PLUS | odapi_get_device_type; readable before the device is opened |
| Serial number | FLMS17243 | odapi_get_serial_number; readable only after open |
| Firmware revision | Flame-S 4.02.0 | Advanced.get_revision_firmware() |
| FPGA revision | 4.05.5 | Advanced.get_revision_fpga() |
| USB vendor / product ID | 0x2457 / 0x101E | Windows PnP instance USB\VID_2457&PID_101E\5&39AD4324&0&6 |
| USB bus-reported description | Ocean Optics Flame-S | DEVPKEY_Device_BusReportedDeviceDesc |
| Device Manager name | Ocean Optics USB2000+ (WinUSB) | Driver INF string |
| USB endpoints | primary OUT 0x01, primary IN 0x81, secondary IN 0x82 | Advanced.get_usb_endpoint_*() |
| USB location on this PC | Port 6 on hub 2 | DEVPKEY_Device_LocationInfo |

### 2.2 Optical and detector parameters

| Parameter | Value |
| --- | --- |
| Pixels (formatted spectrum length) | 2048 |
| Detector | 2048-element linear silicon CCD (Sony ILX511B per the Ocean Insight Flame-S datasheet; not readable from the device) |
| Wavelength range | 339.68 nm (pixel 0) to 1019.66 nm (pixel 2047) |
| Wavelength calibration polynomial | lambda(p) = 339.68143 + 0.380685 p - 2.0555342e-05 p^2 - 1.5339606e-09 p^3  (p = pixel index) |
| Pixel pitch | 0.381 nm at the blue end, 0.334 nm mid-range, 0.277 nm at the red end |
| ADC / full scale | 16-bit, maximum intensity 65535 counts |
| Integration time | minimum 1000 us, maximum 655,350,000 us (655.35 s), increment 1 us; minimum with averaging 1000 us |
| Electric dark pixels | 15 pixels, indices 6 to 20 (used for electric dark correction) |
| Nonlinearity coefficients (8) | 0.88576, 4.57872e-06, 5.5994e-11, -4.88641e-15, 2.84584e-22, 3.169e-24, -5.26495e-29, 2.39364e-34 |
| Trigger mode (current) | 0 = normal / free running. The API defines 1 = software, 2 = synchronization, 3 = external hardware; consult the Flame datasheet for the exact mode set |
| Acquisition delay | 0 us (range 0 to 32767 us, 1 us increment) although the ACQUISITION_DELAY feature flag reads False |
| GPIO | 8 pins |
| Strobe / lamp | Lamp enable off, single strobe off, continuous strobe off |

### 2.3 Feature matrix (OceanDirect FeatureID flags)

| Reported as supported | Reported as not supported |
| --- | --- |
| SERIAL_NUMBER, SPECTROMETER, IRRADIANCE_CAL, EEPROM, STROBE_LAMP, NONLINEARITY_CAL, STRAYLIGHT_CAL, RAW_BUS_ACCESS, CONTINUOUS_STROBE, TEMPERATURE, REVISION, GPIO, QUERY_STATUS, BACK_TO_BACK, TIME_META, DEVICE_INFORMATION | THERMOELECTRIC, WAVELENGTH_CAL, LIGHT_SOURCE, OPTICAL_BENCH, PROCESSING, DATA_BUFFER, ACQUISITION_DELAY, PIXEL_BINNING, SINGLE_STROBE, LED_ACTIVITY, DHCP, IPV4_ADDRESS, PIXEL, AUTO_NULLING, USER_STRING, DEVICE_ALIAS, SERIAL_PORT, SPECTRUM_ACQUISITION_CONTROL, NETWORK_CONFIGURATION, ETHERNET, SHUTTER, HIGH_GAIN_MODE |

Calls that returned an error on this unit ("No such feature on device" or "Command not supported"): system revision, USB VID/PID query, manufacturer and model strings, device alias, user strings, active / optical-dark / transition pixel ranges, bad pixel list, TEC temperature and setpoint, shutter, light source, data buffer, back-to-back scan count, LED, baud rate, auto-nulling, and all network functions. The WAVELENGTH_CAL flag reads False, yet wavelengths are computed correctly from the EEPROM coefficients.

## 3. Windows USB driver

### 3.1 What is installed

| Property | Value |
| --- | --- |
| Driver model | WinUSB (Microsoft user-mode USB driver); service name WinUSB |
| Device class | OceanOpticsUSBDevice, class GUID {BAD36DAB-A3D2-4a0e-8B2E-DA36202187D4} |
| INF file | OOI_USB2000Plus.inf, installed into the driver store as oem185.inf; catalog OOI_USB2000Plus.cat |
| Driver version / date | 1.2.0, 15 June 2012, provider Ocean Optics, Inc. |
| Hardware ID matched | USB\VID_2457&PID_101E |
| Status | OK (Get-PnpDevice), present and enumerated |
| Source | C:\Program Files\Ocean Optics\OceanDirect SDK\winusb_driver (installed by the OceanDirect SDK installer) |

The same WinUSB driver family is used by OmniDriver and OceanView, so installing OceanDirect does not conflict with the legacy BlickO Ocean Optics backend. Only one process can hold the device open at a time, however (see section 7.9).

### 3.2 Driver packages shipped with the SDK

| Folder | Contents |
| --- | --- |
| winusb_driver | INF + CAT for USB2000+, HR2000, HR2000+, HR4000, Flame-NIR, Flame-U, Flame-W, Apex, ADC1000-USB, 1064M, 1064VNIR, Cypress generic bootloaders; dpinst.exe installers for amd64 and i386 with WinUSB / WDF co-installers |
| winusb_driver_offline | INF + CAT for the newer OBP2 units: Ocean FX, HDX, HR2/4/6, NR, QE1/QE2, QEPro, SR2/4/6, ST, Spark |

### 3.3 Installing the driver on another PC

1. Run the OceanDirect SDK installer (installs SDK and drivers), or copy the winusb_driver folder to the target PC.
2. Either run winusb_driver\amd64\dpinst.exe as administrator (installs every INF in the folder), or install only the Flame/USB2000+ INF:

```powershell
pnputil /add-driver "C:\Program Files\Ocean Optics\OceanDirect SDK\winusb_driver\OOI_USB2000Plus.inf" /install
```

1. Plug in the spectrometer. Device Manager must show "Ocean Optics USB2000+ (WinUSB)" under the Ocean Optics USB Devices class.
2. Verify from PowerShell:

```powershell
Get-PnpDevice -PresentOnly | Where-Object InstanceId -match 'VID_2457' | Format-List Status, FriendlyName, InstanceId
pnputil /enum-drivers | Select-String -Context 0,6 'OOI_USB2000Plus'
```

Troubleshooting: if the device appears as an unknown device, the INF was not installed. If opening fails with "device busy" or the probe finds zero devices while Device Manager shows it, another program (OceanView, BlickO with OmniDriver, or a stuck Python process) still holds it; close that program, or disable and re-enable the device in Device Manager to power-cycle it.

## 4. OceanDirect SDK 3.1.4

### 4.1 Installation

| Property | Value |
| --- | --- |
| Product | OceanDirect SDK 3.1.4, publisher Ocean Optics Inc., installed 21 September 2026 |
| Location | C:\Program Files\Ocean Optics\OceanDirect SDK |
| API version reported at run time | 3.1.4 (odapi_get_api_version_numbers) |
| Uninstall | Windows Apps and Features, or doc\Uninstall OceanDirect SDK.lnk |

### 4.2 Folder map

| Path (relative to the SDK folder) | Purpose |
| --- | --- |
| lib\OceanDirect.dll | Native C API, x64, 1,737,216 bytes, 216 exported odapi_* functions. The core library every language binding uses |
| lib\OceanDirect.lib, lib\OceanDirect.exp | Import library and exports for linking C/C++ programs (MSVC x64) |
| lib\NetOceanDirect.dll | .NET wrapper assembly (238,080 bytes); depends on OceanDirect.dll and the .NET runtime (mscoree) |
| include\api\OceanDirectAPI.h | C API header, 4,756 lines, Doxygen-commented; include\api\OceanDirectAPIConstants.h declares the ERROR_* codes; FeatureIds.h lists feature IDs |
| include\api\adapters, include\api\advanced, include\common | C++ adapter classes and advanced feature headers |
| Python\oceandirect\OceanDirectAPI.py | Python ctypes wrapper (3,941 lines): classes OceanDirectAPI, Spectrometer, Spectrometer.Advanced, FeatureID, OceanDirectError |
| Python\oceandirect\sdk_properties.py, od_logger.py, __init__.py | DLL path resolution (relative to the module) and a print-based logger; __init__ is empty |
| Python\oceandirect\lib\OceanDirect.dll | Byte-identical copy of lib\OceanDirect.dll used by the Python wrapper |
| doc\C++, doc\NET, doc\Python | Doxygen reference (HTML and RTF) for each binding; start at doc\Python\html\index.html |
| EULA\OceanDirect_EULA.txt | End-user licence (see 4.6) |
| winusb_driver, winusb_driver_offline | USB driver packages (section 3) |

### 4.3 Run-time dependencies of OceanDirect.dll

| Dependency | Provided by | Status on this PC |
| --- | --- | --- |
| WINUSB.DLL, SETUPAPI.dll, WS2_32.dll, IPHLPAPI.DLL, KERNEL32.dll | Windows (WinUSB device access, device enumeration, sockets for network spectrometers) | Present (Windows 11 Pro 10.0.26200 x64) |
| MSVCP140.dll, VCRUNTIME140.dll, VCRUNTIME140_1.dll, api-ms-win-crt-* | Microsoft Visual C++ 2015-2022 Redistributable (x64) | Installed, v14.50.35719 |
| 64-bit host process | OceanDirect.dll is x64 only; there is no 32-bit build in this SDK | Python 3.12.10 x64 used for testing |

### 4.4 Python wrapper specifics

- Pure ctypes; no pip package. Add `<SDK>\Python` to `sys.path` and import `oceandirect.OceanDirectAPI`.
- Requires **Python 3.9 or newer** because the wrapper uses `list[float]` and `tuple[int, str]` annotations (PEP 585). It is **not** importable from Python 2.7 or Python 3.8. Tested here with Python 3.12.10 (64-bit); numpy is not required.
- The DLL path is derived from the module location (`sdk_properties.py`), so the `oceandirect` folder must be used together with its `lib` subfolder. Call `os.add_dll_directory(<SDK>\Python\oceandirect\lib)` before importing so the VC++ runtime and dependent DLLs resolve on Python 3.8+ style DLL search rules.
- Every SDK call returns an error code that the wrapper converts into an `OceanDirectError` exception carrying `(code, message)`; message text comes from `odapi_get_error_string`.
- `OceanDirectAPI` is a process-wide singleton that calls `odapi_initialize()` on first use. Call `shutdown()` once when the program exits.
- The SDK documentation states that discovery, open and close must be called from one thread at a time. Serialize all device calls behind a lock in multithreaded programs (the viewer app does this).

### 4.5 Core C API used for acquisition

| Function | Purpose |
| --- | --- |
| void odapi_initialize(); void odapi_shutdown(); | Library life cycle (once per process) |
| int odapi_probe_devices(); | Scan USB; returns number of devices found. IDs from a previous probe become invalid after a close |
| int odapi_get_number_of_device_ids(); int odapi_get_device_ids(long *ids, unsigned int max_ids); | Retrieve device IDs |
| void odapi_open_device(long id, int *err); void odapi_close_device(long id, int *err); | Exclusive open / close. Error 10001 on open is a warning (initialisation incomplete) that can be tolerated |
| int odapi_get_serial_number(long id, int *err, char *buf, int len); odapi_get_device_name(...); odapi_get_device_type(...) | Identity strings (serial and name need an open device) |
| int odapi_get_formatted_spectrum_length(long id, int *err); | Pixel count (2048) |
| int odapi_get_wavelengths(long id, int *err, double *wl, int len); | Wavelength per pixel from EEPROM coefficients |
| void odapi_set_integration_time_micros(long id, int *err, unsigned long us); unsigned long odapi_get_integration_time_micros(long id, int *err); | Integration time |
| int odapi_get_formatted_spectrum(long id, int *err, double *buf, int len); | Blocking acquisition of one (averaged) spectrum with corrections applied as configured |
| void odapi_apply_electric_dark_correction_usage(long id, int *err, unsigned char on); void odapi_apply_nonlinearity_correct_usage(long id, int *err, bool on); | Correction switches (both default ON) |
| void odapi_set_scans_to_average(long id, int *err, unsigned int n); void odapi_set_boxcar_width(long id, int *err, unsigned short w); | On-device averaging and boxcar smoothing |
| double odapi_get_maximum_intensity(long id, int *err); | Full-scale value (65535) |
| void odapi_set_trigger_mode(long id, int *err, int mode); | Trigger mode |
| int odapi_get_error_string_length(int code); void odapi_get_error_string(int code, char *buf, int len); | Error text |

Error codes are exported as `extern const int` symbols (ERROR_SUCCESS, ERROR_NO_DEVICE, ERROR_FEATURE_NOT_FOUND, ERROR_TRANSFER_ERROR, ERROR_BAD_USER_BUFFER, ERROR_INPUT_OUT_OF_BOUNDS, ERROR_SPECTROMETER_SATURATED, ERROR_COMMAND_NOT_SUPPORTED, ERROR_INTEGRATION_TIME_BELOW_AVERAGING_MIN, and others) in OceanDirectAPIConstants.h, so C++ code can compare against the symbols instead of magic numbers. Observed numeric values: 5 = "No such feature on device", 15 = invalid argument (used by the Python wrapper), 10001 = initialise-incomplete warning.

### 4.6 Licence (OceanDirect EULA) - points that affect deployment

- Non-exclusive, non-transferable licence for the customer's **internal use** only; the software is licensed, not sold.
- Installation and use on **no more than one device at a time** (clause 4).
- No copying, modification, derivative works, reverse engineering, sublicensing, renting, or **use for any commercial or hosting purpose** (clause 5); a one-time transfer is allowed only together with the spectrometer (clause 6).
- Provided as is, liability capped at 100 USD, governed by Florida law.
- **Action:** before bundling OceanDirect.dll (or the Python wrapper) into the SciGlobPandora installer or shipping it to Pandora sites, obtain written redistribution terms from Ocean Insight. Until then, install the SDK on each PC with the vendor installer and have our software reference it in place.

## 5. Requirements checklist

| Requirement | Why | State on this PC |
| --- | --- | --- |
| Windows 10 or 11, 64-bit | OceanDirect.dll is x64; WinUSB driver has amd64 and i386 variants | Windows 11 Pro 10.0.26200 x64 - OK |
| Ocean Optics WinUSB driver for the unit (OOI_USB2000Plus.inf for Flame-S) | Device enumeration and bulk transfers | Installed v1.2.0, status OK |
| OceanDirect SDK 3.1.4 | Provides OceanDirect.dll, headers, wrappers, documentation | Installed in C:\Program Files\Ocean Optics\OceanDirect SDK |
| Visual C++ 2015-2022 x64 Redistributable | OceanDirect.dll links MSVCP140 / VCRUNTIME140 | Installed v14.50.35719 |
| 64-bit host process | No 32-bit OceanDirect.dll exists | Python 3.12.10 x64 present (also 3.13, 3.14 x64; 3.8 x86 present but unusable) |
| Python 3.9+ (for the Python wrapper) | PEP 585 annotations in OceanDirectAPI.py | Python 3.12.10 verified |
| tkinter (only for the viewer app) | GUI toolkit of the viewer | Tk 8.6 present |
| MSVC toolchain (for the C++ adapter) | Link OceanDirect.lib, include OceanDirectAPI.h | Not verified in this session |
| .NET Framework runtime (only if NetOceanDirect.dll is used) | Managed wrapper | Not evaluated |
| Exclusive access to the USB device | Only one process may open the spectrometer | Close OceanView / BlickO / other scripts before use |
| Free USB 2.0 port, preferably on a root hub | Sustained bulk transfers of 2048 x 16 bit at up to ~390 spectra/s | Port 6 on hub 2 |

## 6. Findings from the hands-on evaluation (21 Sep 2026)

### 6.1 Functional verification

- USB probe found one device (ID 2 on first probe). Open, identity, wavelengths, integration limits, spectrum readout and close all worked from Python 3.12 x64.
- A Tkinter viewer application (`C:\Users\Mini-Pan\Documents\OceanSpectrometer\spectrometer_viewer.py`) was written and its self test passed twice: connect, stream at 5 ms for 2.5 s (501 and 502 spectra), save CSV with 2048 rows, disconnect, rescan, all without errors. The app is described in Appendix F.
- The device reappeared under a new ID (3) after being closed and re-probed, and reopened successfully.

### 6.2 Acquisition rate versus integration time (free running, no averaging)

| Integration time | Spectra per second | Frame period | Overhead beyond integration |
| --- | --- | --- | --- |
| 1 ms | 387 | 2.58 ms | 1.58 ms (USB readout limit) |
| 2 ms | 496 | 2.02 ms | 0.02 ms |
| 5 ms | 200 | 5.00 ms | 0.00 ms |
| 10 ms | 100 | 10.00 ms | 0.00 ms |
| 20 ms | 50 | 20.00 ms | 0.00 ms |
| 50 ms | 20 | 50.0 ms | 0.0 ms |
| 100 ms | 10 | 100.0 ms | 0.0 ms |

Interpretation: readout of the previous frame overlaps the next integration, so from about 2.6 ms upward the frame period equals the integration time. Below that the USB transfer of 2048 x 16-bit values sets a floor of roughly 2.6 ms per spectrum (about 390 spectra/s). For scheduling and timeouts, expected duration = cycles x integration time, with a floor of 2.6 ms per cycle.

### 6.3 On-device averaging

| Scans to average | Duration of one get_formatted_spectrum call at 5 ms |
| --- | --- |
| 1 | 5.0 ms |
| 4 | 19.9 ms |
| 10 | 50.1 ms |

Averaging is linear in time with no extra overhead, so `scans_to_average` is a good mapping for Blick's measurement cycles when the mean (not the sum) is wanted. Note that the SDK reports `scans_to_average = 0` on a freshly opened device; treat 0 as 1 and always set it explicitly.

### 6.4 Baseline and offset (ambient bench light, 10 ms, 30 frames)

| Setting | Mean of active pixels | Mean of electric dark pixels 6-20 | Median per-pixel std over 30 frames |
| --- | --- | --- | --- |
| Electric dark correction ON (SDK default) | 416 counts | -0.1 counts | 42.9 counts |
| Electric dark correction OFF (raw) | 2676 counts | 2273 counts | 41.3 counts |

- The raw electrical offset of this unit is about **2270 counts** (3.5 % of full scale); the SDK's electric dark correction subtracts the mean of pixels 6-20 from every pixel.
- The measurements were taken with the fibre open to room light (peak at pixel 893, about 662 nm, 470 counts corrected), so the 41-43 count standard deviation reflects light fluctuation and is **not** a detector noise specification. Repeat with the input capped to characterise dark noise.
- Because the correction shifts values down by the offset, a saturated pixel reads about 63,265 rather than 65,535 when the correction is on. Saturation detection must use raw counts (correction off) or subtract the offset from the threshold.

### 6.5 Device state after opening

- Electric dark correction: **enabled**. Nonlinearity correction: **enabled**. Boxcar: 0. Trigger mode: 0. Lamp and strobes: off.
- The integration time read back on open is whatever was set last (6000 us at first contact, later the values our scripts had set). Always set it explicitly after opening.
- Nonlinearity correction uses the 8 EEPROM coefficients listed in 2.2; with the correction on, counts are no longer integers.

### 6.6 SDK behaviours and workarounds discovered

| Behaviour | Consequence | Workaround used |
| --- | --- | --- |
| Serial number and model are not readable before odapi_open_device (error 5, "No such feature on device") | A device list cannot show serials without opening each unit | Show odapi_get_device_type (USB2000PLUS) in lists; open, read serial, close to match a target serial |
| Device IDs are invalidated by odapi_close_device and change on the next probe (2 became 3) | Reopening with a cached ID fails | Probe again and fetch fresh IDs before every reconnect |
| Python OceanDirectAPI.open_device returns a cached Spectrometer object even after it was closed, without reopening it | Second connect in the same process appears to work but the handle is closed | Remove the entry from OceanDirectAPI.open_devices after closing, or check Spectrometer.status and call open_device() again |
| OceanDirectAPI.get_serial_number(dev_id) returns bytes for unopened devices | String comparisons fail silently | Decode, or use the Spectrometer method after open |
| get_scans_to_average returns 0 on a fresh device | Division or loop by zero if used naively | Clamp to at least 1 |
| Advanced.get_wavelength_coeffs prints an INFO line to stdout | Console noise | Ignore or redirect stdout |
| Opening an already open device (another process) fails; no acquisition-control feature on Flame-S | A long integration cannot be aborted mid-frame | Keep integration times short enough for the timeout budget; close the device to interrupt |

### 6.7 Gaps versus the current OmniDriver-based backend

- **Temperature:** OmniDriver exposed board and detector temperature (used by `get_temp_OcOpt1`). OceanDirect 3.1.4 only offers TEC temperature calls, which Flame-S rejects. No board temperature is available through OceanDirect for this unit; ask Ocean Insight whether a raw-bus (OBP) query is supported.
- **Abort:** no acquisition control on Flame-S; `abort()` in the HAL must be reported as unsupported.
- **Data buffer / back-to-back:** not supported on Flame-S; acquisition is strictly one blocking call per (averaged) spectrum.
- **Java dependency removed:** OceanDirect needs no JVM or JPype, which simplifies deployment compared with OmniDriver.

## 7. Adding the spectrometer to the operational software

### 7.1 Current state of the Blick software (from the copy on this Desktop)

| Component | Spectrometer support today | Relevance |
| --- | --- | --- |
| BlickO (Python, Blick\src\blick_spectrometer.py, 2,701 lines) | Runs on **32-bit Python 2.7** (python27.dll x86). ROE types: Ava1 (avaspec_v9.14.0.0.dll via ctypes), Hama1 (specu1a.dll), OcOpt1 (**OmniDriver Java** through JPype: OmniDriver.jar, OmniDriver32.dll, blick_ocopt1.jar, OOIUtils.jar), JETI1 (jeti_core.dll) | Cannot load the 64-bit OceanDirect.dll in-process |
| BlickO-CPP (cpp\include\blick\hal\spectrometer.hpp) | Vendor-neutral `ISpectrometer` interface with `RoeType::kOcOpt1`, `SpectrometerConfig`, `Measurement`, `DeviceResult<T>`; only the simulator exists, vendor adapters are planned ("M4") | Ideal insertion point for an OceanDirect adapter |
| Python 3 tools (BlickM Streamlit app, SpecDiagnostic, spectrometer_charactarization.py and similar) | Independent scripts | Can use the OceanDirect Python wrapper directly, as the viewer app does |

### 7.2 Architecture decision: where OceanDirect can run

| Host | Feasible? | Notes |
| --- | --- | --- |
| BlickO-CPP HAL adapter (x64 C++) | Yes - recommended | Link OceanDirect.lib, include OceanDirectAPI.h; implement `OceanDirectSpectrometer : ISpectrometer`. The C++ build must be x64 |
| Python 3.9+ x64 process | Yes | Import the wrapper as in the viewer; suitable for BlickM, diagnostics, calibration scripts |
| .NET (C#) x64 process | Yes | Reference NetOceanDirect.dll; not evaluated here |
| Legacy BlickO 32-bit Python 2.7 | No, not in-process | Keep the OmniDriver backend there, or run a small 64-bit bridge service (Python 3 or C++) that exposes spectra over a local socket or pipe. The bridge adds latency and a second process to supervise; prefer finishing the C++ HAL |

### 7.3 Mapping the HAL interface to OceanDirect

| ISpectrometer method | OceanDirect calls | Behaviour to preserve from reset_OcOpt1 / measure_OcOpt1 |
| --- | --- | --- |
| connect(cfg) | odapi_initialize (once), odapi_probe_devices, odapi_get_device_ids, odapi_open_device, odapi_get_serial_number | Find the unit whose serial equals the operation-file ID; open each candidate to read its serial; report "target device ID not found" otherwise |
| connect(cfg) - checks | odapi_get_formatted_spectrum_length | Compare with `cfg.num_pixels` and fail on mismatch, as the legacy code does |
| connect(cfg) - configuration | odapi_apply_nonlinearity_correct_usage(false), odapi_apply_electric_dark_correction_usage(0), odapi_set_scans_to_average(1), odapi_set_boxcar_width(0) | Legacy backend switches both corrections off, averaging 1, boxcar 0 -> raw counts |
| set_integration_time(us) | odapi_set_integration_time_micros | Clamp to 1000 .. 655,350,000 us; store the value for duration estimates |
| take_measurement(cycles) | odapi_set_scans_to_average(cycles) then odapi_get_formatted_spectrum, or loop cycles times and sum on the host | Fill Measurement.started_at, duration, integration_time_us, cycles, counts; set saturated when any raw pixel >= 65535 |
| abort() | none available (SPECTRUM_ACQUISITION_CONTROL unsupported) | Return DeviceErrorKind::kUnsupported; the watchdog can close the device instead |
| reset() | close, re-probe, open, configure, one test measurement | Same sequence as the legacy reset with ini=True |
| disconnect() | odapi_close_device; odapi_shutdown at program exit | Invalidate the cached device ID |
| Error mapping | ERROR_NO_DEVICE -> kConnectionLost; ERROR_TRANSFER_ERROR -> kTimeout / kProtocol; ERROR_FEATURE_NOT_FOUND, ERROR_COMMAND_NOT_SUPPORTED -> kUnsupported; ERROR_SPECTROMETER_SATURATED -> kSaturation; others -> kIoError | Message text from odapi_get_error_string |

### 7.4 C++ adapter skeleton

```cpp
// ocean_direct_spectrometer.hpp - OceanDirect adapter for blick::hal::ISpectrometer (x64 build only)
#include "blick/hal/spectrometer.hpp"
#include "api/OceanDirectAPI.h"       // <SDK>\include ; link <SDK>\lib\OceanDirect.lib
#include <mutex>

namespace blick::hal {

class OceanDirectSpectrometer final : public ISpectrometer {
 public:
  explicit OceanDirectSpectrometer(std::string serial) : serial_(std::move(serial)) {}
  ~OceanDirectSpectrometer() override { disconnect(); }

  DeviceResult<void> connect(const SpectrometerConfig& cfg) override {
    std::scoped_lock lk(mu_);
    static std::once_flag once; std::call_once(once, [] { odapi_initialize(); });
    cfg_ = cfg;
    odapi_probe_devices();                              // IDs change after every close
    long ids[16]; int n = odapi_get_device_ids(ids, 16);
    int err = 0; char buf[64] = {0};
    for (int i = 0; i < n && id_ < 0; ++i) {            // serial is readable only after open
      odapi_open_device(ids[i], &err);
      if (err != 0 && err != 10001) continue;           // 10001 = initialisation-incomplete warning
      odapi_get_serial_number(ids[i], &err, buf, sizeof buf);
      if (err == 0 && serial_ == buf) id_ = ids[i];
      else odapi_close_device(ids[i], &err);
    }
    if (id_ < 0)
      return fail(DeviceErrorKind::kConnectionLost, "spectrometer " + serial_ + " not found");
    npix_ = odapi_get_formatted_spectrum_length(id_, &err);
    if (npix_ != cfg.num_pixels)
      return fail(DeviceErrorKind::kProtocol, "pixel count mismatch");
    odapi_apply_electric_dark_correction_usage(id_, &err, 0);   // raw counts (OmniDriver parity)
    odapi_apply_nonlinearity_correct_usage(id_, &err, false);
    odapi_set_boxcar_width(id_, &err, 0);
    full_scale_ = odapi_get_maximum_intensity(id_, &err);        // 65535 on Flame-S
    return set_integration_time(cfg.integration_time_us);
  }

  DeviceResult<void> set_integration_time(int us) override {
    std::scoped_lock lk(mu_); int err = 0;
    odapi_set_integration_time_micros(id_, &err, static_cast<unsigned long>(us));
    if (err) return fail(map(err), errstr(err));
    it_us_ = us; return DeviceResult<void>::ok();
  }

  DeviceResult<Measurement> take_measurement(int cycles) override {
    std::scoped_lock lk(mu_); int err = 0;
    odapi_set_scans_to_average(id_, &err, static_cast<unsigned int>(cycles));  // mean of N frames
    std::vector<double> spec(npix_);
    Measurement m; m.started_at = std::chrono::system_clock::now();
    m.integration_time_us = it_us_; m.cycles = cycles;
    odapi_get_formatted_spectrum(id_, &err, spec.data(), npix_);  // blocks ~ cycles x IT
    if (err) return DeviceResult<Measurement>::err({map(err), errstr(err)});
    m.duration = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now() - m.started_at);
    m.counts.resize(npix_);
    for (int i = 0; i < npix_; ++i) {
      m.counts[i] = static_cast<std::uint32_t>(spec[i] + 0.5);
      if (spec[i] >= full_scale_) m.saturated = true;
    }
    return DeviceResult<Measurement>::ok(std::move(m));
  }

  DeviceResult<void> abort() override {   // Flame-S has no acquisition-control feature
    return fail(DeviceErrorKind::kUnsupported,
                "abort not supported; close the device to interrupt");
  }
  DeviceResult<void> disconnect() override {
    std::scoped_lock lk(mu_); int err = 0;
    if (id_ >= 0) odapi_close_device(id_, &err);
    id_ = -1; return DeviceResult<void>::ok();
  }
  DeviceResult<void> reset() override { disconnect(); return connect(cfg_); }
  RoeType roe() const noexcept override { return RoeType::kOcOpt1; }
  bool is_connected() const noexcept override { return id_ >= 0; }

 private:
  static std::string errstr(int e) {
    char b[256] = {0}; odapi_get_error_string(e, b, sizeof b); return b;
  }
  static DeviceErrorKind map(int e);  // see the error-mapping row of table 7.3
  DeviceResult<void> fail(DeviceErrorKind k, std::string msg) {
    return DeviceResult<void>::err({k, std::move(msg)});
  }

  std::string serial_; long id_ = -1; int npix_ = 0; int it_us_ = 0;
  double full_scale_ = 65535; SpectrometerConfig cfg_; std::mutex mu_;
};

}  // namespace blick::hal
```

Build notes: add `<SDK>\include` to the include path and `<SDK>\lib\OceanDirect.lib` to the linker; the target must be x64. At run time OceanDirect.dll must be on the PATH or next to the executable. Unit tests can reuse the existing `test_hal_spectrometer.cpp` pattern with the simulator, and an integration test can require the serial FLMS17243 to be present.

### 7.5 Python 3 module for tools and diagnostics

```python
import os, sys
SDK = r"C:\Program Files\Ocean Optics\OceanDirect SDK"
sys.path.insert(0, os.path.join(SDK, "Python"))
os.add_dll_directory(os.path.join(SDK, "Python", "oceandirect", "lib"))
from oceandirect.OceanDirectAPI import OceanDirectAPI, OceanDirectError


class OceanDirectDevice:
    """Minimal blocking driver: one spectrometer, raw counts. Serialize calls across threads."""

    def __init__(self, serial=None):
        self.od = OceanDirectAPI()
        self.od.find_usb_devices()
        self.dev = self.dev_id = None
        for dev_id in self.od.get_device_ids():
            dev = self.od.open_device(dev_id)
            if dev.status != "open":
                dev.open_device()
            if serial is None or dev.get_serial_number() == serial:
                self.dev, self.dev_id = dev, dev_id
                break
            self.od.close_device(dev_id)
            self.od.open_devices.pop(dev_id, None)
        if self.dev is None:
            raise RuntimeError("spectrometer %s not found" % serial)
        self.dev.set_electric_dark_correction_usage(False)   # raw counts, like BlickO
        self.dev.set_nonlinearity_correction_usage(False)
        self.dev.set_scans_to_average(1)
        self.dev.set_boxcar_width(0)
        self.wavelengths = self.dev.get_wavelengths()
        self.full_scale = self.dev.get_max_intensity()          # 65535

    def set_integration_us(self, us):
        self.dev.set_integration_time(int(us))

    def read(self, cycles=1):
        """Return (counts, saturated). Blocks for about cycles x integration time."""
        self.dev.set_scans_to_average(max(1, cycles))
        counts = self.dev.get_formatted_spectrum()
        return counts, max(counts) >= self.full_scale

    def close(self):
        self.od.close_device(self.dev_id)
        self.od.open_devices.pop(self.dev_id, None)
        self.od.shutdown()


if __name__ == "__main__":
    spec = OceanDirectDevice("FLMS17243")
    spec.set_integration_us(10_000)
    counts, saturated = spec.read(cycles=4)
    print(len(counts), "pixels, peak", max(counts), "saturated" if saturated else "")
    spec.close()
```

### 7.6 Behavioural parity with the OmniDriver backend

1. Match the spectrometer by serial number from the operation file; fail with the same operator message if it is absent.
2. Verify the pixel count against the operation file (2048 for Flame-S).
3. Disable nonlinearity and electric dark correction, set scans-to-average to 1 and boxcar to 0, so Level-0 files contain raw ADC counts with the ~2270 count electrical offset intact, exactly as OmniDriver delivered them.
4. Set the integration time explicitly on every connect and reset.
5. Perform one test measurement after connect (the legacy `ncyreset` behaviour).
6. Record in the Level-0 metadata which driver produced the data (OmniDriver vs OceanDirect 3.1.4) so processing can trace any offset or linearity differences.

### 7.7 Timing, timeouts and saturation

- Expected duration of a measurement = cycles x integration time, plus a floor of about 2.6 ms per cycle for integration times below 2.6 ms. Use this for the data-arrival watchdog instead of a fixed margin.
- The first frame after an integration-time change should be discarded (the viewer and benchmarks did this); it may still carry the previous exposure.
- Saturation: with corrections off, any pixel at 65535 is saturated. If a correction is left on, the threshold is lowered by the electric offset (about 2270 counts on this unit).
- Integration times above a few seconds cannot be interrupted; choose the operation-file limits accordingly.

### 7.8 Deployment checklist for a Pandora PC

1. Install the Ocean Optics WinUSB driver (SDK installer or `pnputil /add-driver ... OOI_USB2000Plus.inf /install`).
2. Install the Visual C++ 2015-2022 x64 Redistributable if missing.
3. Install the OceanDirect SDK 3.1.4 with the vendor installer (licence constraint, section 4.6) or, once redistribution is cleared, ship OceanDirect.dll next to the 64-bit executable.
4. For Python tools: use a 64-bit Python 3.9+ and point `sys.path` / `os.add_dll_directory` at the SDK as in 7.5, or set `OCEANDIRECT_HOME` for the viewer.
5. Close any OmniDriver / OceanView instance before starting the OceanDirect-based software.
6. Smoke test: run the viewer self test, which connects, streams, saves a CSV and disconnects:

```powershell
python "C:\Users\Mini-Pan\Documents\OceanSpectrometer\spectrometer_viewer.py" --selftest
```

### 7.9 Operational guidance

- One process at a time: the device is opened exclusively. A stuck process must be killed (or the device disabled and re-enabled in Device Manager) before another can connect.
- Reconnect procedure: close device, probe again, fetch fresh IDs, open, reconfigure, test measurement.
- Serialize all SDK calls behind one lock; never probe or open from two threads simultaneously.
- Keep the spectrometer on a root-hub USB 2.0 port; at 1 ms integration it sustains about 390 spectra/s (about 1.6 MB/s).
- The SDK prints INFO/ERROR lines through a print-based logger; redirect stdout in services.

---

## Appendix A - Python wrapper to C API mapping (subset)

| Python (oceandirect.OceanDirectAPI) | C function |
| --- | --- |
| OceanDirectAPI() / shutdown() | odapi_initialize / odapi_shutdown |
| find_usb_devices(), find_devices() | odapi_probe_devices (+ odapi_detect_network_devices) |
| get_device_ids() | odapi_get_number_of_device_ids, odapi_get_device_ids |
| open_device(id) -> Spectrometer, close_device(id) | odapi_open_device, odapi_close_device |
| Spectrometer.get_serial_number(), get_model(), get_device_type() | odapi_get_serial_number, odapi_get_device_name, odapi_get_device_type |
| get_formatted_spectrum_length(), get_wavelengths() | odapi_get_formatted_spectrum_length, odapi_get_wavelengths |
| set_integration_time(us), get_integration_time(), get_minimum/maximum_integration_time() | odapi_*_integration_time_micros |
| get_formatted_spectrum() | odapi_get_formatted_spectrum |
| set_scans_to_average(n), set_boxcar_width(w) | odapi_set_scans_to_average, odapi_set_boxcar_width |
| set_electric_dark_correction_usage(b), set_nonlinearity_correction_usage(b) | odapi_apply_electric_dark_correction_usage, odapi_apply_nonlinearity_correct_usage |
| get_max_intensity() | odapi_get_maximum_intensity |
| set_trigger_mode(m), get_trigger_mode() | odapi_set_trigger_mode, odapi_get_trigger_mode |
| get_electric_dark_pixel_indices() | odapi_get_electric_dark_pixel_indices |
| Advanced.get_revision_firmware(), get_revision_fpga() | odapi_adv_get_revision_firmware, odapi_adv_get_revision_fpga |
| Advanced.get_wavelength_coeffs(), get_nonlinearity_coeffs() | odapi_adv_get_wavelength_coeffs, odapi_get_nonlinearity_coeffs |
| is_feature_id_enabled(FeatureID.X) | odapi_is_feature_enabled |

## Appendix B - Raw property dump of FLMS17243

| Property | Value |
| --- | --- |
| API version | (3, 1, 4) |
| model / device_type / serial | Flame-S / USB2000PLUS / FLMS17243 |
| firmware / fpga revision | Flame-S 4.02.0 / 4.05.5 |
| pixel count (formatted) | 2048 |
| wavelengths[0..3] | 339.68143, 340.06209, 340.44272, 340.82330 nm |
| wavelengths[2045..2047] | 1019.10050, 1019.37784, 1019.65512 nm |
| wavelength coefficients | 339.68143, 0.380685, -2.0555342e-05, -1.5339606e-09 |
| nonlinearity coefficients | 0.88576, 4.57872e-06, 5.5994e-11, -4.88641e-15, 2.84584e-22, 3.169e-24, -5.26495e-29, 2.39364e-34 |
| max intensity | 65535.0 |
| integration min / max / increment | 1000 us / 655,350,000 us / 1 us |
| minimum averaging integration time | 1000 us |
| integration time at first contact | 6000 us |
| scans to average / boxcar (fresh open) | 0 / 0 |
| electric dark / nonlinearity correction (fresh open) | True / True |
| electric dark pixels | 15: indices 6..20 |
| trigger mode | 0 |
| acquisition delay / min / max / increment | 0 / 0 / 32767 / 1 us |
| lamp enable / single strobe / continuous strobe | False / False / False |
| gpio pin count | 8 |
| usb endpoints primary out / in, secondary out / in | 1 / 129, 0 / 130 |
| Not supported (error) | system revision, VID/PID, manufacturer/model strings, alias, user string, active/optical-dark/transition pixel ranges, bad pixels, TEC, shutter, light source, data buffer, back-to-back, LED, baud rate, autonull, network |

## Appendix C - Windows and software inventory of this PC

| Item | Value |
| --- | --- |
| Computer | Mini-Pan, Windows 11 Pro 10.0.26200, 64-bit |
| Python interpreters | 3.12.10 x64 (default `python`, numpy 2.5.3, tkinter 8.6, no matplotlib), 3.13 x64, 3.14 x64 (py launcher default), 3.8 x64, 3.8 x86 |
| Visual C++ runtime | x64 v14.50.35719 installed |
| OceanDirect SDK | 3.1.4, installed 2026-09-21 |
| Ocean Optics USB driver | OOI_USB2000Plus.inf 1.2.0 (oem185.inf), status OK |
| Microsoft Word | 16.0 (used to render this document) |
| Blick software copy | Desktop\BlickO-CPP (Python 2.7 x86 BlickO + C++ HAL); Desktop\avaspecx64.dll; BlickM Streamlit app in Documents\Adv_BlickM |

## Appendix D - Verification commands

```powershell
# USB device and driver
Get-PnpDevice -PresentOnly | Where-Object InstanceId -match 'VID_2457' |
  Format-List Status, Class, FriendlyName, InstanceId
Get-PnpDevice -PresentOnly | Where-Object InstanceId -match 'VID_2457' | ForEach-Object {
  Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName DEVPKEY_Device_DriverVersion,
    DEVPKEY_Device_DriverInfPath, DEVPKEY_Device_Service }

# Installed SDK and VC++ runtime
Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*' |
  Where-Object DisplayName -match 'OceanDirect' | Select-Object DisplayName, DisplayVersion
Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64' |
  Select-Object Installed, Version

# End-to-end test with the viewer application
python "C:\Users\Mini-Pan\Documents\OceanSpectrometer\spectrometer_viewer.py" --selftest
```

```python
# Minimal Python check (64-bit Python 3.9+)
import os, sys
SDK = r"C:\Program Files\Ocean Optics\OceanDirect SDK"
sys.path.insert(0, SDK + r"\Python")
os.add_dll_directory(SDK + r"\Python\oceandirect\lib")
from oceandirect.OceanDirectAPI import OceanDirectAPI
od = OceanDirectAPI(); print("API", od.get_api_version_numbers())
od.find_usb_devices(); ids = od.get_device_ids(); print("device ids", ids)
d = od.open_device(ids[0])
print(d.get_model(), d.get_serial_number(), d.get_formatted_spectrum_length(), "px")
d.set_integration_time(10000); s = d.get_formatted_spectrum(); print("peak", max(s))
od.close_device(ids[0]); od.shutdown()
```

## Appendix E - Files produced during the evaluation

| File | Purpose |
| --- | --- |
| C:\Users\Mini-Pan\Documents\OceanSpectrometer\spectrometer_viewer.py | Tkinter viewer / acquisition tool with self test (Appendix F) |
| C:\Users\Mini-Pan\Desktop\Flame-S_FLMS17243_OceanDirect_Integration_Guide.docx | This document |
| C:\Users\Mini-Pan\Desktop\Flame-S_FLMS17243_OceanDirect_Integration_Guide.md | Markdown copy of this document for the repository or wiki |

## Appendix F - The viewer application

`spectrometer_viewer.py` is a single-file Tkinter application built on the OceanDirect Python wrapper. It needs only the standard library plus the SDK. Run it with `python spectrometer_viewer.py`; run `python spectrometer_viewer.py --selftest` for an unattended end-to-end check that prints device identity, spectra received, peak, and saves a CSV.

- **Device row:** Refresh scans USB and lists devices by type and ID; Connect opens the selection and shows model, serial, pixel count and wavelength range.
- **Acquisition settings:** integration time in ms (clamped to the device range), scans to average, boxcar width, electric dark and nonlinearity correction toggles. Changes are applied live, on the acquisition thread, without stopping a running stream.
- **Controls:** Single, Start/Stop continuous, Save CSV (wavelength and counts with a metadata header), Autoscale Y.
- **Plot and status:** live wavelength-versus-counts plot with grid, saturation line at 65535, peak wavelength and counts, spectra per second, red SATURATED warning at 98 % of full scale.
- **Design:** all SDK calls are serialized behind one lock; acquisition runs on a worker thread; the UI polls a queue every 30 ms and draws only the newest spectrum; the app closes the device and shuts the SDK down on exit and handles the reconnect quirks listed in 6.6.
