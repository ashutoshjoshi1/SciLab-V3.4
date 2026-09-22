"""Hama6 (C13016 via the MICRO_USB2_CLR .NET assembly) against a reflection-style fake.

The fake reproduces the assembly's public API and the board's behaviour as
verified against the hardware on 2026-09-18 with Hamamatsu's own sample
sequence: ``Int64 id`` handles from a connection list (``number`` is an
output), microsecond exposures behind a parameter named ``sec`` (31 underTime
below 11 us), an exposure cycle the DLL derives itself (exposure + 2,000 us),
NUL-padded identity strings, key-protected getters that answer 12 unless
``strCheck`` is the assembly's literal, capture mode 0 delivering one frame per
``captureStart`` about one cycle later, ``getCaptureStatus`` 0 while that
frame waits (1 otherwise, 4 on data loss), ``getImageData`` serving the frame
once with code 0 and 6 afterwards, and mode 2 waiting for an external trigger
(no frame, an all-zero echo with code 0).
"""
from __future__ import annotations

import threading
import time

import numpy as np
import pytest

import spectrometer_loader
from domain.spectrometer import validate_spectrometer_backend
from spectrometers import hama6_spectrometer as h6


# --------------------------------------------------------------------------- .NET-style reflection fakes

class FakeType:
    def __init__(self, name, element=None, byref=False):
        self.Name = name
        self._element = element
        self.IsByRef = byref
        self.IsArray = element is not None and not byref

    def GetElementType(self):
        return self._element


def T(name):
    return FakeType(name)


def ARR(name):
    return FakeType(f"{name}[]", element=FakeType(name))


def REF(inner):
    return FakeType(f"{inner.Name}&", element=inner, byref=True)


class FakeParam:
    def __init__(self, name, ptype, is_out=False):
        self.Name = name
        self.ParameterType = ptype
        self.IsOut = is_out


class FakeMethodInfo:
    def __init__(self, name, params, func, return_type="Int64"):
        self.Name = name
        self._params = params
        self.ReturnType = FakeType(return_type)
        self._func = func
        self.IsStatic = False

    def GetParameters(self):
        return list(self._params)

    def Invoke(self, obj, args):
        """Like MethodInfo.Invoke: ``args`` is mutated in place for ref parameters."""
        return self._func(args)


class FakeReflectedType:
    def __init__(self, methods):
        self._methods = methods
        self.IsClass = True

    def GetMethods(self):
        return list(self._methods)


OK = 0
NO_FRAME_YET = 1        # getCaptureStatus while no frame waits
DATA_LOSS = 4
NO_IMAGE = 6            # getImageData once the frame has been consumed
INVALID_DATA = 25
UNDER_TIME = 31
CYCLE_MARGIN_US = 2000.0    # the DLL sets cycle = exposure + 2,000 us on every setExposureTime
DLL_RACE = ("Exception has been thrown by the target of an invocation. ---> "
            "System.Threading.SynchronizationLockException: Object synchronization method was called from an unsynchronized block of code.")


