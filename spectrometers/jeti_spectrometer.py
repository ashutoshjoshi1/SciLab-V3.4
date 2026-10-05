"""
Backend for JETI spectrometers driven by the JETI SDK (jeti_spectro_ex64.dll on
top of jeti_core64.dll, C API).  Written for the JETI **PE60_2** line-array
readout electronics; the SDK drives the other JETI spectrometer electronics
(SDCM3/SDCM4, PE65, spectraval, specbos) through the same calls, so they should
work unchanged.  SciLab type name: ``Jeti1``.

Facts this backend relies on (JETI SDK 4.8.10, downloaded 2026-10-05 from
https://www.jeti.com/support/software-and-drivers/jeti-sdk; see
docs/jeti-pe60-2-integration-guide.md for the full study):

- The PE60_2 is a USB 2.0 High-Speed **CDC virtual COM port** (Windows 10/11
  ship the driver; nothing to install).  It runs JETI's SPECFIRM firmware.
  ``JETI_GetDeviceType`` reports it as type 4 ("SDCM4 / PE60_2 / PE65").
- The SDK finds devices itself: ``JETI_GetNumSpectroEx`` searches FTDI-based
  JETI devices *and every COM port* (about 1 s with nothing attached).  That is
  why the loader probes this backend in Auto mode only when a JETI USB device
  is attached; an explicit "Jeti1" selection always searches.
- ``JETI_StartLightEx(handle, tint_ms, averages)`` starts one measurement and
  returns at once; ``JETI_SpectroStatusEx`` reports busy until it finishes and
  ``JETI_LightPixEx`` then returns the ADC counts per pixel as INT32.  The
  integration time travels with every start, so there is no stale frame after
  an integration-time change.  ``JETI_SpectroBreakEx`` is documented to cancel
  a measurement (the PE60_2 does not honour it; see below).
- Passing an integration time of 0 asks the firmware to *adapt* it
  automatically; this backend never does that (it clamps to the device minimum).
- Handles from the SpectroEx layer are valid for the core layer (JETI's own C
  sample mixes them), which supplies the pixel-to-wavelength polynomial
  (``JETI_GetFit``), the ADC resolution, the minimum integration time and the
  scan delay.  Nothing is written to the device's stored parameters.
- Only x64 DLLs are bundled (SciLab is a 64-bit application).  They link only
  against Windows system libraries; no Visual C++ runtime is needed.
- The SDK licence allows integrating the DLLs into SciLab and distributing them
  with it (spectrometers dll files/JETI_SDK_license.txt).

Acquisition model: ``measure(ncy)`` is synchronous like the OcOpt1 backend.  By
default every cycle is its own single-scan measurement, so SciLab averages the
frames in floating point and gets a per-pixel standard deviation, exactly as
with the other backends.  Setting ``device_averaging = True`` instead lets the
firmware average all cycles in one measurement: one scan delay instead of one
per frame, but integer-truncated means and no per-frame spread.

Verified on 2026-10-05 against a PE60_2 (board 2360093, firmware SPECFIRM_PE60
0.66.0, USB 0C6C:0CDC on COM4): about 6-7 ms per frame on top of the exposure,
and JETI_SpectroBreakEx is not acknowledged (it times out after 0.5 s and the
board finishes the exposure), so the next start waits for the device to go
idle.  ``tools/jeti_smoke_test.py`` is the bench check.
"""

import logging
import os
import struct
import sys
import threading
import time
from ctypes import (
    POINTER,
    byref,
    c_char_p,
    c_float,
    c_int,
    c_int32,
    c_size_t,
    c_ubyte,
    c_uint32,
    c_ushort,
    create_string_buffer,
)

import numpy as np

from spectrometers.spec_xfus import measurement_timeout_s, spec_clock


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SDK location
# ---------------------------------------------------------------------------

IS_64BIT = struct.calcsize("P") * 8 == 64
JETI_SPECTRO_DLL_NAME = "jeti_spectro_ex64.dll" if IS_64BIT else "jeti_spectro_ex.dll"
JETI_CORE_DLL_NAME = "jeti_core64.dll" if IS_64BIT else "jeti_core.dll"
JETI_SDK_HOME_ENV = "JETI_SDK_HOME"


