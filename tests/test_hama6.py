"""Hama6 (C13016 via the MICRO_USB2_CLR .NET assembly) against a reflection-style fake.

The fake reproduces the assembly's public API as reflected on the bench and
the board's observed behaviour: ``Int64 id`` handles from a connection list,
microsecond exposures behind a parameter named ``sec``, NUL-padded identity
strings, capture mode 0 delivering one frame per ``captureStart`` about one
exposure cycle later and mode 2 delivering frames continuously, ``getImageData``
answering 6 until a frame exists and then serving it twice with 0 before 6
resumes, a ``captureStart`` that throws while a continuous capture runs, a
``captureStop`` that may be ineffective, and a ``getCaptureStatus`` stuck at 1.
"""
from __future__ import annotations

import threading
import time

import numpy as np
import pytest

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
NO_FRAME = 6      # what the bench board answers while no new frame exists
DLL_RACE = ("Exception has been thrown by the target of an invocation. ---> "
            "System.Threading.SynchronizationLockException: Object synchronization method was called from an unsynchronized block of code.")


class FakeMicroUsb2Clr:
    """Emulates MICRO_USB2_CLR and the C13016 as observed on the bench."""

    def __init__(self, module_ids=(7,), pixel=288, peak_pixel=120, exposure_us=10000, unit="us", cycle_s=0.02, mode=0):
        self.module_ids = list(module_ids)
        self.pixel = pixel
        self.peak_pixel = peak_pixel
        self.exposure_s = exposure_us / 1e6
        self.unit_scale = 1e6 if unit == "us" else 1.0      # the bench board works in microseconds
        self.cycle_s = cycle_s                              # frame period of the continuous capture
        self.rng = np.random.default_rng(11)
        self.calls = []
        self.argument_types = []
        self.initialized = False
        self.opened = None
        self.mode = mode                                    # 0: single shot (bench default), 2: continuous
        self.capturing = False
        self.serves_left = 0                                # the board serves a frame twice with code 0
        self.stop_ineffective = False                       # captureStop returns 0 but the capture goes on
        self.produces_frames = True
        self.never_ready = False
        self.coefficients_available = True                  # False: getCalibrationCoefficient leaves the buffer at zeros
        self.silent_modes = set()                           # modes in which the board never exposes (bench: mode 0 on 2026-09-18)
        self.next_frame_at = None
        self.exposing_with = None
        self.last_frame = None
        self.stamp = 0
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
        capacity = int(args[1])
        assert capacity > 0, "capacity must be passed in `number`"
        for i, module_id in enumerate(self.module_ids[:capacity]):
            args[0][i] = module_id
        args[1] = min(len(self.module_ids), capacity)
        return OK

    def _open(self, args):
        self.calls.append(f"open:{args[0]}")
        self.argument_types.append(type(args[0]).__name__)
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
        s = self.unit_scale
        args[1], args[2], args[3] = 11e-6 * s, 1.0 * s, 1e-6 * s
        args[4], args[5], args[6] = 1043e-6 * s, 1.1 * s, 1e-6 * s        # cycle limits as on the bench
        return OK

    def _set_exposure(self, args):
        self.argument_types.append(type(args[1]).__name__)
        self.exposure_s = float(args[1]) / self.unit_scale
        return OK

    def _get_exposure(self, args):
        args[1] = self.exposure_s * self.unit_scale
        return OK

    def _spectro_information(self, args):
        args[1], args[2] = "26C00021".ljust(16, "\x00"), "S11639".ljust(16, "\x00")   # the head, as on the bench
        return OK

    def _coefficient(self, args):
        if not self.coefficients_available:
            return OK                               # code 0 but nothing written: no polynomial in this board
        if int(args[1]) == 0 and len(args[2]) >= 6:
            for i, value in enumerate([310.0, 2.65, -1.1e-3, 0.0, 0.0, 0.0]):
                args[2][i] = value
            return OK
        return -5

    def _get_mode(self, args):
        args[1] = self.mode
        return OK

    def _set_mode(self, args):
        self.mode = int(args[1])
        return OK

    def _get_cycle(self, args):
        args[1] = 0.0
        return 12 if args[2] == "" else OK       # key-protected on the bench: 12 with an empty key

    def _trigger_output(self, args):
        args[1] = 1
        return OK

    def _trigger_polarity(self, args):
        args[1] = 3
        return OK

    @property
    def single_shot(self):
        return self.mode == 0

    def _capture_start(self, args):
        self.calls.append("captureStart")
        if self.capturing and not self.single_shot:
            raise Exception(DLL_RACE)              # the DLL's behaviour on a second start of a continuous capture
        self.capturing = True
        self.exposing_with = self.exposure_s
        self.next_frame_at = time.monotonic() + self.cycle_s
        self.serves_left = max(self.serves_left, 1)         # the DLL echoes its buffer once (zeros if empty) after a start
        return OK

    def _capture_stop(self, args):
        self.calls.append("captureStop")
        if not self.stop_ineffective:
            self.capturing = False
        return OK

    def _capture_status(self, args):
        return 1                                   # the bench board answers 1 in every state

    def _make_frame(self, exposure_s):
        px = np.arange(self.pixel, dtype=float)
        frame = 700.0 + 0.4e6 * exposure_s * np.exp(-0.5 * ((px - self.peak_pixel) / 2.5) ** 2) + self.rng.normal(0, 3.0, px.size)
        return np.clip(np.rint(frame), 0, 65535)

    def _advance(self):
        now = time.monotonic()
        exposing = self.produces_frames and self.mode not in self.silent_modes
        while exposing and self.capturing and self.next_frame_at is not None and now >= self.next_frame_at:
            self.last_frame = self._make_frame(self.exposing_with)   # exposed with the setting at frame start
            self.exposing_with = self.exposure_s
            self.stamp += 1
            self.frame_counter += 1
            self.serves_left = 2
            if self.single_shot:
                self.capturing = False             # the capture ends by itself after one frame
                self.next_frame_at = None
            else:
                self.next_frame_at += self.cycle_s

    def _image_data(self, args):
        if self.never_ready:
            return NO_FRAME
        self._advance()
        if self.serves_left <= 0:
            return NO_FRAME                         # buffer left untouched (zeros)
        self.serves_left -= 1
        if self.last_frame is None:
            return OK                               # 2026-09-18: code 0 with 288 zeros and time 0 right after captureStart
        buffer = args[1]
        assert len(buffer) == self.pixel, "getImageData must get a pixel-sized buffer"
        for i, value in enumerate(self.last_frame):
            buffer[i] = int(value)
        args[2][0] = self.stamp * 1000              # same frame, same timestamp until the next cycle
        return OK


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


