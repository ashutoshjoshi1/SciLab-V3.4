"""
Backend for Hamamatsu Mini-Spectrometer Micro Series Plus (C11287-01, C11288-01,
C11160, C11165-01/02, C11513) driven by DCamUSB.dll (Hamamatsu DCam-USB SDK,
SingleDevice variant, V300 controller).

This wraps the C API exported by `DCamUSB.dll` via ctypes and exposes the same
interface as the other backends (`Ava1`, `Hama2`, `Hama3`, `Hama4`) so the rest
of SciLab can stay agnostic.

Tested against: C11288-01.

Note: older Hamamatsu mini-spectrometer modules (e.g. C10785, TM/TG series)
use a different USB interface and SDK and are NOT covered by DCamUSB.dll.
"""

import logging
import os
import sys
import threading
import time
from copy import deepcopy
from ctypes import (
    c_int,
    c_uint32,
    c_uint16,
    c_char,
    byref,
    create_string_buffer,
    windll,
    POINTER,
    c_void_p,
)

import numpy as np

from spec_xfus import spec_clock


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants from DCamUSB.h
# ---------------------------------------------------------------------------

DCAM_BITPIXEL_16 = 16

DCAM_WAITSTATUS_COMPLETED = 0
DCAM_WAITSTATUS_UNCOMPLETED = 1
DCAM_WAIT_INFINITE = -1

DCAM_DEVSTATE_NON = 0
DCAM_DEVSTATE_DEVICE = 1
DCAM_DEVSTATE_NODEVICE = 2
DCAM_DEVSTATE_CONNECT = 3
DCAM_DEVSTATE_BOOT = 4

DCAM_BINNING_AREA = 0
DCAM_BINNING_FULL = 1

DCAM_TRIGMODE_INT = 0
DCAM_TRIGMODE_EXT_EDGE = 1
DCAM_TRIGMODE_EXT_LEVEL = 2

DCAM_DEVINF_TYPE = 0
DCAM_DEVINF_SERIALNO = 1
DCAM_DEVINF_VERSION = 2

DCAM_TIME_UNIT_TYPE1 = 0  # exposure ms / pulse out ms

# Status codes from DCamStatusCode.h
dcamusb_errors = {
    0: "OK",
    1: "Unknown error",
    2: "Library is not initialized",
    3: "Already in-use (already initialized)",
    4: "No driver was found",
    5: "Memory is insufficient",
    6: "The device is not connected",
    9: "Invalid parameter",
    100: "The device is not functioning",
    110: "Overrun has occurred",
    111: "Timeout has occurred",
    120: "Already started",
    200: "Cooling already started",
    201: "Cooling control stopped",
    202: "Failed to communicate with cooling controller",
}


MiniSpec_Spectrometer_Instances = {}
_dcamusb_initialized = False
_dcamusb_initialized_lock = threading.Lock()


# Supported PIDs from WinUsbDCamIF.inf (VID = 0x0661 = Hamamatsu Photonics).
# After firmware upload by the SDK, the device re-enumerates as one of these.
_DCAMUSB_SUPPORTED_PIDS = {
    "3400": "V300 (boot/firmware-not-loaded — generic)",
    "4607": "C11287-01",
    "4608": "C11288-01",
    "4609": "C11160",
    "460A": "C11165-01",
    "460B": "C11165-02",
    "460C": "C11513",
}


def _list_hamamatsu_usb_devices():
    """
    On Windows, enumerate plugged-in USB devices with VID_0661 (Hamamatsu).
    Returns a list of "PID xxxx (model)" strings. Empty if none / non-Windows.
    """
    if sys.platform != "win32":
        return []
    try:
        import subprocess
        cmd = (
            "Get-PnpDevice -PresentOnly | "
            "Where-Object { $_.InstanceId -like '*VID_0661*' } | "
            "Select-Object -ExpandProperty InstanceId"
        )
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return []

    found = []
    for line in (out.stdout or "").splitlines():
        line = line.strip().upper()
        idx = line.find("PID_")
        if idx < 0:
            continue
        pid = line[idx + 4:idx + 8]
        model = _DCAMUSB_SUPPORTED_PIDS.get(pid, "unknown — not supported by DCamUSB.dll")
        found.append("PID " + pid + " (" + model + ")")
    return found


