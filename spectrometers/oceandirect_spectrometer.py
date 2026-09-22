"""
Backend for Ocean Insight / Ocean Optics spectrometers driven by the OceanDirect
SDK (OceanDirect.dll, C API).  Written for and verified against the Flame-S
(serial FLMS17243, USB2000+ device family); every other OceanDirect-supported
unit (USB2000+, HR2000+, HR4000, Flame-NIR/U/W, Ocean FX/HDX/SR/ST, QE Pro...)
uses the same calls and should work unchanged.

The DLL is bound directly through ctypes, exactly like the other SciLab backends
(`Ava1`, `Hama3`, `Hama4`, `MiniSpec`) bind their vendor DLLs.  The vendor Python
wrapper shipped with the SDK is intentionally not used: it needs the SDK folder on
sys.path, prints through a print-based logger, caches closed device objects and
requires Python 3.9+ annotations.  Binding the C API directly keeps SciLab's
"point the app at a DLL" workflow and avoids those quirks.

Key facts (see docs/flame-s-oceandirect-integration-guide.md for the full study):

- OceanDirect.dll is **64-bit only**; SciLab must run in a 64-bit Python.
- The SDK is licensed for use in place and may not be redistributed, so the DLL
  is *not* bundled.  It is looked up in ``OCEANDIRECT_HOME`` or the default
  install folder ``C:\\Program Files\\Ocean Optics\\OceanDirect SDK``.
- The SDK enables electric-dark and nonlinearity correction by default.  This
  backend switches both **off** so SciLab records raw ADC counts (full scale
  65535 on the Flame-S) like every other backend.
- Serial numbers are readable only after a device is opened, and device IDs
  become invalid after a close.  Discovery therefore opens/reads/closes each
  unit, and ``connect()`` always re-probes before opening.
- Flame-S has no acquisition-control feature: a frame cannot be aborted once
  started.  ``abort()`` stops the cycle loop between frames.
- The first frame after an integration-time change may still carry the previous
  exposure and is discarded.
"""

import atexit
import logging
import os
import struct
import sys
import threading
from ctypes import (
    CDLL,
    POINTER,
    byref,
    c_bool,
    c_char_p,
    c_double,
    c_int,
    c_long,
    c_ubyte,
    c_uint,
    c_ulong,
    c_ushort,
    create_string_buffer,
)

import numpy as np

from spec_xfus import spec_clock


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SDK location
# ---------------------------------------------------------------------------

OCEANDIRECT_DLL_NAME = "OceanDirect.dll"
OCEANDIRECT_DEFAULT_SDK_DIR = r"C:\Program Files\Ocean Optics\OceanDirect SDK"
OCEANDIRECT_HOME_ENV = "OCEANDIRECT_HOME"

# Error codes observed on the Flame-S / documented in OceanDirectAPIConstants.h.
# The numeric values are exported by the DLL as `extern const int` symbols; the
# ones below were verified at run time on OceanDirect 3.1.4.
ODAPI_ERROR_SUCCESS = 0
ODAPI_ERROR_FEATURE_NOT_FOUND = 5          # "No such feature on device"
ODAPI_ERROR_INVALID_ARGUMENT = 15
ODAPI_WARN_DEVICE_INITIALIZE_INCOMPLETE = 10001   # tolerated on open

ODAPI_TRIGGER_MODE_NORMAL = 0


def _sdk_candidate_dirs():
    dirs = []
    home = os.environ.get(OCEANDIRECT_HOME_ENV, "").strip().strip('"')
    if home:
        dirs.append(home)
    dirs.append(OCEANDIRECT_DEFAULT_SDK_DIR)
    # A copy dropped next to the other vendor DLLs (only legal once Ocean Insight
    # has granted redistribution terms; see the integration guide, section 4.6).
    dirs.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "spectrometers dll files"))
    return dirs


def find_oceandirect_dll():
    """
    Locate OceanDirect.dll.  Returns the absolute path or "" when not found.

    Search order: %OCEANDIRECT_HOME% (SDK root, its lib folder, or the Python
    wrapper's lib folder), the default SDK install folder, then SciLab's own
    'spectrometers dll files' folder.
    """
    for base in _sdk_candidate_dirs():
        if not base:
            continue
        for rel in (
            OCEANDIRECT_DLL_NAME,
            os.path.join("lib", OCEANDIRECT_DLL_NAME),
            os.path.join("Python", "oceandirect", "lib", OCEANDIRECT_DLL_NAME),
        ):
            candidate = os.path.join(base, rel)
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)
    return ""