@pytest.fixture
def continuous():
    """Board in capture mode 2: frames flow every cycle, a second captureStart throws."""
    device = FakeMicroUsb2Clr(mode=2)
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    assert backend.connect() == "OK", backend.error
    yield backend, device
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
    call = api.call(h6.M_LIST, h6.Buf(h6.MAX_MODULES), h6.MAX_MODULES)
    assert call.result == OK and call.slots[1] == 1 and call.slots[0][0] == 7
    with pytest.raises(TypeError, match="takes 1 argument"):
        api.call(h6.M_OPEN)
    with pytest.raises(RuntimeError, match="has no method"):
        api.call("MICRO_USB2_nonexistent")


# --------------------------------------------------------------------------- connection workflow

def test_connect_follows_initialize_list_open_workflow(spec, device):
    assert device.calls[:2] == ["initialize", "open:7"]
    assert device.argument_types[0] == "int" and "float" in device.argument_types
    assert spec.module_id == 7 and spec.module_ids == [7]
    assert spec.sn == "326VA021" and spec.name.startswith("Hamamatsu C13016 micro-spectrometer circuit")   # NUL padding stripped
    assert spec.module_info["firmware"] == "1.08"
    assert spec.capture_modes_supported == 32 and spec.capture_mode == 0
    assert spec.diagnostics["capture_state"]["trigger"] == {"output": 1, "polarity": 3}
    assert spec.diagnostics["capture_state"]["status_before_start"] == 1
    assert spec.npix_active == 288 and spec.nbits == 16 and spec.eff_saturation_limit == 65535.0
    assert spec.exposure_unit == "us"
    assert spec.min_it_ms == pytest.approx(0.011) and spec.max_it_ms == pytest.approx(1000.0)
    assert spec.it_step_ms == pytest.approx(0.001)
    assert spec.cycle_limits_s == {"lower": pytest.approx(1.043e-3), "upper": pytest.approx(1.1)}
    assert spec.calibration_coefficients == [310.0, 2.65, -1.1e-3, 0.0, 0.0, 0.0]
    assert spec.wavelengths_nm[0] == pytest.approx(310.0 + 2.65 - 1.1e-3)
    assert spec.it_ms == pytest.approx(10.0)
    assert spec.rcm.size == 288
    assert device.frame_counter == 2 and device.calls.count("captureStart") == 2   # single shot: a start per frame
    assert spec._continuous is False and not device.capturing
    assert spec.frame_period_s == pytest.approx(device.cycle_s, abs=0.015)
    assert spec.diagnostics[h6.M_OPEN] == OK and spec.diagnostics["module_ids"] == [7]
    assert validate_spectrometer_backend(spec) == []


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
    device.method(h6.M_OPEN)._func = lambda args: -100
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    result = backend.connect()
    assert "open(id=7) returned -100" in result and h6.LIBRARY.users == 0