class FakeMicroUsb2Clr:
    """Emulates MICRO_USB2_CLR and the C13016 as verified on 2026-09-18."""

    def __init__(self, module_ids=(7,), pixel=288, peak_pixel=120, exposure_us=11.0, mode=0):
        self.module_ids = list(module_ids)
        self.pixel = pixel
        self.peak_pixel = peak_pixel
        self.exposure_us = float(exposure_us)                 # the board powers up at 11 us
        self.cycle_us = self.exposure_us + CYCLE_MARGIN_US    # frame period, derived by the DLL from the exposure
        self.key = h6.CHECK_KEY                               # what the key-protected getters compare strCheck with
        self.mode = mode                                      # 0 single shot, 2 external trigger (1 hangs the real DLL)
        self.rejected_modes = {3, 4, 5, 6, 7, 8, 9}           # setCaptureMode answers 11 (invalidMode)
        self.rng = np.random.default_rng(11)
        self.calls = []
        self.argument_types = []
        self.initialized = False
        self.opened = None
        self.capturing = False
        self.frame_due_at = None                              # when the running exposure completes
        self.exposing_with = None
        self.ready_frame = None                               # the frame waiting in the DLL buffer
        self.produces_frames = True
        self.frames_left = None                               # None: unlimited; an int: frames the board will still deliver
        self.start_raises = False                             # captureStart throws inside the DLL
        self.stop_ineffective = False                         # captureStop answers 0 but the board keeps capturing
        self.data_loss_once = False                           # getCaptureStatus answers 4 once for the next ready frame
        self.invalid_data_once = False                        # getImageData answers 25 once for the next ready frame
        self.coefficients_available = True                    # False: getCalibrationCoefficient leaves the buffer at zeros
        self.stamp = 24                                       # the board repeats timestamps between frames
        self.frame_counter = 0
        ID = FakeParam("id", T("Int64"))
        methods = [
            FakeMethodInfo("MICRO_USB2_initialize", [], self._initialize),
            FakeMethodInfo("MICRO_USB2_uninitialize", [], self._uninitialize),
            FakeMethodInfo("MICRO_USB2_getModuleConnectionList",
                           [FakeParam("list", ARR("Int16")), FakeParam("number", REF(T("UInt16")))], self._connection_list),
            FakeMethodInfo("MICRO_USB2_open", [ID], self._open),
            FakeMethodInfo("MICRO_USB2_close", [ID], self._close),
            FakeMethodInfo("MICRO_USB2_getModuleInformationForLabVIEW",
                           [ID] + [FakeParam(n, REF(T("String"))) for n in ("vender", "product", "serial", "firmware", "driver", "library")],
                           self._information),
            FakeMethodInfo("MICRO_USB2_getModulePropertyForLabVIEW",
                           [ID, FakeParam("pixel", REF(T("Int64"))), FakeParam("resolution", REF(T("Int64"))), FakeParam("capturemode", REF(T("Int64")))],
                           self._property),
            FakeMethodInfo("MICRO_USB2_getExposurePropertyForLabVIEW",
                           [ID] + [FakeParam(n, REF(T("Double"))) for n in ("lowertime", "uppertime", "timestep", "lowercycle", "uppercycle", "cyclestep")],
                           self._exposure_property),
            FakeMethodInfo("MICRO_USB2_setExposureTime", [ID, FakeParam("sec", T("Double"))], self._set_exposure),
            FakeMethodInfo("MICRO_USB2_getExposureTime", [ID, FakeParam("sec", REF(T("Double")))], self._get_exposure),
            FakeMethodInfo("MICRO_USB2_getCalibrationCoefficient",
                           [ID, FakeParam("index", T("Int64")), FakeParam("coefficient", ARR("Double"))], self._coefficient),
            FakeMethodInfo("MICRO_USB2_captureStart", [ID], self._capture_start),
            FakeMethodInfo("MICRO_USB2_captureStop", [ID], self._capture_stop),
            FakeMethodInfo("MICRO_USB2_getCaptureStatus", [ID], self._capture_status),
            FakeMethodInfo("MICRO_USB2_getCaptureMode", [ID, FakeParam("mode", REF(T("Int64")))], self._get_mode),
            FakeMethodInfo("MICRO_USB2_setCaptureMode", [ID, FakeParam("mode", T("Int64"))], self._set_mode),
            FakeMethodInfo("MICRO_USB2_getExposureCycle",
                           [ID, FakeParam("sec", REF(T("Double"))), FakeParam("strCheck", T("String"))], self._get_cycle),
            FakeMethodInfo("MICRO_USB2_getWaveLengthPropertyForLabVIEW",
                           [ID, FakeParam("lowerwavelength", REF(T("Int64"))), FakeParam("upperwavelength", REF(T("Int64"))),
                            FakeParam("strCheck", T("String"))], self._wavelength_property),
            FakeMethodInfo("MICRO_USB2_getTriggerOutput", [ID, FakeParam("output", REF(T("Int64")))], self._trigger_output),
            FakeMethodInfo("MICRO_USB2_getTriggerPolarity", [ID, FakeParam("polarity", REF(T("Int64")))], self._trigger_polarity),
            FakeMethodInfo("MICRO_USB2_getSpectroInformationForLabVIEW",
                           [ID, FakeParam("serial", REF(T("String"))), FakeParam("sensor", REF(T("String")))], self._spectro_information),
            FakeMethodInfo("MICRO_USB2_getImageData",
                           [ID, FakeParam("imageData", REF(ARR("UInt16"))), FakeParam("time", REF(ARR("UInt64")))], self._image_data),
            FakeMethodInfo("GetType", [], lambda a: None, "Type"),
        ]
        self._type = FakeReflectedType(methods)

    def GetType(self):
        return self._type

    def method(self, name):
        return next(m for m in self._type._methods if m.Name == name)

    def count(self, name):
        return self.calls.count(name)

    # -- emulated .NET methods (args list, mutated in place) --
    def _initialize(self, args):
        self.calls.append("initialize")
        self.initialized = True
        return OK

    def _uninitialize(self, args):
        self.calls.append("uninitialize")
        self.initialized = False
        return OK

    def _connection_list(self, args):
        assert self.initialized, "getModuleConnectionList before initialize"
        self.argument_types.append(("number", type(args[1]).__name__))
        for i, module_id in enumerate(self.module_ids[:len(args[0])]):
            args[0][i] = module_id
        args[1] = min(len(self.module_ids), len(args[0]))    # `number` is an output: the count detected
        return OK

    def _open(self, args):
        self.calls.append(f"open:{args[0]}")
        self.argument_types.append(("id", type(args[0]).__name__))
        assert args[0] in self.module_ids, f"open with unknown id {args[0]}"
        self.opened = args[0]
        return OK

    def _close(self, args):
        self.calls.append(f"close:{args[0]}")
        self.opened = None
        return OK

    def _information(self, args):
        padded = [s.ljust(32, "\x00") for s in ("Hamamatsu", "C13016", "326VA021", "1.08")]   # NUL-padded like the DLL
        args[1], args[2], args[3], args[4], args[5], args[6] = *padded, "1.00", "1.00"
        return OK

    def _property(self, args):
        args[1], args[2], args[3] = self.pixel, 16, 32
        return OK

    def _exposure_property(self, args):
        args[1], args[2], args[3] = 11.0, 1_000_000.0, 1.0                 # exposure limits and step, microseconds
        args[4], args[5], args[6] = 1043.0, 1_100_000.0, 1.0               # cycle limits and step
        return OK

    def _set_exposure(self, args):
        self.calls.append("setExposureTime")
        self.argument_types.append(("sec", type(args[1]).__name__))
        value = float(args[1])
        if value < 11.0:
            return UNDER_TIME
        if value > 1_000_000.0:
            return 30                                          # overTime
        self.exposure_us = value
        self.cycle_us = value + CYCLE_MARGIN_US                # the DLL keeps the frame period consistent itself
        return OK

    def _get_exposure(self, args):
        args[1] = self.exposure_us
        return OK

    def _spectro_information(self, args):
        args[1], args[2] = "26C00021".ljust(16, "\x00"), "S11639".ljust(16, "\x00")   # the head
        return OK

    def _coefficient(self, args):
        if not self.coefficients_available:
            return OK                               # code 0 but nothing written: no polynomial in this board
        if int(args[1]) == 0 and len(args[2]) >= 6:
            for i, value in enumerate([310.0, 2.65, -1.1e-3, 0.0, 0.0, 0.0]):
                args[2][i] = value
            return OK
        for i in range(len(args[2])):
            args[2][i] = float("nan")               # index 1 (sensitivity) is unused: NaN on the bench
        return OK

    def _get_mode(self, args):
        args[1] = self.mode
        return OK

    def _set_mode(self, args):
        self.calls.append(f"setCaptureMode:{int(args[1])}")
        if int(args[1]) in self.rejected_modes:
            return 11                              # invalidMode
        if self.capturing:
            return 35                              # nowCapturing
        self.mode = int(args[1])
        return OK

    def _get_cycle(self, args):
        if args[2] != self.key:
            args[1] = 0.0
            return 12                              # invalidParameter: wrong strCheck
        args[1] = self.cycle_us
        return OK

    def _wavelength_property(self, args):
        if args[3] != self.key:
            return 12
        args[1], args[2] = 200, 800
        return OK

    def _trigger_output(self, args):
        args[1] = 1
        return OK

    def _trigger_polarity(self, args):
        args[1] = 3
        return OK

    def _capture_start(self, args):
        self.calls.append("captureStart")
        if self.start_raises:
            raise Exception(DLL_RACE)
        self.capturing = True
        if self.mode != h6.CAPTURE_MODE_TRIGGER:           # trigger mode waits for an external edge that never comes
            self.frame_due_at = time.monotonic() + self.cycle_us / 1e6
            self.exposing_with = self.exposure_us
        return OK

    def _capture_stop(self, args):
        self.calls.append("captureStop")
        if not self.stop_ineffective:
            self.capturing = False
            self.frame_due_at = None
        return OK

    def _make_frame(self, exposure_us):
        px = np.arange(self.pixel, dtype=float)
        frame = 700.0 + 0.4 * exposure_us * np.exp(-0.5 * ((px - self.peak_pixel) / 2.5) ** 2) + self.rng.normal(0, 3.0, px.size)
        return np.clip(np.rint(frame), 0, 65535)

    def _advance(self):
        if self.frame_due_at is None or time.monotonic() < self.frame_due_at:
            return
        self.frame_due_at = None                              # single shot: one exposure per start
        if not self.produces_frames or self.frames_left == 0:
            return
        if self.frames_left is not None:
            self.frames_left -= 1
        self.ready_frame = self._make_frame(self.exposing_with)
        self.frame_counter += 1

    def _capture_status(self, args):
        self.calls.append("getCaptureStatus")
        self._advance()
        if self.ready_frame is None:
            return NO_FRAME_YET
        if self.data_loss_once:
            self.data_loss_once = False
            self.ready_frame = None
            return DATA_LOSS
        return OK

    def _image_data(self, args):
        self.calls.append("getImageData")
        self._advance()
        buffer = args[1]
        assert len(buffer) == self.pixel, "getImageData must get a pixel-sized buffer"
        if self.ready_frame is None:
            if self.capturing and self.mode == h6.CAPTURE_MODE_TRIGGER:
                return OK                           # the echo: code 0 with an all-zero buffer while no trigger came
            return NO_IMAGE
        if self.invalid_data_once:
            self.invalid_data_once = False
            self.ready_frame = None
            return INVALID_DATA
        for i, value in enumerate(self.ready_frame):
            buffer[i] = int(value)
        args[2][0] = self.stamp
        self.ready_frame = None                     # consumed: the next call answers 6
        return OK