# ---------------------------------------------------------------------------
# Process-wide SDK handle.  OceanDirect keeps global state (device table), the
# documentation requires discovery/open/close to be serialized, and a DLL can
# only be loaded once per process, so everything goes through this module-level
# handle behind one re-entrant lock.
# ---------------------------------------------------------------------------

_sdk_lock = threading.RLock()
_sdk = {"handle": None, "path": "", "initialized": False}


def _bind(handle):
    """Attach argtypes / restypes for every C entry point this backend uses."""
    h = handle
    err_p = POINTER(c_int)

    h.odapi_initialize.argtypes = []
    h.odapi_initialize.restype = None
    h.odapi_shutdown.argtypes = []
    h.odapi_shutdown.restype = None
    h.odapi_get_api_version_numbers.argtypes = [POINTER(c_uint), POINTER(c_uint), POINTER(c_uint)]
    h.odapi_get_api_version_numbers.restype = None

    h.odapi_probe_devices.argtypes = []
    h.odapi_probe_devices.restype = c_int
    h.odapi_get_number_of_device_ids.argtypes = []
    h.odapi_get_number_of_device_ids.restype = c_int
    h.odapi_get_device_ids.argtypes = [POINTER(c_long), c_uint]
    h.odapi_get_device_ids.restype = c_int
    h.odapi_open_device.argtypes = [c_long, err_p]
    h.odapi_open_device.restype = None
    h.odapi_close_device.argtypes = [c_long, err_p]
    h.odapi_close_device.restype = None

    h.odapi_get_error_string_length.argtypes = [c_int]
    h.odapi_get_error_string_length.restype = c_int
    h.odapi_get_error_string.argtypes = [c_int, c_char_p, c_int]
    h.odapi_get_error_string.restype = c_int

    h.odapi_get_device_type.argtypes = [c_long, err_p, c_char_p, c_uint]
    h.odapi_get_device_type.restype = c_int
    h.odapi_get_device_name.argtypes = [c_long, err_p, c_char_p, c_uint]
    h.odapi_get_device_name.restype = c_int
    h.odapi_get_serial_number.argtypes = [c_long, err_p, c_char_p, c_int]
    h.odapi_get_serial_number.restype = c_int

    h.odapi_get_formatted_spectrum_length.argtypes = [c_long, err_p]
    h.odapi_get_formatted_spectrum_length.restype = c_int
    h.odapi_get_formatted_spectrum.argtypes = [c_long, err_p, POINTER(c_double), c_int]
    h.odapi_get_formatted_spectrum.restype = c_int
    h.odapi_get_wavelengths.argtypes = [c_long, err_p, POINTER(c_double), c_int]
    h.odapi_get_wavelengths.restype = c_int
    h.odapi_get_maximum_intensity.argtypes = [c_long, err_p]
    h.odapi_get_maximum_intensity.restype = c_double

    h.odapi_set_integration_time_micros.argtypes = [c_long, err_p, c_ulong]
    h.odapi_set_integration_time_micros.restype = None
    h.odapi_get_integration_time_micros.argtypes = [c_long, err_p]
    h.odapi_get_integration_time_micros.restype = c_ulong
    h.odapi_get_minimum_integration_time_micros.argtypes = [c_long, err_p]
    h.odapi_get_minimum_integration_time_micros.restype = c_ulong
    h.odapi_get_maximum_integration_time_micros.argtypes = [c_long, err_p]
    h.odapi_get_maximum_integration_time_micros.restype = c_ulong

    h.odapi_apply_electric_dark_correction_usage.argtypes = [c_long, err_p, c_ubyte]
    h.odapi_apply_electric_dark_correction_usage.restype = None
    h.odapi_apply_nonlinearity_correct_usage.argtypes = [c_long, err_p, c_bool]
    h.odapi_apply_nonlinearity_correct_usage.restype = None
    h.odapi_get_electric_dark_correction_usage.argtypes = [c_long, err_p]
    h.odapi_get_electric_dark_correction_usage.restype = c_bool
    h.odapi_get_nonlinearity_correct_usage.argtypes = [c_long, err_p]
    h.odapi_get_nonlinearity_correct_usage.restype = c_bool

    h.odapi_set_scans_to_average.argtypes = [c_long, err_p, c_uint]
    h.odapi_set_scans_to_average.restype = None
    h.odapi_get_scans_to_average.argtypes = [c_long, err_p]
    h.odapi_get_scans_to_average.restype = c_uint
    h.odapi_set_boxcar_width.argtypes = [c_long, err_p, c_ushort]
    h.odapi_set_boxcar_width.restype = None
    h.odapi_get_boxcar_width.argtypes = [c_long, err_p]
    h.odapi_get_boxcar_width.restype = c_ushort

    h.odapi_adv_set_trigger_mode.argtypes = [c_long, err_p, c_int]
    h.odapi_adv_set_trigger_mode.restype = None
    h.odapi_adv_get_trigger_mode.argtypes = [c_long, err_p]
    h.odapi_adv_get_trigger_mode.restype = c_int

    h.odapi_get_electric_dark_pixel_indices.argtypes = [c_long, err_p, POINTER(c_int), c_int]
    h.odapi_get_electric_dark_pixel_indices.restype = c_int
    h.odapi_adv_get_revision_firmware.argtypes = [c_long, err_p, c_char_p, c_int]
    h.odapi_adv_get_revision_firmware.restype = c_ushort
    h.odapi_adv_get_revision_fpga.argtypes = [c_long, err_p, c_char_p, c_int]
    h.odapi_adv_get_revision_fpga.restype = c_ushort
    h.odapi_adv_abort_acquisition.argtypes = [c_long, err_p]
    h.odapi_adv_abort_acquisition.restype = None