def test_missing_method_reports_api_and_writes_dump():
    device = FakeMicroUsb2Clr()
    device._type._methods = [m for m in device._type._methods if m.Name != h6.M_IMAGE]
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    result = backend.connect()
    assert "lacks MICRO_USB2_getImageData" in result and str(h6.API_DUMP_FILE) in result
    assert "MICRO_USB2_captureStart(Int64 id) -> Int64" in h6.API_DUMP_FILE.read_text(encoding="utf-8")


def test_stale_frame_alone_does_not_pass_the_probe():
    device = FakeMicroUsb2Clr()
    device.last_frame = device._make_frame(0.01)            # left over from the vendor application
    device.stamp = 41
    device.serves_left = 2
    device.produces_frames = False
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    backend.frame_timeout_s = 0.1
    result = backend.connect()
    assert "No new frame within 0.1 s" in result and "getImageData codes {0: " in result
    assert "cycle limits {'lower': 0.00104" in result
    assert not device.capturing and device.opened is None    # capture stopped, module closed
    assert device.mode == 0                                  # the fallback to mode 2 was undone


def test_frameless_board_mode_is_switched_to_continuous_and_restored():
    device = FakeMicroUsb2Clr(mode=0)
    device.silent_modes = {0}                                # bench 2026-09-18: mode 0 answered 6 for 18 s
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    backend.frame_timeout_s = 0.2
    assert backend.connect() == "OK", backend.error
    assert device.mode == 2 and backend.capture_mode == 2 and backend.original_capture_mode == 0
    assert backend._continuous is True                       # learned afresh in the new mode
    assert backend.measure(ncy=2) == "OK" and backend.wait_for_measurement() == "OK"
    assert backend.ncy_handled == 2
    backend.disconnect()
    assert device.mode == 0 and not device.capturing and device.opened is None


def test_empty_buffer_echo_after_start_is_not_a_frame():
    device = FakeMicroUsb2Clr(mode=0)
    device.silent_modes = {0, 2}
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    backend.frame_timeout_s = 0.1
    result = backend.connect()
    assert "no capture mode delivered a frame (mode 0: No new frame" in result and "; mode 2: No new frame" in result
    assert "getImageData codes {0: 1, " in result           # the echo (code 0, all zeros) was seen once and rejected
    assert device.mode == 0 and device.opened is None


def test_head_serial_and_sensor_name_the_device(spec, device):
    assert spec.module_info["head_serial"] == "26C00021" and spec.module_info["sensor"] == "S11639"
    assert spec.name == "Hamamatsu C13016 micro-spectrometer circuit with head 26C00021 (S11639)"
    assert spec.wavelengths_nm[0] == pytest.approx(310.0 + 2.65 - 1.1e-3)     # board polynomial at p = 1


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


def test_disconnect_stops_closes_and_uninitializes(spec, device):
    spec.disconnect()
    assert device.opened is None and not device.initialized and not device.capturing
    assert device.calls[-3:] == ["captureStop", "close:7", "uninitialize"]


# --------------------------------------------------------------------------- parameters

def test_set_it_converts_to_the_device_unit(spec, device):
    assert spec.set_it(250.0) == "OK"
    assert device.exposure_s == pytest.approx(0.25) and spec.it_ms == pytest.approx(250.0)
    assert spec.set_it(0.001) == "OK"                       # clamped to the 11 us floor
    assert device.exposure_s == pytest.approx(11e-6)
    assert device.calls.count("captureStop") == 0           # set while capturing, no restart needed


def test_seconds_based_exposure_api_is_detected():
    device = FakeMicroUsb2Clr(unit="s")
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    assert backend.connect() == "OK", backend.error
    assert backend.exposure_unit == "s" and backend.min_it_ms == pytest.approx(0.011)
    assert backend.set_it(20.0) == "OK" and device.exposure_s == pytest.approx(0.02)
    backend.disconnect()


