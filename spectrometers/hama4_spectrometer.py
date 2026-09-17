import logging
import ctypes
import os
import sys
import threading
import platform
import numpy as np
from time import sleep
from copy import deepcopy
from spec_xfus import spec_clock, calc_msl, split_cycles

if sys.version_info[0] < 3:
    # Python 2.x
    from Queue import Queue
else:
    # Python 3.x
    from queue import Queue

# Logger setup
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ctypes structures that mirror those expected by HiasApi.dll
# ---------------------------------------------------------------------------

class HiasOption(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("size", ctypes.c_size_t),
        ("option", ctypes.c_char * 32),
        ("parameter", ctypes.c_void_p),
    ]

class HiasDeviceInfo(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("size", ctypes.c_size_t),
        ("device_id", ctypes.c_int32),
        ("device_name", ctypes.c_char * 64),
        ("serial_number", ctypes.c_char * 64),
        ("device_version", ctypes.c_char * 64),
        ("device_category", ctypes.c_char * 64),
        ("device_status", ctypes.c_int32),
        ("interface_type", ctypes.c_int32),
    ]

class HiasBufferGetPtrParameter(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("size", ctypes.c_size_t),
        ("select", ctypes.c_int32),
        ("frame_index", ctypes.c_int32),
    ]

class HiasBufferFormat_XYData(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("size", ctypes.c_size_t),
        ("buffer_type", ctypes.c_int32),
        ("x_type", ctypes.c_int32),
        ("number_of_x", ctypes.c_int32),
        ("buffer_x", ctypes.c_void_p),
        ("y_type", ctypes.c_int32),
        ("number_of_y", ctypes.c_int32),
        ("buffer_y", ctypes.c_void_p),
    ]

# Map of data-type ids returned by HiasApi to ctypes types
_HIAS_DATA_TYPES = {
    2: ctypes.c_uint16,
    5: ctypes.c_int32,
    8: ctypes.c_float,
    9: ctypes.c_double,
}

# ---------------------------------------------------------------------------
# HiasApi error codes (return values from many Hias* functions)
# From HiasApi.h - codes 0x0..0x0A are success / non-fatal warnings;
# codes >= 0x80000000 are real errors.
# ---------------------------------------------------------------------------
_HIAS_WARNING_THRESHOLD = 0x80000000  # codes below this are OK / warnings

hama4_errors = {
    0x00000000: "OK",                                        # HiasResult_Success
    0x00000001: "OK",                                        # HiasResult_NoNeedToCall
    0x00000002: "OK",                                        # HiasResult_FailedToSomeLoadLibraries (partial, non-fatal)
    0x00000003: "OK",                                        # HiasResult_FailedToSomeInterfacesInitialize (partial, non-fatal)
    0x00000004: "OK",                                        # HiasResult_FailedToSomeInterfacesDeviceFind (partial, non-fatal)
    0x00000005: "OK",                                        # HiasResult_NoFoundDevice
    0x00000006: "OK",                                        # HiasResult_RoundedValueWasSet
    0x00000007: "OK",                                        # HiasResult_PendingToStop
    0x00000008: "OK",                                        # HiasResult_PendingCompletion
    0x00000009: "OK",                                        # HiasResult_OldVersionRuntimeUsed
    0x0000000A: "OK",                                        # HiasResult_WaitAborted
    0x80000000: "Unknown error",                             # HiasResult_Unknown
    0x80000001: "Not implemented",                           # HiasResult_NotImplemented
    0x80000002: "Not supported",                             # HiasResult_NotSupported
    0x80000003: "Requested data not found",                  # HiasResult_NoFoundData
    0x80000004: "Configuration file not found",              # HiasResult_NoFoundConfiguration
    0x80000005: "Invalid configuration file",                # HiasResult_InvalidConfiguration
    0x80000006: "Failed to create log file",                 # HiasResult_FailedToCreateLogFile
    0x80000007: "Required DLL not found",                    # HiasResult_NoFoundLibraries
    0x80000008: "Version mismatch between DLLs",             # HiasResult_MismatchVersionBetweenLibraries
    0x80000009: "Failed to load required DLL",               # HiasResult_FailedToLoadLibrary
    0x8000000A: "Not initialized",                           # HiasResult_NotInitialized
    0x8000000B: "Already initialized",                       # HiasResult_AlreadyInitialized
    0x8000000C: "Failed to initialize all interfaces",       # HiasResult_FailedToInterfaceInitialize
    0x8000000D: "Failed to search devices on all interfaces",# HiasResult_FailedToDeviceFind
    0x8000000E: "Failed to open device",                     # HiasResult_FailedToOpen
    0x8000000F: "Failed to initialize device",               # HiasResult_FailedToDeviceInitialized
    0x80000010: "Device firmware version mismatch",          # HiasResult_MismatchDeviceFirmware
    0x80000011: "Device already opened",                     # HiasResult_AlreadyOpened
    0x80000012: "Memory not allocated",                      # HiasResult_NotAllocatedMemory
    0x80000013: "Memory already allocated",                  # HiasResult_AlreadyAllocatedMemory
    0x80000014: "Cannot operate while acquiring",            # HiasResult_FailedToOperationBecauseAcquiring
    0x80000015: "Need to call AcquisitionStop",              # HiasResult_NeedToCallAcquisitionStop
    0x80000016: "Cannot register more callbacks",            # HiasResult_CannotRegisterCallbackAnymore
    0x80000017: "Same callback already registered",          # HiasResult_SameCallbackRegistered
    0x80000018: "Callback not found",                        # HiasResult_NoFoundCallback
    0x80000019: "Invalid handle",                            # HiasResult_InvalidHandle
    0x8000001A: "Structure size mismatch",                   # HiasResult_MismatchStructureSize
    0x8000001B: "Structure type mismatch",                   # HiasResult_MismatchStructureType
    0x8000001C: "Invalid parameter",                         # HiasResult_InvalidParameter
    0x8000001D: "Feature not found",                         # HiasResult_NoFoundFeature
    0x8000001E: "Function mismatch",                         # HiasResult_MismatchFunction
    0x8000001F: "Value out of range",                        # HiasResult_ValueOutOfRange
    0x80000020: "Index out of range",                        # HiasResult_IndexOutOfRange
    0x80000021: "Timeout",                                   # HiasResult_Timeout
    0x80000022: "Data corrupted",                            # HiasResult_DataCorrupted
    0x80000023: "Device malfunction",                        # HiasResult_DeviceMalfunction
    0x80000024: "Not enough buffer",                         # HiasResult_NotEnoughBuffer
    0x80000025: "Core/AddOn version mismatch",               # HiasResult_RequiredToUpdateCoreVersion
}

# ---------------------------------------------------------------------------
# Global bookkeeping (matches hama2/hama3 patterns)
# ---------------------------------------------------------------------------
Hama4_Spectrometer_Instances = {}
Hama4_devs_info = {}  # {dev_index: {"sn": ..., "name": ..., ...}}

# Track whether the HiasApi dll was already initialised globally
_hias_dll_initialized = False