def frame_sequences(calls):
    """Per-frame call groups starting at each captureStart (identity calls left out)."""
    groups, current = [], None
    for call in calls:
        if call == "captureStart":
            if current:
                groups.append(current)
            current = [call]
        elif current is not None and call in ("getCaptureStatus", "getImageData", "captureStop"):
            current.append(call)
    if current:
        groups.append(current)
    return groups


@pytest.fixture(autouse=True)
def isolated_library(monkeypatch, tmp_path):
    h6.LIBRARY.users = 0
    monkeypatch.setattr(h6, "API_DUMP_FILE", tmp_path / "micro_usb2_api_dump.txt")
    yield
    h6.LIBRARY.users = 0


@pytest.fixture
def device():
    return FakeMicroUsb2Clr()


@pytest.fixture
def spec(device):
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    assert backend.connect() == "OK", backend.error
    yield backend
    backend.disconnect()


# --------------------------------------------------------------------------- reflection layer

def test_reflection_describes_real_signatures(device):
    api = h6.MicroUsb2Api(device)
    described = api.describe()
    assert "MICRO_USB2_getImageData(Int64 id, ref UInt16[] imageData, ref UInt64[] time) -> Int64" in described
    assert "MICRO_USB2_setExposureTime(Int64 id, Double sec) -> Int64" in described
    assert api.has(h6.M_OPEN) and not api.has("MICRO_USB2_readEeprom")