def test_set_it_rejected_by_device_is_an_error(spec, device):
    device.method(h6.M_SET_EXPOSURE)._func = lambda args: -3   # device rejects the request, also after a stop
    result = spec.set_it(100.0)
    assert result == "Could not set integration time: setExposureTime(100000 us) returned -3"
    assert spec.it_ms == pytest.approx(10.0)
    assert device.calls.count("captureStop") == 1           # the retry after stopping the capture was tried


def test_silently_ignored_exposure_is_detected_by_readback(spec, device):
    device.method(h6.M_SET_EXPOSURE)._func = lambda args: OK    # says OK, keeps 10 ms
    result = spec.set_it(100.0)
    assert result.startswith("Could not set integration time: device kept 10.0000 ms")


def test_continuous_frame_exposed_before_an_it_change_is_discarded(continuous):
    spec, device = continuous
    assert spec._continuous is True and device.capturing
    assert spec.set_it(20.0) == "OK"
    assert spec.measure(ncy=1) == "OK" and spec.wait_for_measurement() == "OK"
    assert spec.rcm.max() == pytest.approx(700 + 0.4e6 * 0.02, rel=0.02)   # not the 10 ms frame still exposing


def test_single_shot_it_change_needs_no_discard(spec, device):
    assert spec.set_it(20.0) == "OK"
    starts = device.calls.count("captureStart")
    assert spec.measure(ncy=1) == "OK" and spec.wait_for_measurement() == "OK"
    assert device.calls.count("captureStart") == starts + 1                  # exactly one start for one frame
    assert spec.rcm.max() == pytest.approx(700 + 0.4e6 * 0.02, rel=0.02)


def test_continuous_capture_is_left_running(continuous):
    spec, device = continuous
    assert device.calls.count("captureStart") == 2                           # the second one threw and taught the driver
    assert "SynchronizationLockException" in str(spec.diagnostics[h6.M_CAPTURE_START])
    spec.abort_on_saturation = False
    assert spec.measure(ncy=4) == "OK" and spec.wait_for_measurement() == "OK"
    assert spec.ncy_handled == 4 and device.calls.count("captureStart") == 2  # no further starts
    assert device.capturing


# --------------------------------------------------------------------------- acquisition

def test_measure_averages_consecutive_frames(spec, device):
    spec.abort_on_saturation = False
    assert spec.set_it(5.0) == "OK"
    assert spec.measure(ncy=5) == "OK"
    assert spec.wait_for_measurement() == "OK"
    assert spec.ncy_handled == 5
    assert device.frame_counter == 2 + 5                    # probe frames + measured frames, one start each
    assert int(np.argmax(spec.rcm)) == device.peak_pixel
    assert spec.rcm.max() == pytest.approx(700 + 0.4e6 * 0.005, rel=0.02)
    assert spec.rcs.max() < 20.0                            # noise, not frame duplication
    assert device.calls.count("captureStart") == 7 and device.calls.count("captureStop") == 0


def test_frame_timeout_reports_codes(spec, device):
    device.never_ready = True
    spec.frame_timeout_s = 0.2
    spec.measure(ncy=1)
    result = spec.wait_for_measurement()
    assert result.startswith("Acquisition failed: No new frame within 0.2 s")
    assert f"getImageData codes {{{NO_FRAME}: " in result and "captureStart=0" in result
    assert "captureStatus=1" in result and "capture mode 0 of supported 32" in result and "diagnose_c13016.py" in result
    assert not device.capturing                             # stopped so the next attempt starts afresh


def test_request_stop_unblocks_waiter(spec, device):
    device.cycle_s = 0.3
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


def test_capture_start_race_is_tolerated(continuous):
    spec, device = continuous
    device.stop_ineffective = True                          # captureStop returns 0 but the board keeps capturing
    spec.abort()
    assert device.capturing
    assert spec.measure(ncy=2) == "OK" and spec.wait_for_measurement() == "OK"
    assert "raised" in str(spec.diagnostics[h6.M_CAPTURE_START])
    assert spec.ncy_handled == 2


# --------------------------------------------------------------------------- discovery + environment

def test_enumerate_devices_lists_connection_list_entries():
    device = FakeMicroUsb2Clr(module_ids=(4, 5))
    found = h6.enumerate_devices("dummy.dll", api_factory=lambda _path: h6.MicroUsb2Api(device))
    assert [d["serial"] for d in found] == ["C13016-1", "C13016-2"]
    assert device.calls == ["initialize", "uninitialize"]  # nothing left open after discovery


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