def _shutdown_sdk_at_exit():
    with _sdk_lock:
        handle = _sdk["handle"]
        if handle is not None and _sdk["initialized"]:
            try:
                handle.odapi_shutdown()
            except Exception:
                pass
            _sdk["initialized"] = False


def _load_sdk(dll_path):
    """
    Load OceanDirect.dll once per process.  Returns ("OK", handle) or (error, None).
    A later request with a different path re-uses the already loaded library
    (Windows cannot load two copies of the same module) and logs a warning.
    """
    with _sdk_lock:
        if _sdk["handle"] is not None:
            if dll_path and os.path.normcase(os.path.abspath(dll_path)) != os.path.normcase(_sdk["path"]):
                logger.warning("OceanDirect.dll already loaded from %s; ignoring %s", _sdk["path"], dll_path)
            return "OK", _sdk["handle"]

        if struct.calcsize("P") * 8 != 64:
            return ("OceanDirect.dll is 64-bit only but this Python process is 32-bit. "
                    "Run SciLab with a 64-bit Python."), None

        path = dll_path or find_oceandirect_dll()
        if not path or not os.path.isfile(path):
            return (OCEANDIRECT_DLL_NAME + " not found. Install the OceanDirect SDK "
                    "(default: " + OCEANDIRECT_DEFAULT_SDK_DIR + "), set " + OCEANDIRECT_HOME_ENV +
                    ", or point the Setup tab at OceanDirect.dll. Path tried: '" + str(path) + "'"), None

        try:
            if hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(os.path.dirname(path))
                except Exception:
                    pass
            handle = CDLL(path)
            _bind(handle)
        except Exception as exc:
            return ("Could not load " + OCEANDIRECT_DLL_NAME + " from '" + path + "': " + str(exc) +
                    ". The Microsoft Visual C++ 2015-2022 x64 runtime must be installed."), None

        _sdk["handle"] = handle
        _sdk["path"] = os.path.abspath(path)
        return "OK", handle


def _initialize_sdk():
    with _sdk_lock:
        handle = _sdk["handle"]
        if handle is None:
            return "OceanDirect.dll is not loaded"
        if not _sdk["initialized"]:
            try:
                handle.odapi_initialize()
            except Exception as exc:
                return "odapi_initialize failed: " + str(exc)
            _sdk["initialized"] = True
            atexit.register(_shutdown_sdk_at_exit)
        return "OK"


def _error_string(handle, code):
    """Human-readable text for an odapi error code."""
    if code == ODAPI_ERROR_SUCCESS:
        return "OK"
    try:
        length = int(handle.odapi_get_error_string_length(int(code)))
        if length <= 0:
            length = 128
        buf = create_string_buffer(length + 1)
        handle.odapi_get_error_string(int(code), buf, length + 1)
        text = buf.value.decode("utf-8", errors="ignore").strip()
    except Exception:
        text = ""
    return (text or "unknown error") + " (OceanDirect error " + str(int(code)) + ")"