def test_call_prepares_buffers_outs_and_scalars(device):
    api = h6.MicroUsb2Api(device)
    api.call(h6.M_INIT)
    call = api.call(h6.M_LIST, h6.Buf(h6.MAX_MODULES), 0)
    assert call.result == OK and call.slots[1] == 1 and call.slots[0][0] == 7
    with pytest.raises(TypeError, match="takes 1 argument"):
        api.call(h6.M_OPEN)
    with pytest.raises(RuntimeError, match="has no method"):
        api.call("MICRO_USB2_nonexistent")


# --------------------------------------------------------------------------- connection workflow

def test_connect_follows_the_sample_sequence(spec, device):
    """initialize, list, open, stop leftovers, (mode already 0), setExposureTime, then one start/status/read/stop per frame."""
    assert device.calls[:3] == ["initialize", "open:7", "captureStop"]
    assert ("id", "int") in device.argument_types and ("sec", "float") in device.argument_types
    assert spec.module_id == 7 and spec.module_ids == [7]
    assert spec.sn == "326VA021" and spec.module_info["firmware"] == "1.08"                 # NUL padding stripped
    assert spec.name == "Hamamatsu C13016 micro-spectrometer circuit with head 26C00021 (S11639)"
    assert spec.capture_mode == 0 and spec.board_capture_mode == 0 and spec.capture_mode_mask == 32
    assert not any(c.startswith("setCaptureMode") for c in device.calls)                 # already single shot
    assert spec.diagnostics["capture_state"]["trigger"] == {"output": 1, "polarity": 3}
    assert spec.wavelength_range_nm == (200, 800)
    assert spec.npix_active == 288 and spec.nbits == 16 and spec.eff_saturation_limit == 65535.0
    assert spec.min_it_ms == pytest.approx(0.011) and spec.max_it_ms == pytest.approx(1000.0)
    assert spec.it_step_ms == pytest.approx(0.001)
    assert spec.exposure_limits_us["cycle_lower"] == 1043.0
    assert spec.it_ms == pytest.approx(10.0) and device.exposure_us == 10000.0
    assert spec.exposure_cycle_us == 12000.0 and spec.exposure_cycle_s == pytest.approx(0.012)   # read back, never set
    assert spec.calibration_coefficients == [310.0, 2.65, -1.1e-3, 0.0, 0.0, 0.0]
    assert spec.wavelengths_nm[0] == pytest.approx(310.0 + 2.65 - 1.1e-3)
    assert spec.rcm.size == 288 and device.frame_counter == 2                            # two probe frames
    groups = frame_sequences(device.calls)
    assert len(groups) == 2
    for group in groups:                                                                 # Hamamatsu's loop, per frame
        assert group[0] == "captureStart" and group[-2:] == ["getImageData", "captureStop"]
        assert set(group[1:-2]) == {"getCaptureStatus"} and group.count("getImageData") == 1
    assert not device.capturing and spec.diagnostics[h6.M_CAPTURE_START] == 0
    assert spec.frame_period_s == pytest.approx(0.012, abs=0.02)
    assert spec.diagnostics[h6.M_OPEN] == OK and spec.diagnostics["module_ids"] == [7]
    assert validate_spectrometer_backend(spec) == []