def _bundled_dll_dir():
    """SciLab's 'spectrometers dll files' folder, in the source tree or a PyInstaller build (as the loader does)."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "spectrometers dll files")


JETI_BUNDLED_DIR = _bundled_dll_dir()
# USB vendor ID registered to JETI Technische Instrumente GmbH.
JETI_USB_VENDOR_ID = 0x0C6C

# Error codes from jeti_core.h / jeti_spectro_ex.h (SDK 4.8.10), with the header's own wording.
JETI_SUCCESS = 0x00
JETI_BUSY = 0x0E
JETI_FATAL_ERROR = 0xFF
JETI_ERRORS = {
    0x01: ("JETI_ERROR_OPEN", "device already open"),
    0x02: ("JETI_ERROR_OPEN_PORT", "could not open COM-port"),
    0x03: ("JETI_ERROR_PORT_SETTING", "could not set COM-port settings"),
    0x04: ("JETI_ERROR_BUFFER_SIZE", "could not set buffer size of COM-port"),
    0x05: ("JETI_ERROR_PURGE", "could not purge buffers of COM-port"),
    0x06: ("JETI_ERROR_TIMEOUT_SETTING", "could not set COM-port timeout"),
    0x07: ("JETI_ERROR_SEND", "could not send to device"),
    0x08: ("JETI_TIMEOUT", "communication timeout"),
    0x09: ("JETI_BREAK", "break"),
    0x0A: ("JETI_ERROR_RECEIVE", "could not receive from device"),
    0x0B: ("JETI_ERROR_NAK", "command not supported or invalid argument"),
    0x0C: ("JETI_ERROR_CONVERT", "could not convert received data"),
    0x0D: ("JETI_ERROR_PARAMETER", "invalid argument"),
    0x0E: ("JETI_BUSY", "device busy"),
    0x11: ("JETI_CHECKSUM_ERROR", "invalid checksum of received data"),
    0x12: ("JETI_INVALID_STEPWIDTH", "invalid step width"),
    0x13: ("JETI_INVALID_NUMBER", "invalid device number"),
    0x14: ("JETI_NOT_CONNECTED", "device not connected"),
    0x15: ("JETI_INVALID_HANDLE", "invalid device handle"),
    0x16: ("JETI_INVALID_CALIB", "invalid calibration file number"),
    0x17: ("JETI_CALIB_NOT_READ", "calibration data not read"),
    0x20: ("JETI_OVEREXPOSURE", "overexposure"),
    0x22: ("JETI_MEASURE_FAIL", "measurement failed"),
    0x23: ("JETI_ADAPTION_FAIL", "adaption failed"),
    0x50: ("JETI_FILE_NOT_FOUND", "straylight file not found"),
    0x51: ("JETI_NO_SLM_DIR", "could not find or create straylight directory"),
    0x52: ("JETI_NO_STRAYLIGHT", "no straylight file"),
    0x53: ("JETI_NO_MEM", "not enough memory for straylight matrix"),
    0x54: ("JETI_NO_SN", "could not read serial number"),
    0x80: ("JETI_DLL_ERROR", "internal DLL error"),
    0x81: ("JETI_CALC_ERROR", "calculation error"),
    0xFF: ("JETI_FATAL_ERROR", "fatal communication error"),
    0x100: ("JETI_ERROR_READ", "could not read from COM port"),
}

# JETI_GetDeviceType / JETI_GetDeviceInfo codes (Programmer's Guide jeti_core, 3.2.3).
JETI_DEVICE_TYPES = {
    0: "generic JETI device",
    1: "specbos (xx01)",
    2: "specbos 1211",
    3: "spectraval 1501/1511 or SDCM3",
    4: "SDCM4 / PE60_2 / PE65",
    5: "specbos 25x1",
}
JETI_DEVICE_TYPE_PE60 = 4

# Integration-time limits: JETI_SetTintConf documents 60000 ms as the maximum;
# JETI_StartDarkEx documents 1 ms as the lower end when the device does not say.
JETI_MAX_IT_MS = 60000.0
JETI_FALLBACK_MIN_IT_MS = 1.0
JETI_MAX_AVERAGES = 10000          # wAver range of JETI_StartLightEx


def _candidate_dirs():
    dirs = []
    home = os.environ.get(JETI_SDK_HOME_ENV, "").strip().strip('"')
    if home:
        sub = "Win64" if IS_64BIT else "Win32"
        dirs.extend([home, os.path.join(home, sub)])
    dirs.append(JETI_BUNDLED_DIR)
    return dirs


def find_jeti_dll():
    """
    Locate jeti_spectro_ex64.dll (jeti_core64.dll must sit next to it).  Returns
    the absolute path or "" when not found.  Search order: %JETI_SDK_HOME% (the
    SDK root or its Win64 folder), then SciLab's bundled 'spectrometers dll files'.
    """
    for base in _candidate_dirs():
        candidate = os.path.join(base, JETI_SPECTRO_DLL_NAME)
        if os.path.isfile(candidate) and os.path.isfile(os.path.join(base, JETI_CORE_DLL_NAME)):
            return os.path.abspath(candidate)
    return ""


def resolve_dll_paths(dll_path=""):
    """
    Turn whatever the operator pointed at into (spectro_ex DLL, core DLL) paths.

    Accepts the SpectroEx DLL, the core DLL, the SDK folder or its Win64 folder.
    A 32-bit DLL name in a 64-bit process is redirected to the 64-bit sibling
    (the SDK ships both, in Win32/ and Win64/).  Raises RuntimeError with an
    operator-facing message when the pair cannot be found.
    """
    path = (dll_path or "").strip().strip('"')
    if not path:
        path = find_jeti_dll()
        if not path:
            raise RuntimeError(
                JETI_SPECTRO_DLL_NAME + " not found. It ships in SciLab's '" + os.path.basename(JETI_BUNDLED_DIR) +
                "' folder; otherwise download the JETI SDK from jeti.com and point the Setup tab's DLL path at "
                "its Win64\\" + JETI_SPECTRO_DLL_NAME + " (or set " + JETI_SDK_HOME_ENV + ").")

    if os.path.isdir(path):
        for folder in (path, os.path.join(path, "Win64" if IS_64BIT else "Win32")):
            if os.path.isfile(os.path.join(folder, JETI_SPECTRO_DLL_NAME)):
                path = os.path.join(folder, JETI_SPECTRO_DLL_NAME)
                break
        else:
            raise RuntimeError(JETI_SPECTRO_DLL_NAME + " not found in folder '" + path + "'.")

    folder = os.path.dirname(os.path.abspath(path))
    name = os.path.basename(path).lower()
    if name not in (JETI_SPECTRO_DLL_NAME, JETI_CORE_DLL_NAME):
        # e.g. jeti_spectro_ex.dll (32-bit) picked in a 64-bit SciLab, or jeti_radio64.dll.
        siblings = [os.path.join(folder, JETI_SPECTRO_DLL_NAME),
                    os.path.join(os.path.dirname(folder), "Win64" if IS_64BIT else "Win32", JETI_SPECTRO_DLL_NAME)]
        found = next((s for s in siblings if os.path.isfile(s)), "")
        if not found:
            raise RuntimeError(
                "'" + os.path.basename(path) + "' is not the JETI DLL SciLab needs: point the DLL path at " +
                JETI_SPECTRO_DLL_NAME + " (" + ("64" if IS_64BIT else "32") + "-bit, from the SDK's " +
                ("Win64" if IS_64BIT else "Win32") + " folder).")
        folder = os.path.dirname(os.path.abspath(found))

    spectro = os.path.join(folder, JETI_SPECTRO_DLL_NAME)
    core = os.path.join(folder, JETI_CORE_DLL_NAME)
    missing = [p for p in (spectro, core) if not os.path.isfile(p)]
    if missing:
        raise RuntimeError("JETI SDK DLL missing: " + ", ".join(missing) +
                           ". Both " + JETI_SPECTRO_DLL_NAME + " and " + JETI_CORE_DLL_NAME +
                           " must be in the same folder.")
    return os.path.abspath(spectro), os.path.abspath(core)


# ---------------------------------------------------------------------------
# Process-wide SDK handle.  A DLL can be loaded only once per process and the
# SDK keeps a global device table, so loading and every call go through this
# module-level state behind one re-entrant lock.  The measurement loop holds
# the lock per call only, never across its sleeps.
# ---------------------------------------------------------------------------

_dll_lock = threading.RLock()
_sdk = {"spectro": None, "core": None, "path": "", "dll_dir_cookie": None,
        "spectro_version": "", "core_version": ""}


def _bind(spectro, core):
    """Attach argtypes / restypes for every C entry point this backend uses (all __stdcall)."""
    handle_t = c_size_t                       # DWORD_PTR: pointer-sized unsigned
    for fn, args in (
        (spectro.JETI_GetSpectroExDLLVersion, [POINTER(c_ushort)] * 3),
        (spectro.JETI_GetNumSpectroEx, [POINTER(c_uint32)]),
        (spectro.JETI_GetSerialSpectroEx, [c_uint32, c_char_p, c_char_p, c_char_p]),
        (spectro.JETI_OpenSpectroEx, [c_uint32, POINTER(handle_t)]),
        (spectro.JETI_CloseSpectroEx, [handle_t]),
        (spectro.JETI_StartLightEx, [handle_t, c_float, c_ushort]),
        (spectro.JETI_LightPixEx, [handle_t, POINTER(c_int32)]),
        (spectro.JETI_SpectroStatusEx, [handle_t, POINTER(c_int)]),
        (spectro.JETI_SpectroBreakEx, [handle_t]),
        (spectro.JETI_PixelCountEx, [handle_t, POINTER(c_uint32)]),
        (spectro.JETI_SpectroTintEx, [handle_t, POINTER(c_float)]),
        (core.JETI_GetCoreDLLVersion, [POINTER(c_ushort)] * 3),
        (core.JETI_GetDeviceType, [handle_t, POINTER(c_ubyte)]),
        (core.JETI_GetFirmwareVersion, [handle_t, c_char_p]),
        (core.JETI_GetFit, [handle_t, POINTER(c_float)]),
        (core.JETI_GetADCRes, [handle_t, POINTER(c_ubyte)]),
        (core.JETI_GetMinTintConf, [handle_t, POINTER(c_float)]),
        (core.JETI_GetSDelay, [handle_t, POINTER(c_uint32)]),
    ):
        fn.argtypes = args
        fn.restype = c_uint32


def _version(fn):
    major, minor, build = c_ushort(0), c_ushort(0), c_ushort(0)
    try:
        if int(fn(byref(major), byref(minor), byref(build))) == JETI_SUCCESS:
            return "%d.%d.%d" % (major.value, minor.value, build.value)
    except Exception:
        pass
    return ""


def _load_sdk(dll_path=""):
    """
    Load jeti_core64.dll and jeti_spectro_ex64.dll once per process.  Returns
    ("OK", (spectro, core)) or (error text, None).  A later request with another
    path re-uses the loaded pair (Windows cannot load two modules of the same
    name) and logs a warning.
    """
    with _dll_lock:
        if _sdk["spectro"] is not None:
            if dll_path:
                try:
                    wanted = resolve_dll_paths(dll_path)[0]
                except RuntimeError:
                    wanted = dll_path
                if os.path.normcase(os.path.abspath(wanted)) != os.path.normcase(_sdk["path"]):
                    logger.warning("JETI SDK already loaded from %s; ignoring %s", _sdk["path"], dll_path)
            return "OK", (_sdk["spectro"], _sdk["core"])

        try:
            spectro_path, core_path = resolve_dll_paths(dll_path)
        except RuntimeError as exc:
            return str(exc), None

        try:
            from ctypes import WinDLL
        except ImportError:
            return "The JETI SDK is a Windows DLL; this platform cannot load it.", None

        folder = os.path.dirname(spectro_path)
        try:
            cookie = os.add_dll_directory(folder) if hasattr(os, "add_dll_directory") else None
        except OSError:
            cookie = None
        try:
            # Core first: jeti_spectro_ex64.dll imports jeti_core64.dll by name and
            # then binds to this already-loaded module.
            core = WinDLL(core_path)
            spectro = WinDLL(spectro_path)
            _bind(spectro, core)
        except (OSError, AttributeError) as exc:
            return ("Could not load the JETI SDK from '" + folder + "': " + str(exc) +
                    (". This SciLab is 64-bit and needs the SDK's Win64 DLLs." if IS_64BIT else "")), None

        _sdk.update(spectro=spectro, core=core, path=spectro_path, dll_dir_cookie=cookie,
                    spectro_version=_version(spectro.JETI_GetSpectroExDLLVersion),
                    core_version=_version(core.JETI_GetCoreDLLVersion))
        logger.info("JETI SDK loaded from %s (spectro_ex %s, core %s)",
                    folder, _sdk["spectro_version"], _sdk["core_version"])
        return "OK", (spectro, core)


def error_text(code):
    """Operator-facing text for a JETI SDK return code (always mentions the hex code)."""
    code = int(code)
    if code == JETI_SUCCESS:
        return "OK"
    if 0x2710 <= code <= 0x2AFC:
        return "Windows sockets error %d (JETI error 0x%X)" % (code, code)
    name, text = JETI_ERRORS.get(code, ("", "unknown error"))
    message = "%s (JETI error 0x%02X%s)" % (text, code, (" " + name) if name else "")
    if code == JETI_FATAL_ERROR:
        message += ("; disconnect in SciLab, unplug and re-plug the JETI USB cable, then connect again "
                    "(a virtual-COM-port device can only be reset by hand)")
    elif code in (0x02, 0x14):
        message += "; close JETI VersaSpec / LiVal or any terminal holding the COM port"
    return message


def _read_serials(spectro, index):
    """(board, spectrometer, device) serial strings of device *index*; empty strings when unreadable."""
    board, spec, device = (create_string_buffer(64) for _ in range(3))
    rc = int(spectro.JETI_GetSerialSpectroEx(c_uint32(index), board, spec, device))
    if rc != JETI_SUCCESS:
        logger.warning("JETI_GetSerialSpectroEx(%d) failed: %s", index, error_text(rc))
        return "", "", ""
    return tuple(_clean(buf.value) for buf in (board, spec, device))


def _clean(raw):
    text = raw.decode("ascii", errors="ignore") if isinstance(raw, bytes) else str(raw)
    text = text.strip().strip("\x00").strip()
    return "" if text in ("0", "-") else text


def preferred_serial(board, spec, device):
    """The serial SciLab files data under: device, else spectrometer, else electronics board serial."""
    return device or spec or board


def _count_devices(spectro):
    count = c_uint32(0)
    rc = int(spectro.JETI_GetNumSpectroEx(byref(count)))
    if rc != JETI_SUCCESS:
        raise RuntimeError("JETI device search failed: " + error_text(rc))
    return int(count.value)


def enumerate_devices(dll_path=""):
    """
    Discovery for the loader: [{"serial", "label", "type": "Jeti1", "board", "spectrometer", "device"}].
    Searches FTDI-based JETI devices and every COM port (about a second).
    Raises RuntimeError when the SDK cannot be loaded or the search fails.
    """
    res, libs = _load_sdk(dll_path)
    if res != "OK":
        raise RuntimeError(res)
    spectro = libs[0]
    devices = []
    with _dll_lock:
        for index in range(_count_devices(spectro)):
            board, spec, device = _read_serials(spectro, index)
            serial = preferred_serial(board, spec, device) or ("Jeti1-%d" % (index + 1))
            details = ", ".join(label + " " + value for label, value in
                                (("board", board), ("spectrometer", spec), ("device", device)) if value)
            devices.append({
                "serial": serial,
                "label": serial + " (JETI" + ((" " + details) if details else "") + ") [Jeti1]",
                "type": "Jeti1",
                "board": board,
                "spectrometer": spec,
                "device": device,
            })
    return devices


def wavelengths_from_fit(fit, npix):
    """
    lambda(p) = fit0 + fit1*p + ... + fit4*p**4 for p = 0..npix-1 (JETI_GetFit).
    Returns an empty array when the polynomial is missing or implausible
    (non-finite, not monotonic, or outside 100-3000 nm), so a wrong calibration
    never masquerades as a wavelength axis.
    """
    coeffs = [float(c) for c in fit]
    if npix <= 1 or not coeffs or not any(coeffs[1:]):
        return np.array([])
    p = np.arange(npix, dtype=np.float64)
    wl = np.polynomial.polynomial.polyval(p, coeffs)
    steps = np.diff(wl)
    if (not np.all(np.isfinite(wl)) or not (np.all(steps > 0) or np.all(steps < 0))
            or wl.min() < 100.0 or wl.max() > 3000.0):
        return np.array([])
    return wl


class _FrameStats(object):
    """Running mean / std / (max - mean) over frames, as the other backends compute rcm/rcs/rcl."""

    def __init__(self, npix):
        self.n = 0
        self.accum = np.zeros(npix, dtype=np.float64)
        self.sq_accum = np.zeros(npix, dtype=np.float64)
        self.max_seen = np.full(npix, -np.inf, dtype=np.float64)

    def add(self, frame):
        self.accum += frame
        self.sq_accum += frame * frame
        self.max_seen = np.maximum(self.max_seen, frame)
        self.n += 1

    def result(self):
        n = max(1, self.n)
        mean = self.accum / n
        var = np.clip(self.sq_accum / n - mean * mean, 0.0, None)
        spread = np.zeros_like(mean) if self.n == 0 else self.max_seen - mean
        return mean, np.sqrt(var), spread


Jeti_Spectrometer_Instances = {}


class Jeti_Spectrometer(object):
    """
    Control class for JETI spectrometer electronics (PE60_2 and relatives) through
    the JETI SDK.  Exposes the SciLab backend contract (connect / disconnect /
    set_it / measure / wait_for_measurement / abort / request_stop plus sn,
    npix_active, rcm, rcs, it_ms, eff_saturation_limit, abort_on_saturation).
    """

    # Poll cadence while a frame is in flight.  time.sleep is high-resolution on
    # Windows from Python 3.11, so 1 ms costs little and keeps short exposures fast.
    poll_interval_s = 0.001
    # A frame that has not finished within 2 x (scan delay + exposure) plus this
    # margin is cancelled and reported, instead of hanging the caller.
    frame_timeout_margin_s = 5.0

    def __init__(self):
        self.spec_type = "Jeti1"

        self.debug_mode = 0
        self.dll_path = ""

        self.sn = ""
        self.alias = "1"
        self.model = ""
        self.device_type_code = None
        self.board_sn = ""
        self.spectrometer_sn = ""
        self.device_sn = ""
        self.fw_version = ""

        # Defaults until the device answers at connect time.
        self.npix_active = 0
        self.npix_blind_left = 0
        self.npix_blind_right = 0
        self.nbits = 16
        self.min_it_ms = JETI_FALLBACK_MIN_IT_MS
        self.max_it_ms = JETI_MAX_IT_MS
        self.eff_saturation_limit = float(2 ** 16 - 1)
        self.abort_on_saturation = True
        self.fit = []
        self.wl = np.array([])
        self.scan_delay_ms = 0.0
        self.frame_overhead_ms = None          # measured on the connect test frame
        # False: one single-scan measurement per cycle (float mean + per-pixel std).
        # True: the firmware averages all cycles in one measurement (fewer round trips).
        self.device_averaging = False

        # Internal state
        self._spectro = None
        self._core = None
        self.handle = 0
        self.device_index = -1
        self.is_open = False
        self.it_ms = None
        self.last_errcode = 0
        self.error = "OK"
        self.logger = logger
        self._frame_buf = None

        self._meas_lock = threading.Lock()
        self._meas_done_event = threading.Event()
        self._meas_done_event.set()
        self._abort_event = threading.Event()
        self._busy_until = None                # end of a cancelled exposure still running on the device
        self.measuring = False
        self._abort_requested = False
        self._stop_requested = False

        # Result arrays expected by the rest of the app
        self.rcm = np.array([])
        self.rcs = np.array([])
        self.rcl = np.array([])

        # Stats
        self.ncy_requested = 0
        self.ncy_read = 0
        self.ncy_handled = 0
        self.ncy_saturated = 0
        self.meas_start_time = None

        # Optional event the orchestrator may attach
        self.external_meas_done_event = None
        self.recovering = False

    # ------------------------------------------------------------------
    # Bookkeeping helpers
    # ------------------------------------------------------------------
    def initialize_spec_logger(self):
        self.logger = logging.getLogger("spec" + self.alias)

    def reset_spec_data(self):
        self.rcm = np.zeros(self.npix_active, dtype=np.float64)
        self.rcs = np.zeros(self.npix_active, dtype=np.float64)
        self.rcl = np.zeros(self.npix_active, dtype=np.float64)
        self.ncy_read = 0
        self.ncy_handled = 0
        self.ncy_saturated = 0

    def get_error(self, code):
        """Translate a JETI return code into "OK" or a description; remembers the code."""
        self.last_errcode = int(code)
        return error_text(code)

    def get_device_info(self):
        """Identity and configuration summary for display / logging."""
        return {
            "type": self.spec_type,
            "model": self.model,
            "device_type_code": self.device_type_code,
            "sn": self.sn,
            "board_sn": self.board_sn,
            "spectrometer_sn": self.spectrometer_sn,
            "device_sn": self.device_sn,
            "firmware": self.fw_version,
            "sdk": "spectro_ex %s, core %s" % (_sdk["spectro_version"], _sdk["core_version"]),
            "dll": _sdk["path"],
            "npix": self.npix_active,
            "wl_fit": list(self.fit),
            "wl_range_nm": (float(self.wl[0]), float(self.wl[-1])) if len(self.wl) else None,
            "it_range_ms": (self.min_it_ms, self.max_it_ms),
            "adc_bits": self.nbits,
            "saturation": self.eff_saturation_limit,
            "scan_delay_ms": self.scan_delay_ms,
            "frame_overhead_ms": self.frame_overhead_ms,
            "device_averaging": self.device_averaging,
        }

    # ------------------------------------------------------------------
    # DLL lifecycle
    # ------------------------------------------------------------------
    def load_spec_dll(self):
        res, libs = _load_sdk(self.dll_path)
        if res != "OK":
            return res
        self._spectro, self._core = libs
        self.dll_path = _sdk["path"]
        return "OK"

    def initialize_dll(self):
        # The JETI SDK has no global initialisation call; loading is enough.
        return "OK" if self._spectro is not None else self.load_spec_dll()

    # ------------------------------------------------------------------
    # Connect / disconnect
    # ------------------------------------------------------------------
    def connect(self):
        self.logger.info("--- Connecting JETI spectrometer " + str(self.alias) + " ---")
        res = self.initialize_dll()
        if res != "OK":
            self.error = res
            return res

        spectro = self._spectro
        with _dll_lock:
            if self.is_open:
                self._close_handle()

            try:
                count = _count_devices(spectro)
            except RuntimeError as exc:
                self.error = str(exc)
                return self.error
            if count == 0:
                self.error = ("No JETI spectrometer found. Check the USB cable and power (a passive hub "
                              "cannot power the PE60_2), that Device Manager lists it under 'Ports (COM & LPT)', "
                              "and that JETI VersaSpec / LiVal or a terminal program is not holding its COM port.")
                return self.error

            wanted = (self.sn or "").strip().lower()
            chosen = -1
            listed = []
            for index in range(count):
                serials = _read_serials(spectro, index)
                listed.append(preferred_serial(*serials) or "?")
                if not wanted or wanted in [s.lower() for s in serials if s]:
                    chosen = index
                    self.board_sn, self.spectrometer_sn, self.device_sn = serials
                    break
            if chosen < 0:
                self.error = ("JETI spectrometer '" + self.sn + "' not found. Found: " + ", ".join(listed))
                return self.error

            handle = c_size_t(0)
            rc = int(spectro.JETI_OpenSpectroEx(c_uint32(chosen), byref(handle)))
            if rc != JETI_SUCCESS:
                self.error = "JETI_OpenSpectroEx failed: " + self.get_error(rc)
                return self.error
            self.handle = int(handle.value)
            self.device_index = chosen
            self.is_open = True
            self.sn = preferred_serial(self.board_sn, self.spectrometer_sn, self.device_sn) or self.sn or \
                ("Jeti1-%d" % (chosen + 1))

            npix = c_uint32(0)
            rc = int(spectro.JETI_PixelCountEx(self.handle, byref(npix)))
            if rc != JETI_SUCCESS or npix.value == 0:
                self.error = "JETI_PixelCountEx failed: " + (self.get_error(rc) if rc else "0 pixels")
                self._close_handle()
                return self.error
            self.npix_active = int(npix.value)
            self._read_device_parameters()

            self._frame_buf = (c_int32 * self.npix_active)()
            self.reset_spec_data()

        # A previous disconnect() left the abort flag raised; this session starts clean.
        self._abort_requested = False
        self._abort_event.clear()
        self.it_ms = None
        res = self.set_it(max(self.min_it_ms, 10.0))
        if res != "OK":
            self.error = res
            self.disconnect()
            return res

        # One test frame: proves the measurement path end to end and measures the
        # fixed cost of a frame (scan delay, prescans, transfer) for the log.
        started = time.perf_counter()
        frame, res = self._acquire(1)
        if res != "OK":
            self.error = "JETI test measurement failed: " + res
            self.disconnect()
            return self.error
        self.frame_overhead_ms = max(0.0, (time.perf_counter() - started) * 1000.0 - self.it_ms)

        Jeti_Spectrometer_Instances[self.sn or self.alias] = self
        self.logger.info(
            "JETI connected: model=%s, sn=%s (board %s, spectrometer %s, device %s), pixels=%d, fw=%s, "
            "ADC %d bit, IT %.3f-%.0f ms, scan delay %.0f ms, frame overhead %.1f ms, wavelengths %s",
            self.model, self.sn, self.board_sn or "-", self.spectrometer_sn or "-", self.device_sn or "-",
            self.npix_active, self.fw_version or "?", self.nbits, self.min_it_ms, self.max_it_ms,
            self.scan_delay_ms, self.frame_overhead_ms,
            ("%.1f-%.1f nm" % (self.wl[0], self.wl[-1])) if len(self.wl) else "not available")
        self.error = "OK"
        return "OK"

    def _read_device_parameters(self):
        """Identity and limits from the core DLL.  Every read is optional: a failure only logs."""
        core = self._core
        h = self.handle

        def read(label, fn, *args):
            try:
                rc = int(fn(h, *args))
            except Exception as exc:          # pragma: no cover - defensive against an odd DLL build
                self.logger.info("JETI %s not available: %s", label, exc)
                return False
            if rc != JETI_SUCCESS:
                self.logger.info("JETI %s not available: %s", label, error_text(rc))
                return False
            return True

        dev_type = c_ubyte(0)
        if read("device type", core.JETI_GetDeviceType, byref(dev_type)):
            self.device_type_code = int(dev_type.value)
            self.model = JETI_DEVICE_TYPES.get(self.device_type_code, "JETI device type %d" % self.device_type_code)
            if self.device_type_code != JETI_DEVICE_TYPE_PE60:
                self.logger.info("JETI device type %d (%s) is not the PE60_2 family; continuing.",
                                 self.device_type_code, self.model)
        else:
            self.model = "JETI device"

        fw = create_string_buffer(512)
        if read("firmware version", core.JETI_GetFirmwareVersion, fw):
            self.fw_version = _clean(fw.value)

        fit = (c_float * 8)()
        if read("wavelength fit", core.JETI_GetFit, fit):
            self.fit = [float(fit[i]) for i in range(5)]
            self.wl = wavelengths_from_fit(self.fit, self.npix_active)
            if not len(self.wl):
                self.logger.warning("JETI wavelength fit %s is not a plausible pixel-to-wavelength "
                                    "polynomial; no wavelength axis.", self.fit)

        adc = c_ubyte(0)
        if read("ADC resolution", core.JETI_GetADCRes, byref(adc)) and 8 <= adc.value <= 24:
            self.nbits = int(adc.value)
        self.eff_saturation_limit = float(2 ** self.nbits - 1)

        min_tint = c_float(0.0)
        if read("minimum integration time", core.JETI_GetMinTintConf, byref(min_tint)) and min_tint.value > 0:
            self.min_it_ms = float(min_tint.value)
        else:
            self.min_it_ms = JETI_FALLBACK_MIN_IT_MS

        sdelay = c_uint32(0)
        if read("scan delay", core.JETI_GetSDelay, byref(sdelay)):
            self.scan_delay_ms = float(sdelay.value)

    def _close_handle(self):
        """Close the open handle (caller holds _dll_lock or is single-threaded)."""
        res = "OK"
        if self.is_open and self._spectro is not None:
            try:
                rc = int(self._spectro.JETI_CloseSpectroEx(self.handle))
                res = self.get_error(rc)
            except Exception as exc:
                res = "JETI_CloseSpectroEx raised: " + str(exc)
        self.is_open = False
        self.handle = 0
        self.device_index = -1
        self._busy_until = None
        return res

    def disconnect(self, *args, **kwargs):
        if not self.is_open:
            return "OK"
        self._abort_requested = True
        self._abort_event.set()
        # Let a measurement in flight notice the abort and cancel itself first.
        self._meas_done_event.wait(self.frame_timeout_margin_s)
        with _dll_lock:
            res = self._close_handle()
        key = self.sn or self.alias
        if Jeti_Spectrometer_Instances.get(key) is self:
            del Jeti_Spectrometer_Instances[key]
        self.logger.info("JETI disconnected (" + res + ").")
        return res

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------
    def set_it(self, it_ms):
        """
        Store the integration time (ms) for the next measurement, clamped to the
        device range.  JETI sends it with every start, so no DLL call is needed;
        the clamp also guarantees 0 (the firmware's "adapt automatically") is
        never sent.
        """
        try:
            it_ms = float(it_ms)
        except (TypeError, ValueError):
            return "Invalid integration time: " + str(it_ms)
        if not np.isfinite(it_ms):
            return "Invalid integration time: " + str(it_ms)
        if not self.is_open:
            return "JETI spectrometer not connected"
        self.it_ms = min(max(it_ms, self.min_it_ms), self.max_it_ms)
        return "OK"

    def _break(self):
        """
        Send JETI_SpectroBreakEx.  On the PE60_2 (firmware 0.66.0, verified
        2026-10-05) the firmware does not acknowledge it: the call returns
        JETI_TIMEOUT after 0.5 s and the device stays busy until the cancelled
        exposure would have ended.  Callers therefore record when that is
        (``_busy_until``) and the next start waits for the device to go idle.
        """
        try:
            with _dll_lock:
                rc = int(self._spectro.JETI_SpectroBreakEx(self.handle))
        except Exception:
            self.logger.debug("JETI_SpectroBreakEx failed", exc_info=True)
            return
        if rc != JETI_SUCCESS:
            self.logger.info("JETI_SpectroBreakEx: %s; the device finishes the exposure in flight first.",
                             error_text(rc))

    def _wait_until_idle(self, until):
        """
        Poll until the device reports idle.  *until* is the monotonic time by
        which the exposure still running on the device should end; the wait gives
        up frame_timeout_margin_s after that.  Returns "OK", "aborted" or an error.
        """
        deadline = max(until, time.monotonic()) + self.frame_timeout_margin_s
        busy = c_int(1)
        while True:
            with _dll_lock:
                rc = int(self._spectro.JETI_SpectroStatusEx(self.handle, byref(busy)))
            if rc != JETI_SUCCESS:
                return "JETI_SpectroStatusEx: " + self.get_error(rc)
            if not busy.value:
                self._busy_until = None
                return "OK"
            now = time.monotonic()
            if now > deadline:
                return ("the JETI device is still busy with a cancelled measurement; disconnect, unplug and "
                        "re-plug the JETI USB cable, then connect again")
            if self._abort_requested:
                return "aborted"
            remaining = until - now
            if remaining > 0.05:
                self._abort_event.wait(min(remaining - 0.02, 0.5))
            else:
                time.sleep(0.005)

    def _acquire(self, averages):
        """
        One StartLightEx / poll / LightPixEx round trip with *averages* scans
        averaged by the firmware.  Returns (frame as float64, "OK") or (None, error).
        Honours abort requests while the frame is in flight; a cancelled exposure
        keeps the device busy until it ends, so the next call waits for that first.
        """
        spectro = self._spectro
        h = self.handle
        if not self.is_open or self._frame_buf is None:
            return None, "JETI spectrometer not connected"
        if self._busy_until is not None:
            res = self._wait_until_idle(self._busy_until)
            if res != "OK":
                return None, res

        started = time.monotonic()
        with _dll_lock:
            rc = int(spectro.JETI_StartLightEx(h, c_float(self.it_ms), c_ushort(int(averages))))
        if rc == JETI_BUSY:
            # Still exposing for someone else, e.g. a reconnect right after a cancelled long
            # exposure: wait for it (up to the longest possible exposure) and start once more.
            self.logger.info("JETI device busy at start; waiting for the measurement in flight to end.")
            res = self._wait_until_idle(started + JETI_MAX_IT_MS / 1000.0)
            if res != "OK":
                return None, res
            started = time.monotonic()
            with _dll_lock:
                rc = int(spectro.JETI_StartLightEx(h, c_float(self.it_ms), c_ushort(int(averages))))
        if rc != JETI_SUCCESS:
            return None, "JETI_StartLightEx: " + self.get_error(rc)

        expected_s = (self.scan_delay_ms + self.it_ms * averages) / 1000.0
        frame_end = started + expected_s
        deadline = started + 2.0 * expected_s + self.frame_timeout_margin_s
        # Nothing can be ready before the exposure ends; sleep most of it, waking at once on abort.
        if expected_s > 0.03:
            self._abort_event.wait(expected_s - 0.02)

        busy = c_int(1)
        while True:
            if self._abort_requested:
                self._break()
                self._busy_until = frame_end
                return None, "aborted"
            with _dll_lock:
                rc = int(spectro.JETI_SpectroStatusEx(h, byref(busy)))
            if rc != JETI_SUCCESS:
                self._break()
                self._busy_until = time.monotonic()
                return None, "JETI_SpectroStatusEx: " + self.get_error(rc)
            if not busy.value:
                break
            if time.monotonic() > deadline:
                self._break()
                self._busy_until = time.monotonic()
                return None, ("no frame within %.1f s (integration time %.3f ms x %d, scan delay %.0f ms); "
                              "measurement cancelled" % (2.0 * expected_s + self.frame_timeout_margin_s,
                                                         self.it_ms, averages, self.scan_delay_ms))
            time.sleep(self.poll_interval_s)

        with _dll_lock:
            rc = int(spectro.JETI_LightPixEx(h, self._frame_buf))
            if rc != JETI_SUCCESS:
                return None, "JETI_LightPixEx: " + self.get_error(rc)
            frame = np.frombuffer(self._frame_buf, dtype=np.int32, count=self.npix_active).astype(np.float64)
        return frame, "OK"

    def measure(self, ncy=1):
        """
        Synchronous capture of *ncy* cycles; mean -> rcm, std -> rcs, (max - mean) -> rcl.
        Returns "OK" or an error string.  The call blocks for about
        ncy x (integration time + frame overhead), so wait_for_measurement()
        returns immediately afterwards.
        """
        if not self._meas_lock.acquire(blocking=False):
            return "Measurement already in progress"

        self._meas_done_event.clear()
        self.measuring = True
        self._abort_requested = False
        self._abort_event.clear()
        self._stop_requested = False
        self.error = "OK"
        self.ncy_requested = max(1, int(ncy))
        self.ncy_read = 0
        self.ncy_handled = 0
        self.ncy_saturated = 0
        self.meas_start_time = spec_clock.now()

        try:
            res = self._capture_and_average(self.ncy_requested)
            if res != "OK":
                self.error = res
            return res
        finally:
            self.measuring = False
            self._meas_done_event.set()
            if self.external_meas_done_event is not None and not self.recovering:
                try:
                    self.external_meas_done_event.set()
                except Exception:
                    pass
            self._meas_lock.release()

    def _capture_and_average(self, ncy):
        if not self.is_open or self._spectro is None:
            return "JETI spectrometer not connected"
        if self.it_ms is None:
            return "Integration time not set"

        if self.device_averaging and ncy > 1:
            averages = min(ncy, JETI_MAX_AVERAGES)
            frame, res = self._acquire(averages)
            if res == "aborted":
                return "Measurement stopped by user" if self._stop_requested else "Measurement aborted"
            if res != "OK":
                return res
            self.rcm = frame
            self.rcs = np.zeros_like(frame)          # per-frame spread is not available
            self.rcl = np.zeros_like(frame)
            self.ncy_read = self.ncy_handled = averages
            if frame.max() >= self.eff_saturation_limit:
                self.ncy_saturated = averages
            return "OK"

        stats = _FrameStats(self.npix_active)
        for i in range(ncy):
            if self._abort_requested:
                self.logger.info("JETI measurement aborted before cycle %d.", i + 1)
                break
            frame, res = self._acquire(1)
            if res == "aborted":
                self.logger.info("JETI measurement aborted during cycle %d.", i + 1)
                break
            if res != "OK":
                return "Cycle %d/%d failed: %s" % (i + 1, ncy, res)
            stats.add(frame)
            self.ncy_read += 1
            self.ncy_handled += 1
            if frame.max() >= self.eff_saturation_limit:
                self.ncy_saturated += 1
                if self.abort_on_saturation:
                    self.logger.info("Saturation detected at cycle %d; aborting.", i + 1)
                    break

        if stats.n == 0:
            return "Measurement stopped by user" if self._stop_requested else "No frames acquired"
        self.rcm, self.rcs, self.rcl = stats.result()
        return "OK"

    def wait_for_measurement(self, timeout=None):
        """
        measure() blocks already, so this normally returns at once with the last
        status.  The wait is bounded (default from spec_xfus.measurement_timeout_s)
        so a stalled DLL call can never hang the caller forever.  A run the
        operator stopped reports "Measurement stopped by user" once, because the
        cycle loop still returns the frames it had already averaged.
        """
        if timeout is None:
            per_frame_ms = (self.it_ms or 10.0) + (self.frame_overhead_ms or self.scan_delay_ms or 0.0)
            timeout = measurement_timeout_s(self.ncy_requested or 1, per_frame_ms)
        if not self._meas_done_event.wait(timeout):
            self.error = "JETI measurement timed out after %.1f s" % float(timeout)
            return self.error
        if self._stop_requested:
            self._stop_requested = False
            if self.error == "OK":
                self.error = "Measurement stopped by user"
        return self.error

    def abort(self, ignore_errors=False):
        """
        Cancel the measurement in flight: the measuring thread sees the flag within
        one poll interval, sends JETI_SpectroBreakEx and ends the cycle loop.  Only
        that thread talks to the device, so the SDK is never called concurrently on
        one handle.
        """
        self._abort_requested = True
        self._abort_event.set()
        return "OK"

    def request_stop(self):
        """
        What the UI Stop button calls: cancel the frame in flight and wake any
        thread blocked in wait_for_measurement().  The flag separates a user stop
        from a clean finish.
        """
        self._stop_requested = True
        res = self.abort(ignore_errors=True)
        self._meas_done_event.set()
        return res
