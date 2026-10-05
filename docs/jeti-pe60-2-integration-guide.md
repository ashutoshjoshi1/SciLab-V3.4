# JETI PE60_2 integration guide

How SciLab drives the JETI PE60_2 line-array readout electronics (backend
**Jeti1**, `spectrometers/jeti_spectrometer.py`), what the vendor documents
say, and what was measured on the bench. Written on 2026-10-05 against a
PE60_2 attached to the development PC.

## 1. Sources

All downloaded on 2026-10-05 from JETI's public support pages and kept in
`C:\Users\Mini-Pan\Downloads\JETI\` on the development PC:

| Document | Where on jeti.com | Local file |
| --- | --- | --- |
| JETI SDK 4.8.10 (DLLs, headers, programmer's guides, samples) | Support → Software → JETI SDK | `jeti_sdk.zip`, unpacked to `jeti_sdk\` |
| SDK changelog | same page | `jeti_sdk_changelog.pdf` |
| PE60_2 datasheet (December 2024) | Products → Readout electronics → PE60_2 | `PE60_2_datasheet.pdf` |
| PE60_2 hardware description, rev. 2.5 | same page | `PE60_2_operating_instructions.pdf` |
| SPECFIRM firmware command manual, rev. 140 | same page | `JETI_firmware_manual.pdf` |

Product page: <https://www.jeti.com/products/readout-electronics/readout-electronics-pe60-2>.
SDK page: <https://www.jeti.com/support/software-and-drivers/jeti-sdk>.

## 2. The hardware

The PE60_2 is a readout board, not a complete spectrometer: a 252 MHz 32-bit
MCU with a 16-bit 10 MS/s ADC that drives a line sensor on a separate signal
board connected through JETI's 16-pin port. JETI lists Hamamatsu CMOS, NMOS,
InGaAs and back-thinned CCD sensors (S11639, S13496, S11071, S10420, G11620
and others). It runs JETI's SPECFIRM firmware, which takes SCPI-like commands.

Interfaces: USB 2.0 High Speed as a **CDC virtual COM port**, optional
Ethernet, LV-TTL UART, SPI, trigger input, a shutter/lamp output and 8 GPIOs.
It can be powered from USB (an active port; a passive hub cannot supply the
500 mA), from 5 V or from 7–28 V.

On Windows 10 and 11 the inbox `usbser.inf` driver binds to it; JETI supplies
drivers only for Windows 7 and 8. Measured on the bench:

| Property | Value |
| --- | --- |
| USB ID | `VID_0C6C&PID_0CDC` (0C6C is JETI's registered vendor ID) |
| Device Manager | Ports → "USB Serial Device (COM4)", manufacturer Microsoft |
| Bus-reported description | "JETI PE60_2" |
| `*IDN?` | `JETI_PE60` |
| `*VERS?` | `SPECFIRM_PE60 VERSION 0.66.0 190723` |

## 3. The SDK

The SDK is a set of plain C DLLs (`__stdcall`), 32- and 64-bit:

| DLL | Purpose |
| --- | --- |
| `jeti_core64.dll` | full device control: search, open, parameters, raw commands |
| `jeti_spectro_ex64.dll` | spectrometric measurements with control over time and averaging |
| `jeti_spectro64.dll` | simple blocking spectrometric measurements |
| `jeti_radio64.dll`, `jeti_radio_ex64.dll` | radiometric and colorimetric values for calibrated instruments |

SciLab uses SpectroEx for measurements and core for parameters.
`jeti_spectro_ex64.dll` imports `jeti_core64.dll` by name, so both must sit in
the same folder; SciLab loads core first. Both DLLs link only against Windows
system libraries, so no Visual C++ runtime is needed. They are digitally
signed from 4.8.10 on.

The SDK licence (`license.txt`, copied to
`spectrometers dll files/JETI_SDK_license.txt`) grants the right "to integrate
the SOFTWARE PRODUCT in their software products ... and to distribute these
products under their own trade name", without fees. It forbids reverse
engineering and modifying the DLLs. SciLab therefore bundles the two x64 DLLs
unchanged. This differs from OceanDirect, whose licence forbids redistribution.

Handles are shared between the layers: JETI's own C sample opens a device with
`JETI_OpenCOMDevice` (core) and then calls `JETI_StartLightEx` (SpectroEx) on
that handle.

`JETI_GetDeviceType` reports type 4, "SDCM4 / PE60_2 / PE65". The SDK
changelog lists PE60_2 support from version 4.6.0 (April 2021).

## 4. Call sequence used by SciLab

```text
discovery   JETI_GetNumSpectroEx(&n)                      searches FTDI devices and every COM port
            JETI_GetSerialSpectroEx(i, board, spec, dev)  three serials, 16 characters each