def test_connect_switches_a_board_found_in_trigger_mode_to_single_shot():
    device = FakeMicroUsb2Clr(mode=2)                        # left in external-trigger mode: no frame would ever come
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    assert backend.connect() == "OK", backend.error
    assert backend.board_capture_mode == 2 and backend.capture_mode == 0 and device.mode == 0
    assert device.count("setCaptureMode:0") == 1
    assert device.calls.index("captureStop") < device.calls.index("setCaptureMode:0") < device.calls.index("captureStart")
    backend.disconnect()


def test_connect_reports_a_mode_the_board_refuses():
    device = FakeMicroUsb2Clr(mode=2)
    device.rejected_modes.add(0)
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    result = backend.connect()
    assert "setCaptureMode(0) answered 11 (invalidMode)" in result
    assert device.opened is None and h6.LIBRARY.users == 0


def test_connect_uses_device_index_from_label():
    device = FakeMicroUsb2Clr(module_ids=(3, 9))
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    backend.sn = "C13016-2"
    assert backend.connect() == "OK", backend.error
    assert backend.module_id == 9 and device.opened == 9
    backend.disconnect()


def test_connect_without_modules_is_explained():
    device = FakeMicroUsb2Clr(module_ids=())
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    result = backend.connect()
    assert result.startswith("Could not connect to the C13016: no micro-spectrometer circuit reported")
    assert "Diagnostics:" in result and "initialize=0" in result
    assert device.calls[-1] == "uninitialize"              # library released on failure
    assert h6.LIBRARY.users == 0