def _read_string(fn, dev_id, err, size=64):
    buf = create_string_buffer(size)
    fn(dev_id, byref(err), buf, size)
    if err.value != ODAPI_ERROR_SUCCESS:
        return ""
    return buf.value.decode("utf-8", errors="ignore").strip()


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
        if self.n == 0:
            spread = np.zeros_like(mean)
        else:
            spread = self.max_seen - mean
        return mean, np.sqrt(var), spread


OceanDirect_Spectrometer_Instances = {}


class OceanDirect_Spectrometer(object):
    """
    Control class for Ocean Insight spectrometers through OceanDirect.dll.
    Exposes the SciLab backend contract (connect / disconnect / set_it / measure /
    wait_for_measurement plus sn, npix_active, rcm...).
    """

    def __init__(self):
        self.spec_type = "OcOpt1"

        self.debug_mode = 0
        self.simulation_mode = False
        self.dll_path = ""

        self.sn = ""
        self.alias = "1"
        self.model = ""
        self.device_type = ""
        self.fw_version = ""
        self.fpga_version = ""
        self.sdk_version = ""

        # Flame-S defaults; refreshed from the device at connect time.
        self.npix_active = 2048
        self.npix_blind_left = 0
        self.npix_blind_right = 0
        self.nbits = 16
        self.min_it_ms = 1.0
        self.max_it_ms = 655350.0
        self.discriminator_factor = 1.0
        self.eff_saturation_limit = 2 ** 16 - 1
        self.abort_on_saturation = True
        self.wl = np.array([])                # wavelength per pixel (nm)
        self.electric_dark_pixels = []

        # Internal state
        self.dll_handler = None
        self.device_id = -1
        self.is_open = False
        self.it_ms = None
        self.last_errcode = 0
        self.error = "OK"
        self.logger = logger

        self._meas_lock = threading.Lock()
        self._meas_done_event = threading.Event()
        self._meas_done_event.set()
        self.measuring = False
        self._abort_requested = False
        self._discard_next_frame = False
        self._spec_buf = None

        # Result arrays expected by the rest of the app
        self.rcm = np.array([])
        self.rcs = np.array([])
        self.rcl = np.array([])
        self.sy = np.zeros(self.npix_active, dtype=np.float64)
        self.syy = np.zeros(self.npix_active, dtype=np.float64)
        self.sxy = np.zeros(self.npix_active, dtype=np.float64)

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
        self.sy = np.zeros(self.npix_active, dtype=np.float64)
        self.syy = np.zeros(self.npix_active, dtype=np.float64)
        self.sxy = np.zeros(self.npix_active, dtype=np.float64)
        self.ncy_read = 0
        self.ncy_handled = 0
        self.ncy_saturated = 0

    def get_error(self, code):
        """Translate an odapi error code into "OK" or a description; remembers the code."""
        code = int(code)
        self.last_errcode = code
        if code == ODAPI_ERROR_SUCCESS:
            return "OK"
        if self.dll_handler is None:
            return "OceanDirect error " + str(code)
        return _error_string(self.dll_handler, code)

    def get_device_info(self):
        """Identity and configuration summary for display / logging."""
        return {
            "type": self.spec_type,
            "model": self.model,
            "device_type": self.device_type,
            "sn": self.sn,
            "firmware": self.fw_version,
            "fpga": self.fpga_version,
            "sdk": self.sdk_version,
            "dll": _sdk["path"],
            "npix": self.npix_active,
            "wl_range_nm": (float(self.wl[0]), float(self.wl[-1])) if len(self.wl) else None,
            "it_range_ms": (self.min_it_ms, self.max_it_ms),
            "saturation": self.eff_saturation_limit,
            "electric_dark_pixels": list(self.electric_dark_pixels),
        }

    # ------------------------------------------------------------------
    # DLL / SDK lifecycle
    # ------------------------------------------------------------------
    def load_spec_dll(self):
        if self.simulation_mode:
            return "OK"
        res, handle = _load_sdk(self.dll_path)
        if res != "OK":
            return res
        self.dll_handler = handle
        self.dll_path = _sdk["path"]
        try:
            major, minor, point = c_uint(0), c_uint(0), c_uint(0)
            handle.odapi_get_api_version_numbers(byref(major), byref(minor), byref(point))
            self.sdk_version = str(major.value) + "." + str(minor.value) + "." + str(point.value)
        except Exception:
            self.sdk_version = ""
        return "OK"

    def initialize_dll(self):
        if self.simulation_mode:
            return "OK"
        if self.dll_handler is None:
            res = self.load_spec_dll()
            if res != "OK":
                return res
        return _initialize_sdk()

    def _probe_ids(self):
        """Re-scan USB and return the current list of device IDs (IDs change after every close)."""
        h = self.dll_handler
        with _sdk_lock:
            h.odapi_probe_devices()
            n = int(h.odapi_get_number_of_device_ids())
            if n <= 0:
                return []
            ids = (c_long * n)()
            got = int(h.odapi_get_device_ids(ids, n))
            return [int(ids[i]) for i in range(max(0, min(got, n)))]

    def _open_id(self, dev_id):
        """Open a device by ID; returns "OK" or an error text.  Error 10001 is a tolerated warning."""
        err = c_int(0)
        self.dll_handler.odapi_open_device(dev_id, byref(err))
        if err.value in (ODAPI_ERROR_SUCCESS, ODAPI_WARN_DEVICE_INITIALIZE_INCOMPLETE):
            return "OK"
        return self.get_error(err.value)

    def _close_id(self, dev_id):
        err = c_int(0)
        try:
            self.dll_handler.odapi_close_device(dev_id, byref(err))
        except Exception:
            pass

    def _identity(self, dev_id):
        """Serial / model / device type / pixel count of an *open* device."""
        err = c_int(0)
        h = self.dll_handler
        serial = _read_string(h.odapi_get_serial_number, dev_id, err)
        model = _read_string(h.odapi_get_device_name, dev_id, err)
        dev_type = _read_string(h.odapi_get_device_type, dev_id, err)
        npix = int(h.odapi_get_formatted_spectrum_length(dev_id, byref(err)))
        return serial, model, dev_type, npix

    def get_number_of_devices(self):
        if self.simulation_mode:
            return "OK", 1
        if self.dll_handler is None:
            res = self.initialize_dll()
            if res != "OK":
                return res, 0
        try:
            ids = self._probe_ids()
        except Exception as exc:
            return "OceanDirect probe failed: " + str(exc), 0
        return "OK", len(ids)

    def get_all_devices_info(self, ndev):
        """
        Returns ("OK", {index: {"id": serial, "model": ..., "device_type": ..., "npix": ...}}).
        The serial number is only readable on an open device, so each unit is
        opened, read and closed again (a unit held open by another program is
        listed by device type only).
        """
        if self.simulation_mode:
            return "OK", {0: {"id": "SIM-FLMS00000", "model": "Flame-S (simulated)",
                              "device_type": "USB2000PLUS", "npix": self.npix_active}}
        if ndev == 0:
            return "OK", {}
        if self.dll_handler is None:
            res = self.initialize_dll()
            if res != "OK":
                return res, {}

        info = {}
        with _sdk_lock:
            ids = self._probe_ids()
            for index, dev_id in enumerate(ids):
                err = c_int(0)
                dev_type = _read_string(self.dll_handler.odapi_get_device_type, dev_id, err) or "OceanDirect"
                res = self._open_id(dev_id)
                if res != "OK":
                    self.logger.warning("OceanDirect device id %s (%s) could not be opened: %s", dev_id, dev_type, res)
                    info[index] = {"id": "", "model": dev_type + " (busy: " + res + ")",
                                   "device_type": dev_type, "npix": 0}
                    continue
                try:
                    serial, model, dev_type2, npix = self._identity(dev_id)
                finally:
                    self._close_id(dev_id)
                info[index] = {"id": serial or ("OcOpt1-" + str(index + 1)),
                               "model": model or dev_type2 or dev_type,
                               "device_type": dev_type2 or dev_type,
                               "npix": npix}
        return "OK", info

    # ------------------------------------------------------------------
    # Connect / disconnect
    # ------------------------------------------------------------------
    def connect(self):
        if self.simulation_mode:
            self.logger.info("--- Connecting OceanDirect spectrometer (Simulation Mode) ---")
            self.sn = self.sn or "SIM-FLMS00000"
            self.model = "Flame-S (simulated)"
            self.device_type = "USB2000PLUS"
            self.wl = 339.68143 + 0.380685 * np.arange(self.npix_active) \
                - 2.0555342e-05 * np.arange(self.npix_active) ** 2
            self.is_open = True
            self.reset_spec_data()
            return self.set_it(10.0)

        self.logger.info("--- Connecting OceanDirect spectrometer " + str(self.alias) + " ---")

        res = self.initialize_dll()
        if res != "OK":
            self.error = res
            return res

        h = self.dll_handler
        with _sdk_lock:
            if self.is_open and self.device_id >= 0:
                self._close_id(self.device_id)
                self.is_open = False
                self.device_id = -1

            try:
                ids = self._probe_ids()
            except Exception as exc:
                self.error = "OceanDirect probe failed: " + str(exc)
                return self.error
            if not ids:
                self.error = ("No OceanDirect spectrometer found on USB. Check the cable, that Device "
                              "Manager lists it under 'Ocean Optics USB Devices' (WinUSB driver), and "
                              "that OceanView or another program is not holding it open.")
                return self.error

            wanted = (self.sn or "").strip()
            found_serials = []
            chosen = -1
            open_errors = []
            for dev_id in ids:
                res = self._open_id(dev_id)
                if res != "OK":
                    open_errors.append(res)
                    continue
                serial, model, dev_type, npix = self._identity(dev_id)
                found_serials.append(serial or "?")
                if not wanted or serial == wanted:
                    chosen = dev_id
                    self.sn = serial or wanted
                    self.model = model
                    self.device_type = dev_type
                    if npix > 0:
                        self.npix_active = npix
                    break
                self._close_id(dev_id)

            if chosen < 0:
                if wanted:
                    self.error = ("OceanDirect spectrometer '" + wanted + "' not found. Found: " +
                                  (", ".join(found_serials) or "none") +
                                  ((". Open errors: " + "; ".join(open_errors)) if open_errors else ""))
                else:
                    self.error = ("OceanDirect spectrometer could not be opened: " +
                                  ("; ".join(open_errors) or "unknown error") +
                                  ". Close OceanView / other programs using it, or disable and "
                                  "re-enable the device in Device Manager.")
                return self.error

            self.device_id = chosen
            self.is_open = True

            err = c_int(0)
            # ---- device parameters -------------------------------------------------
            wl = (c_double * self.npix_active)()
            n_wl = int(h.odapi_get_wavelengths(chosen, byref(err), wl, self.npix_active))
            if err.value == ODAPI_ERROR_SUCCESS and n_wl > 0:
                self.wl = np.frombuffer(wl, dtype=np.float64, count=n_wl).copy()
            else:
                self.wl = np.array([])

            err = c_int(0)
            it_min = int(h.odapi_get_minimum_integration_time_micros(chosen, byref(err)))
            if err.value == ODAPI_ERROR_SUCCESS and it_min > 0:
                self.min_it_ms = it_min / 1000.0
            err = c_int(0)
            it_max = int(h.odapi_get_maximum_integration_time_micros(chosen, byref(err)))
            if err.value == ODAPI_ERROR_SUCCESS and it_max > 0:
                self.max_it_ms = it_max / 1000.0

            err = c_int(0)
            max_int = float(h.odapi_get_maximum_intensity(chosen, byref(err)))
            if err.value == ODAPI_ERROR_SUCCESS and max_int > 0:
                self.eff_saturation_limit = max_int
                self.nbits = int(round(np.log2(max_int + 1)))

            self.fw_version = _read_string(h.odapi_adv_get_revision_firmware, chosen, c_int(0), 32)
            self.fpga_version = _read_string(h.odapi_adv_get_revision_fpga, chosen, c_int(0), 32)

            err = c_int(0)
            edp = (c_int * 64)()
            n_edp = int(h.odapi_get_electric_dark_pixel_indices(chosen, byref(err), edp, 64))
            self.electric_dark_pixels = [int(edp[i]) for i in range(max(0, min(n_edp, 64)))] \
                if err.value == ODAPI_ERROR_SUCCESS else []

            # ---- configuration: raw counts, no averaging, no smoothing ----------------
            # The SDK turns electric-dark and nonlinearity correction ON by default;
            # SciLab wants raw ADC counts (parity with the other backends).
            for name, call in (
                ("electric dark correction off",
                 lambda e: h.odapi_apply_electric_dark_correction_usage(chosen, byref(e), 0)),
                ("nonlinearity correction off",
                 lambda e: h.odapi_apply_nonlinearity_correct_usage(chosen, byref(e), False)),
                ("scans to average = 1",
                 lambda e: h.odapi_set_scans_to_average(chosen, byref(e), 1)),
                ("boxcar width = 0",
                 lambda e: h.odapi_set_boxcar_width(chosen, byref(e), 0)),
            ):
                err = c_int(0)
                call(err)
                if err.value != ODAPI_ERROR_SUCCESS:
                    self.error = "OceanDirect could not set " + name + ": " + self.get_error(err.value)
                    self._close_id(chosen)
                    self.is_open = False
                    self.device_id = -1
                    return self.error

            # Free-running trigger.  Not every unit exposes the call; failure is not fatal.
            err = c_int(0)
            try:
                h.odapi_adv_set_trigger_mode(chosen, byref(err), ODAPI_TRIGGER_MODE_NORMAL)
            except Exception:
                pass
            if err.value != ODAPI_ERROR_SUCCESS:
                self.logger.info("OceanDirect trigger mode not set (%s); continuing.", self.get_error(err.value))

            self._spec_buf = (c_double * self.npix_active)()
            self.reset_spec_data()

            # Integration time must be set explicitly after every open (the device
            # keeps whatever was set last).
            res = self.set_it(max(self.min_it_ms, 10.0))
            if res != "OK":
                self.error = res
                self._close_id(chosen)
                self.is_open = False
                self.device_id = -1
                return res

            # One test frame, like the legacy reset behaviour; also flushes the
            # frame that may still carry the previous exposure.
            frame, res = self._read_frame()
            if res != "OK":
                self.error = "OceanDirect test measurement failed: " + res
                self._close_id(chosen)
                self.is_open = False
                self.device_id = -1
                return self.error
            self._discard_next_frame = False

        OceanDirect_Spectrometer_Instances[self.sn or self.alias] = self
        self.logger.info(
            "OceanDirect connected: model=" + str(self.model) + ", sn=" + str(self.sn) +
            ", type=" + str(self.device_type) + ", pixels=" + str(self.npix_active) +
            ", IT range " + str(self.min_it_ms) + "-" + str(self.max_it_ms) + " ms" +
            ", full scale " + str(self.eff_saturation_limit) +
            ", SDK " + str(self.sdk_version) + ", fw " + str(self.fw_version)
        )
        self.error = "OK"
        return "OK"

    def disconnect(self, *args, **kwargs):
        if self.simulation_mode:
            self.is_open = False
            return "OK"
        if not self.is_open or self.dll_handler is None:
            self.is_open = False
            self.device_id = -1
            return "OK"
        self._abort_requested = True
        res = "OK"
        with _sdk_lock:
            err = c_int(0)
            try:
                self.dll_handler.odapi_close_device(self.device_id, byref(err))
                res = self.get_error(err.value)
            except Exception as exc:
                res = "odapi_close_device raised: " + str(exc)
            self.is_open = False
            self.device_id = -1          # IDs are invalid after a close; connect() re-probes
        key = self.sn or self.alias
        if key in OceanDirect_Spectrometer_Instances:
            del OceanDirect_Spectrometer_Instances[key]
        self.logger.info("OceanDirect disconnected (" + res + ").")
        return res

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------
    def set_it(self, it_ms):
        try:
            it_ms = float(it_ms)
        except Exception:
            return "Invalid integration time: " + str(it_ms)
        if it_ms < self.min_it_ms:
            it_ms = self.min_it_ms
        if it_ms > self.max_it_ms:
            it_ms = self.max_it_ms

        if self.simulation_mode:
            if self.it_ms != it_ms:
                self._discard_next_frame = True
            self.it_ms = it_ms
            return "OK"

        if self.dll_handler is None or not self.is_open:
            return "OceanDirect spectrometer not connected"

        micros = int(round(it_ms * 1000.0))
        with _sdk_lock:
            err = c_int(0)
            self.dll_handler.odapi_set_integration_time_micros(self.device_id, byref(err), micros)
            res = self.get_error(err.value)
        if res == "OK":
            if self.it_ms is None or abs(self.it_ms - it_ms) > 1e-9:
                # The frame in flight still uses the previous exposure.
                self._discard_next_frame = True
            self.it_ms = it_ms
        else:
            self.error = res
        return res

    def _read_frame(self):
        """
        Blocking read of one spectrum (raw counts as float64).  Returns (frame, "OK")
        or (None, error).  Blocks for about one integration time (floor ~2.6 ms on
        the Flame-S for the USB transfer of 2048 x 16 bit).
        """
        h = self.dll_handler
        with _sdk_lock:
            if h is None or not self.is_open or self._spec_buf is None:
                return None, "OceanDirect spectrometer not connected"
            err = c_int(0)
            n = int(h.odapi_get_formatted_spectrum(self.device_id, byref(err), self._spec_buf, self.npix_active))
            if err.value != ODAPI_ERROR_SUCCESS:
                return None, "odapi_get_formatted_spectrum: " + self.get_error(err.value)
            if n <= 0:
                return None, "odapi_get_formatted_spectrum returned no data"
            frame = np.frombuffer(self._spec_buf, dtype=np.float64, count=min(n, self.npix_active)).copy()
        if frame.size < self.npix_active:
            frame = np.pad(frame, (0, self.npix_active - frame.size))
        return frame, "OK"

    def _simulated_frame(self):
        base = (self.it_ms or 1.0) * 100.0
        rng = np.random.default_rng()
        frame = rng.normal(loc=base + 2270.0, scale=max(1.0, base * 0.02), size=self.npix_active)
        return np.clip(frame, 0, self.eff_saturation_limit).astype(np.float64)

    def measure(self, ncy=1):
        """
        Synchronous capture of *ncy* frames; mean -> rcm, std -> rcs, (max - mean) -> rcl.
        Returns "OK" or an error string.  The call blocks for about ncy x IT, so
        wait_for_measurement() returns immediately afterwards.
        """
        if not self._meas_lock.acquire(blocking=False):
            return "Measurement already in progress"

        self._meas_done_event.clear()
        self.measuring = True
        self._abort_requested = False
        self.error = "OK"
        self.ncy_requested = int(ncy)
        self.ncy_read = 0
        self.ncy_handled = 0
        self.ncy_saturated = 0
        self.meas_start_time = spec_clock.now()

        try:
            res = self._capture_and_average(int(ncy))
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
        if not self.simulation_mode and (self.dll_handler is None or not self.is_open):
            return "OceanDirect spectrometer not connected"
        if ncy < 1:
            ncy = 1

        read = self._simulated_frame if self.simulation_mode else None

        # Discard the frame that may still carry the previous integration time.
        if self._discard_next_frame:
            if read is not None:
                read()
            else:
                frame, res = self._read_frame()
                if res != "OK":
                    return res
            self._discard_next_frame = False

        stats = _FrameStats(self.npix_active)
        for i in range(ncy):
            if self._abort_requested:
                self.logger.info("OceanDirect measurement aborted before cycle " + str(i + 1) + ".")
                break
            if read is not None:
                frame = read()
            else:
                frame, res = self._read_frame()
                if res != "OK":
                    return "Cycle " + str(i + 1) + "/" + str(ncy) + " failed: " + res
            if self.discriminator_factor != 1.0:
                frame = frame * self.discriminator_factor

            stats.add(frame)
            self.ncy_read += 1
            self.ncy_handled += 1

            if frame.max() >= self.eff_saturation_limit:
                self.ncy_saturated += 1
                if self.abort_on_saturation:
                    self.logger.info("Saturation detected at cycle " + str(i + 1) + "; aborting.")
                    break

        if stats.n == 0:
            return "No frames acquired"
        self.rcm, self.rcs, self.rcl = stats.result()
        return "OK"

    def wait_for_measurement(self):
        # measure() blocks already; this just returns the last status.
        self._meas_done_event.wait()
        return self.error

    def abort(self, ignore_errors=False):
        """
        Stop the cycle loop after the frame in flight.  Flame-S has no acquisition
        control feature, so a single frame cannot be interrupted; the SDK call is
        attempted for units that do support it and its failure is ignored.
        """
        self._abort_requested = True
        if self.simulation_mode or self.dll_handler is None or not self.is_open:
            return "OK"
        try:
            with _sdk_lock:
                err = c_int(0)
                self.dll_handler.odapi_adv_abort_acquisition(self.device_id, byref(err))
        except Exception:
            pass
        return "OK"