def _augment_minispec_error(msg):
    """Add Windows USB diagnostic context to a MiniSpec error string."""
    devices = _list_hamamatsu_usb_devices()
    if not devices:
        return (
            msg + " (No Hamamatsu USB device detected by Windows. "
            "Check the cable and that Device Manager lists it under "
            "'Hamamatsu USB Camera Module', not 'Unknown Device'.)"
        )
    only_boot = all("PID 3400" in d for d in devices)
    suffix = " Plugged in: " + ", ".join(devices) + "."
    if only_boot:
        suffix += (
            " Only the generic V300 boot PID is present — DcamInitialize "
            "could not upload firmware to switch the device to a model PID. "
            "This SDK supports C11287-01/C11288-01/C11160/C11165/C11513 only; "
            "older modules (e.g. C10785, TM/TG series) need a different SDK."
        )
    elif "no driver" in msg.lower():
        # Windows sees the device at a supported model PID, but DCamUSB.dll
        # reports no driver — WinUsb .inf is not bound to the device.
        suffix += (
            " The device is enumerated but the Hamamatsu WinUsb driver is "
            "not bound to it. Install the Hamamatsu DCam-USB driver "
            "(WinUsbDCamIF.inf, shipped with the DCam-USB SDK) on this "
            "computer: open Device Manager, right-click the Hamamatsu "
            "entry -> 'Update driver' -> 'Browse my computer' -> point to "
            "the SDK's driver folder. After install the device should "
            "appear under 'Hamamatsu USB Camera Module'."
        )
    return msg + "." + suffix