def test_open_failure_code_is_reported():
    device = FakeMicroUsb2Clr()
    device.method(h6.M_OPEN)._func = lambda args: 3
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    result = backend.connect()
    assert "open(id=7) returned 3 (notAccess)" in result and h6.LIBRARY.users == 0


def test_missing_method_reports_api_and_writes_dump():
    device = FakeMicroUsb2Clr()
    device._type._methods = [m for m in device._type._methods if m.Name != h6.M_CAPTURE_STATUS]
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    result = backend.connect()
    assert "lacks MICRO_USB2_getCaptureStatus" in result and str(h6.API_DUMP_FILE) in result
    assert "MICRO_USB2_captureStart(Int64 id) -> Int64" in h6.API_DUMP_FILE.read_text(encoding="utf-8")


def test_frameless_board_is_reported_with_the_status_codes():
    device = FakeMicroUsb2Clr()
    device.produces_frames = False
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    backend.frame_timeout_s = 0.1
    result = backend.connect()
    assert "No frame within 0.1 s at IT 10.0 ms" in result
    assert "getCaptureStatus codes {1 (unsuccess): " in result and "getImageData codes {none}" in result
    assert "capture mode 0 (single shot)" in result and "exposure cycle 12000.0 us" in result
    assert "diagnose_c13016.py" in result
    assert not device.capturing and device.opened is None    # capture stopped, module closed


def test_a_single_frame_does_not_pass_the_two_frame_probe():
    device = FakeMicroUsb2Clr()
    device.frames_left = 1                                   # the board delivers one frame and then nothing
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    backend.frame_timeout_s = 0.1
    result = backend.connect()
    assert "No frame within 0.1 s" in result and device.frame_counter == 1
    assert device.opened is None


def test_blocked_dll_call_is_reported_instead_of_freezing(monkeypatch):
    device = FakeMicroUsb2Clr()
    start = device.method("MICRO_USB2_captureStart")
    real_start = start._func

    def blocked_start(args):
        time.sleep(0.6)                                  # the DLL never comes back in time
        return real_start(args)

    start._func = blocked_start
    monkeypatch.setattr(h6, "DLL_CALL_TIMEOUT_S", 0.2)
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    result = backend.connect()
    assert "captureStart did not return within 0.2 s" in result and "restart SciLab" in result
    assert backend._calls.blocked_on == "captureStart" and not backend._connected


def test_coefficient_file_overrides_the_board_polynomial(tmp_path):
    device = FakeMicroUsb2Clr()
    (tmp_path / h6.COEFFICIENTS_FILE_NAME).write_text('{"default": [190.5, 0.87, 0, 0, 0, 0]}', encoding="utf-8")
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    backend.dll_path = str(tmp_path / "MICRO_USB2_CLR.dll")
    assert backend.connect() == "OK", backend.error
    assert backend.calibration_coefficients[:2] == [190.5, 0.87]
    assert backend.diagnostics["calibration"]["board"][:2] == [310.0, 2.65]  # the board's polynomial is still recorded
    backend.disconnect()


def test_coefficient_file_supplies_the_wavelength_axis(tmp_path):
    device = FakeMicroUsb2Clr()
    device.coefficients_available = False
    (tmp_path / h6.COEFFICIENTS_FILE_NAME).write_text(
        '{"26C00021": {"model": "C16767MA-20", "coefficients": [190.5, 0.87, -1.2e-5, 0, 0, 0]},'
        ' "default": [300, 1, 0, 0, 0, 0]}', encoding="utf-8")
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    backend.dll_path = str(tmp_path / "MICRO_USB2_CLR.dll")
    assert backend.connect() == "OK", backend.error
    assert backend.calibration_coefficients[:2] == [190.5, 0.87]              # the head's entry wins over "default"
    assert backend.wavelengths_nm.size == 288
    assert backend.wavelengths_nm[0] == pytest.approx(190.5 + 0.87 - 1.2e-5)
    assert backend.name == "Hamamatsu C13016 micro-spectrometer circuit with C16767MA-20 head 26C00021 (S11639)"
    assert backend.diagnostics["calibration_file"]["key"] == "26C00021"
    backend.disconnect()