connect     JETI_OpenSpectroEx(i, &handle)
            JETI_PixelCountEx(handle, &npix)
            core: JETI_GetDeviceType, JETI_GetFirmwareVersion, JETI_GetFit (5 floats),
                  JETI_GetADCRes, JETI_GetMinTintConf, JETI_GetSDelay
            one test frame
per cycle   JETI_StartLightEx(handle, tint_ms, 1)        returns at once
            JETI_SpectroStatusEx(handle, &busy)           poll every 1 ms until busy == 0
            JETI_LightPixEx(handle, int32[npix])          raw ADC counts
stop        JETI_SpectroBreakEx(handle)                   see section 6
disconnect  JETI_CloseSpectroEx(handle)
```

ctypes types: `DWORD_PTR` handles are `c_size_t`, `BOOL` is a 32-bit `c_int`,
`WORD` is `c_ushort`, `FLOAT` is `c_float`, and every function returns a
`DWORD` error code (0 = success). The error names and texts are in
`jeti_core.h`; SciLab shows the text with the hex code, for example
"device busy (JETI error 0x0E JETI_BUSY)".

Decisions in the backend:

* **The integration time travels with every start**, so `set_it` makes no DLL
  call, and no frame carries a previous exposure. `tint = 0` means "adapt
  automatically" to the firmware, so SciLab clamps to the device minimum and
  never sends 0.
* **One firmware scan per cycle.** SciLab averages in floating point and keeps
  the per-pixel standard deviation, like its other backends. The firmware can
  average instead (`wAver`), which is faster but returns integer-truncated
  means; `device_averaging = True` selects that.
* **Serial number:** the device serial, else the spectrometer serial, else the
  electronics board serial. A requested serial matches any of the three.
* **No parameter writes.** SciLab never sends `*PARA` settings or `*PARA:SAVE`.
* **Wavelengths** come from `JETI_GetFit`: λ(p) = fit0 + fit1·p + … + fit4·p⁴.
  The axis is dropped when the polynomial is zero, non-monotonic or outside
  100–3000 nm.

## 5. Auto-detect and the COM-port search

`JETI_GetNumDevices` and `JETI_GetNumSpectroEx` open and query **every COM
port** on the PC; with nothing attached the search takes about 1.1 s, with the
PE60_2 about 2 s. SciLab's laser controllers (OBIS, CUBE, relay) sit on COM
ports that are still closed while the spectrometer connects, so a blind search
would send JETI's probe to them. SciLab's loader therefore probes Jeti1 in Auto
only when the USB pre-scan sees vendor `0C6C`, and never when the pre-scan
fails. Selecting Jeti1 explicitly always searches.

## 6. Measured behaviour

Board 2360093, firmware 0.66.0, SDK 4.8.10, 64-bit Python 3.12:

| Measurement | Result |
| --- | --- |
| Discovery (`JETI_GetNumSpectroEx`) | 2.1–2.2 s |
| Connect from the Setup tab | 4.3 s including discovery |
| Pixels / ADC / saturation | 2048 / 16 bit / 65535 |
| Integration-time range | 1.88 ms (`*CONF:MINTIN?`) to 60 000 ms |
| Scan delay (`*PARA:SDEL?`) | 0 ms |
| Fixed cost per frame | 6–7 ms (start, poll, transfer of 2048 × INT32) |
| 50 frames at 1.88 ms | 7.96 ms per frame, 126 spectra/s |
| 10 frames at 20 ms | 27.0 ms per frame |
| 20 × 10 ms, one scan per cycle | 337 ms |
| 20 × 10 ms, firmware averaging | 207 ms; means integer, 0.4 counts lower |

**Break is not acknowledged.** `JETI_SpectroBreakEx` during a 2 s or 5 s
exposure returns `JETI_TIMEOUT` (0x08) after 0.50 s. The device then reports
busy until the original exposure would have ended (1.2 s and 4.2 s later), and
a `JETI_StartLightEx` in that window returns `JETI_BUSY` (0x0E). Once status
reports idle, the next frame works normally. The backend therefore:

* returns from Stop after the 0.5 s break (0.8 s in total for a 5 s exposure),
* records when the cancelled exposure ends and waits for idle before the next
  start (2.2 s after stopping a 3 s exposure),
* and, if a start still answers `JETI_BUSY`, waits for idle and starts once
  more (covers a reconnect right after a cancelled exposure).

`JETI_CloseSpectroEx` during an exposure also waits for it: disconnecting 0.3 s
into a 3 s exposure took 2.7 s. An immediate reconnect then worked normally.

`JETI_ArbitraryCommand` (core header only, not in the programmer's guide)
returns only the first line of a multi-line answer, so `*PARA:ALLPARA?` shows
just the first parameter; single queries such as `*PARA:SENS?` work.

## 7. State of the bench board

Read-only queries on 2026-10-05:

| Parameter | Value |
| --- | --- |
| Board serial / spectrometer / device | 2360093 / empty / empty |
| Sensor (`*PARA:SENS?`) | `126 2048 (S11071 series)` |
| Pixel range, binning, direction | 0–2047, 1, 0 |
| ADC | 16 bit, 4 V range, offset −140 mV, gain 1.3, oversampling 1 |
| PDA gain | 1 (high) |
| Offset correction / ANC factor | range 0 0 (off) / 1.00 (off) |
| Lamp/shutter output | enabled, normal mode; scan delay 0 ms |
| Wavelength fit | 150 + 0.5·p (JETI's placeholder) |

Every frame so far, at 1.88 ms to 1000 ms, had signal only on
pixels 0–44: a decay from about 54 000–58 500 counts down to about 2 800.
Pixels 45–2047 read exactly 0. The counts do not change with integration
time. With no offset correction configured, exactly 0 means the video signal
sits below the ADC's zero, so no sensor signal is reaching the board. Check
that the S11071 head is connected to the PE60_2's sensor port and powered, and
look at a lamp, before trusting measurements. The placeholder fit also means
the board has no wavelength calibration; that has to come from the optical
bench the board is built into.

## 8. Bench check

```powershell
.venv-build\Scripts\python tools\jeti_smoke_test.py            # first JETI device
.venv-build\Scripts\python tools\jeti_smoke_test.py 2360093    # by serial
```

It lists COM ports with their USB IDs, discovers and connects through the
loader, prints the device summary, steps the integration time, compares the
two averaging modes, measures the frame rate, stops a 5 s exposure and
measures again, reconnects, and runs Auto discovery. It ends with
`SMOKE TEST PASSED`.

## 9. Using the PE60_2 outside SciLab

Minimal C, following JETI's `SpectroExSample.c`:

```c
DWORD n = 0; DWORD_PTR dev = 0; BOOL busy = TRUE; DWORD npix = 0;
JETI_GetNumSpectroEx(&n);                    /* about 2 s: probes every COM port */
JETI_OpenSpectroEx(0, &dev);
JETI_PixelCountEx(dev, &npix);
INT32 *counts = calloc(npix, sizeof(INT32));
JETI_StartLightEx(dev, 10.0f, 1);            /* 10 ms, 1 scan; never 0 ms */
do { JETI_SpectroStatusEx(dev, &busy); Sleep(1); } while (busy);
JETI_LightPixEx(dev, counts);
JETI_CloseSpectroEx(dev);
```

Without the SDK, the board takes SPECFIRM commands on its COM port. According
to the firmware manual (not tried on the bench):
8N1, any baud rate (115 200 is symbolic; the transfer runs at USB High Speed),
`*MEAS:LIGHT <tint> <aver> 1` returns ACK, then BEL when the measurement is
done, then the binary spectrum (format 1: a 16-bit length, then 16-bit
little-endian pixels). See the firmware manual, sections 4.6 and 7.