class MiniSpec_Spectrometer(object):
    """
    Control class for Hamamatsu Mini-Spectrometer Micro Series via DCamUSB.dll
    (SingleDevice SDK variant). One device per process.
    """

    def __init__(self):
        self.spec_type = "MiniSpec"

        self.debug_mode = 0
        self.simulation_mode = False
        self.dll_path = ""

        self.sn = ""
        self.alias = "1"

        # Will be filled in at connect-time from the device itself.
        self.npix_active = 2068
        self.npix_blind_left = 0
        self.npix_blind_right = 0
        self.nbits = 16
        self.max_it_ms = 10000.0
        self.min_it_ms = 1.0
        self.discriminator_factor = 1.0
        self.eff_saturation_limit = 2 ** 16 - 1

        self.abort_on_saturation = True

        # Internal state
        self.dll_handler = None
        self.errors = dcamusb_errors
        self.is_open = False
        self.it_ms = None
        self.last_errcode = 0
        self.error = "OK"
        self.logger = logger

        self._capture_bytes = 0
        self._capture_buf = None     # ctypes buffer reused between captures
        self._image_width = 0
        self._image_height = 0

        self._meas_lock = threading.Lock()
        self._meas_done_event = threading.Event()
        self._meas_done_event.set()

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

    def get_error(self, ok_flag):
        """
        DCamUSB functions return BOOL. On FALSE call DcamGetLastError() for
        the actual code. Returns "OK" or a human-readable description.
        """
        if ok_flag:
            self.last_errcode = 0
            return "OK"
        try:
            code = int(self.dll_handler.DcamGetLastError())
        except Exception as exc:
            return "DcamGetLastError() raised: " + str(exc)
        self.last_errcode = code
        return self.errors.get(code, "DCamUSB error code " + str(code))

    # ------------------------------------------------------------------
    # DLL / device lifecycle
    # ------------------------------------------------------------------
    def load_spec_dll(self):
        if self.simulation_mode:
            return "OK"
        if not self.dll_path or not os.path.isfile(self.dll_path):
            return "DCamUSB.dll path not set or file missing: '" + str(self.dll_path) + "'"
        try:
            if hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(os.path.dirname(self.dll_path))
                except Exception:
                    pass
            self.dll_handler = windll.LoadLibrary(self.dll_path)
        except Exception as exc:
            return "Could not load DCamUSB.dll: " + str(exc)

        # Bind argtypes / restypes for the functions we use.
        h = self.dll_handler
        h.DcamInitialize.argtypes = []
        h.DcamInitialize.restype = c_int
        h.DcamUninitialize.argtypes = []
        h.DcamUninitialize.restype = c_int
        h.DcamOpen.argtypes = []
        h.DcamOpen.restype = c_int
        h.DcamClose.argtypes = []
        h.DcamClose.restype = c_int
        h.DcamGetDeviceState.argtypes = [POINTER(c_int)]
        h.DcamGetDeviceState.restype = c_int
        h.DcamGetImageSize.argtypes = [POINTER(c_int), POINTER(c_int)]
        h.DcamGetImageSize.restype = c_int
        h.DcamGetBitPerPixel.argtypes = [POINTER(c_int)]
        h.DcamGetBitPerPixel.restype = c_int
        h.DcamGetCaptureBytes.argtypes = [POINTER(c_int)]
        h.DcamGetCaptureBytes.restype = c_int
        h.DcamSetMeasureDataCount.argtypes = [c_int]
        h.DcamSetMeasureDataCount.restype = c_int
        h.DcamGetMeasureDataCount.argtypes = [POINTER(c_int)]
        h.DcamGetMeasureDataCount.restype = c_int
        h.DcamSetBinning.argtypes = [c_int]
        h.DcamSetBinning.restype = c_int
        h.DcamGetBinning.argtypes = [POINTER(c_int)]
        h.DcamGetBinning.restype = c_int
        h.DcamSetTriggerMode.argtypes = [c_int]
        h.DcamSetTriggerMode.restype = c_int
        h.DcamSetExposureTime.argtypes = [c_int]
        h.DcamSetExposureTime.restype = c_int
        h.DcamGetExposureTime.argtypes = [POINTER(c_int)]
        h.DcamGetExposureTime.restype = c_int
        h.DcamSetGain.argtypes = [c_int]
        h.DcamSetGain.restype = c_int
        h.DcamSetOffset.argtypes = [c_int]
        h.DcamSetOffset.restype = c_int
        h.DcamSetStandardTimeUnit.argtypes = [c_int]
        h.DcamSetStandardTimeUnit.restype = c_int
        h.DcamCapture.argtypes = [c_void_p, c_int]
        h.DcamCapture.restype = c_int
        h.DcamStop.argtypes = []
        h.DcamStop.restype = c_int
        h.DcamWait.argtypes = [POINTER(c_uint32), c_int]
        h.DcamWait.restype = c_int
        h.DcamGetDeviceInformation.argtypes = [c_int, c_void_p, c_int]
        h.DcamGetDeviceInformation.restype = c_int
        h.DcamDeviceIsAlive.argtypes = []
        h.DcamDeviceIsAlive.restype = c_int
        h.DcamGetLastError.argtypes = []
        h.DcamGetLastError.restype = c_uint32

        return "OK"

    def initialize_dll(self):
        """
        Call DcamInitialize() once globally. Idempotent across instances.
        Returns "OK" or a description.
        """
        global _dcamusb_initialized
        if self.simulation_mode:
            return "OK"
        with _dcamusb_initialized_lock:
            if _dcamusb_initialized:
                return "OK"
            ok = self.dll_handler.DcamInitialize()
            res = self.get_error(ok)
            if res == "OK":
                _dcamusb_initialized = True
            elif self.last_errcode == 3:
                # Already initialized — treat as OK
                _dcamusb_initialized = True
                res = "OK"
            return res

    def get_number_of_devices(self):
        """
        DCamUSB SingleDevice variant doesn't expose enumeration. Probe by
        opening the device; if it opens we have one, otherwise zero.
        Returns ("OK", count).
        """
        if self.simulation_mode:
            return "OK", 1

        # Quick check — if the SDK already sees a device but isn't connected,
        # state will be DCAM_DEVSTATE_DEVICE.
        state = c_int(0)
        ok = self.dll_handler.DcamGetDeviceState(byref(state))
        res = self.get_error(ok)
        if res != "OK":
            return _augment_minispec_error(res), 0

        if state.value in (
            DCAM_DEVSTATE_DEVICE,
            DCAM_DEVSTATE_CONNECT,
            DCAM_DEVSTATE_BOOT,
        ):
            return "OK", 1

        # Maybe a device is plugged in but the state cache is stale; try Open.
        ok = self.dll_handler.DcamOpen()
        if ok:
            # Close again so connect() can do its thing cleanly.
            self.dll_handler.DcamClose()
            return "OK", 1
        # Distinguish "no device" from real errors
        code = int(self.dll_handler.DcamGetLastError())
        self.last_errcode = code
        if code == 6:  # NotConnected
            return "OK", 0
        msg = self.errors.get(code, "DCamUSB error code " + str(code))
        return _augment_minispec_error(msg), 0

    def get_all_devices_info(self, ndev):
        """
        Returns ("OK", {0: {"id": serial, "model": devtype}}). Requires the
        device to be openable. We open, query, then close.
        """
        if self.simulation_mode:
            return "OK", {0: {"id": "SIM-MINISPEC", "model": "C11288-SIM"}}
        if ndev == 0:
            return "OK", {}

        ok = self.dll_handler.DcamOpen()
        res = self.get_error(ok)
        if res != "OK":
            return res, {}

        info = {}
        try:
            serial_buf = create_string_buffer(64)
            type_buf = create_string_buffer(64)
            self.dll_handler.DcamGetDeviceInformation(DCAM_DEVINF_SERIALNO, serial_buf, 64)
            self.dll_handler.DcamGetDeviceInformation(DCAM_DEVINF_TYPE, type_buf, 64)
            serial = serial_buf.value.decode("ascii", errors="ignore").strip()
            devtype = type_buf.value.decode("ascii", errors="ignore").strip()
            info[0] = {"id": serial or "MiniSpec-1", "model": devtype or "Hamamatsu Mini-Spectrometer"}
        finally:
            self.dll_handler.DcamClose()

        return "OK", info

    # ------------------------------------------------------------------
    # Connect / disconnect
    # ------------------------------------------------------------------
    def connect(self):
        if self.simulation_mode:
            self.logger.info("--- Connecting MiniSpec spectrometer (Simulation Mode) ---")
            self.is_open = True
            self.reset_spec_data()
            return "OK"

        self.logger.info("--- Connecting MiniSpec spectrometer " + str(self.alias) + " ---")

        if self.dll_handler is None:
            res = self.load_spec_dll()
            if res != "OK":
                self.error = res
                return res
            res = self.initialize_dll()
            if res != "OK":
                self.error = res
                return res

        ok = self.dll_handler.DcamOpen()
        res = self.get_error(ok)
        if res != "OK":
            self.error = res
            return res
        self.is_open = True

        # Standard mini-spec config: full-line binning, internal trigger,
        # 1 line per measurement, exposure in ms.
        try:
            self.dll_handler.DcamSetStandardTimeUnit(DCAM_TIME_UNIT_TYPE1)
        except Exception:
            pass
        try:
            self.dll_handler.DcamSetBinning(DCAM_BINNING_FULL)
        except Exception:
            pass
        try:
            self.dll_handler.DcamSetTriggerMode(DCAM_TRIGMODE_INT)
        except Exception:
            pass
        try:
            self.dll_handler.DcamSetMeasureDataCount(1)
        except Exception:
            pass

        # Discover actual image dimensions and bit depth from the device
        width = c_int(0)
        height = c_int(0)
        bits = c_int(0)
        cap_bytes = c_int(0)
        if self.dll_handler.DcamGetImageSize(byref(width), byref(height)):
            self._image_width = int(width.value)
            self._image_height = int(height.value)
        if self.dll_handler.DcamGetBitPerPixel(byref(bits)):
            self.nbits = int(bits.value) or self.nbits
        if self.dll_handler.DcamGetCaptureBytes(byref(cap_bytes)):
            self._capture_bytes = int(cap_bytes.value)

        if self._image_width > 0:
            self.npix_active = self._image_width
        if self._capture_bytes <= 0:
            # Fall back to width × height × 2 bytes (16-bit)
            self._capture_bytes = max(1, self._image_width) * max(1, self._image_height) * 2

        # Allocate a reusable capture buffer of uint16.
        n_words = max(1, self._capture_bytes // 2)
        self._capture_buf = (c_uint16 * n_words)()

        # Re-size data arrays to match real pixel count
        self.eff_saturation_limit = 2 ** self.nbits - 1
        self.reset_spec_data()

        # Pull the serial number off the device for display
        try:
            sn_buf = create_string_buffer(64)
            if self.dll_handler.DcamGetDeviceInformation(DCAM_DEVINF_SERIALNO, sn_buf, 64):
                detected_sn = sn_buf.value.decode("ascii", errors="ignore").strip()
                if detected_sn:
                    self.sn = detected_sn
        except Exception:
            pass

        # Set a safe initial integration time
        initial_it = max(self.min_it_ms, 10.0)
        res = self.set_it(initial_it)
        if res != "OK":
            self.error = res
            return res

        MiniSpec_Spectrometer_Instances[self.sn or self.alias] = self
        self.logger.info(
            "MiniSpec connected: sn=" + str(self.sn) +
            ", pixels=" + str(self.npix_active) +
            ", bits=" + str(self.nbits)
        )
        self.error = "OK"
        return "OK"

    def disconnect(self, *args, **kwargs):
        if self.simulation_mode:
            self.is_open = False
            return "OK"
        if not self.is_open or self.dll_handler is None:
            return "OK"
        try:
            self.dll_handler.DcamStop()
        except Exception:
            pass
        ok = self.dll_handler.DcamClose()
        res = self.get_error(ok)
        self.is_open = False
        key = self.sn or self.alias
        if key in MiniSpec_Spectrometer_Instances:
            del MiniSpec_Spectrometer_Instances[key]
        self.logger.info("MiniSpec disconnected (" + res + ").")
        return res

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------
    def set_it(self, it_ms):
        # Clamp to hardware-supported range
        if it_ms < self.min_it_ms:
            it_ms = self.min_it_ms
        if it_ms > self.max_it_ms:
            it_ms = self.max_it_ms

        if self.simulation_mode:
            self.it_ms = float(it_ms)
            return "OK"

        ok = self.dll_handler.DcamSetExposureTime(c_int(int(round(float(it_ms)))))
        res = self.get_error(ok)
        if res == "OK":
            self.it_ms = float(it_ms)
        else:
            self.error = res
        return res

    def measure(self, ncy=1):
        """
        Synchronous capture of *ncy* frames, averaged into self.rcm.
        Returns "OK" or an error string. The orchestrator follows up with
        wait_for_measurement(); we already block here, so wait_for_measurement
        returns immediately.
        """
        if not self._meas_lock.acquire(blocking=False):
            return "Measurement already in progress"

        self._meas_done_event.clear()
        self.error = "OK"
        self.ncy_requested = ncy
        self.ncy_read = 0
        self.ncy_handled = 0
        self.ncy_saturated = 0
        self.meas_start_time = spec_clock.now()

        try:
            if self.simulation_mode:
                # Generate a fake spectrum scaled by IT
                base = (self.it_ms or 1.0) * 100.0
                rng = np.random.default_rng()
                frames = rng.normal(loc=base, scale=base * 0.02, size=(ncy, self.npix_active))
                frames = np.clip(frames, 0, self.eff_saturation_limit).astype(np.float64)
                self.rcm = frames.mean(axis=0)
                self.rcs = frames.std(axis=0)
                self.rcl = frames.max(axis=0) - frames.min(axis=0)
                self.ncy_read = ncy
                self.ncy_handled = ncy
                return "OK"

            return self._capture_and_average(ncy)
        finally:
            self._meas_done_event.set()
            if self.external_meas_done_event is not None and not self.recovering:
                try:
                    self.external_meas_done_event.set()
                except Exception:
                    pass
            self._meas_lock.release()

    def _capture_and_average(self, ncy):
        if self.dll_handler is None or not self.is_open:
            return "MiniSpec not connected"

        n_words = len(self._capture_buf)
        accum = np.zeros(self.npix_active, dtype=np.float64)
        sq_accum = np.zeros(self.npix_active, dtype=np.float64)
        max_seen = np.zeros(self.npix_active, dtype=np.float64)

        # Timeout: max(1s, 5x exposure) per frame.
        per_frame_timeout_ms = int(max(1000.0, 5.0 * (self.it_ms or 100.0)))

        for i in range(int(ncy)):
            ok = self.dll_handler.DcamCapture(self._capture_buf, c_int(self._capture_bytes))
            res = self.get_error(ok)
            if res != "OK":
                return "DcamCapture failed (cycle " + str(i + 1) + "): " + res

            status = c_uint32(DCAM_WAITSTATUS_UNCOMPLETED)
            ok = self.dll_handler.DcamWait(byref(status), c_int(per_frame_timeout_ms))
            res = self.get_error(ok)
            if res != "OK":
                # Best-effort stop before bailing out
                try:
                    self.dll_handler.DcamStop()
                except Exception:
                    pass
                return "DcamWait failed (cycle " + str(i + 1) + "): " + res
            if status.value != DCAM_WAITSTATUS_COMPLETED:
                try:
                    self.dll_handler.DcamStop()
                except Exception:
                    pass
                return "DcamWait did not complete (status=" + str(status.value) + ")"

            # Copy buffer into numpy. Buffer is full image (width*height*2 bytes).
            raw = np.frombuffer(self._capture_buf, dtype=np.uint16, count=n_words)
            # First image_width samples are the spectrum (height should be 1
            # after full-line binning).
            frame = raw[: self.npix_active].astype(np.float64) * self.discriminator_factor

            accum += frame
            sq_accum += frame * frame
            max_seen = np.maximum(max_seen, frame)
            self.ncy_read += 1
            self.ncy_handled += 1

            if frame.max() >= self.eff_saturation_limit:
                self.ncy_saturated += 1
                if self.abort_on_saturation:
                    self.logger.info("Saturation detected at cycle " + str(i + 1) + "; aborting.")
                    break

        n = max(1, self.ncy_handled)
        mean = accum / n
        var = sq_accum / n - mean * mean
        var = np.clip(var, 0.0, None)
        self.rcm = mean
        self.rcs = np.sqrt(var)
        self.rcl = max_seen - mean

        return "OK"

    def wait_for_measurement(self):
        # measure() blocks already; this just returns the last status.
        self._meas_done_event.wait()
        return self.error

    def abort(self, ignore_errors=False):
        if self.simulation_mode:
            return "OK"
        if self.dll_handler is None or not self.is_open:
            return "OK"
        try:
            ok = self.dll_handler.DcamStop()
            if ok:
                return "OK"
            res = self.get_error(ok)
            return "OK" if ignore_errors else res
        except Exception as exc:
            return "OK" if ignore_errors else "DcamStop raised: " + str(exc)
