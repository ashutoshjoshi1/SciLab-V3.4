"""Hamamatsu micro-series backend ("Hama6"): C13016 evaluation circuit and C13985 module.

The C13016 is Hamamatsu's USB evaluation circuit for the fingertip micro-
spectrometer heads (C12880MA, C16767MA); the C13985 is the same head with the
circuit built in. Hamamatsu's PC software is ``u-ApsSpecEvaluation.exe`` with
``MICRO_USB2_CLR.dll``, a .NET assembly (class ``MICRO_USB2_CLR``, every method
returning an ``Int64`` ``Cusb2Err`` status) loaded here through *pythonnet*.
The "Minispectrometer H-micro edition" download ships the assembly, the WinUSB
driver (``VID_0661&PID_2936``, "C13016 USB2(HPK)"), the function specification
K29-B61584B and console samples in VB.NET and C++/CLI; this driver follows the
samples' sequence and was verified against the hardware on 2026-09-18
(C13016 serial 326VA021, firmware 1.08, head 26C00021 / S11639):

1. ``initialize``; ``getModuleConnectionList(Int16[] list, ref UInt16 number)``
   (``number`` is an output: the sample passes 0); ``open(id)``.
2. ``setCaptureMode(id, 0)``: mode 0 is the documented "specified number of
   measurement" mode, one frame per ``captureStart``. Mode 2 waits for an
   external trigger (no frame without one, ``getImageData`` echoes an all-zero
   buffer with code 0). Mode 1 exists only in the assembly's enum: it delivers
   one frame and the next ``setCaptureMode`` then blocks inside the DLL until
   the process is killed (the board itself recovers), so it is never selected.
3. ``setExposureTime(id, us)``: **microseconds** (parameter named ``sec``),
   11 .. 1,000,000 per ``getExposurePropertyForLabVIEW``; 31 underTime / 30
   overTime outside. The DLL sets the exposure *cycle* (frame period) itself to
   exposure + 2,000 us. Setting the cycle through the key-protected
   ``setExposureCycle`` (as earlier versions did, exposure + 1,032 us) starved
   the board of frames in every mode: it is not touched any more.
4. Per frame: ``captureStart``; poll ``getCaptureStatus`` until it answers 0
   ("captured frames exist"; 1 while none, 4 on data loss); ``getImageData``
   (code 0 with the 288 counts and a timestamp; the buffer is consumed and the
   next call answers 6 notImage); ``captureStop``. A frame is ready about one
   cycle after the start and a start/read/stop round trip costs about 26 ms,
   dominated by ``captureStart`` (about 13 ms). A ready frame waits unread
   without loss. Timestamps repeat between frames, so they never identify a
   new frame: consumption does.
5. ``close``; ``uninitialize``.

Every DLL call runs on one worker thread with a watchdog
(``DLL_CALL_TIMEOUT_S``): a call that does not return is reported as an error
instead of freezing the application, since the DLL can deadlock (mode 1
above; a second ``captureStart`` while a capture ran on the bench). Calls go
through .NET reflection (``MethodInfo.Invoke``) with every scalar boxed inside
the CLR: pythonnet 3 turns a Python ``int`` stored into an ``object[]`` into a
``Python.Runtime.PyInt`` wrapper that Invoke cannot convert to ``Int64``, and
its own overload resolution rejects ``ref`` parameters outright.

Wavelengths come from ``getCalibrationCoefficient`` (index 0: A0, B1..B5 with
p = 1..288) or, when an operator file ``hama6_wavelength_coefficients.json``
next to the DLL has an entry for the head serial, from that file (the board
on the bench keeps a 320 .. 888 nm polynomial while its head is a UV
C16767MA-20; the file wins).
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from spectrometers.spec_xfus import measurement_timeout_s, spec_clock

LOGGER = logging.getLogger(__name__)

DLL_NAME = "MICRO_USB2_CLR.dll"
CLASS_NAME = "MICRO_USB2_CLR"
API_DUMP_FILE = Path(__file__).resolve().parent / "micro_usb2_api_dump.txt"  # written when Connect fails
# Where Hamamatsu's installer puts the assembly; the loader falls back to it when the DLL is not next to the others.
VENDOR_INSTALL_DLL = Path(r"C:\Program Files\HamamatsuMinispectrometer for C13016\EvaluationSoftware") / DLL_NAME

DEFAULT_PIXEL_COUNT = 288
DEFAULT_BITS = 16
MIN_IT_MS = 0.011        # 11 us (C12880MA datasheet; getExposurePropertyForLabVIEW lower limit)
MAX_IT_MS = 1000.0       # 1 s (upper limit)
DEFAULT_IT_MS = 10.0
US_PER_MS = 1000.0       # the exposure API works in microseconds (function specification K29-B61584B)
OK_CODE = 0
COEFFICIENT_COUNT = 6    # wavelength = A0 + B1*p + ... + B5*p^5, p = 1..npix
# Operator-supplied wavelength polynomial, next to the DLL:
# {"<head serial>": [A0, B1, B2, B3, B4, B5]} or {"<head serial>": {"model": "C16767MA-20", "coefficients": [...]}},
# keyed by the head serial (getSpectroInformationForLabVIEW), the module serial or "default".
COEFFICIENTS_FILE_NAME = "hama6_wavelength_coefficients.json"
MAX_MODULES = 8          # "maximum 8 units can be connected to 1 PC" (K29-B61584B); the samples allocate 8 slots
TIMESTAMP_SLOTS = 1      # UInt64[] handed to getImageData; the DLL writes slot 0 (the samples allocate one)
POLL_MIN_S = 0.001
POLL_MAX_S = 0.02
FRAME_TIMEOUT_MIN_S = 2.0
FRAME_OVERHEAD_S = 0.03            # start/status/read/stop round trip beyond the exposure cycle (about 26 ms measured)
DLL_CALL_TIMEOUT_S = 15.0          # a DLL call that has not returned by then is reported instead of freezing Connect
FRAME_RETRIES = 3                  # captures restarted after a data-loss status or an unusable frame

# Usb2Struct.Usb2CaptureMode. The function specification documents 0 and 2 only; 1 hangs the DLL (see module docstring).
CAPTURE_MODE_SINGLE_SHOT = 0
CAPTURE_MODE_FREE_RUN = 1
CAPTURE_MODE_TRIGGER = 2
CAPTURE_MODE_NAMES = {0: "single shot", 1: "free run (undocumented)", 2: "external trigger", 3: "edge"}

# Every key-protected method of the assembly compares its `strCheck` argument with this literal (from the IL of
# MICRO_USB2_getExposureCycle and friends). The driver only *reads* with it (exposure cycle, wavelength range).
CHECK_KEY = "ichino"

# Usb2Struct.Cusb2Err: the Int64 status every method returns (K29-B61584B, "Return Code").
ERROR_NAMES = {
    0: "success", 1: "unsuccess", 2: "notList", 3: "notAccess", 4: "notFrame", 5: "notAcPower", 6: "notImage",
    7: "notBuffer", 8: "notData", 9: "notDownload", 10: "invalidDeviceIndex", 11: "invalidMode", 12: "invalidParameter",
    13: "invalidTriggermode", 14: "invalidTriggerpolarity", 15: "invalidTriggeroutput", 16: "invalidBinning",
    17: "invalidPosition", 18: "invalidSize", 19: "invalidGain", 20: "invalidOffset", 21: "invalidCoolingstatus",
    22: "invalidFrame", 23: "invalidEventID", 24: "invalidEventhandle", 25: "invalidData", 26: "invalidIndex",
    27: "invalidNumber", 28: "invalidAddress", 29: "invalidLength", 30: "overTime", 31: "underTime",
    32: "overTemperature", 33: "underTemperature", 34: "overArea", 35: "nowCapturing", 36: "nowFirmUpdate",
    37: "initialized", 38: "opened", 39: "existDevice", 40: "FirmCorrupted", 41: "ReadImageStatus",
    42: "InvalidEepromParameter",
}
ERR_UNSUCCESS = 1          # getCaptureStatus: no captured frame yet
ERR_NOT_FRAME = 4          # getCaptureStatus: data loss; stop and restart the capture
ERR_NOT_IMAGE = 6          # getImageData: the buffer holds no (new) frame
ERR_INVALID_MODE = 11
ERR_INVALID_PARAMETER = 12
ERR_INVALID_DATA = 25      # getImageData: frame retrieved but flagged invalid by the header
ERR_OVER_TIME = 30
ERR_UNDER_TIME = 31
ERR_NOW_CAPTURING = 35

# Method names exactly as exported by MICRO_USB2_CLR.
M_INIT = "MICRO_USB2_initialize"
M_UNINIT = "MICRO_USB2_uninitialize"
M_LIST = "MICRO_USB2_getModuleConnectionList"
M_OPEN = "MICRO_USB2_open"
M_CLOSE = "MICRO_USB2_close"
M_INFO = "MICRO_USB2_getModuleInformationForLabVIEW"
M_PROPERTY = "MICRO_USB2_getModulePropertyForLabVIEW"
M_EXPOSURE_PROPERTY = "MICRO_USB2_getExposurePropertyForLabVIEW"
M_SET_EXPOSURE = "MICRO_USB2_setExposureTime"
M_GET_EXPOSURE = "MICRO_USB2_getExposureTime"
M_COEFFICIENT = "MICRO_USB2_getCalibrationCoefficient"
M_CAPTURE_START = "MICRO_USB2_captureStart"
M_CAPTURE_STOP = "MICRO_USB2_captureStop"
M_CAPTURE_STATUS = "MICRO_USB2_getCaptureStatus"
M_GET_CAPTURE_MODE = "MICRO_USB2_getCaptureMode"
M_SET_CAPTURE_MODE = "MICRO_USB2_setCaptureMode"
M_GET_CYCLE = "MICRO_USB2_getExposureCycle"                          # (Int64 id, ref Double sec, String strCheck)
M_IMAGE = "MICRO_USB2_getImageData"
M_TRIGGER_OUTPUT = "MICRO_USB2_getTriggerOutput"
M_TRIGGER_POLARITY = "MICRO_USB2_getTriggerPolarity"
M_SPECTRO_INFO = "MICRO_USB2_getSpectroInformationForLabVIEW"        # (Int64 id, ref String serial, ref String sensor): the head
M_WAVELENGTH_PROPERTY = "MICRO_USB2_getWaveLengthPropertyForLabVIEW"  # (Int64 id, ref Int64 lower, ref Int64 upper, String strCheck)
REQUIRED_METHODS = (M_INIT, M_LIST, M_OPEN, M_CLOSE, M_SET_EXPOSURE, M_CAPTURE_START, M_CAPTURE_STATUS, M_IMAGE, M_CAPTURE_STOP)


def code_text(code) -> str:
    """'6 (notImage)' for a Cusb2Err status code."""
    try:
        return f"{int(code)} ({ERROR_NAMES.get(int(code), 'unknown')})"
    except (TypeError, ValueError):
        return str(code)


def mode_text(mode) -> str:
    """'0 (single shot)' for a Usb2CaptureMode value."""
    try:
        return f"{int(mode)} ({CAPTURE_MODE_NAMES.get(int(mode), 'unknown')})"
    except (TypeError, ValueError):
        return str(mode)


def wavelengths_from_coefficients(coefficients: Sequence[float], npix: int) -> np.ndarray:
    """A0 + B1*p + ... + B5*p^5 with p = 1..npix (Hamamatsu's pixel numbering)."""
    pixels = np.arange(1, int(npix) + 1, dtype=float)
    result = np.zeros_like(pixels)
    for power, coefficient in enumerate(coefficients):
        result += float(coefficient) * pixels ** power
    return result


def plausible_coefficients(coefficients: Sequence[float]) -> bool:
    """Loose sanity check for a C12880MA/C16767MA wavelength polynomial (A0 in nm, B1 in nm/pixel)."""
    if len(coefficients) < 2:
        return False
    a0, a1 = float(coefficients[0]), float(coefficients[1])
    return bool(np.isfinite(a0) and np.isfinite(a1) and 100.0 <= a0 <= 2000.0 and 0.0 < abs(a1) < 20.0)


def _is_array_like(value) -> bool:
    if value is None or isinstance(value, (str, bytes, bool, int, float)):
        return False
    try:
        len(value)
        iter(value)
        return True
    except TypeError:
        return False


def _to_float_array(value) -> np.ndarray:
    """Convert a .NET array / list into float64 (empty for None)."""
    if value is None:
        return np.array([], dtype=float)
    if isinstance(value, np.ndarray):
        return value.astype(float, copy=False)
    if _is_array_like(value):
        return np.asarray([float(v) for v in value], dtype=float)
    return np.asarray([float(value)], dtype=float)


def _text(value) -> str:
    """Decode a fixed-width, NUL-padded .NET string buffer."""
    if value is None:
        return ""
    try:
        return str(value).replace("\x00", "").strip()
    except Exception:
        return ""


def _first_line(exc: BaseException) -> str:
    return str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__


# ---------------------------------------------------------------------------
# Reflection model (works for real .NET MethodInfo objects and for test fakes
# that expose the same attribute names)
# ---------------------------------------------------------------------------

_INTEGER_TYPES = {"Byte", "SByte", "Int16", "UInt16", "Int32", "UInt32", "Int64", "UInt64"}
_FLOAT_TYPES = {"Single", "Double", "Decimal"}


class Buf:
    """Marker: allocate an array parameter of this length for the callee to fill."""

    def __init__(self, length: int) -> None:
        self.length = int(length)


class _Out:
    """Marker for a ``ref``/``out`` scalar the callee writes (a default value is passed in)."""

    def __repr__(self) -> str:
        return "OUT"


OUT = _Out()


@dataclass
class ParamSpec:
    name: str
    type_name: str            # element type name for arrays/byref, e.g. "UInt16", "Double", "String"
    is_out: bool = False
    is_byref: bool = False
    is_array: bool = False
    clr_type: Any = None      # the real System.Type when available


@dataclass
class Overload:
    name: str
    params: List[ParamSpec]
    return_type: str
    method_info: Any
    is_static: bool = False

    def signature(self) -> str:
        parts = []
        for p in self.params:
            prefix = "out " if p.is_out else ("ref " if p.is_byref else "")
            parts.append(f"{prefix}{p.type_name}{'[]' if p.is_array else ''} {p.name}")
        return f"{self.name}({', '.join(parts)}) -> {self.return_type}"


@dataclass
class CallResult:
    result: Any                                  # the method's return value (Int64 status for this assembly)
    slots: List[Any] = field(default_factory=list)   # every argument after the call (ref/out/buffers filled)
    overload: Optional[Overload] = None


def _type_name(clr_type) -> Tuple[str, bool, bool]:
    """(element type name, is_byref, is_array) for a System.Type or fake."""
    is_byref = bool(getattr(clr_type, "IsByRef", False))
    if is_byref:
        clr_type = clr_type.GetElementType()
    is_array = bool(getattr(clr_type, "IsArray", False))
    element = clr_type.GetElementType() if is_array else clr_type
    return str(getattr(element, "Name", element)), is_byref, is_array


def reflect_overloads(target: Any) -> Dict[str, List[Overload]]:
    """Public methods of ``target`` (instance or static class) via .NET-style reflection."""
    reflected_type = None
    if hasattr(target, "GetMethods") and hasattr(target, "IsClass"):
        reflected_type = target                      # a System.Type: static class
    elif hasattr(target, "GetType"):
        try:
            reflected_type = target.GetType()
        except Exception:
            reflected_type = None
    if reflected_type is None or not hasattr(reflected_type, "GetMethods"):
        return {}
    overloads: Dict[str, List[Overload]] = {}
    try:
        methods = list(reflected_type.GetMethods())
    except Exception:
        return {}
    for method in methods:
        name = str(method.Name)
        if name in {"GetType", "ToString", "Equals", "GetHashCode", "ReferenceEquals", "MemberwiseClone", "Finalize"}:
            continue
        params = []
        for p in method.GetParameters():
            element_name, is_byref, is_array = _type_name(p.ParameterType)
            params.append(ParamSpec(
                name=str(p.Name), type_name=element_name, is_out=bool(getattr(p, "IsOut", False)),
                is_byref=is_byref, is_array=is_array, clr_type=p.ParameterType,
            ))
        return_type = str(getattr(getattr(method, "ReturnType", None), "Name", "Void"))
        overloads.setdefault(name, []).append(
            Overload(name=name, params=params, return_type=return_type, method_info=method,
                     is_static=bool(getattr(method, "IsStatic", False)))
        )
    return overloads


def _clr_type_of(spec: ParamSpec):
    """The System.Type a value for this parameter must have (None outside the CLR / for fakes)."""
    try:
        import System  # pythonnet
    except ImportError:
        return None
    if spec.clr_type is None or not isinstance(spec.clr_type, System.Type):
        return None
    return spec.clr_type.GetElementType() if spec.is_byref else spec.clr_type


def _default_scalar(spec: ParamSpec):
    if spec.type_name in _INTEGER_TYPES:
        return 0
    if spec.type_name in _FLOAT_TYPES:
        return 0.0
    if spec.type_name == "Boolean":
        return False
    return None            # String / struct: null, the callee assigns


def _coerce_scalar(value, spec: ParamSpec):
    """Bring a Python value to the Python shape of the declared type; .NET objects pass through."""
    if not isinstance(value, (int, float, bool, str)):
        return value
    if spec.type_name in _INTEGER_TYPES:
        return int(round(float(value)))
    if spec.type_name in _FLOAT_TYPES:
        return float(value)
    if spec.type_name == "Boolean":
        return bool(value)
    if spec.type_name == "String":
        return str(value)
    return value


def _new_array(spec: ParamSpec, length: int, values: Optional[Sequence] = None):
    """Allocate (or fill) an array of the parameter's element type."""
    array_type = _clr_type_of(spec)
    if array_type is not None:
        import System  # pythonnet

        element = array_type.GetElementType()
        array = System.Array.CreateInstance(element, int(length if values is None else len(values)))
        if values is not None:
            for i, v in enumerate(values):
                array[i] = v          # typed indexer: pythonnet converts to the element type
        return array
    if values is not None:
        return list(values)
    fill = 0.0 if spec.type_name in _FLOAT_TYPES else 0
    return [fill] * int(length)


def _prepare_arguments(overload: Overload, args: Sequence[Any]) -> List[Any]:
    if len(args) != len(overload.params):
        raise TypeError(f"{overload.signature()} takes {len(overload.params)} argument(s), {len(args)} given")
    prepared: List[Any] = []
    for spec, value in zip(overload.params, args):
        if spec.is_array:
            if isinstance(value, Buf):
                prepared.append(_new_array(spec, value.length))
            elif value is OUT or value is None:
                prepared.append(None)
            else:
                prepared.append(_new_array(spec, 0, list(value)))
        elif value is OUT or value is None:
            prepared.append(_default_scalar(spec))
        else:
            prepared.append(_coerce_scalar(value, spec))
    return prepared


def _box_arguments(overload: Overload, args: List[Any]):
    """Build the object[] for MethodInfo.Invoke with every scalar boxed *inside* the CLR.

    Writing the value into a one-element array of the declared type and copying
    that element into the object[] keeps the boxed value native; assigning a
    Python int to an object slot would store a Python.Runtime.PyInt instead.
    """
    import System  # pythonnet

    boxed = System.Array.CreateInstance(System.Object, len(args))
    for index, (spec, value) in enumerate(zip(overload.params, args)):
        if value is None:
            continue
        param_type = _clr_type_of(spec)
        if param_type is not None and isinstance(value, (int, float, bool, str)):
            holder = System.Array.CreateInstance(param_type, 1)
            holder[0] = value
            System.Array.Copy(holder, 0, boxed, index, 1)
        else:
            boxed[index] = value      # .NET array or object: stored as-is
    return boxed


def _is_clr_method(overload: Overload) -> bool:
    try:
        from System.Reflection import MethodBase  # pythonnet
    except ImportError:
        return False
    return isinstance(overload.method_info, MethodBase)


def _invoke(overload: Overload, target: Any, args: List[Any]) -> Tuple[Any, List[Any]]:
    """MethodInfo.Invoke with a CLR-boxed object[] (real .NET) or a plain list (fake)."""
    instance = None if overload.is_static else target
    if _is_clr_method(overload):
        boxed = _box_arguments(overload, args)
        result = overload.method_info.Invoke(instance, boxed)
        return result, [boxed[i] for i in range(len(args))]
    result = overload.method_info.Invoke(instance, args)   # fake: mutates `args` in place
    return result, list(args)


class MicroUsb2Api:
    """Reflection-based access to the loaded ``MICRO_USB2_CLR`` object (or a test fake)."""

    def __init__(self, target: Any) -> None:
        self.target = target
        self.overloads = reflect_overloads(target)
        if not self.overloads:
            raise RuntimeError(f"{DLL_NAME}: could not reflect any public method of {target!r}")
        self.public_methods = sorted(self.overloads)
        self.last_codes: Dict[str, Any] = {}

    def has(self, name: str) -> bool:
        return name in self.overloads

    def describe(self) -> str:
        return "; ".join(ov.signature() for name in self.public_methods for ov in self.overloads[name])

    def call(self, name: str, *args) -> CallResult:
        overloads = self.overloads.get(name)
        if not overloads:
            raise RuntimeError(f"{DLL_NAME} has no method {name}")
        overload = next((o for o in overloads if len(o.params) == len(args)), overloads[0])
        prepared = _prepare_arguments(overload, args)
        result, slots = _invoke(overload, self.target, prepared)
        self.last_codes[name] = result
        return CallResult(result=result, slots=slots, overload=overload)


def load_clr_assembly(dll_path: str):
    """Load MICRO_USB2_CLR.dll with pythonnet and return the device class (instance or static type)."""
    try:
        import clr  # pythonnet
    except ImportError as exc:
        raise RuntimeError(
            "pythonnet is required for the C13016/Hama6 backend (pip install pythonnet). "
            f"{DLL_NAME} is a .NET assembly and cannot be loaded with ctypes."
        ) from exc
    path = Path(dll_path)
    if not path.is_file():
        raise RuntimeError(f"{DLL_NAME} not found at '{dll_path}'. Copy it from Hamamatsu's u-ApsSpecEvaluation folder "
                           f"({VENDOR_INSTALL_DLL.parent}).")
    clr.AddReference(str(path.with_suffix("")))
    import System  # noqa: E402  (available once the CLR is loaded)
    from System.Reflection import Assembly  # noqa: E402
    assembly = Assembly.LoadFrom(str(path))
    public_types = [t for t in assembly.GetTypes() if t.IsPublic and t.IsClass]
    if not public_types:
        raise RuntimeError(f"{DLL_NAME} exposes no public classes.")
    chosen = next((t for t in public_types if t.Name == CLASS_NAME), None)
    if chosen is None:
        chosen = max(public_types, key=lambda t: sum(1 for m in t.GetMethods() if str(m.Name).startswith("MICRO_USB2_")))
    LOGGER.info("%s: public classes %s; using %s", DLL_NAME, [t.FullName for t in public_types], chosen.FullName)
    if chosen.IsAbstract and chosen.IsSealed:
        return chosen  # static class: bind to the type itself
    try:
        return System.Activator.CreateInstance(chosen)
    except Exception as exc:
        LOGGER.info("%s: %s has no parameterless constructor (%s); using static methods", DLL_NAME, chosen.FullName, exc)
        return chosen


class _Library:
    """Process-wide initialize/uninitialize reference count for the assembly."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.users = 0

    def acquire(self, invoke: Callable[[str], Any]) -> None:
        with self.lock:
            if self.users == 0:
                invoke(M_INIT)
            self.users += 1

    def release(self, invoke: Callable[[str], Any]) -> None:
        with self.lock:
            self.users = max(0, self.users - 1)
            if self.users == 0:
                try:
                    invoke(M_UNINIT)
                except Exception as exc:
                    LOGGER.debug("%s failed: %s", M_UNINIT, exc)


class _CallThread:
    """One thread per backend runs every DLL call, so a call that never returns is reported instead of freezing the app.

    The DLL can deadlock: ``setCaptureMode`` after a mode-1 capture never
    returned (2026-09-18, this PC), nor did a second ``captureStart`` while a
    capture ran (bench).
    """

    def __init__(self, name: str) -> None:
        self._queue: "queue.Queue" = queue.Queue()
        self.blocked_on: Optional[str] = None
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            fn, args, future = item
            try:
                future.set_result(fn(*args))
            except BaseException as exc:  # forwarded to the caller
                future.set_exception(exc)

    def run(self, fn: Callable, label: str, timeout: float, *args):
        if self.blocked_on:
            raise RuntimeError(f"{DLL_NAME} is blocked inside {self.blocked_on}; restart SciLab and re-plug the C13016")
        future: Future = Future()
        self._queue.put((fn, args, future))
        try:
            return future.result(timeout)
        except FutureTimeoutError:
            self.blocked_on = label
            raise RuntimeError(f"{label} did not return within {timeout:g} s: {DLL_NAME} is blocked; "
                               "restart SciLab and re-plug the C13016") from None

    def close(self) -> None:
        self._queue.put(None)


LIBRARY = _Library()


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class Hama6_Spectrometer:
    """SciLab backend for the C13016 evaluation circuit / C13985 module."""

    spec_type = "Hama6"

    def __init__(self, api: Optional[MicroUsb2Api] = None, api_factory: Optional[Callable[[str], MicroUsb2Api]] = None):
        # --- contract attributes ---
        self.sn = ""
        self.alias = "1"
        self.debug_mode = 0
        self.name = "Hamamatsu C13016 micro-spectrometer circuit"
        self.dll_path = ""
        self.npix_active = DEFAULT_PIXEL_COUNT
        self.npix_blind_left = 0
        self.npix_blind_right = 0
        self.nbits = DEFAULT_BITS
        self.eff_saturation_limit = float(2 ** DEFAULT_BITS - 1)
        self.abort_on_saturation = True
        self.simulation_mode = False
        self.measuring = False
        self.it_ms: Optional[float] = None
        self.error = "OK"
        self.rcm = np.array([], dtype=float)
        self.rcs = np.array([], dtype=float)
        self.ncy_requested = 0
        self.ncy_handled = 0
        self.ncy_saturated = 0
        self.meas_start_time = None

        self.min_it_ms = MIN_IT_MS
        self.max_it_ms = MAX_IT_MS
        self.it_step_ms: Optional[float] = None
        self.exposure_limits_us: Dict[str, float] = {}     # lower/upper/step and cycle lower/upper/step, microseconds
        self.exposure_cycle_us: Optional[float] = None     # frame period the DLL derived from the exposure (read back)
        self.exposure_cycle_s: Optional[float] = None
        self.frame_period_s: Optional[float] = None        # measured start-to-start period between consecutive frames
        self.calibration_coefficients: List[float] = []
        self.wavelengths_nm = np.array([], dtype=float)
        self.device_index = 0
        self.module_id: Optional[int] = None
        self.module_ids: List[int] = []
        self.module_info: Dict[str, str] = {}
        self.capture_mode: Optional[int] = None            # active mode (single shot once connected)
        self.board_capture_mode: Optional[int] = None      # the mode the board was found in at connect
        self.capture_mode_mask: Optional[int] = None       # `capturemode` field of the module property (32 on the bench)
        self.wavelength_range_nm: Optional[Tuple[int, int]] = None   # getWaveLengthPropertyForLabVIEW (key-protected read)
        self.frame_timeout_s: Optional[float] = None       # None: derived from the integration time
        self.diagnostics: Dict[str, Any] = {}              # method -> last status code / probed value

        self._api = api
        self._api_factory = api_factory
        self._connected = False
        self._library_held = False
        self._opened = False
        self._calls: Optional[_CallThread] = None          # one worker thread runs every DLL call (watchdog)
        self._capturing = False
        self._last_frame_at: Optional[float] = None
        self._io_lock = threading.RLock()
        self._done = threading.Event()
        self._done.set()
        self._abort = threading.Event()
        self._stop_requested = False
        self._worker: Optional[threading.Thread] = None
        self.logger = LOGGER

    def initialize_spec_logger(self) -> None:
        self.logger = logging.getLogger(f"spec{self.alias}.hama6")

    # -- API loading ---------------------------------------------------------------

    def _ensure_api(self) -> MicroUsb2Api:
        if self._api is None:
            if self._api_factory is not None:
                self._api = self._api_factory(self.dll_path)
            else:
                self._api = MicroUsb2Api(load_clr_assembly(self.dll_path))
        return self._api

    def load_spec_dll(self) -> str:
        try:
            self._ensure_api()
            return "OK"
        except Exception as exc:
            self.error = str(exc)
            return self.error

    def _record(self, method: str, value: Any) -> None:
        self.diagnostics[method] = value

    def _rc(self, method: str, *args) -> CallResult:
        """Call a method on the DLL worker thread (watchdog) under the I/O lock and remember its status code."""
        with self._io_lock:
            if self._calls is None:
                self._calls = _CallThread(f"hama6-dll-{self.alias}")
            call = self._calls.run(self._api.call, method.replace("MICRO_USB2_", ""), DLL_CALL_TIMEOUT_S, method, *args)
        self._record(method, call.result)
        return call

    def _invoke_library(self, method: str):
        """initialize / uninitialize through the same worker thread (None when the assembly lacks the method)."""
        if not self._api.has(method):
            return None
        return self._rc(method).result

    def _check_methods(self, required: Sequence[str]) -> None:
        missing = [m for m in required if not self._api.has(m)]
        if missing:
            raise RuntimeError(f"{DLL_NAME} lacks {', '.join(missing)}; this backend targets the C13016 evaluation assembly")

    # -- discovery -------------------------------------------------------------------

    def _connection_list(self) -> List[int]:
        """Module ids reported by getModuleConnectionList (`number` is an output; the samples pass 0)."""
        call = self._rc(M_LIST, Buf(MAX_MODULES), 0)
        if call.result != OK_CODE:
            raise RuntimeError(f"getModuleConnectionList answered {code_text(call.result)}")
        modules = call.slots[0]
        try:
            number = int(call.slots[1] or 0)
        except (TypeError, ValueError):
            number = 0
        number = max(0, min(number, MAX_MODULES, len(modules) if modules is not None else 0))
        ids = [int(modules[i]) for i in range(number) if int(modules[i]) >= 0]   # -1 marks an undetected slot
        self._record("module_ids", ids)
        return ids

    def get_number_of_devices(self):
        try:
            self._ensure_api()
            self._check_methods((M_INIT, M_LIST))
            LIBRARY.acquire(self._invoke_library)
            try:
                return "OK", len(self._connection_list())
            finally:
                LIBRARY.release(self._invoke_library)
        except Exception as exc:
            return str(exc), 0

    # -- module readout ----------------------------------------------------------------

    def _read_module_information(self) -> None:
        if not self._api.has(M_INFO):
            return
        call = self._rc(M_INFO, self.module_id, OUT, OUT, OUT, OUT, OUT, OUT)
        keys = ("vendor", "product", "serial", "firmware", "driver", "library")
        self.module_info = {key: _text(value) for key, value in zip(keys, call.slots[1:7])}
        if self.module_info.get("serial"):
            self.sn = self.module_info["serial"]
        if self._api.has(M_SPECTRO_INFO):
            try:  # the attached head: its serial ("unit") and sensor
                head = self._rc(M_SPECTRO_INFO, self.module_id, OUT, OUT)
                self.module_info["head_serial"] = _text(head.slots[1])
                self.module_info["sensor"] = _text(head.slots[2])
            except Exception as exc:
                self.logger.debug("%s failed: %s", M_SPECTRO_INFO, exc)
        self._update_name()
        self._record("module_info", dict(self.module_info))

    def _update_name(self) -> None:
        product = self.module_info.get("product") or "C13016"
        head = [self.module_info.get("head_model"),
                f"head {self.module_info['head_serial']}" if self.module_info.get("head_serial") else None,
                f"({self.module_info['sensor']})" if self.module_info.get("sensor") else None]
        head = [part for part in head if part]
        self.name = f"Hamamatsu {product} micro-spectrometer circuit" + (f" with {' '.join(head)}" if head else "")

    def _read_module_property(self) -> Tuple[int, int, Optional[int]]:
        if not self._api.has(M_PROPERTY):
            return 0, 0, None
        call = self._rc(M_PROPERTY, self.module_id, OUT, OUT, OUT)
        try:
            pixel, resolution, mask = (int(v or 0) for v in call.slots[1:4])
        except (TypeError, ValueError):
            pixel, resolution, mask = 0, 0, None
        self._record("module_property", {"pixel": pixel, "resolution": resolution, "capturemode": mask})
        return pixel, resolution, mask

    def _read_exposure_property(self) -> None:
        """Exposure and cycle limits in microseconds (K29-B61584B: "Unit is usec")."""
        if not self._api.has(M_EXPOSURE_PROPERTY):
            return
        call = self._rc(M_EXPOSURE_PROPERTY, self.module_id, OUT, OUT, OUT, OUT, OUT, OUT)
        try:
            lower, upper, step, cycle_lower, cycle_upper, cycle_step = (float(v or 0.0) for v in call.slots[1:7])
        except (TypeError, ValueError):
            return
        self.exposure_limits_us = {"lower": lower, "upper": upper, "step": step,
                                   "cycle_lower": cycle_lower, "cycle_upper": cycle_upper, "cycle_step": cycle_step}
        self._record("exposure_property", dict(self.exposure_limits_us))
        if call.result != OK_CODE or not (0.0 < lower < upper):
            return
        self.min_it_ms = lower / US_PER_MS
        self.max_it_ms = upper / US_PER_MS
        if step > 0.0:
            self.it_step_ms = step / US_PER_MS

    def _read_capture_mode(self) -> Optional[int]:
        if not self._api.has(M_GET_CAPTURE_MODE):
            return None
        call = self._rc(M_GET_CAPTURE_MODE, self.module_id, OUT)
        if call.result != OK_CODE or call.slots[1] is None:
            return None
        self.capture_mode = int(call.slots[1])
        return self.capture_mode

    def _read_exposure_cycle(self) -> Optional[float]:
        """The frame period the DLL keeps for the current exposure (key-protected read; informational)."""
        if not self._api.has(M_GET_CYCLE):
            return None
        try:
            call = self._rc(M_GET_CYCLE, self.module_id, OUT, CHECK_KEY)
        except Exception as exc:
            self.logger.debug("%s failed: %s", M_GET_CYCLE, exc)
            return None
        if call.result != OK_CODE or not call.slots[1]:
            return None
        self.exposure_cycle_us = float(call.slots[1])
        self.exposure_cycle_s = self.exposure_cycle_us / (US_PER_MS * 1000.0)
        return self.exposure_cycle_us

    def _read_capture_state(self) -> None:
        """Capture mode, exposure cycle, wavelength range and trigger settings, for the log and error messages."""
        self._read_capture_mode()
        self._read_exposure_cycle()
        if self._api.has(M_WAVELENGTH_PROPERTY):
            try:
                span = self._rc(M_WAVELENGTH_PROPERTY, self.module_id, OUT, OUT, CHECK_KEY)
                if span.result == OK_CODE and span.slots[1] is not None and span.slots[2] is not None:
                    self.wavelength_range_nm = (int(span.slots[1]), int(span.slots[2]))
            except Exception as exc:
                self.logger.debug("%s failed: %s", M_WAVELENGTH_PROPERTY, exc)
        trigger = {}
        for key, method in (("output", M_TRIGGER_OUTPUT), ("polarity", M_TRIGGER_POLARITY)):
            if self._api.has(method):
                try:
                    trigger[key] = self._rc(method, self.module_id, OUT).slots[1]
                except Exception as exc:
                    self.logger.debug("%s failed: %s", method, exc)
        self._record("capture_state", {"mode": self.capture_mode, "mask": self.capture_mode_mask,
                                       "exposure_cycle_us": self.exposure_cycle_us, "trigger": trigger,
                                       "wavelength_range_nm": self.wavelength_range_nm})

    def _select_single_shot_mode(self) -> None:
        """Put the board in mode 0 (one frame per captureStart), as Hamamatsu's samples do before the first start.

        The mode persists in the board. Changing it is safe while nothing is
        being captured (this driver stops any leftover capture right after
        open); after a mode-1 capture the same call deadlocks the DLL, which
        the watchdog turns into an error naming the call.
        """
        self.board_capture_mode = self.capture_mode
        if self.capture_mode == CAPTURE_MODE_SINGLE_SHOT:
            return
        if not self._api.has(M_SET_CAPTURE_MODE):
            raise RuntimeError(f"the board is in capture mode {mode_text(self.capture_mode)} and the assembly cannot change it")
        self.logger.info("Board found in capture mode %s; selecting %s", mode_text(self.capture_mode),
                         mode_text(CAPTURE_MODE_SINGLE_SHOT))
        code = self._rc(M_SET_CAPTURE_MODE, self.module_id, CAPTURE_MODE_SINGLE_SHOT).result
        if code != OK_CODE:
            raise RuntimeError(f"setCaptureMode({CAPTURE_MODE_SINGLE_SHOT}) answered {code_text(code)}")
        mode = self._read_capture_mode()
        if mode is not None and mode != CAPTURE_MODE_SINGLE_SHOT:
            raise RuntimeError(f"the board kept capture mode {mode_text(mode)} after setCaptureMode({CAPTURE_MODE_SINGLE_SHOT})")
        self.capture_mode = CAPTURE_MODE_SINGLE_SHOT

    def _read_calibration(self) -> None:
        """Wavelength polynomial: the operator's file wins over the board, which keeps the polynomial of the head it was calibrated with."""
        board: List[float] = []
        raw: Dict[str, Any] = {}
        if self._api.has(M_COEFFICIENT):
            try:  # index 0: wavelength coefficients A0, B1..B5 (index 1, sensitivity, is unused: NaN on the bench)
                call = self._rc(M_COEFFICIENT, self.module_id, 0, Buf(COEFFICIENT_COUNT))
                block = [float(v) for v in _to_float_array(call.slots[2])[:COEFFICIENT_COUNT]]
                raw["board"] = block
                if call.result == OK_CODE and plausible_coefficients(block):
                    board = block
            except Exception as exc:
                self.logger.warning("Could not read wavelength coefficients: %s", exc)
        from_file = self._coefficients_from_file()
        if from_file:
            raw["file"] = from_file
            if board and board != from_file:
                self.logger.info("Board polynomial %s replaced by %s from %s", board, from_file, self._coefficients_file())
        coefficients = from_file or board
        self._record("calibration", raw)
        self.calibration_coefficients = coefficients
        self.wavelengths_nm = (wavelengths_from_coefficients(coefficients, self.npix_active)
                               if coefficients else np.array([], dtype=float))
        if not coefficients and raw:
            self.logger.warning("Wavelength coefficients from the module look implausible (%s); using pixel axis "
                                "(supply them in %s next to the DLL)", raw, COEFFICIENTS_FILE_NAME)

    def _coefficients_file(self) -> Optional[Path]:
        if not self.dll_path:
            return None
        try:
            return Path(self.dll_path).with_name(COEFFICIENTS_FILE_NAME)
        except ValueError:
            return None

    def _coefficients_from_file(self) -> List[float]:
        """Wavelength polynomial from the operator's file (Hamamatsu's final inspection sheet)."""
        path = self._coefficients_file()
        if path is None or not path.is_file():
            return []
        try:
            table = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.logger.warning("Could not read %s: %s", path, exc)
            return []
        if not isinstance(table, dict):
            self.logger.warning("%s must hold an object keyed by head serial, module serial or 'default'", path)
            return []
        for key in (self.module_info.get("head_serial"), self.module_info.get("serial"), self.sn, "default"):
            entry = table.get(key) if key else None
            if entry is None:
                continue
            if isinstance(entry, dict):
                if entry.get("model"):
                    self.module_info["head_model"] = str(entry["model"])
                    self._update_name()
                entry = entry.get("coefficients")
            try:
                values = [float(v) for v in (entry or [])]
            except (TypeError, ValueError):
                values = []
            if plausible_coefficients(values):
                self._record("calibration_file", {"path": str(path), "key": key})
                return values
            self.logger.warning("Coefficients for '%s' in %s look implausible: %s", key, path, entry)
        return []

    # -- contract: connection --------------------------------------------------------

    def connect(self) -> str:
        try:
            self._ensure_api()
            self._check_methods(REQUIRED_METHODS)
            # The loader passes the discovered label ("C13016-<n>") as the serial.
            if self.sn.startswith("C13016-") and self.sn[7:].isdigit():
                self.device_index = max(0, int(self.sn[7:]) - 1)
            LIBRARY.acquire(self._invoke_library)
            self._library_held = True
            self.module_ids = self._connection_list()
            if not self.module_ids:
                raise RuntimeError("no micro-spectrometer circuit reported by getModuleConnectionList; "
                                   "check the USB cable and that u-ApsSpecEvaluation is closed")
            if self.device_index >= len(self.module_ids):
                raise RuntimeError(f"device #{self.device_index + 1} requested but only {len(self.module_ids)} module(s) connected")
            self.module_id = self.module_ids[self.device_index]
            open_code = self._rc(M_OPEN, self.module_id).result
            self._opened = True
            if open_code != OK_CODE:
                raise RuntimeError(f"open(id={self.module_id}) returned {code_text(open_code)}; is u-ApsSpecEvaluation still running?")
            self._stop_capture(force=True)          # a capture left running by a previous session
            self._read_module_information()
            pixel, resolution, mask = self._read_module_property()
            if pixel <= 0 and not self.module_info.get("serial"):
                raise RuntimeError(f"open(id={self.module_id}) succeeded but the module reports no properties")
            if pixel > 0:
                self.npix_active = pixel
            if 8 <= resolution <= 32:
                self.nbits = resolution
                self.eff_saturation_limit = float(2 ** resolution - 1)
            self.capture_mode_mask = mask
            self._read_exposure_property()
            self._read_capture_state()
            self._select_single_shot_mode()
            self._read_calibration()
            if not self.sn or self.sn.startswith("C13016-"):
                self.sn = self.sn or f"C13016-{self.device_index + 1}"
            res = self.set_it(self.it_ms if self.it_ms is not None else DEFAULT_IT_MS)
            if res != "OK":
                raise RuntimeError(res)
            self._probe_frames()
            self._connected = True
            self.error = "OK"
            self.logger.info("Connected %s, module %s (id %s): %d pixels, %d bit, IT %.3f-%.1f ms, capture mode %s%s, "
                             "exposure cycle %s us, frames every %s s, wavelength range %s nm, coefficients %s; codes %s",
                             self.name, self.sn, self.module_id, self.npix_active, self.nbits, self.min_it_ms, self.max_it_ms,
                             mode_text(self.capture_mode),
                             f" (board was in {mode_text(self.board_capture_mode)})"
                             if self.board_capture_mode not in (None, self.capture_mode) else "",
                             self.exposure_cycle_us,
                             f"{self.frame_period_s:.3f}" if self.frame_period_s else "?",
                             self.wavelength_range_nm or "?",
                             self.calibration_coefficients or "not provided", self._codes_text())
            return "OK"
        except Exception as exc:
            self.error = f"Could not connect to the C13016: {exc}. Diagnostics: {self._codes_text()}.{self._dump_api()}"
            self.logger.error(self.error)
            self._teardown()
            return self.error

    def _probe_frames(self) -> None:
        """Two frames prove the capture loop works (the first could be a buffer left over from another session)."""
        self._last_frame_at = None
        first = self._acquire_frame()
        second = self._acquire_frame() if first is not None else None
        if second is None:
            raise RuntimeError("connection probe aborted")
        counts = second
        if counts.size != self.npix_active:
            self.logger.info("getImageData returned %d values for %d pixels; using the frame size", counts.size, self.npix_active)
            self.npix_active = int(counts.size)
            if self.calibration_coefficients:
                self.wavelengths_nm = wavelengths_from_coefficients(self.calibration_coefficients, self.npix_active)
        self.rcm = counts.copy()
        self.rcs = np.zeros_like(counts)
        self._record("frame_period_s", round(self.frame_period_s, 3) if self.frame_period_s else None)

    def _codes_text(self) -> str:
        return ", ".join(f"{k.replace('MICRO_USB2_', '')}={v}" for k, v in self.diagnostics.items()) or "none"

    def _dump_api(self) -> str:
        """Write the assembly's public API (with signatures) to a file the operator can send along."""
        if self._api is None:
            return ""
        try:
            lines = [f"{DLL_NAME} public API ({time.strftime('%Y-%m-%d %H:%M:%S')})", ""]
            lines += self._api.describe().split("; ")
            lines += ["", f"diagnostics: {self._codes_text()}"]
            API_DUMP_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return f" The assembly's public API was written to {API_DUMP_FILE}."
        except Exception as write_exc:
            self.logger.debug("Could not write API dump: %s", write_exc)
            return ""

    def _teardown(self) -> None:
        if self._api is None:
            return
        for step in (self._stop_capture, self._close_module, self._release_library):
            try:
                step()
            except Exception as exc:
                self.logger.debug("teardown step failed: %s", exc)
        self._connected = False
        if self._calls is not None and not self._calls.blocked_on:
            self._calls.close()
            self._calls = None

    def _close_module(self) -> None:
        if self._opened and self.module_id is not None and self._api.has(M_CLOSE):
            self._rc(M_CLOSE, self.module_id)
        self._opened = False

    def _release_library(self) -> None:
        if self._library_held:
            LIBRARY.release(self._invoke_library)
        self._library_held = False

    def disconnect(self, *args, **kwargs) -> str:
        self.abort(ignore_errors=True)
        self._teardown()
        return "OK"

    # -- contract: parameters -------------------------------------------------------

    def set_it(self, it_ms: float) -> str:
        if self._api is None or self.module_id is None:
            self.error = "Spectrometer not connected"
            return self.error
        try:
            requested = float(it_ms)
        except (TypeError, ValueError):
            self.error = f"Invalid integration time '{it_ms}'"
            return self.error
        clamped = min(self.max_it_ms, max(self.min_it_ms, requested))
        if abs(clamped - requested) > 1e-9:
            self.logger.info("Integration time %.3f ms clamped to the device range -> %.3f ms", requested, clamped)
        try:
            if self._capturing:          # settings change between captures; the next frame request starts afresh
                self._stop_capture()
            exposure_us = clamped * US_PER_MS
            code = self._rc(M_SET_EXPOSURE, self.module_id, exposure_us).result
            if code != OK_CODE:
                raise RuntimeError(f"setExposureTime({exposure_us:g} us) returned {code_text(code)}")
            actual = clamped
            if self._api.has(M_GET_EXPOSURE):
                readback_ms = float(self._rc(M_GET_EXPOSURE, self.module_id, OUT).slots[1] or 0.0) / US_PER_MS
                if readback_ms > 0.0:
                    tolerance = max(0.002, 0.01 * clamped, 2.0 * (self.it_step_ms or 0.0))
                    if abs(readback_ms - clamped) > tolerance:
                        raise RuntimeError(
                            f"device kept {readback_ms:.4f} ms after requesting {clamped:.4f} ms "
                            f"(setExposureTime returned {code_text(code)})")
                    actual = readback_ms
            self._read_exposure_cycle()   # the DLL derives the frame period from the exposure (exposure + 2,000 us)
        except Exception as exc:
            self.error = f"Could not set integration time: {exc}"
            self.logger.error(self.error)
            return self.error
        self.it_ms = actual
        self.frame_period_s = None       # re-measured at the new exposure
        self.error = "OK"
        return "OK"

    # -- contract: acquisition -------------------------------------------------------

    def _frame_period_s(self) -> float:
        """Measured start-to-start period, else the exposure cycle plus the call overhead."""
        if self.frame_period_s and 0.0 < self.frame_period_s <= 30.0:
            return self.frame_period_s
        if self.exposure_cycle_s and self.exposure_cycle_s > 0.0:
            cycle = self.exposure_cycle_s
        else:
            cycle = (self.it_ms or DEFAULT_IT_MS) / 1000.0
        return cycle + FRAME_OVERHEAD_S

    def _frame_timeout(self) -> float:
        if self.frame_timeout_s is not None:
            return self.frame_timeout_s
        it_s = (self.it_ms or DEFAULT_IT_MS) / 1000.0
        return max(FRAME_TIMEOUT_MIN_S, 3.0 * it_s + 1.0)

    def _start_capture(self) -> None:
        """captureStart for one frame (single shot), with recovery for a start that throws inside the DLL.

        On the bench a start threw a SynchronizationLockException while the
        board was still capturing for a killed process; a throwing start did
        not start, so the driver stops, retries once, re-opens the module,
        retries once more and then asks for a re-plug.
        """
        for attempt, remedy in enumerate(("captureStop", "close and open", None)):
            try:
                code = self._rc(M_CAPTURE_START, self.module_id).result
            except Exception as exc:
                self._record(M_CAPTURE_START, f"raised {_first_line(exc)}")
                if remedy is None:
                    raise RuntimeError(
                        f"captureStart keeps failing with '{_first_line(exc)}': the C13016 is probably still capturing for a "
                        "previous session. Close other programs using it, unplug and re-plug the board, then Connect again.")
                self.logger.warning("captureStart raised '%s' (attempt %d); trying %s", _first_line(exc), attempt + 1, remedy)
                if remedy == "captureStop":
                    self._stop_capture(force=True)
                else:
                    self._reopen_module()
                continue
            if code == ERR_NOW_CAPTURING:
                self.logger.info("captureStart answered %s: a capture was still running", code_text(code))
            elif code != OK_CODE:
                raise RuntimeError(f"captureStart returned {code_text(code)}")
            self._capturing = True
            return

    def _reopen_module(self) -> None:
        """close, open and captureStop: a fresh device object in the DLL for a board in an unknown state."""
        self._rc(M_CLOSE, self.module_id)
        self._opened = False
        code = self._rc(M_OPEN, self.module_id).result
        self._opened = True
        if code != OK_CODE:
            self.logger.warning("open(id=%s) answered %s while recovering", self.module_id, code_text(code))
        self._stop_capture(force=True)

    def _stop_capture(self, force: bool = False) -> None:
        """captureStop; with force=True also when the driver believes nothing runs (leftovers of another session)."""
        if not self._capturing and not force:
            return
        self._capturing = False
        if self.module_id is None or not self._api.has(M_CAPTURE_STOP):
            return
        self._rc(M_CAPTURE_STOP, self.module_id)

    def _frame_from(self, call: CallResult) -> Tuple[np.ndarray, int]:
        counts = _to_float_array(call.slots[1])
        stamps = call.slots[2]
        stamp = 0
        try:
            if stamps is not None and len(stamps) > 0:
                stamp = int(stamps[0])
        except (TypeError, ValueError):
            stamp = 0
        return counts, stamp

    def _note_frame(self, started_at: float) -> None:
        if self._last_frame_at is not None:
            interval = started_at - self._last_frame_at
            self.frame_period_s = interval if self.frame_period_s is None else 0.7 * self.frame_period_s + 0.3 * interval
        self._last_frame_at = started_at

    def _acquire_frame(self) -> Optional[np.ndarray]:
        """One frame the way Hamamatsu's SampleAPL takes it: start, wait for the status, read, stop (None when aborted)."""
        timeout = self._frame_timeout()
        it_s = (self.it_ms or DEFAULT_IT_MS) / 1000.0
        poll = min(POLL_MAX_S, max(POLL_MIN_S, it_s / 4.0))
        status_codes: Dict[Any, int] = {}
        image_codes: Dict[Any, int] = {}
        last_error: Optional[str] = None
        restarts: List[str] = []
        for attempt in range(FRAME_RETRIES):
            started_at = time.monotonic()
            self._start_capture()
            deadline = started_at + timeout
            restart_reason: Optional[str] = None
            while True:
                if self._abort.is_set():
                    self._stop_capture()
                    return None
                status = self._rc(M_CAPTURE_STATUS, self.module_id).result
                status_codes[status] = status_codes.get(status, 0) + 1
                if status == OK_CODE:                         # "captured frames exist"
                    try:
                        call = self._rc(M_IMAGE, self.module_id, Buf(self.npix_active), Buf(TIMESTAMP_SLOTS))
                    except Exception as exc:
                        last_error = _first_line(exc)
                        restart_reason = f"getImageData raised {last_error}"
                        break
                    image_codes[call.result] = image_codes.get(call.result, 0) + 1
                    counts, stamp = self._frame_from(call)
                    if call.result == OK_CODE and counts.size and np.any(counts):
                        self._stop_capture()
                        self._note_frame(started_at)
                        self._record("last_frame", {"stamp": stamp, "pixels": int(counts.size)})
                        return counts
                    restart_reason = (f"getImageData answered {code_text(call.result)} with "
                                      f"{'an all-zero' if counts.size else 'no'} buffer for a frame reported ready")
                    break
                if status == ERR_NOT_FRAME:                   # data loss: "stop capturing and restart the measurements"
                    restart_reason = f"getCaptureStatus answered {code_text(status)}"
                    break
                if time.monotonic() > deadline:
                    self._stop_capture()
                    status_text = ", ".join(f"{code_text(k)}: {v}" for k, v in status_codes.items())
                    image_text = ", ".join(f"{code_text(k)}: {v}" for k, v in image_codes.items()) or "none"
                    raise RuntimeError(
                        f"No frame within {timeout:.1f} s at IT {self.it_ms} ms (getCaptureStatus codes {{{status_text}}}, "
                        f"getImageData codes {{{image_text}}}, captureStart={self.diagnostics.get(M_CAPTURE_START)}, "
                        f"capture mode {mode_text(self.capture_mode)}, exposure cycle {self.exposure_cycle_us} us"
                        f"{', restarts: ' + '; '.join(restarts) if restarts else ''}"
                        f"{', last error: ' + last_error if last_error else ''}). "
                        f"Run tools/diagnose_c13016.py on this PC and send its output.")
                time.sleep(poll)
            self._stop_capture()
            restarts.append(restart_reason or "unknown")
            self.logger.warning("Capture restarted (%d/%d): %s", attempt + 1, FRAME_RETRIES, restart_reason)
        raise RuntimeError(f"No usable frame after {FRAME_RETRIES} captures at IT {self.it_ms} ms ({'; '.join(restarts)}). "
                           f"Run tools/diagnose_c13016.py on this PC and send its output.")

    def measure(self, ncy: int = 1) -> str:
        if not self._connected:
            self.error = "Spectrometer not connected"
            return self.error
        if self.it_ms is None:
            self.error = "Integration time not set"
            return self.error
        if self.measuring:
            return "Measurement already in progress"
        self.ncy_requested = max(1, int(ncy))
        self.ncy_handled = 0
        self.ncy_saturated = 0
        self.error = "OK"
        self._stop_requested = False
        self._abort.clear()
        self._last_frame_at = None
        self.measuring = True
        self._done.clear()
        self.meas_start_time = spec_clock.now()
        self._worker = threading.Thread(target=self._acquire_loop, name=f"hama6-acquire-{self.alias}", daemon=True)
        self._worker.start()
        return "OK"

    def _acquire_loop(self) -> None:
        shift = None
        sum_y = sum_yy = None
        handled = saturated = 0
        try:
            for _ in range(self.ncy_requested):
                if self._abort.is_set():
                    break
                counts = self._acquire_frame()
                if counts is None:
                    break
                if shift is None:
                    shift = counts.copy()
                    sum_y = np.zeros_like(counts)
                    sum_yy = np.zeros_like(counts)
                if counts.size != shift.size:
                    raise RuntimeError(f"Frame size changed mid-measurement ({counts.size} vs {shift.size} pixels)")
                delta = counts - shift
                sum_y += delta
                sum_yy += delta * delta
                handled += 1
                if bool(counts.max() >= self.eff_saturation_limit):
                    saturated += 1
                    if self.abort_on_saturation:
                        break
            if handled:
                mean_delta = sum_y / handled
                self.rcm = shift + mean_delta
                if handled > 1:
                    variance = (sum_yy - handled * mean_delta * mean_delta) / (handled - 1)
                    self.rcs = np.sqrt(np.maximum(variance, 0.0))
                else:
                    self.rcs = np.zeros_like(self.rcm)
                self.npix_active = int(self.rcm.size)
            elif not self._abort.is_set():
                self.error = "No frames were acquired"
            self.ncy_handled = handled
            self.ncy_saturated = saturated
        except Exception as exc:
            if not self._abort.is_set():
                self.error = f"Acquisition failed: {exc}"
                self.logger.error(self.error)
            try:
                self._stop_capture()      # a fresh start on the next attempt
            except Exception as stop_exc:
                self.logger.debug("captureStop failed: %s", stop_exc)
        finally:
            if self._abort.is_set():      # user Stop: the capture is down before the waiter is released
                try:
                    self._stop_capture()
                except Exception as stop_exc:
                    self.logger.debug("captureStop failed: %s", stop_exc)
            self.measuring = False
            self._done.set()

    def wait_for_measurement(self, timeout: Optional[float] = None) -> str:
        if timeout is None:
            # One start/read/stop round trip per frame on top of the exposure itself.
            timeout = measurement_timeout_s(self.ncy_requested, self.it_ms) + self.ncy_requested * self._frame_period_s()
        if not self._done.wait(timeout):
            self.error = f"Timeout after {timeout:.1f} s waiting for measurement (ncy={self.ncy_requested}, IT={self.it_ms} ms)"
            self.logger.error(self.error)
            self.abort(ignore_errors=True)
        elif self._stop_requested:
            self._stop_requested = False
            if self.error == "OK":
                self.error = "Measurement stopped by user"
        return self.error

    def abort(self, ignore_errors: bool = False) -> str:
        self._abort.set()
        worker = self._worker
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=2.0)
        if self._api is not None:
            try:
                self._stop_capture()
            except Exception as exc:
                self.logger.debug("captureStop failed: %s", exc)
        self.measuring = False
        self._done.set()
        return "OK"

    def request_stop(self) -> str:
        self._stop_requested = True
        return self.abort(ignore_errors=True)

    def get_wavelengths(self) -> np.ndarray:
        return self.wavelengths_nm.copy()


def enumerate_devices(dll_path: str, api_factory: Optional[Callable[[str], MicroUsb2Api]] = None) -> List[Dict[str, str]]:
    """Discovery for the loader: one entry per module the assembly reports."""
    spec = Hama6_Spectrometer(api_factory=api_factory)
    spec.dll_path = dll_path
    res, count = spec.get_number_of_devices()
    if res != "OK":
        raise RuntimeError(res)
    return [{"serial": f"C13016-{index + 1}", "label": f"C13016 #{index + 1} (micro-spectrometer circuit) [Hama6]",
             "type": "Hama6", "index": str(index)} for index in range(int(count))]
