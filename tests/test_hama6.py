"""Hama6 (C13016 via the MICRO_USB2_CLR .NET assembly) against a reflection-style fake.

The fake reproduces the assembly's real public API as reflected on the bench
(``Int64 id`` handles from a connection list, seconds as ``Double``, ``ref``
out-parameters, ``ref UInt16[]`` image buffers) with .NET-like ``MethodInfo`` /
``ParameterInfo`` objects, so the reflective argument preparation, the
initialize/open/captureStart/getImageData workflow and the "new frame"
detection are all exercised without pythonnet.
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
NOT_READY = -1
BUSY = 6          # what the bench board answered on every getImageData poll while capturing


class FakeMicroUsb2Clr:
    """Emulates MICRO_USB2_CLR: the real signatures, plausible behaviour, unknown-polarity codes."""

    def __init__(self, module_ids=(7,), pixel=288, peak_pixel=120, exposure_us=10000, unit="us"):
        self.module_ids = list(module_ids)
        self.pixel = pixel
        self.peak_pixel = peak_pixel
        self.exposure_s = exposure_us / 1e6
        self.unit_scale = 1e6 if unit == "us" else 1.0      # the bench board works in microseconds
        self.rng = np.random.default_rng(11)
        self.calls = []
        self.initialized = False
        self.opened = None
        self.capturing = False
        self.ready_at = 0.0
        self.frame_counter = 0
        self.never_ready = False
        self.deliver_after_stop = False        # frame readable only once captureStop was called
        self.frame_pending = False
        self.mode = 0
        self.working_modes = None              # None: frames in any mode; else the set of modes that produce frames
        self.cycle = 0.0
        self.argument_types = []
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
        args[1], args[2], args[3], args[4], args[5], args[6] = 11e-6 * s, 1.0 * s, 1e-6 * s, 0.0, 10.0 * s, 1e-3 * s
        return OK

    def _set_exposure(self, args):
        self.argument_types.append(type(args[1]).__name__)
        self.exposure_s = float(args[1]) / self.unit_scale
        return OK

    def _get_exposure(self, args):
        args[1] = self.exposure_s * self.unit_scale
        return OK

    def _coefficient(self, args):
        if int(args[1]) == 0 and len(args[2]) >= 6:
            for i, value in enumerate([310.0, 2.65, -1.1e-3, 0.0, 0.0, 0.0]):
                args[2][i] = value
            return OK
        return -5

    def _capture_start(self, args):
        self.calls.append("captureStart")
        self.capturing = True
        self.ready_at = time.monotonic() + self.exposure_s
        return OK

    def _capture_stop(self, args):
        self.calls.append("captureStop")
        if self.capturing and time.monotonic() >= self.ready_at:
            self.frame_pending = True
        self.capturing = False
        return OK

    def _capture_status(self, args):
        return 1 if self.capturing else 0

    def _get_mode(self, args):
        args[1] = self.mode
        return OK

    def _set_mode(self, args):
        self.mode = int(args[1])
        return OK

    def _get_cycle(self, args):
        args[1] = self.cycle
        return 12 if args[2] == "" else OK       # key-protected on the bench: 12 with an empty key

    def _trigger_output(self, args):
        args[1] = 0
        return OK

    def _trigger_polarity(self, args):
        args[1] = 1
        return OK

    def _image_data(self, args):
        if self.never_ready:
            return NOT_READY                        # buffer left untouched (zeros)
        if self.working_modes is not None and self.mode not in self.working_modes:
            return BUSY                             # what the bench board did in its start-up mode 0
        if self.deliver_after_stop:
            if self.capturing:
                return BUSY
            if not self.frame_pending:
                return NOT_READY
            self.frame_pending = False
        elif not self.capturing or time.monotonic() < self.ready_at:
            return NOT_READY
        buffer = args[1]
        assert len(buffer) == self.pixel, "getImageData must get a pixel-sized buffer"
        px = np.arange(self.pixel, dtype=float)
        frame = 700.0 + 0.4e6 * self.exposure_s * np.exp(-0.5 * ((px - self.peak_pixel) / 2.5) ** 2) + self.rng.normal(0, 3.0, px.size)
        for i, value in enumerate(np.clip(np.rint(frame), 0, 65535)):
            buffer[i] = int(value)
        self.frame_counter += 1
        args[2][0] = self.frame_counter * 1000      # single-shot: one frame per captureStart
        self.capturing = False
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
    assert spec.sn == "326VA021" and spec.name == "Hamamatsu C13016 micro-spectrometer"   # NUL padding stripped
    assert spec.module_info["firmware"] == "1.08"
    assert spec.capture_modes_supported == 32 and spec.capture_mode == 0 and spec.exposure_cycle == 0.0
    assert spec.diagnostics["capture_state"]["trigger"] == {"output": 0, "polarity": 1}
    assert spec.diagnostics["exposure_property"]["cycle"] == {"lower": 0.0, "upper": 10.0 * 1e6, "step": 1e-3 * 1e6}
    assert not spec._deliver_after_stop
    assert spec.npix_active == 288 and spec.nbits == 16 and spec.eff_saturation_limit == 65535.0
    assert spec.exposure_unit == "us"
    assert spec.min_it_ms == pytest.approx(0.011) and spec.max_it_ms == pytest.approx(1000.0)
    assert spec.it_step_ms == pytest.approx(0.001)
    assert spec.calibration_coefficients == [310.0, 2.65, -1.1e-3, 0.0, 0.0, 0.0]
    assert spec.wavelengths_nm[0] == pytest.approx(310.0 + 2.65 - 1.1e-3)
    assert spec.it_ms == pytest.approx(10.0)
    assert spec.rcm.size == 288 and not device.capturing      # probe frames taken, capture stopped
    assert device.frame_counter == 2                          # a single frame could be a stale buffer
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


def test_missing_method_reports_api_and_writes_dump():
    device = FakeMicroUsb2Clr()
    device._type._methods = [m for m in device._type._methods if m.Name != h6.M_IMAGE]
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    result = backend.connect()
    assert "lacks MICRO_USB2_getImageData" in result and str(h6.API_DUMP_FILE) in result
    assert "MICRO_USB2_captureStart(Int64 id) -> Int64" in h6.API_DUMP_FILE.read_text(encoding="utf-8")


def test_disconnect_stops_closes_and_uninitializes(spec, device):
    spec.disconnect()
    assert device.opened is None and not device.initialized and not device.capturing
    assert device.calls[-2:] == ["close:7", "uninitialize"]


# --------------------------------------------------------------------------- parameters

def test_set_it_converts_to_the_device_unit(spec, device):
    assert spec.set_it(250.0) == "OK"
    assert device.exposure_s == pytest.approx(0.25) and spec.it_ms == pytest.approx(250.0)
    assert spec.set_it(0.001) == "OK"                       # clamped to the 11 us floor
    assert device.exposure_s == pytest.approx(11e-6)


def test_seconds_based_exposure_api_is_detected():
    device = FakeMicroUsb2Clr(unit="s")
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    assert backend.connect() == "OK", backend.error
    assert backend.exposure_unit == "s" and backend.min_it_ms == pytest.approx(0.011)
    assert backend.set_it(20.0) == "OK" and device.exposure_s == pytest.approx(0.02)
    backend.disconnect()


def test_open_failure_code_is_reported():
    device = FakeMicroUsb2Clr()
    device.method(h6.M_OPEN)._func = lambda args: -100
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    result = backend.connect()
    assert "open(id=7) returned -100" in result and h6.LIBRARY.users == 0


def test_set_it_rejected_by_device_is_an_error(spec, device):
    device.method(h6.M_SET_EXPOSURE)._func = lambda args: -3   # device rejects the request
    result = spec.set_it(100.0)
    assert result == "Could not set integration time: setExposureTime(100000 us) returned -3"
    assert spec.it_ms == pytest.approx(10.0)


def test_silently_ignored_exposure_is_detected_by_readback(spec, device):
    device.method(h6.M_SET_EXPOSURE)._func = lambda args: OK    # says OK, keeps 10 ms
    result = spec.set_it(100.0)
    assert result.startswith("Could not set integration time: device kept 10.0000 ms")


# --------------------------------------------------------------------------- acquisition

def test_measure_averages_new_frames_only(spec, device):
    spec.abort_on_saturation = False
    assert spec.set_it(5.0) == "OK"
    assert spec.measure(ncy=5) == "OK"
    assert spec.wait_for_measurement() == "OK"
    assert spec.ncy_handled == 5
    assert device.frame_counter == 7                        # 2 probe + 5 measured, each a distinct frame
    assert int(np.argmax(spec.rcm)) == device.peak_pixel
    assert spec.rcm.max() == pytest.approx(700 + 0.4e6 * 0.005, rel=0.02)
    assert spec.rcs.max() < 20.0                            # noise, not frame duplication
    assert device.calls[-1] == "captureStop"


def test_frame_timeout_reports_codes(spec, device):
    device.never_ready = True
    spec.frame_timeout_s = 0.2
    spec.measure(ncy=1)
    result = spec.wait_for_measurement()
    assert result.startswith("Acquisition failed: No frame within 0.2 s")
    assert "captureStart=0" in result and f"getImageData codes {{{NOT_READY}: " in result
    assert "captureStatus=0" in result and "tried after captureStop: True" in result   # fallback was attempted
    assert "capture mode 0 of supported 32" in result and "diagnose_c13016.py" in result


def test_frames_readable_only_after_stop_are_learned():
    device = FakeMicroUsb2Clr()
    device.deliver_after_stop = True
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    assert backend.connect() == "OK", backend.error          # probe frame arrives via the fallback
    assert backend._deliver_after_stop
    backend.abort_on_saturation = False
    backend.set_it(5.0)
    assert backend.measure(ncy=3) == "OK"
    assert backend.wait_for_measurement() == "OK"
    assert backend.ncy_handled == 3 and device.frame_counter == 5
    assert device.calls.count("captureStart") == 5 and device.calls.count("captureStop") == 5
    backend.disconnect()


def test_connect_switches_to_an_advertised_capture_mode_when_needed():
    device = FakeMicroUsb2Clr()
    device.working_modes = {32}                             # frames only in the advertised mode, not in mode 0
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    backend.frame_timeout_s = 0.1
    assert backend.connect() == "OK", backend.error
    assert device.mode == 32 and backend.capture_mode == 32 and backend.diagnostics["capture_mode_used"] == 32
    assert backend.measure(ncy=2) == "OK" and backend.wait_for_measurement() == "OK"
    backend.disconnect()


def test_connect_restores_the_mode_when_no_mode_yields_frames():
    device = FakeMicroUsb2Clr()
    device.working_modes = set()
    backend = h6.Hama6_Spectrometer(api=h6.MicroUsb2Api(device))
    backend.frame_timeout_s = 0.1
    result = backend.connect()
    assert "no frame in capture mode 0, nor in modes [32, 1, 2, 3, 4, 8, 16, 64]" in result
    assert "Unplug and re-plug" in result and f"getImageData codes {{{BUSY}: " in result
    assert device.mode == 0 and device.opened is None and h6.LIBRARY.users == 0


def test_request_stop_unblocks_waiter(spec, device):
    spec.set_it(300.0)
    spec.measure(ncy=20)
    threading.Timer(0.15, spec.request_stop).start()
    t0 = time.monotonic()
    result = spec.wait_for_measurement()
    assert time.monotonic() - t0 < 2.0
    assert result == "Measurement stopped by user"
    assert not device.capturing


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