class Hama4_Spectrometer(object):
    """
    Control class for Hamamatsu mini-spectrometers that use the HiasApi.dll
    (e.g. C16449MA).

    Public API is same as Hama2 / Hama3
    """

    def __init__(self):
        self.spec_type = "Hama4"

        # Note: (E = ext) (I = int)

        # ---Parameters---
        self.debug_mode = 0            # (E) 0=disabled, >0=enabled
        self.simulation_mode = False   # (E) simulation mode flag
        self.dll_path = ""             # (E) full path to HiasApi.dll

        # Spec identity
        self.sn = ""                   # (E) serial number to connect to
        self.alias = "1"               # (E) spectrometer alias ("1", "2", ...)

        # Detector / pixel configuration
        self.npix_active = 256         # (E) number of active pixels (C16449MA => 256; override via IOF)
        self.npix_blind_left = 0       # (E) blind pixels left
        self.npix_blind_right = 0      # (E) blind pixels right
        self.nbits = 16                # (E) AD converter bits
        self.max_it_ms = 10000.0       # (E) max integration time [ms]
        self.min_it_ms = 0.011         # (E) min integration time [ms]  (= 11 us)
        self.discriminator_factor = 1.0  # (E) multiplicative factor on counts
        self.eff_saturation_limit = 2**16 - 1  # (E) saturation threshold [counts]
        self.cycle_timeout_ms = 4000   # (I) per-cycle timeout [ms]

        # Working mode
        self.abort_on_saturation = True   # (E)
        self.max_ncy_per_meas_default = 100  # (E)
        self.max_ncy_per_meas = self.max_ncy_per_meas_default  # (I)
        self.max_it_ms_for_meas_pack = 1000  # (I) above this IT only 1 cy per pack

        # Performance tests
        self.performance_test_it_ms_list = np.arange(0.05, 10.1, 1.0)  # (I) [ms]
        self.performance_test_ncy_list = [1, 10, 30, 50, 70, 90, 110, 130, 150]

        # Simulation mode
        self.simulated_rc_min = int(0.2 * (2**self.nbits - 1))
        self.simulated_rc_max = int(0.5 * (2**self.nbits - 1))
        self.simudur = 0.1

        # -------- Internal variables (do not modify) --------
        self.dll_handler = None      # (E) WinDLL handle
        self.h_device = None         # (I) HiasApi device handle (c_uint64)
        self.spec_id = None          # (E) logical id  (= serial number string or index)
        self.parlist = None          # (E) parameter list placeholder
        self.it_ms = None            # (E) currently set integration time [ms]
        self.it_us = None            # (I) currently set integration time [us] (HiasApi unit)
        self.logger = None           # (E)

        # Measure control
        self.measuring = False       # (E)
        self.recovering = False      # (E)
        self.docatch = False         # (E)
        self.is_streaming = False    # (I) HiasApi stream state
        self.ncy_requested = 0       # (E)
        self.ncy_per_meas = [1]      # (I)
        self.ncy_read = 0            # (E)
        self.ncy_handled = 0         # (E)
        self.ncy_saturated = 0       # (E)
        self.internal_meas_done_event = threading.Event()

        # Data output
        self.rcm = np.array([])
        self.rcs = np.array([])
        self.rcl = np.array([])
        self.sy  = np.zeros(self.npix_active, dtype=np.float64)
        self.syy = np.zeros(self.npix_active, dtype=np.float64)
        self.sxy = np.zeros(self.npix_active, dtype=np.float64)
        self.arrival_times = []
        self.meas_start_time = 0
        self.meas_end_time = 0
        self.data_handling_end_time = 0

        # External event
        self.external_meas_done_event = None  # (E)

        # Queues & threads
        self.read_data_queue = Queue()
        self.handle_data_queue = Queue()
        self.data_arrival_watchdog_thread = None
        self.data_handling_watchdog_thread = None

        # Error handling
        self.errors = hama4_errors
        self.error = "OK"
        self.last_errcode = 0

    # ===================================================================
    # Main Control Functions
    # ===================================================================

    def initialize_spec_logger(self):
        """Create a dedicated logger for this spectrometer instance."""
        self.logger = logging.getLogger("spec" + self.alias)

    def connect(self):
        """
        Connect to the spectrometer and initialise it.
        Returns "OK" or an error description string.
        """
        global _hias_dll_initialized

        res = "OK"
        ndev = 0

        # Reset data
        self.reset_spec_data()

        if self.simulation_mode:
            self.logger.info("--- Connecting spectrometer " + self.alias + "... (Simulation Mode ON) ---")
            self.spec_id = len(Hama4_Spectrometer_Instances) + 1
            Hama4_Spectrometer_Instances[self.spec_id] = self
        else:
            self.logger.info("--- Connecting " + str(self.spec_type) + " spectrometer " + self.alias + "... ---")

            # Load the HiasApi dll
            res = self.load_spec_dll()

            # Initialise the dll (HiasInitialize)
            if res == "OK":
                res = self.initialize_dll()

            # Get number of connected devices
            if res == "OK":
                res, ndev = self.get_number_of_devices()

            # Get all device info (serial numbers, names)
            if res == "OK":
                res, devs_info = self.get_all_devices_info(ndev)

            # Find the device with matching serial number
            if res == "OK":
                res, dev_id, _ = self.find_spec_info(devs_info)

            # Open device
            if res == "OK":
                res = self._open_device(dev_id)

            # Register instance
            if res == "OK":
                self.spec_id = self.sn
                Hama4_Spectrometer_Instances[self.spec_id] = self

            # Configure acquisition mode
            if res == "OK":
                res = self._configure_device()

            # Allocate buffer
            if res == "OK":
                res = self._alloc_buffer()

            # Determine number of pixels
            if res == "OK":
                res = self._detect_npix()

            # Query device exposure time limits
            if res == "OK":
                self._query_exposure_limits()

        # Set initial integration time
        if res == "OK":
            for it in [self.min_it_ms * 2, self.min_it_ms]:
                res = self.set_it(it)
                if res != "OK":
                    break
            sleep(0.2)

        # Start watchdog threads
        if res == "OK":
            if self.data_arrival_watchdog_thread is None:
                self.logger.info("Starting data arrival watchdog thread...")
                self.data_arrival_watchdog_thread = threading.Thread(target=self.data_arrival_watchdog)
                self.data_arrival_watchdog_thread.start()
            if self.data_handling_watchdog_thread is None:
                self.logger.info("Starting data handling watchdog thread...")
                self.data_handling_watchdog_thread = threading.Thread(target=self.data_handling_watchdog)
                self.data_handling_watchdog_thread.start()
            self.logger.info("Spectrometer connected.")

        self.error = res
        return res

    def set_it(self, it_ms):
        """
        Set the integration (exposure) time in milliseconds.
        HiasApi expects exposure time in **microseconds** via HiasFeatureSetNumber.
        """
        res = "OK"

        # Clamp to device-supported range
        if it_ms < self.min_it_ms:
            self.logger.warning("set_it: requested IT " + str(it_ms) + " ms is below minimum " +
                                str(self.min_it_ms) + " ms. Clamping to minimum.")
            it_ms = self.min_it_ms
        if it_ms > self.max_it_ms:
            self.logger.warning("set_it: requested IT " + str(it_ms) + " ms is above maximum " +
                                str(self.max_it_ms) + " ms. Clamping to maximum.")
            it_ms = self.max_it_ms

        it_us = float(it_ms) * 1000.0  # ms -> us

        if self.simulation_mode:
            if self.debug_mode >= 2:
                self.logger.debug("Setting integration time to " + str(it_ms) + " ms (Simulation mode)")
            self.it_ms = it_ms
            self.it_us = it_us
        else:
            if self.h_device is None or (hasattr(self.h_device, 'value') and self.h_device.value == 0):
                res = "Cannot set integration time: device handle is not valid. Is the spectrometer connected?"
                self.logger.error(res)
                self.error = res
                return res

            # HiasApi may reject parameter changes while streaming — stop first
            if self.is_streaming:
                self.logger.info("set_it: stopping stream before changing exposure time.")
                self._stop_stream()
                sleep(0.2)

            if self.debug_mode >= 2:
                self.logger.debug("Setting integration time to " + str(it_ms) + " ms (" + str(it_us) + " us)")

            # Retry up to 3 times with increasing delay for transient device errors
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    rc = self.dll_handler.HiasFeatureSetNumber(
                        self.h_device,
                        b"ExposureTime",
                        ctypes.c_double(it_us),
                    )
                    res = self.get_error(rc)
                    if res == "OK":
                        self.it_ms = it_ms
                        self.it_us = it_us
                        break
                    else:
                        if attempt < max_retries - 1:
                            delay = 0.3 * (attempt + 1)
                            self.logger.warning("set_it: attempt " + str(attempt + 1) + "/" + str(max_retries) +
                                                " failed (" + res + "), retrying in " + str(delay) + "s...")
                            sleep(delay)
                        else:
                            res = "Could not set integration time to " + str(it_ms) + " ms. Error: " + res
                except Exception as e:
                    res = "Exception setting integration time: " + str(e)
                    self.logger.exception(e)
                    break

        self.error = res
        return res

    def measure(self, ncy=1):
        """
        Request *ncy* measurement cycles (non-blocking).
        The actual work is done by the data_arrival_watchdog / measure_blocking.
        """
        self.measuring = True
        self.internal_meas_done_event.clear()
        self.docatch = True
        self.error = "OK"

        self.read_data_queue.put(ncy)

        return self.error

    def abort(self, ignore_errors=False):
        """Stop any ongoing measurement / streaming."""
        self.docatch = False
        res = "OK"
        if self.simulation_mode:
            if self.debug_mode > 1:
                self.logger.info("abort, stopping any ongoing measurement (simulation mode)...")
        else:
            if self.debug_mode > 0:
                self.logger.info("abort, stopping any ongoing measurement...")
            try:
                self._stop_stream()
            except Exception as e:
                res = "Exception stopping measurement: " + str(e)
                if ignore_errors:
                    self.logger.warning("abort, " + res)
                    res = "OK"
                else:
                    self.logger.exception(e)
        self.error = res
        return res

    def read_aux_sensor(self, sname="detector"):
        """
        Read an auxiliary sensor (temperature, etc.).
        HiasApi does not expose temperature on all models.
        """
        res = "OK"
        if self.simulation_mode:
            return res, 11.1

        value = -99.0
        if sname == "detector":
            # Try reading a temperature feature if available
            try:
                val_c = ctypes.c_double()
                rc = self.dll_handler.HiasFeatureGetNumber(
                    self.h_device,
                    b"SensorTemperature",
                    ctypes.byref(val_c),
                )
                res = self.get_error(rc)
                if res == "OK":
                    value = float(val_c.value)
                    if self.debug_mode >= 2:
                        self.logger.debug("read_aux_sensor, " + sname + " temperature: " + str(value) + " degC")
                else:
                    self.logger.warning("read_aux_sensor, could not read '" + sname + "' temperature, err= " + res)
            except Exception as e:
                res = "Exception reading aux sensor: " + str(e)
                self.logger.warning(res)
        else:
            res = "Unknown sensor name: '" + sname + "' for spec " + self.alias
            self.logger.error(res)

        return res, value

    def disconnect(self, dofree=False, ignore_errors=False):
        """
        Disconnect the spectrometer and optionally finalise the dll.
        """
        res = "OK"

        if self.spec_id in Hama4_Spectrometer_Instances:
            del Hama4_Spectrometer_Instances[self.spec_id]

        if self.simulation_mode:
            self.logger.info("Disconnecting spectrometer " + self.alias + "... (Simulation mode)")
        else:
            if self.h_device is not None and self.h_device.value != 0:
                self.logger.info("Disconnecting spectrometer " + self.alias + "...")
                self._stop_stream()
                res = self._close_device(ignore_errors=ignore_errors)
                if dofree:
                    res2 = self._finalize_dll(ignore_errors=ignore_errors)
                    if res == "OK":
                        res = res2
            else:
                self.logger.info("Skipping disconnection: Spectrometer " + self.alias + " is already disconnected.")

        # Shut down watchdog threads
        if self.data_arrival_watchdog_thread is not None:
            self.logger.info("Closing data arrival watchdog thread of spectrometer " + self.alias + ".")
            self.read_data_queue.put(None)
            self.data_arrival_watchdog_thread.join()
            self.data_arrival_watchdog_thread = None
        if self.data_handling_watchdog_thread is not None:
            self.logger.info("Closing data handling watchdog thread of spectrometer " + self.alias + ".")
            self.handle_data_queue.put((None, (None, None, None)))
            self.data_handling_watchdog_thread.join()
            self.data_handling_watchdog_thread = None
            self.logger.info("Side watchdog threads of spectrometer " + self.alias + " closed.")

        self.error = res
        return res

    def recovery(self, ntry=3, dofree=False):
        """
        Soft recovery: try to regain control of the spectrometer.
        """
        self.docatch = False
        res = "NOK"

        for i in range(ntry):
            self.logger.warning("Recovering spectrometer " + self.alias + "... (try " + str(i + 1) + "/" + str(ntry) + ")")

            res = self.abort(ignore_errors=False)
            sleep(0.5)

            if res == "OK":
                self.logger.info("Spectrometer " + self.alias + " could abort any ongoing measurement.")
                res = self.set_it(it_ms=self.it_ms)
                sleep(0.5)

            if res == "OK":
                self.logger.info("Spectrometer " + self.alias + " recovered after aborting and re-setting IT.")
                break

            if res != "OK":
                _ = self.disconnect(dofree=True)
                sleep(2)
                res = self.connect()
                if res == "OK":
                    self.logger.info("Spectrometer " + self.alias + " recovered after disconnect + reconnect.")
                    break
                else:
                    self.logger.warning("Recovery of spectrometer " + self.alias + " failed.")
                    sleep(3)

        return res

    # ===================================================================
    # Main control (used internally by this library)
    # ===================================================================

    def measure_blocking(self, ncy=10):
        """
        Blocking measurement of *ncy* cycles.
        Cycles are acquired one-by-one or in packs (via max_ncy_per_meas).
        The stream runs continuously across all packs to avoid gaps.
        """
        self.measuring = True

        _ = self.abort(ignore_errors=True)
        self.internal_meas_done_event.clear()
        self.docatch = True
        self.ncy_requested = ncy
        self.reset_spec_data()

        # Adjust pack size depending on IT
        if self.it_ms is not None and self.it_ms < self.max_it_ms_for_meas_pack:
            self.max_ncy_per_meas = self.max_ncy_per_meas_default
        else:
            self.max_ncy_per_meas = 1

        self.ncy_per_meas, packs_info = split_cycles(self.max_ncy_per_meas, ncy)

        if self.debug_mode > 0:
            self.logger.info("Starting measurement, ncy=" + str(ncy) + ", IT=" + str(self.it_ms) + " ms, npacks=" + packs_info)

        res = "OK"

        # For online mode: flush buffer, start stream once, discard first stale frame
        if not self.simulation_mode:
            self._flush_buffer()
            res = self._start_stream()
            if res != "OK":
                self.measuring = False
                self.error = res
                if self.external_meas_done_event is not None and not self.recovering:
                    self.external_meas_done_event.set()
                return res
            # Discard the first frame (may contain stale/partial exposure data)
            self._discard_first_frame()

        self.meas_start_time = spec_clock.now()

        for pack_idx, ncy_pack in enumerate(self.ncy_per_meas):
            if not self.docatch:
                res = "Measurement Aborted"
                self.logger.info(res)
                break

            if self.debug_mode > 2:
                self.logger.debug("Measuring pack " + str(pack_idx + 1) + "/" + str(len(self.ncy_per_meas)) +
                                  ", ncy_pack=" + str(ncy_pack))

            res = self.measure_pack(ncy_pack)
            if res != "OK":
                res = "Could not measure pack " + str(pack_idx + 1) + "/" + str(len(self.ncy_per_meas)) + \
                      ", ncy_pack=" + str(ncy_pack) + ", IT=" + str(self.it_ms) + "ms. " + res
                self.logger.warning(res)
                break
            elif self.internal_meas_done_event.is_set():
                break

        # Stop stream once after all packs
        if not self.simulation_mode:
            self._stop_stream()

        self.measuring = False
        self.error = res

        if self.external_meas_done_event is not None and not self.recovering:
            self.external_meas_done_event.set()

        return res

    def measure_pack(self, ncy_pack=1):
        """
        Measure a pack of *ncy_pack* cycles.
        For HiasApi the stream is already running (managed by measure_blocking);
        this method simply grabs *ncy_pack* frames from the continuous stream.
        """
        if self.simulation_mode:
            simulated_duration = ncy_pack * (self.it_ms / 1000.0) * self.simudur
            sleep(simulated_duration)
            rc = np.random.randint(
                int(self.simulated_rc_min / self.discriminator_factor),
                int(self.simulated_rc_max / self.discriminator_factor),
                (ncy_pack, self.npix_active),
            )
            self.arrival_times.append(spec_clock.now())
            for i in range(ncy_pack):
                self.ncy_read += 1
                issat = self.handle_cycle_data(self.ncy_read, rc[i, :], [], [])
                if issat and self.abort_on_saturation:
                    self.logger.info("Measurement aborted due to saturation in cycle " +
                                     str(self.ncy_read) + "/" + str(self.ncy_requested))
                    self.measurement_done()
                    break
                else:
                    if self.ncy_handled == self.ncy_requested:
                        self.measurement_done()
                        break
            return "OK"

        # --- Online mode (stream is already running) ---
        for i in range(ncy_pack):
            if not self.docatch:
                self.measurement_done()
                return "OK"

            # Wait for a frame
            x_data, y_data = self._wait_and_get_frame()
            arrival_time = spec_clock.now()

            if y_data is None:
                return "Timeout or error while waiting for frame data."

            # Trim / adjust to npix_active
            if len(y_data) > self.npix_active:
                y_data = y_data[:self.npix_active]
            elif len(y_data) < self.npix_active:
                return "Received " + str(len(y_data)) + " pixels, expected " + str(self.npix_active)

            self.arrival_times.append(arrival_time)
            self.ncy_read += 1

            if self.debug_mode > 2:
                self.logger.debug("Data arrived for cycle " + str(self.ncy_read))

            issat = self.handle_cycle_data(self.ncy_read, y_data.astype(np.float64), [], [])
            if issat and self.abort_on_saturation:
                self.logger.info("Measurement aborted due to saturation in cycle " +
                                 str(self.ncy_read) + "/" + str(self.ncy_requested))
                self.measurement_done()
                break
            else:
                if self.ncy_handled == self.ncy_requested:
                    self.measurement_done()
                    break

        return "OK"

    def wait_for_measurement(self):
        """Block until the current measurement is fully handled."""
        self.internal_meas_done_event.wait()
        return self.error

    # ===================================================================
    # Auxiliary functions - DLL / device management
    # ===================================================================

    def get_error(self, resdll):
        """
        Interpret a HiasApi return code.
        Codes 0x0..0x7FFFFFFF are success / non-fatal warnings => "OK".
        Codes >= 0x80000000 are real errors.
        The DLL returns c_int32 which Python may represent as negative;
        convert to unsigned 32-bit first.
        """
        if hasattr(resdll, 'value'):
            resdll = resdll.value
        # Convert signed 32-bit to unsigned so our lookup table works
        if isinstance(resdll, int) and resdll < 0:
            resdll = resdll & 0xFFFFFFFF

        self.last_errcode = resdll
        if isinstance(resdll, int) and resdll < _HIAS_WARNING_THRESHOLD:
            if resdll != 0 and self.logger:
                self.logger.info("HiasApi non-fatal warning code " + hex(resdll) +
                                 " (" + self.errors.get(resdll, "unknown") + ")")
            return "OK"
        if resdll in self.errors:
            return self.errors[resdll]
        return "HiasApi error code " + hex(resdll) + " (decimal: " + str(resdll) + ")"

    def load_spec_dll(self):
        """Load HiasApi.dll and define function signatures."""
        res = "OK"
        self.logger.info("Loading dll: " + self.dll_path)

        if not os.path.exists(self.dll_path):
            res = "The dll file does not exist: " + self.dll_path
            self.logger.error(res)
            return res

        try:
            dll_dir = os.path.dirname(self.dll_path)
            # Python >=3.8 requires add_dll_directory for search paths
            if hasattr(os, 'add_dll_directory'):
                os.add_dll_directory(dll_dir)
            self.dll_handler = ctypes.WinDLL(self.dll_path)
            self._define_dll_signatures()
            if self.debug_mode > 0:
                self.logger.debug("dll_handler: " + str(self.dll_handler))
        except Exception as e:
            res = "Exception loading dll: " + str(e)
            self.logger.exception(e)

        self.error = res
        return res

    def _define_dll_signatures(self):
        """
        Set argtypes / restype for the HiasApi functions we use.
        This is important for 64-bit correctness and prevents ctypes
        from defaulting to c_int.
        """
        h = self.dll_handler
        # HiasInitialize
        h.HiasInitialize.argtypes = [ctypes.POINTER(HiasOption)]
        h.HiasInitialize.restype = ctypes.c_int32

        # HiasFinalize
        h.HiasFinalize.argtypes = [ctypes.POINTER(HiasOption)]
        h.HiasFinalize.restype = ctypes.c_int32

        # HiasDeviceGetList
        h.HiasDeviceGetList.argtypes = [
            ctypes.POINTER(ctypes.POINTER(HiasDeviceInfo)),
            ctypes.POINTER(ctypes.c_int32),
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(HiasOption),
        ]
        h.HiasDeviceGetList.restype = ctypes.c_int32

        # HiasDeviceOpen / Close
        h.HiasDeviceOpen.argtypes = [ctypes.c_int32, ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(HiasOption)]
        h.HiasDeviceOpen.restype = ctypes.c_int32
        h.HiasDeviceClose.argtypes = [ctypes.c_uint64, ctypes.POINTER(HiasOption)]
        h.HiasDeviceClose.restype = ctypes.c_int32

        # Feature set / get
        h.HiasFeatureSetString.argtypes = [ctypes.c_uint64, ctypes.c_char_p, ctypes.c_char_p]
        h.HiasFeatureSetString.restype = ctypes.c_int32
        h.HiasFeatureSetNumber.argtypes = [ctypes.c_uint64, ctypes.c_char_p, ctypes.c_double]
        h.HiasFeatureSetNumber.restype = ctypes.c_int32
        h.HiasFeatureGetNumber.argtypes = [ctypes.c_uint64, ctypes.c_char_p, ctypes.POINTER(ctypes.c_double)]
        h.HiasFeatureGetNumber.restype = ctypes.c_int32

        # Buffer alloc / free / getptr
        h.HiasBufferAlloc.argtypes = [ctypes.c_uint64, ctypes.c_int32, ctypes.POINTER(ctypes.c_int32)]
        h.HiasBufferAlloc.restype = ctypes.c_int32
        h.HiasBufferFree.argtypes = [ctypes.c_uint64]
        h.HiasBufferFree.restype = ctypes.c_int32
        h.HiasBufferGetPtr.argtypes = [
            ctypes.c_uint64,
            ctypes.POINTER(HiasBufferGetPtrParameter),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        h.HiasBufferGetPtr.restype = ctypes.c_int32

        # Stream start / stop / wait
        h.HiasStreamAcquisitionStart.argtypes = [ctypes.c_uint64]
        h.HiasStreamAcquisitionStart.restype = ctypes.c_int32
        h.HiasStreamAcquisitionStop.argtypes = [ctypes.c_uint64]
        h.HiasStreamAcquisitionStop.restype = ctypes.c_int32
        h.HiasStreamWait.argtypes = [ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32, ctypes.c_int32]
        h.HiasStreamWait.restype = ctypes.c_int32

    def initialize_dll(self):
        """Call HiasInitialize.

        HiasApi.dll looks for ``hias.conf`` relative to the current
        working directory.  We therefore temporarily ``chdir`` into the
        directory that contains the DLL (which should also contain
        ``hias.conf``) before calling ``HiasInitialize``.
        """
        global _hias_dll_initialized
        res = "OK"
        self.logger.info("Initialising HiasApi dll...")
        if _hias_dll_initialized:
            self.logger.info("HiasApi dll already initialised.")
            return res

        # Determine the conf directory.  hias.conf is expected either
        # next to the DLL or in a sibling "conf" folder (Hiasphere SDK
        # layout: <root>/bin/HiasApi.dll  +  <root>/conf/hias.conf).
        dll_dir = os.path.dirname(os.path.abspath(self.dll_path))
        conf_beside_dll = os.path.join(dll_dir, "hias.conf")
        conf_in_sibling = os.path.join(os.path.dirname(dll_dir), "conf", "hias.conf")

        if os.path.isfile(conf_beside_dll):
            conf_dir = dll_dir
        elif os.path.isfile(conf_in_sibling):
            conf_dir = os.path.join(os.path.dirname(dll_dir), "conf")
        else:
            conf_dir = dll_dir  # fallback
            self.logger.warning(
                "hias.conf not found next to DLL (" + dll_dir +
                ") or in sibling conf dir.  HiasInitialize will likely fail.")

        saved_cwd = os.getcwd()
        try:
            os.chdir(conf_dir)
            self.logger.info("Changed CWD to '" + conf_dir + "' for HiasInitialize")
            opt = HiasOption(size=ctypes.sizeof(HiasOption))
            rc = self.dll_handler.HiasInitialize(ctypes.byref(opt))
            res = self.get_error(rc)
            if res == "OK":
                _hias_dll_initialized = True
                self.logger.info("HiasApi dll initialised successfully.")
            else:
                res = "Could not initialise HiasApi dll: " + res
                self.logger.error(res)
        except Exception as e:
            res = "Exception initialising dll: " + str(e)
            self.logger.exception(e)
        finally:
            os.chdir(saved_cwd)

        self.error = res
        return res

    def get_number_of_devices(self):
        """Return (res, ndev) - number of connected devices."""
        res = "OK"
        ndev = 0
        if self.simulation_mode:
            self.logger.info("get_number_of_devices, simulating a connected device.")
            return res, 1

        try:
            info_ptr = ctypes.POINTER(HiasDeviceInfo)()
            num = ctypes.c_int32(0)
            opt = HiasOption(size=ctypes.sizeof(HiasOption))
            rc = self.dll_handler.HiasDeviceGetList(
                ctypes.byref(info_ptr),
                ctypes.byref(num),
                0xFFFFFFFF,
                0xFFFFFFFF,
                ctypes.byref(opt),
            )
            res = self.get_error(rc)
            if res != "OK":
                res = "Could not get device list: " + res
                self.logger.error(res)
            else:
                ndev = num.value
                if ndev == 0:
                    res = "No " + self.spec_type + " spectrometers detected."
                    self.logger.error(res)
                else:
                    self.logger.info("Found " + str(ndev) + " " + self.spec_type + " device(s).")
                    # Cache the info pointer for get_all_devices_info
                    self._cached_info_ptr = info_ptr
        except Exception as e:
            res = "Exception getting number of devices: " + str(e)
            self.logger.exception(e)

        self.error = res
        return res, ndev

    def get_all_devices_info(self, ndev):
        """
        Return (res, devs_info) with a dict of device info keyed by index.
        """
        res = "OK"
        devs_info = {}

        if self.simulation_mode:
            devs_info[0] = {"id": self.sn, "name": "Simulated " + self.spec_type}
            return res, devs_info

        info_ptr = getattr(self, '_cached_info_ptr', None)
        if info_ptr is None:
            # Re-query
            res2, ndev2 = self.get_number_of_devices()
            if res2 != "OK":
                return res2, devs_info
            info_ptr = getattr(self, '_cached_info_ptr', None)
            if info_ptr is None:
                return "No device info available.", devs_info

        for i in range(ndev):
            dev = info_ptr[i]
            sn = dev.serial_number
            name = dev.device_name
            # Python 2/3 compat: decode bytes if needed
            if isinstance(sn, bytes):
                sn = sn.decode('utf-8', errors='replace')
            if isinstance(name, bytes):
                name = name.decode('utf-8', errors='replace')
            devs_info[i] = {
                "id": sn.strip(),
                "name": name.strip(),
                "device_id": dev.device_id,
            }
            self.logger.info("Device " + str(i) + ": SN=" + str(devs_info[i]["id"]) +
                             ", name=" + str(devs_info[i]["name"]))

        return res, devs_info

    def find_spec_info(self, devs_info):
        """Find the device index whose serial number matches self.sn."""
        spec_id = None
        spec_info = {}
        res = "OK"
        for dev_num in devs_info:
            dev_sn = str(devs_info[dev_num].get("id","")).strip("\x00").strip()
            if dev_sn == self.sn:
                spec_id = devs_info[dev_num]["device_id"]
                spec_info = devs_info[dev_num]
                self.logger.info("Found " + self.spec_type + " spectrometer with SN " + self.sn)
                break
        else:
            sns_found = [str(devs_info[k].get("id","")).strip("\x00").strip() for k in devs_info]
            res = "No " + self.spec_type + " spectrometer with SN " + self.sn + \
                  " found. Connected: " + str(sns_found)
        return res, spec_id, spec_info

    def _open_device(self, dev_id):
        """Open the HiasApi device by its device_id."""
        res = "OK"
        try:
            self.h_device = ctypes.c_uint64(0)
            opt = HiasOption(size=ctypes.sizeof(HiasOption))
            rc = self.dll_handler.HiasDeviceOpen(
                ctypes.c_int32(dev_id),
                ctypes.byref(self.h_device),
                ctypes.byref(opt),
            )
            res = self.get_error(rc)
            if res != "OK":
                res = "Could not open device (id=" + str(dev_id) + "): " + res
                self.logger.error(res)
            else:
                self.logger.info("Device opened, handle=" + str(self.h_device.value))
        except Exception as e:
            res = "Exception opening device: " + str(e)
            self.logger.exception(e)
        self.error = res
        return res

    def _configure_device(self):
        """Set AcquisitionMode=Continuous, TriggerMode=Off."""
        res = "OK"
        try:
            rc = self.dll_handler.HiasFeatureSetString(self.h_device, b"AcquisitionMode", b"Continuous")
            res = self.get_error(rc)
            if res != "OK":
                res = "Could not set AcquisitionMode: " + res
                self.logger.error(res)
                return res
            rc = self.dll_handler.HiasFeatureSetString(self.h_device, b"AcquisitionTriggerMode", b"Off")
            res = self.get_error(rc)
            if res != "OK":
                res = "Could not set AcquisitionTriggerMode: " + res
                self.logger.error(res)
        except Exception as e:
            res = "Exception configuring device: " + str(e)
            self.logger.exception(e)
        self.error = res
        return res

    def _alloc_buffer(self, num_frames=10):
        """Allocate ring buffer in HiasApi."""
        res = "OK"
        try:
            alloc_count = ctypes.c_int32(0)
            rc = self.dll_handler.HiasBufferAlloc(
                self.h_device,
                ctypes.c_int32(num_frames),
                ctypes.byref(alloc_count),
            )
            res = self.get_error(rc)
            if res != "OK":
                res = "Could not allocate buffer: " + res
                self.logger.error(res)
            else:
                self.logger.info("Buffer allocated, frames=" + str(alloc_count.value))
        except Exception as e:
            res = "Exception allocating buffer: " + str(e)
            self.logger.exception(e)
        self.error = res
        return res

    def _detect_npix(self):
        """
        Grab a single quick frame to learn the pixel count reported by the hardware.
        If the hardware pixel count differs from self.npix_active we warn but accept
        the hardware value so that later reads are consistent.
        """
        res = "OK"
        try:
            # Start a short stream, grab one frame
            res = self._start_stream()
            if res != "OK":
                self.logger.warning("_detect_npix: could not start stream to detect npix: " + res)
                return "OK"  # non-fatal

            try:
                x_data, y_data = self._wait_and_get_frame(timeout_ms=int(self.cycle_timeout_ms))
            finally:
                self._stop_stream()
                sleep(0.5)  # Allow device to fully settle after stream stop

            if y_data is not None and len(y_data) > 0:
                hw_npix = len(y_data)
                if hw_npix != self.npix_active:
                    self.logger.info("Hardware reports " + str(hw_npix) + " pixels (configured: " +
                                     str(self.npix_active) + "). Using configured value.")
                else:
                    self.logger.info("Number of pixels confirmed: " + str(hw_npix))
        except Exception as e:
            self.logger.warning("_detect_npix exception (non-fatal): " + str(e))
        return "OK"

    def _query_exposure_limits(self):
        """
        Query the device for the actual supported min/max exposure time.
        Updates self.min_it_ms and self.max_it_ms if the device reports values.
        Non-fatal if the query fails (keeps the configured defaults).
        """
        if self.simulation_mode:
            return

        # Try querying ExposureTimeMin
        try:
            val_min = ctypes.c_double()
            rc = self.dll_handler.HiasFeatureGetNumber(
                self.h_device,
                b"ExposureTimeMin",
                ctypes.byref(val_min),
            )
            res = self.get_error(rc)
            if res == "OK" and val_min.value > 0:
                hw_min_ms = val_min.value / 1000.0  # us -> ms
                if hw_min_ms != self.min_it_ms:
                    self.logger.info("Device min exposure time: " + str(hw_min_ms) +
                                     " ms (configured: " + str(self.min_it_ms) + " ms). Using device value.")
                    self.min_it_ms = hw_min_ms
                else:
                    self.logger.info("Device min exposure time confirmed: " + str(hw_min_ms) + " ms")
            else:
                self.logger.info("Could not query ExposureTimeMin (" + res + "), using configured value: " +
                                 str(self.min_it_ms) + " ms")
        except Exception as e:
            self.logger.info("ExposureTimeMin query not supported: " + str(e))

        # Try querying ExposureTimeMax
        try:
            val_max = ctypes.c_double()
            rc = self.dll_handler.HiasFeatureGetNumber(
                self.h_device,
                b"ExposureTimeMax",
                ctypes.byref(val_max),
            )
            res = self.get_error(rc)
            if res == "OK" and val_max.value > 0:
                hw_max_ms = val_max.value / 1000.0  # us -> ms
                if hw_max_ms != self.max_it_ms:
                    self.logger.info("Device max exposure time: " + str(hw_max_ms) +
                                     " ms (configured: " + str(self.max_it_ms) + " ms). Using device value.")
                    self.max_it_ms = hw_max_ms
                else:
                    self.logger.info("Device max exposure time confirmed: " + str(hw_max_ms) + " ms")
            else:
                self.logger.info("Could not query ExposureTimeMax (" + res + "), using configured value: " +
                                 str(self.max_it_ms) + " ms")
        except Exception as e:
            self.logger.info("ExposureTimeMax query not supported: " + str(e))

    def _flush_buffer(self):
        """Free and re-allocate the ring buffer to discard all stale frames."""
        try:
            self.dll_handler.HiasBufferFree(self.h_device)
            alloc_count = ctypes.c_int32(0)
            rc = self.dll_handler.HiasBufferAlloc(
                self.h_device,
                ctypes.c_int32(10),
                ctypes.byref(alloc_count),
            )
            res = self.get_error(rc)
            if res != "OK":
                self.logger.warning("_flush_buffer: re-alloc returned " + res)
            elif self.debug_mode >= 2:
                self.logger.debug("_flush_buffer: ring buffer reset (" +
                                  str(alloc_count.value) + " frames)")
        except Exception as e:
            self.logger.warning("_flush_buffer exception (non-fatal): " + str(e))

    def _discard_first_frame(self):
        """
        Read and throw away the first frame after a stream start.

        The very first frame may carry a partial or stale exposure from before
        the stream was (re)started, which would appear as a spike / outlier.
        """
        try:
            timeout = int((self.it_ms if self.it_ms else 100) * 2 + 500)
            _, _ = self._wait_and_get_frame(timeout_ms=timeout)
            if self.debug_mode >= 2:
                self.logger.debug("_discard_first_frame: warm-up frame discarded")
        except Exception:
            pass  # non-fatal – if there is no frame we simply continue

    def _start_stream(self):
        """Start the HiasApi acquisition stream."""
        if self.is_streaming:
            return "OK"
        res = "OK"
        try:
            rc = self.dll_handler.HiasStreamAcquisitionStart(self.h_device)
            res = self.get_error(rc)
            if res == "OK":
                self.is_streaming = True
            else:
                res = "HiasStreamAcquisitionStart failed: " + res
        except Exception as e:
            res = "Exception starting stream: " + str(e)
            self.logger.exception(e)
        return res

    def _stop_stream(self):
        """Stop the HiasApi acquisition stream."""
        if not self.is_streaming:
            return
        try:
            self.dll_handler.HiasStreamAcquisitionStop(self.h_device)
        except Exception:
            pass
        self.is_streaming = False

    def _wait_and_get_frame(self, timeout_ms=None):
        """
        Wait for the next frame and return (x_array, y_array) as numpy arrays.
        Returns (None, None) on timeout / error.
        """
        if timeout_ms is None:
            timeout_ms = int((self.it_ms if self.it_ms else 100) + self.cycle_timeout_ms)

        event = ctypes.c_uint32(0)
        rc = self.dll_handler.HiasStreamWait(
            self.h_device,
            ctypes.byref(event),
            ctypes.c_uint32(0x1),  # HIAS_STREAM_EVENT_FRAMEREADY
            ctypes.c_int32(timeout_ms),
        )
        res = self.get_error(rc)
        if res != "OK":
            if self.debug_mode >= 2:
                self.logger.debug("HiasStreamWait returned: " + res)
            return None, None

        # Use frame_index=-1 to always retrieve the most recent (latest) frame
        # instead of index 0 which reads the oldest buffered frame
        param = HiasBufferGetPtrParameter(
            size=ctypes.sizeof(HiasBufferGetPtrParameter),
            select=0,
            frame_index=-1,
        )
        raw_ptr = ctypes.c_void_p()
        rc = self.dll_handler.HiasBufferGetPtr(
            self.h_device,
            ctypes.byref(param),
            ctypes.byref(raw_ptr),
        )
        res = self.get_error(rc)
        if res != "OK" or not raw_ptr.value:
            if self.debug_mode >= 2:
                self.logger.debug("HiasBufferGetPtr returned: " + res)
            return None, None

        xy = ctypes.cast(raw_ptr, ctypes.POINTER(HiasBufferFormat_XYData)).contents
        num_pixels = xy.number_of_x

        if num_pixels <= 0 or not xy.buffer_x or not xy.buffer_y:
            return None, None

        x_ctype = _HIAS_DATA_TYPES.get(xy.x_type, ctypes.c_double)
        y_ctype = _HIAS_DATA_TYPES.get(xy.y_type, ctypes.c_double)

        x_buf = ctypes.cast(xy.buffer_x, ctypes.POINTER(x_ctype * num_pixels))
        y_buf = ctypes.cast(xy.buffer_y, ctypes.POINTER(y_ctype * num_pixels))

        return np.array(x_buf.contents, dtype=np.float64), np.array(y_buf.contents, dtype=np.float64)

    def _close_device(self, ignore_errors=False):
        """Free buffer, close device handle."""
        res = "OK"
        try:
            self.dll_handler.HiasBufferFree(self.h_device)
            opt = HiasOption(size=ctypes.sizeof(HiasOption))
            rc = self.dll_handler.HiasDeviceClose(self.h_device, ctypes.byref(opt))
            res = self.get_error(rc)
            if res != "OK":
                msg = "Could not close device: " + res
                if ignore_errors:
                    self.logger.warning(msg)
                    res = "OK"
                else:
                    self.logger.error(msg)
            self.h_device = ctypes.c_uint64(0)
        except Exception as e:
            res = "Exception closing device: " + str(e)
            if ignore_errors:
                self.logger.warning(res)
                res = "OK"
            else:
                self.logger.exception(e)
        self.spec_id = None
        return res

    def _finalize_dll(self, ignore_errors=False):
        """Call HiasFinalize to release the dll session."""
        global _hias_dll_initialized
        res = "OK"

        if not _hias_dll_initialized:
            self.logger.info("HiasApi dll already finalised.")
            return res

        if len(Hama4_Spectrometer_Instances) != 0:
            self.logger.info("Not finalising dll - another spectrometer still needs it.")
            return res

        try:
            self.logger.info("Finalising HiasApi dll...")
            opt = HiasOption(size=ctypes.sizeof(HiasOption))
            rc = self.dll_handler.HiasFinalize(ctypes.byref(opt))
            res_check = self.get_error(rc)
            if res_check != "OK":
                msg = "Could not finalise dll: " + res_check
                if ignore_errors:
                    self.logger.warning(msg)
                else:
                    res = msg
                    self.logger.error(msg)
            else:
                _hias_dll_initialized = False
                self.logger.info("HiasApi dll finalised.")
        except Exception as e:
            msg = "Exception finalising dll: " + str(e)
            if ignore_errors:
                self.logger.warning(msg)
            else:
                res = msg
                self.logger.exception(e)
        return res

    # ===================================================================
    # Auxiliary - measurement data handling
    # ===================================================================

    def reset_spec_data(self):
        """Called at the start of every new measurement."""
        while not self.read_data_queue.empty():
            _ = self.read_data_queue.get()
        while not self.handle_data_queue.empty():
            _ = self.handle_data_queue.get()

        self.ncy_read = 0
        self.ncy_handled = 0
        self.ncy_saturated = 0
        self.sy  = np.zeros(self.npix_active, dtype=np.float64)
        self.syy = np.zeros(self.npix_active, dtype=np.float64)
        self.sxy = np.zeros(self.npix_active, dtype=np.float64)
        self.arrival_times = []
        self.meas_start_time = 0
        self.meas_end_time = 0
        self.data_handling_end_time = 0

    def data_arrival_watchdog(self):
        """Side thread: waits for ncy signals, triggers measure_blocking."""
        self.logger.info("Started data arrival watchdog...")
        while not self.read_data_queue.empty():
            _ = self.read_data_queue.get()

        while True:
            ncy = self.read_data_queue.get()  # blocking
            if ncy is None:
                self.logger.info("Exiting data arrival watchdog thread...")
                break
            res = self.measure_blocking(ncy)
            self.error = res
            if res != "OK":
                self.internal_meas_done_event.set()
        self.logger.info("Exiting data arrival watchdog")

    def data_handling_watchdog(self):
        """Side thread: handles queued cycle data (used by threaded measure path)."""
        while not self.handle_data_queue.empty():
            _ = self.handle_data_queue.get()

        while True:
            (ncy_read, (rc, rc_blind_left, rc_blind_right)) = self.handle_data_queue.get()
            if ncy_read is None:
                self.logger.info("Exiting data handling watchdog thread of spectrometer " + self.alias + "...")
                break
            elif not self.docatch:
                continue
            else:
                issat = self.handle_cycle_data(ncy_read, rc, rc_blind_left, rc_blind_right)
                if issat and self.abort_on_saturation:
                    self.logger.info("data_handling_watchdog, saturation detected in spec " + self.alias +
                                     ", ncy=" + str(ncy_read) + "/" + str(self.ncy_requested))
                    self.docatch = False
                    while not self.read_data_queue.empty():
                        sleep(0.1)
                    _ = self.abort()
                    while not self.handle_data_queue.empty():
                        _ = self.handle_data_queue.get()
                    self.measurement_done()
                else:
                    if self.debug_mode >= 3:
                        self.logger.debug("data_handling_watchdog, ncy handled=" +
                                          str(self.ncy_handled) + "/" + str(self.ncy_requested))
                    if self.ncy_handled == self.ncy_requested:
                        self.measurement_done()

    def handle_cycle_data(self, ncy_read, rc, rc_blind_left, rc_blind_right):
        """
        Process one cycle of raw data:
        - apply discriminator factor
        - check saturation
        - accumulate into sy / syy / sxy
        Returns True if this cycle is saturated.
        """
        cycle_index = ncy_read - 1
        rc = np.asarray(rc, dtype=np.float64)

        if self.discriminator_factor != 1:
            rc = rc * float(self.discriminator_factor)

        rcmax = rc.max()
        rcmin = rc.min()

        if rcmin < 0:
            self.logger.warning("handle_cycle_data, negative counts detected!")
        if np.isnan(rcmax) or np.isnan(rcmin):
            self.logger.warning("handle_cycle_data, NaN counts detected!")

        issat = rcmax >= self.eff_saturation_limit
        if issat and self.abort_on_saturation:
            return issat

        # Accumulate
        self.sy  = self.sy + rc
        self.syy = self.syy + rc ** 2
        self.sxy = self.sxy + cycle_index * rc
        self.ncy_handled += 1
        if issat:
            self.ncy_saturated += 1

        return issat

    def measurement_done(self):
        """Final actions when a measurement is complete."""
        if len(self.arrival_times) > 0:
            self.meas_end_time = self.arrival_times[-1]
        else:
            self.meas_end_time = spec_clock.now()

        x = np.arange(self.ncy_handled)
        _, self.rcm, self.rcs, self.rcl = calc_msl(self.alias, x, self.sxy, self.sy, self.syy)

        if self.debug_mode >= 1:
            self.logger.debug("Measurement done for spec " + self.alias)

        self.data_handling_end_time = spec_clock.now()
        self.docatch = False
        self.internal_meas_done_event.set()

    # ===================================================================
    # Performance & statistics
    # ===================================================================

    def performance_test(self, fpath=""):
        """
        Run a series of measurements at varying IT and ncy,
        output cycle-delay-time statistics to a file.
        """
        res = "OK"
        presults = np.array([])
        self.logger.info("Doing performance test")
        its, ncys, real_dur_meass, cdts_mean, cdts_median = [], [], [], [], []
        test_start_time = spec_clock.now()

        for it in self.performance_test_it_ms_list:
            it = round(it, 3)
            self.set_it(it_ms=it)
            sleep(it * 1e-3)
            for ncy in self.performance_test_ncy_list:
                ncy = int(ncy)
                res = self.measure_blocking(ncy=ncy)
                if res != "OK":
                    break
                cdt_mean, cdt_median, real_dur_meas, _, _, _ = self.calc_performance_stats(showinfo=False)
                its.append(it)
                ncys.append(ncy)
                real_dur_meass.append(real_dur_meas)
                cdts_mean.append(cdt_mean)
                cdts_median.append(cdt_median)
                self.logger.info("IT=" + str(it) + " ms, ncy=" + str(ncy) +
                                 ", cdt_mean=" + str(cdt_mean) + " ms/cy, cdt_median=" + str(cdt_median) + " ms/cy")

        if res == "OK":
            test_end_time = spec_clock.now()
            self.logger.info("Performance test duration: " + str(test_end_time - test_start_time) + " s")
            presults = np.array([its, ncys, real_dur_meass, cdts_mean, cdts_median]).T
            header = "IT[ms]; ncy; real_dur[ms]; cdt_mean[ms/cy]; cdt_median[ms/cy]"
            ptest_filepath = os.path.join(fpath, self.sn + "_performance_test.txt")
            with open(ptest_filepath, "w") as f:
                np.savetxt(f, presults, fmt="%.4f", delimiter=";", header=header, comments="#")
            self.logger.info("Finished performance test")
        else:
            res = "Error during performance test: " + res
            self.logger.error(res)

        self.error = res
        return res, presults

    def calc_performance_stats(self, showinfo=True):
        """
        Calculate cycle-delay-time statistics of the last measurement.
        Returns (cdt_mean, cdt_median, real_dur_meas, real_dur_fdh, deltas_max, deltas_min).
        """
        real_dur_meas = 1000.0 * (self.meas_end_time - self.meas_start_time)
        real_dur_fdh = 1000.0 * (self.data_handling_end_time - self.meas_end_time)
        real_dur_total = real_dur_meas + real_dur_fdh
        expected_min = self.ncy_requested * self.it_ms
        cdt_mean = max(0, (real_dur_meas - expected_min) / self.ncy_requested) if self.ncy_requested > 0 else 0
        deltas_max = np.nan
        deltas_min = np.nan

        if self.max_ncy_per_meas == 1:
            if len(self.arrival_times) <= 1:
                case = 1
                cdt_median = cdt_mean
            else:
                case = 2
                deltas = 1000.0 * np.diff(self.arrival_times)
                deltas_mean = np.mean(deltas)
                deltas_median = np.median(deltas)
                deltas_max = np.max(deltas)
                deltas_min = np.min(deltas)
                stdev = np.std(deltas)
                cdt_median = deltas_median - self.it_ms
        else:
            if len(self.arrival_times) <= 1:
                case = 3
                cdt_median = cdt_mean
            else:
                case = 4
                cdts = []
                prev_time = self.meas_start_time
                for ncy_pack, arrival in zip(self.ncy_per_meas, self.arrival_times):
                    pack_dur = 1000.0 * (arrival - prev_time)
                    expected_pack_dur = ncy_pack * self.it_ms
                    cdt = max(0, (pack_dur - expected_pack_dur) / ncy_pack)
                    cdts.append(cdt)
                    prev_time = arrival
                cdt_packs_max = np.max(cdts)
                cdt_packs_min = np.min(cdts)
                cdt_packs_mean = np.mean(cdts)
                cdt_packs_std = np.std(cdts)
                cdt_packs_median = np.median(cdts)
                cdt_median = cdt_packs_median

        if showinfo:
            self.logger.info("---Spec " + self.alias + " measurement stats:---")
            self.logger.info("measured " + str(self.ncy_read) + " cycles at IT=" + str(self.it_ms) + " ms")
            self.logger.info("Real_dur(meas)=" + str(real_dur_meas) + "ms, Real_dur(fdh)=" +
                             str(real_dur_fdh) + "ms, Real_dur(total)=" + str(real_dur_total) + "ms")
            self.logger.info("Expected_min_dur (ncy*IT) = " + str(expected_min) + "ms")
            self.logger.info("cdt_mean=" + str(cdt_mean) + " ms/cy, cdt_median=" + str(cdt_median) + " ms/cy")
            self.logger.info("--------")

        return cdt_mean, cdt_median, real_dur_meas, real_dur_fdh, deltas_max, deltas_min

    # ===================================================================
    # Recovery helpers
    # ===================================================================

    def reset_device(self):
        """Not available for HiasApi devices - raise or warn."""
        self.logger.warning("reset_device is not supported for " + self.spec_type + " spectrometers.")
        return "reset_device not supported"

    def test_recovery(self):
        """
        Interactive test: start a long measurement, ask the user to replug,
        then attempt recovery.
        """
        self.logger.info("Testing recovery of spectrometer " + self.alias)

        res = self.set_it(it_ms=8000.0)
        if res == "OK":
            self.measure(ncy=1)
            self.docatch = False

        if res == "OK":
            j = 10
            while j > 0:
                self.logger.info("------>  Unplug & Plug spectrometer " + self.alias +
                                 " (" + self.sn + ") USB now. (" + str(j) + "/10s) <------")
                j -= 1
                sleep(1)

        if res == "OK":
            res = self.recovery()

        if res == "OK":
            res = self.set_it(it_ms=100.0)
            if res == "OK":
                res = self.measure(ncy=1)
            if res == "OK":
                res = self.wait_for_measurement()

        if res == "OK":
            self.logger.info("Recovery test of spectrometer " + self.alias + " was successful.")
        else:
            self.logger.error("Recovery test of spectrometer " + self.alias + " failed. res=" + res)

        return res


# =======================================================================
# Standalone testing  (python hama4_spectrometer.py)
# =======================================================================

if __name__ == "__main__":

    print("Testing Hama4 Spectrometer Class")

    # ---- Select Testing parameters ----
    dll_path = r"C:\Program Files\Hamamatsu\Hiasphere\bin\HiasApi.dll"

    instruments = ["TestBench"]
    simulation_mode = [False]
    debug_mode_list = [1]
    do_recovery_test = False
    do_performance_test = False
    performance_test_fpath = "C:/Temp"

    # ---- Testing Code ----
    SP = []
    for i in range(len(instruments)):
        instr = instruments[i]
        SP.append(Hama4_Spectrometer())

        SP[i].dll_path = dll_path
        SP[i].debug_mode = debug_mode_list[i]
        SP[i].simulation_mode = simulation_mode[i]
        SP[i].alias = str(i + 1)

        if instr == "TestBench":
            SP[i].sn = "725YA001" 
            SP[i].npix_active = 256
            SP[i].min_it_ms = 0.005
            SP[i].performance_test_it_ms_list = np.arange(0.05, 10.1, 1.0)
            SP[i].performance_test_ncy_list = [1, 10, 100, 200, 500, 1000]
        else:
            raise ValueError("Instrument " + instr + " not recognised.")

        SP[i].initialize_spec_logger()

    # Logging
    if np.any(debug_mode_list):
        loglevel = logging.DEBUG
    else:
        loglevel = logging.INFO

    log_fmt = "[%(asctime)s.%(msecs)03d] [%(levelname)s] [%(name)s] [%(message)s]"
    log_datefmt = "%Y%m%dT%H%M%SZ"
    formatter = logging.Formatter(log_fmt, log_datefmt)

    logfile = "C:/Temp/hama4_spec_test_" + "_".join(instruments) + ".txt"
    # Create log directory if it doesn't exist
    logdir = os.path.dirname(logfile) or "."
    if logdir and not os.path.exists(logdir):
        os.makedirs(logdir)
    print("logging into: " + logfile)
    logging.basicConfig(level=loglevel, format=log_fmt, datefmt=log_datefmt, filename=logfile)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(loglevel)
    console_handler.setFormatter(formatter)
    logging.getLogger().addHandler(console_handler)

    # --- Test actions ---
    res = "OK"

    logger.info("--- Connecting spectrometers ---")
    for i in range(len(SP)):
        try:
            res = SP[i].connect()
        except Exception as e:
            logger.exception(e)
            res = "Exception while connecting: " + str(e)
        if res != "OK":
            break

    try:
        if res == "OK":
            logger.info("--- Setting IT ---")
            for i in range(len(SP)):
                res = SP[i].set_it(it_ms=200.0)
                if res != "OK":
                    break

        if res == "OK":
            logger.info("--- Starting measurements ---")
            for i in range(len(SP)):
                res = SP[i].measure(ncy=10)

        if res == "OK":
            for i in range(len(SP)):
                if SP[i].measuring:
                    res = SP[i].wait_for_measurement()
            logger.info("All measurements done")

        if res == "OK":
            logger.info("Waiting 5 seconds")
            sleep(5)

        if res == "OK" and do_recovery_test:
            sleep(3)
            logger.info("--- Testing soft-recovery ---")
            res = SP[-1].test_recovery()
            sleep(1)

            if res == "OK":
                logger.info("--- Final Test - Setting IT ---")
                for i in range(len(SP)):
                    res = SP[i].set_it(it_ms=200.0)
                    if res != "OK":
                        break

            if res == "OK":
                logger.info("--- Final Test - Starting measurements ---")
                for i in range(len(SP)):
                    res = SP[i].measure(ncy=10)
                    if res != "OK":
                        break

            if res == "OK":
                for i in range(len(SP)):
                    if SP[i].measuring:
                        logger.info("--- Waiting for spec " + SP[i].alias + " ---")
                        res = SP[i].wait_for_measurement()

        if res == "OK" and do_performance_test:
            for i in range(len(SP)):
                SP[i].performance_test(fpath=performance_test_fpath)
                logger.info("Performance test for spec " + str(i + 1) + " finished.")

    except Exception as e:
        logger.exception(e)

    logger.info("--- Test Finished, disconnecting... ---")
    sleep(2)
    for i in range(len(SP)):
        res = SP[i].disconnect(dofree=True)

    logger.info("--- Finished ---")