def test_without_coefficients_anywhere_the_pixel_axis_is_used(tmp_path):
    device = FakeMicroUsb2Clr()
    device.coefficients_available = False
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    backend.dll_path = str(tmp_path / "MICRO_USB2_CLR.dll")                  # no coefficient file next to it
    assert backend.connect() == "OK", backend.error
    assert backend.calibration_coefficients == [] and backend.wavelengths_nm.size == 0
    backend.disconnect()


def test_disconnect_closes_and_uninitializes(spec, device):
    spec.disconnect()
    assert device.opened is None and not device.initialized and not device.capturing
    assert device.calls[-2:] == ["close:7", "uninitialize"]


def test_capture_left_running_by_a_previous_session_is_stopped_first():
    device = FakeMicroUsb2Clr()
    device.capturing = True                                  # a killed process never stopped its capture
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    assert backend.connect() == "OK", backend.error
    assert device.calls.index("captureStop") < device.calls.index("captureStart")   # stop before the very first start
    backend.disconnect()


# --------------------------------------------------------------------------- parameters

def test_set_it_converts_to_microseconds_and_reads_the_cycle_back(spec, device):
    assert spec.set_it(250.0) == "OK"
    assert device.exposure_us == 250000.0 and spec.it_ms == pytest.approx(250.0)
    assert spec.exposure_cycle_us == 252000.0                # the DLL's own cycle, read with the key
    assert spec.set_it(0.001) == "OK"                       # clamped to the 11 us floor
    assert device.exposure_us == 11.0 and spec.exposure_cycle_us == 2011.0
    assert spec._frame_period_s() < 0.1                     # timeouts follow the cycle
    assert not device.capturing


def test_the_driver_never_sets_the_exposure_cycle():
    """exposure + 1,032 us through setExposureCycle starved the board of frames; the DLL manages the cycle."""
    assert not hasattr(h6, "M_SET_CYCLE")
    assert "MICRO_USB2_setExposureCycle" not in open(h6.__file__, encoding="utf-8").read()


def test_set_it_rejected_by_device_is_an_error(spec, device):
    device.method(h6.M_SET_EXPOSURE)._func = lambda args: UNDER_TIME
    result = spec.set_it(100.0)
    assert result == "Could not set integration time: setExposureTime(100000 us) returned 31 (underTime)"
    assert spec.it_ms == pytest.approx(10.0)


def test_silently_ignored_exposure_is_detected_by_readback(spec, device):
    device.method(h6.M_SET_EXPOSURE)._func = lambda args: OK    # says OK, keeps 10 ms
    result = spec.set_it(100.0)
    assert result.startswith("Could not set integration time: device kept 10.0000 ms")


# --------------------------------------------------------------------------- acquisition

def test_measure_averages_consecutive_frames(spec, device):
    spec.abort_on_saturation = False
    assert spec.set_it(5.0) == "OK"
    starts = device.count("captureStart")
    assert spec.measure(ncy=5) == "OK"
    assert spec.wait_for_measurement() == "OK"
    assert spec.ncy_handled == 5
    assert device.frame_counter == 2 + 5                    # probe frames + measured frames
    assert device.count("captureStart") == starts + 5       # one start per frame
    assert int(np.argmax(spec.rcm)) == device.peak_pixel
    assert spec.rcm.max() == pytest.approx(700 + 0.4 * 5000, rel=0.02)
    assert spec.rcs.max() < 20.0                            # noise, not frame duplication
    assert not device.capturing                             # stopped after the last frame
    groups = frame_sequences(device.calls)[-5:]
    assert all(g[-2:] == ["getImageData", "captureStop"] and g.count("getImageData") == 1 for g in groups)


def test_frame_exposed_before_an_it_change_is_not_used(spec, device):
    assert spec.set_it(20.0) == "OK"
    assert spec.measure(ncy=1) == "OK" and spec.wait_for_measurement() == "OK"
    assert spec.rcm.max() == pytest.approx(700 + 0.4 * 20000, rel=0.02)   # exposed at 20 ms, not the 10 ms probe


def test_data_loss_status_restarts_the_capture(spec, device):
    device.data_loss_once = True                            # getCaptureStatus answers 4 for the next frame
    starts = device.count("captureStart")
    assert spec.measure(ncy=1) == "OK" and spec.wait_for_measurement() == "OK"
    assert spec.ncy_handled == 1 and device.count("captureStart") == starts + 2
    assert spec.rcm.max() == pytest.approx(700 + 0.4 * 10000, rel=0.02)


def test_invalid_frame_is_discarded_and_retaken(spec, device):
    device.invalid_data_once = True                         # getImageData answers 25 for the next frame
    starts = device.count("captureStart")
    assert spec.measure(ncy=1) == "OK" and spec.wait_for_measurement() == "OK"
    assert spec.ncy_handled == 1 and device.count("captureStart") == starts + 2


def test_frame_timeout_reports_codes(spec, device):
    device.produces_frames = False
    spec.frame_timeout_s = 0.2
    spec.measure(ncy=1)
    result = spec.wait_for_measurement()
    assert result.startswith("Acquisition failed: No frame within 0.2 s")
    assert "getCaptureStatus codes {1 (unsuccess): " in result and "captureStart=0" in result
    assert "capture mode 0 (single shot)" in result and "diagnose_c13016.py" in result
    assert not device.capturing                             # stopped so the next attempt starts afresh


def test_request_stop_unblocks_waiter(spec, device):
    assert spec.set_it(300.0) == "OK"                       # 302 ms per frame
    spec.measure(ncy=20)
    threading.Timer(0.15, spec.request_stop).start()
    t0 = time.monotonic()
    result = spec.wait_for_measurement()
    assert time.monotonic() - t0 < 2.0
    assert result == "Measurement stopped by user"
    assert not device.capturing


def test_wait_timeout_accounts_for_the_frame_period(spec):
    spec.frame_period_s = 1.1
    spec.ncy_requested = 20
    spec._done.set()
    started = time.monotonic()
    assert spec.wait_for_measurement() == "OK"
    assert time.monotonic() - started < 0.5                 # returns at once when done...
    assert spec._frame_period_s() == pytest.approx(1.1)     # ...and the derived timeout includes ncy * period


def test_start_that_throws_is_retried_then_reported(spec, device):
    device.start_raises = True                              # the board still captures for a killed process
    device.stop_ineffective = True
    starts = device.count("captureStart")
    assert spec.measure(ncy=2) == "OK"
    result = spec.wait_for_measurement()
    assert result.startswith("Acquisition failed: captureStart keeps failing with 'Exception has been thrown")
    assert "unplug and re-plug the board" in result
    assert device.count("captureStart") == starts + 3          # plain, after captureStop, after close + open
    assert device.count("open:7") == 2 and device.count("close:7") == 1   # re-opened once while recovering
    assert "raised" in str(spec.diagnostics[h6.M_CAPTURE_START])


# --------------------------------------------------------------------------- discovery + environment

def test_enumerate_devices_lists_connection_list_entries():
    device = FakeMicroUsb2Clr(module_ids=(4, 5))
    found = h6.enumerate_devices("dummy.dll", api_factory=lambda _path: h6.MicroUsb2Api(device))
    assert [d["serial"] for d in found] == ["C13016-1", "C13016-2"]
    assert device.calls == ["initialize", "uninitialize"]  # nothing left open after discovery


def test_loader_falls_back_to_the_vendor_install_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(spectrometer_loader, "DLLS_DIR", tmp_path / "dlls")            # no bundled copy
    installed = tmp_path / "EvaluationSoftware" / h6.DLL_NAME
    monkeypatch.setattr(spectrometer_loader, "HAMA6_VENDOR_INSTALL_DLL", installed)
    assert spectrometer_loader.suggest_default_dll_path("Hama6") == ""
    installed.parent.mkdir()
    installed.write_bytes(b"MZ")
    assert spectrometer_loader.suggest_default_dll_path("Hama6") == str(installed)


def test_missing_pythonnet_gives_actionable_error(monkeypatch, tmp_path):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "clr":
            raise ImportError("No module named clr")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="pythonnet"):
        h6.load_clr_assembly(str(tmp_path / h6.DLL_NAME))


def test_plausible_coefficients_guard():
    assert h6.plausible_coefficients([310.0, 2.65, 0.0])
    assert not h6.plausible_coefficients([0.0, 0.0, 0.0])
    assert not h6.plausible_coefficients([310.0])
