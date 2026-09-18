"""Hamamatsu micro-series backend ("Hama6"): C13016 evaluation circuit and C13985 module.

The C13016 is Hamamatsu's USB evaluation circuit for the fingertip micro-
spectrometer heads (C12880MA, C16767MA); the C13985 is the same head with the
circuit built in. The head on the bench is a **C16767MA-20** (UV, 190 to
440 nm, 288 pixels); the board reports the head's serial (26C00021) and
sensor (S11639) through ``getSpectroInformationForLabVIEW`` but not its model.
Wavelengths come from the six coefficients the board reports or, when those
are missing or implausible, from ``hama6_wavelength_coefficients.json`` next
to the DLL (Hamamatsu's final inspection sheet, keyed by head serial; an
entry may also name the head model). Hamamatsu's PC software for them is ``u-ApsSpecEvaluation.exe``
with ``MICRO_USB2_CLR.dll`` (technical note KACC9003E, table 2-9), a .NET
assembly that mirrors the C-style ``MICRO_USB2`` library one-to-one. It is
loaded here through *pythonnet* (``import clr``).

Public API of the assembly as reflected on the bench (2026-09-17), class
``MICRO_USB2_CLR``; every method returns an ``Int64`` status code::

    MICRO_USB2_initialize() / MICRO_USB2_uninitialize()
    MICRO_USB2_getModuleConnectionList(Int16[] list, ref UInt16 number)
    MICRO_USB2_open(Int64 id) / MICRO_USB2_close(Int64 id)
    MICRO_USB2_getModuleInformationForLabVIEW(Int64 id, ref String vender, ref String product,
                                              ref String serial, ref String firmware, ref String driver, ref String library)
    MICRO_USB2_getModulePropertyForLabVIEW(Int64 id, ref Int64 pixel, ref Int64 resolution, ref Int64 capturemode)
    MICRO_USB2_getExposurePropertyForLabVIEW(Int64 id, ref Double lowertime, ref Double uppertime, ref Double timestep,
                                             ref Double lowercycle, ref Double uppercycle, ref Double cyclestep)
    MICRO_USB2_setExposureTime(Int64 id, Double sec) / MICRO_USB2_getExposureTime(Int64 id, ref Double sec)
    MICRO_USB2_getCalibrationCoefficient(Int64 id, Int64 index, Double[] coefficient)
    MICRO_USB2_captureStart(Int64 id) / MICRO_USB2_captureStop(Int64 id) / MICRO_USB2_getCaptureStatus(Int64 id)
    MICRO_USB2_getImageData(Int64 id, ref UInt16[] imageData, ref UInt64[] time)

Established on the bench with a C13016 (serial 326VA021, firmware 1.08):

* every method returned ``0`` while working, so ``0`` is treated as success;
* the exposure API works in **microseconds** although the parameter is named
  ``sec`` -- ``getExposurePropertyForLabVIEW`` reported 11 .. 1,000,000, step 1,
  and cycle limits 1,043 .. 1,100,000; the unit is re-derived on every connect;
* identity strings are NUL-padded fixed-width buffers;
* the module id from ``getModuleConnectionList`` was ``0``; the module property
  ``capturemode`` read 32 (a capability mask), ``getCaptureMode`` 0 or 2 (it
  persists in the board); key-protected methods (``strCheck``) answer 12 to an
  empty key, so the exposure *cycle* (frame period) can be neither read nor set;
* ``getImageData`` answers 6 while no new frame exists and 0 with a frame (the
  same frame is served once more before 6 resumes); the first frame after
  ``captureStart`` took about a second to appear, i.e. the stored exposure
  cycle governs the latency;
* in capture mode 0 exactly one frame followed a ``captureStart`` (single shot)
  and a second start was accepted; in mode 2 a second ``captureStart`` while
  the capture still ran threw a ``SynchronizationLockException`` inside the
  DLL (continuous capture);
* ``getCaptureStatus`` read 1 before any start, after starts and after stops:
  it carries no usable information;
* 2026-09-18 (``tools/diagnose_c13016.py``, board in mode 0): no frame at all
  in 18 s of polling at 10 and 50 ms; right after each ``captureStart`` one
  poll answered 0 with 288 zeros and timestamp 0 (the DLL echoing its empty
  buffer), then 6 until the stop. The "single shot" frames of the day before
  were therefore most likely the same echo of a buffer that still held a real
  frame: mode 0 does not expose on its own, and mode 2 is the only mode that
  has delivered frames. ``getImageSize`` is key-protected too (12).

The driver therefore calls ``captureStart`` before every frame until a start
throws; from then on the capture is known to be continuous and is left
running. New frames are recognised by a fresh timestamp or changed pixel
data (an all-zero buffer is never a frame), the frame already exposing when
the integration time changes is discarded, and the probe at connect demands
two consecutive new frames so a stale buffer cannot pass. The probe runs in
the board's own capture mode first; when that yields nothing the driver
switches to mode 2 (``FALLBACK_CAPTURE_MODES``), probes again and restores
the board's original mode at disconnect.

Calls go through .NET reflection (``MethodInfo.Invoke``) with every scalar boxed
inside the CLR: pythonnet 3 turns a Python ``int`` stored into an ``object[]``
into a ``Python.Runtime.PyInt`` wrapper that Invoke cannot convert to ``Int64``,
and its own overload resolution rejects ``ref`` parameters outright.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from spectrometers.spec_xfus import measurement_timeout_s, spec_clock

LOGGER = logging.getLogger(__name__)

DLL_NAME = "MICRO_USB2_CLR.dll"
CLASS_NAME = "MICRO_USB2_CLR"
API_DUMP_FILE = Path(__file__).resolve().parent / "micro_usb2_api_dump.txt"  # written when Connect fails

DEFAULT_PIXEL_COUNT = 288
DEFAULT_BITS = 16
MIN_IT_MS = 0.011        # 11 us (C12880MA datasheet)
MAX_IT_MS = 1000.0       # 1 s
OK_CODE = 0              # status code every method returned while working on the bench
EXPOSURE_UNIT_FACTORS_TO_MS = {"us": 1e-3, "s": 1e3}
COEFFICIENT_COUNT = 6    # wavelength = a0 + a1*p + ... + a5*p^5, p = 1..npix
# Operator-supplied wavelength polynomial for boards that report none, next to the DLL:
# {"<head serial>": [A0, B1, B2, B3, B4, B5]} or {"<head serial>": {"model": "C16767MA-20", "coefficients": [...]}},
# keyed by the head serial (getSpectroInformationForLabVIEW), the module serial or "default".
COEFFICIENTS_FILE_NAME = "hama6_wavelength_coefficients.json"
MAX_MODULES = 32         # capacity of the connection-list buffer
TIMESTAMP_SLOTS = 8      # UInt64[] buffer handed to getImageData
POLL_MIN_S = 0.002
POLL_MAX_S = 0.05
FRAME_TIMEOUT_MIN_S = 2.5          # a frame may take a whole exposure cycle (up to 1.1 s) to appear
DEFAULT_FRAME_PERIOD_S = 1.5       # assumed cycle when the board does not report cycle limits
CONTINUOUS_CAPTURE_MODE = 2        # the only capture mode that has delivered frames on the bench
FALLBACK_CAPTURE_MODES = (CONTINUOUS_CAPTURE_MODE,)   # tried in order when the board's own mode yields no frame

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
M_GET_CYCLE = "MICRO_USB2_getExposureCycle"
M_SET_CYCLE = "MICRO_USB2_setExposureCycle"
M_IMAGE = "MICRO_USB2_getImageData"
M_TRIGGER_OUTPUT = "MICRO_USB2_getTriggerOutput"
M_TRIGGER_POLARITY = "MICRO_USB2_getTriggerPolarity"
M_SPECTRO_INFO = "MICRO_USB2_getSpectroInformationForLabVIEW"   # (Int64 id, ref String serial, ref String sensor): the head
REQUIRED_METHODS = (M_INIT, M_LIST, M_OPEN, M_CLOSE, M_SET_EXPOSURE, M_CAPTURE_START, M_IMAGE)


def wavelengths_from_coefficients(coefficients: Sequence[float], npix: int) -> np.ndarray:
    """a0 + a1*p + ... + a5*p^5 with p = 1..npix (Hamamatsu's pixel numbering)."""
    pixels = np.arange(1, int(npix) + 1, dtype=float)
    result = np.zeros_like(pixels)
    for power, coefficient in enumerate(coefficients):
        result += float(coefficient) * pixels ** power
    return result


def plausible_coefficients(coefficients: Sequence[float]) -> bool:
    """Loose sanity check for a C12880MA/C16767MA wavelength polynomial (a0 in nm, a1 in nm/pixel)."""
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
        raise RuntimeError(f"{DLL_NAME} not found at '{dll_path}'. Copy it from Hamamatsu's u-ApsSpecEvaluation folder.")
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

    def acquire(self, api: MicroUsb2Api, record: Callable[[str, Any], None]) -> None:
        with self.lock:
            if self.users == 0:
                record(M_INIT, api.call(M_INIT).result)
            self.users += 1

    def release(self, api: MicroUsb2Api, record: Callable[[str, Any], None]) -> None:
        with self.lock:
            self.users = max(0, self.users - 1)
            if self.users == 0 and api.has(M_UNINIT):
                try:
                    record(M_UNINIT, api.call(M_UNINIT).result)
                except Exception as exc:
                    LOGGER.debug("%s failed: %s", M_UNINIT, exc)


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
        self.exposure_unit = "us"                          # re-derived from getExposurePropertyForLabVIEW
        self.cycle_limits_s: Dict[str, float] = {}         # exposure-cycle (frame period) limits, seconds
        self.frame_period_s: Optional[float] = None        # measured between consecutive frames
        self.calibration_coefficients: List[float] = []
        self.wavelengths_nm = np.array([], dtype=float)
        self.device_index = 0
        self.module_id: Optional[int] = None
        self.module_ids: List[int] = []
        self.module_info: Dict[str, str] = {}
        self.capture_mode: Optional[int] = None            # active mode from getCaptureMode
        self.original_capture_mode: Optional[int] = None   # the board's own mode when the driver had to change it
        self.capture_modes_supported: Optional[int] = None # `capturemode` field of the module property
        self.exposure_cycle: Any = None                    # raw getExposureCycle value (key-protected: unreliable)
        self.frame_timeout_s: Optional[float] = None       # None: derived from integration time and cycle
        self.diagnostics: Dict[str, Any] = {}              # method -> last status code / probed value

        self._api = api
        self._api_factory = api_factory
        self._connected = False
        self._library_held = False
        self._opened = False
        self._capturing = False
        self._continuous: Optional[bool] = None            # learned: True = start throws while running, False = one frame per start
        self._status_idle: Any = None                      # getCaptureStatus before the first captureStart
        self._frames_to_discard = 0                        # frames already exposing when the IT changed
        self._last_stamp: Optional[int] = None
        self._last_counts: Optional[np.ndarray] = None
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
        """Call a method under the I/O lock and remember its status code."""
        with self._io_lock:
            call = self._api.call(method, *args)
        self._record(method, call.result)
        return call

    def _check_methods(self, required: Sequence[str]) -> None:
        missing = [m for m in required if not self._api.has(m)]
        if missing:
            raise RuntimeError(f"{DLL_NAME} lacks {', '.join(missing)}; this backend targets the C13016 evaluation assembly")

    # -- discovery -------------------------------------------------------------------

    def _connection_list(self) -> List[int]:
        """Module ids reported by getModuleConnectionList (capacity passed in `number`)."""
        call = self._rc(M_LIST, Buf(MAX_MODULES), MAX_MODULES)
        modules = call.slots[0]
        try:
            number = int(call.slots[1] or 0)
        except (TypeError, ValueError):
            number = 0
        number = max(0, min(number, MAX_MODULES, len(modules) if modules is not None else 0))
        ids = [int(modules[i]) for i in range(number)]
        self._record("module_ids", ids)
        return ids

    def get_number_of_devices(self):
        try:
            api = self._ensure_api()
            self._check_methods((M_INIT, M_LIST))
            LIBRARY.acquire(api, self._record)
            try:
                return "OK", len(self._connection_list())
            finally:
                LIBRARY.release(api, self._record)
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
            try:  # the attached head: its serial and sensor (the bench head 26C00021 / S11639 is a C16767MA-20)
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
            pixel, resolution, mode = (int(v or 0) for v in call.slots[1:4])
        except (TypeError, ValueError):
            pixel, resolution, mode = 0, 0, None
        self._record("module_property", {"pixel": pixel, "resolution": resolution, "capturemode": mode})
        return pixel, resolution, mode

    def _read_capture_state(self) -> None:
        """Active capture mode, exposure cycle and trigger settings, for the log and error messages."""
        if self._api.has(M_GET_CAPTURE_MODE):
            try:
                mode = self._rc(M_GET_CAPTURE_MODE, self.module_id, OUT).slots[1]
                self.capture_mode = int(mode) if mode is not None else None
            except Exception as exc:
                self.logger.debug("%s failed: %s", M_GET_CAPTURE_MODE, exc)
        if self._api.has(M_GET_CYCLE):
            try:  # (Int64 id, ref Double sec, String strCheck): the key is unknown; the call answers 12 with ""
                self.exposure_cycle = self._rc(M_GET_CYCLE, self.module_id, OUT, "").slots[1]
            except Exception as exc:
                self.logger.debug("%s failed: %s", M_GET_CYCLE, exc)
        trigger = {}
        for key, method in (("output", M_TRIGGER_OUTPUT), ("polarity", M_TRIGGER_POLARITY)):
            if self._api.has(method):
                try:
                    trigger[key] = self._rc(method, self.module_id, OUT).slots[1]
                except Exception as exc:
                    self.logger.debug("%s failed: %s", method, exc)
        if self._api.has(M_CAPTURE_STATUS):
            self._status_idle = self._rc(M_CAPTURE_STATUS, self.module_id).result
        self._record("capture_state", {"mode": self.capture_mode, "supported": self.capture_modes_supported,
                                       "exposure_cycle": self.exposure_cycle, "trigger": trigger,
                                       "status_before_start": self._status_idle})

    def _read_exposure_property(self) -> None:
        if not self._api.has(M_EXPOSURE_PROPERTY):
            return
        call = self._rc(M_EXPOSURE_PROPERTY, self.module_id, OUT, OUT, OUT, OUT, OUT, OUT)
        try:
            lower, upper, step, cycle_lower, cycle_upper, cycle_step = (float(v or 0.0) for v in call.slots[1:7])
        except (TypeError, ValueError):
            return
        record = {"lower": lower, "upper": upper, "step": step,
                  "cycle": {"lower": cycle_lower, "upper": cycle_upper, "step": cycle_step}}
        if not (0.0 < lower < upper):
            self._record("exposure_property", {**record, "unit": "ignored"})
            return
        # A 1 s ceiling expressed in seconds is a small number; in microseconds it is 1e6.
        self.exposure_unit = "s" if upper <= 100.0 else "us"
        factor = EXPOSURE_UNIT_FACTORS_TO_MS[self.exposure_unit]
        self._record("exposure_property", {**record, "unit": self.exposure_unit})
        self.min_it_ms = lower * factor
        self.max_it_ms = upper * factor
        if step > 0.0:
            self.it_step_ms = step * factor
        if 0.0 < cycle_lower <= cycle_upper:
            self.cycle_limits_s = {"lower": cycle_lower * factor / 1000.0, "upper": cycle_upper * factor / 1000.0}

    def _to_device_exposure(self, it_ms: float) -> float:
        return it_ms / EXPOSURE_UNIT_FACTORS_TO_MS[self.exposure_unit]

    def _from_device_exposure(self, value) -> float:
        return float(value or 0.0) * EXPOSURE_UNIT_FACTORS_TO_MS[self.exposure_unit]

    def _read_calibration(self) -> None:
        coefficients: List[float] = []
        raw: Dict[str, Any] = {}
        if self._api.has(M_COEFFICIENT):
            try:
                call = self._rc(M_COEFFICIENT, self.module_id, 0, Buf(COEFFICIENT_COUNT))
                block = [float(v) for v in _to_float_array(call.slots[2])[:COEFFICIENT_COUNT]]
                raw["index0"] = block
                if plausible_coefficients(block):
                    coefficients = block
                else:  # maybe `index` selects the coefficient and the array holds one value
                    singles = []
                    for index in range(COEFFICIENT_COUNT):
                        call = self._rc(M_COEFFICIENT, self.module_id, index, Buf(1))
                        values = _to_float_array(call.slots[2])
                        singles.append(float(values[0]) if values.size else float("nan"))
                    raw["per_index"] = singles
                    if plausible_coefficients(singles):
                        coefficients = singles
            except Exception as exc:
                self.logger.warning("Could not read wavelength coefficients: %s", exc)
        if not coefficients:
            from_file = self._coefficients_from_file()
            if from_file:
                if raw:
                    self.logger.info("Wavelength coefficients from the module look implausible (%s); using %s",
                                     raw, self._coefficients_file())
                coefficients = from_file
                raw["file"] = from_file
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
        """Wavelength polynomial from the operator's file (Hamamatsu's final inspection sheet) when the board has none."""
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
            api = self._ensure_api()
            self._check_methods(REQUIRED_METHODS)
            # The loader passes the discovered label ("C13016-<n>") as the serial.
            if self.sn.startswith("C13016-") and self.sn[7:].isdigit():
                self.device_index = max(0, int(self.sn[7:]) - 1)
            LIBRARY.acquire(api, self._record)
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
                raise RuntimeError(f"open(id={self.module_id}) returned {open_code}; is u-ApsSpecEvaluation still running?")
            self._read_module_information()
            pixel, resolution, mode = self._read_module_property()
            if pixel <= 0 and not self.module_info.get("serial"):
                raise RuntimeError(f"open(id={self.module_id}) succeeded but the module reports no properties")
            if pixel > 0:
                self.npix_active = pixel
            if 8 <= resolution <= 32:
                self.nbits = resolution
                self.eff_saturation_limit = float(2 ** resolution - 1)
            self.capture_modes_supported = mode
            self._read_capture_state()
            self._read_exposure_property()
            self._read_calibration()
            if not self.sn or self.sn.startswith("C13016-"):
                self.sn = self.sn or f"C13016-{self.device_index + 1}"
            res = self.set_it(self.it_ms if self.it_ms is not None else 10.0)
            if res != "OK":
                raise RuntimeError(res)
            self._probe_frames_in_a_working_mode()
            self._connected = True
            self.error = "OK"
            self.logger.info("Connected Hama6 %s (id %s): %d pixels, %d bit, IT %.3f-%.1f ms (device unit %s), "
                             "capture mode %s%s, frames every %s s, coefficients %s; codes %s",
                             self.sn, self.module_id, self.npix_active, self.nbits, self.min_it_ms, self.max_it_ms,
                             self.exposure_unit, self.capture_mode,
                             f" (board was in mode {self.original_capture_mode})" if self.original_capture_mode is not None else "",
                             f"{self.frame_period_s:.2f}" if self.frame_period_s else "?",
                             self.calibration_coefficients or "not provided", self._codes_text())
            return "OK"
        except Exception as exc:
            self.error = f"Could not connect to the C13016: {exc}. Diagnostics: {self._codes_text()}.{self._dump_api()}"
            self.logger.error(self.error)
            self._teardown()
            return self.error

    def _probe_frames(self) -> None:
        """Two consecutive new frames prove the capture runs (a single one may be a stale buffer)."""
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

    def _probe_frames_in_a_working_mode(self) -> None:
        """Probe the board's own capture mode first; when it yields no frame, try the continuous mode.

        Bench, 2026-09-18: in mode 0 the board answered 6 to every poll for
        18 s at 10 and 50 ms (only an echo of the empty buffer, code 0 with 288
        zeros and timestamp 0, right after each captureStart); mode 2 is the
        only mode that has delivered frames. The original mode is restored at
        disconnect.
        """
        try:
            self._probe_frames()
            return
        except RuntimeError as exc:
            if self._abort.is_set() or not self._api.has(M_SET_CAPTURE_MODE):
                raise
            failures = [f"mode {self.capture_mode}: {exc}"]
        for mode in FALLBACK_CAPTURE_MODES:
            if mode == self.capture_mode:
                continue
            self.logger.info("No frame in the board's capture mode %s; trying capture mode %s", self.capture_mode, mode)
            self._switch_capture_mode(mode)
            try:
                self._probe_frames()
                self.logger.info("Capture mode %s delivers frames (the board's mode %s is restored at disconnect)",
                                 self.capture_mode, self.original_capture_mode)
                return
            except RuntimeError as exc:
                if self._abort.is_set():
                    raise
                failures.append(f"mode {self.capture_mode}: {exc}")
        self._restore_capture_mode()
        raise RuntimeError("no capture mode delivered a frame (" + "; ".join(failures) + ")")

    def _switch_capture_mode(self, mode: int) -> None:
        """captureStop, setCaptureMode, read the mode back and forget everything learned about the capture."""
        self._stop_capture()
        if self.original_capture_mode is None:
            self.original_capture_mode = self.capture_mode
        code = self._rc(M_SET_CAPTURE_MODE, self.module_id, int(mode)).result
        if code != OK_CODE:
            self.logger.warning("%s(%s) returned %s", M_SET_CAPTURE_MODE, mode, code)
        readback = None
        if self._api.has(M_GET_CAPTURE_MODE):
            readback = self._rc(M_GET_CAPTURE_MODE, self.module_id, OUT).slots[1]
        self.capture_mode = int(readback) if readback is not None else int(mode)
        self._continuous = None
        self._frames_to_discard = 0
        self._last_stamp = None
        self._last_counts = None
        self._last_frame_at = None
        self.frame_period_s = None

    def _restore_capture_mode(self) -> None:
        """Put the board back into the mode it had before the driver changed it (a no-op otherwise)."""
        original, self.original_capture_mode = self.original_capture_mode, None
        if original is None or original == self.capture_mode or self.module_id is None or not self._api.has(M_SET_CAPTURE_MODE):
            return
        self._stop_capture()
        code = self._rc(M_SET_CAPTURE_MODE, self.module_id, int(original)).result
        if code != OK_CODE:
            self.logger.warning("%s(%s) returned %s while restoring the board's mode", M_SET_CAPTURE_MODE, original, code)
        self.capture_mode = original

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
        for step in (self._stop_capture, self._restore_capture_mode, self._close_module, self._release_library):
            try:
                step()
            except Exception as exc:
                self.logger.debug("teardown step failed: %s", exc)
        self._connected = False

    def _close_module(self) -> None:
        if self._opened and self.module_id is not None and self._api.has(M_CLOSE):
            self._rc(M_CLOSE, self.module_id)
        self._opened = False

    def _release_library(self) -> None:
        if self._library_held:
            LIBRARY.release(self._api, self._record)
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
            device_value = self._to_device_exposure(clamped)
            code = self._rc(M_SET_EXPOSURE, self.module_id, device_value).result
            if code != OK_CODE and self._capturing:
                self.logger.info("setExposureTime returned %s while capturing; stopping the capture and retrying", code)
                self._stop_capture()
                code = self._rc(M_SET_EXPOSURE, self.module_id, device_value).result
            if code != OK_CODE:
                raise RuntimeError(f"setExposureTime({device_value:g} {self.exposure_unit}) returned {code}")
            actual = clamped
            if self._api.has(M_GET_EXPOSURE):
                readback_ms = self._from_device_exposure(self._rc(M_GET_EXPOSURE, self.module_id, OUT).slots[1])
                if readback_ms > 0.0:
                    tolerance = max(0.002, 0.01 * clamped, 2.0 * (self.it_step_ms or 0.0))
                    if abs(readback_ms - clamped) > tolerance:
                        raise RuntimeError(
                            f"device kept {readback_ms:.4f} ms after requesting {clamped:.4f} ms "
                            f"(setExposureTime returned {code})")
                    actual = readback_ms
        except Exception as exc:
            self.error = f"Could not set integration time: {exc}"
            self.logger.error(self.error)
            return self.error
        if self._capturing and self._continuous is not False and (self.it_ms is None or abs(actual - self.it_ms) > 1e-9):
            self._frames_to_discard = 1      # a continuously exposing frame still uses the previous setting
        self.it_ms = actual
        self.error = "OK"
        return "OK"

    # -- contract: acquisition -------------------------------------------------------

    def _frame_period_s(self) -> float:
        candidates = [v for v in (self.frame_period_s, self.cycle_limits_s.get("upper")) if v and 0.0 < v <= 30.0]
        return max(candidates) if candidates else DEFAULT_FRAME_PERIOD_S

    def _frame_timeout(self) -> float:
        if self.frame_timeout_s is not None:
            return self.frame_timeout_s
        it_s = (self.it_ms or 10.0) / 1000.0
        return max(FRAME_TIMEOUT_MIN_S, 3.0 * it_s + 2.0 * self._frame_period_s())

    def _start_capture(self) -> None:
        """captureStart before waiting for a frame, until the capture proves continuous.

        Mode 0 delivered exactly one frame per captureStart (single shot) and
        accepted the next start; in mode 2 a second start threw inside the DLL
        because the capture still ran (continuous). Starting before every frame
        serves both: the first exception marks the capture as continuous and no
        further start is attempted while it runs.
        """
        if self._capturing and self._continuous:
            return
        try:
            code = self._rc(M_CAPTURE_START, self.module_id).result
            if code != OK_CODE:
                self.logger.warning("%s returned %s", M_CAPTURE_START, code)
            if self._capturing and self._continuous is None:
                self._continuous = False
                self.logger.info("Capture mode %s behaves as single shot: one frame per captureStart", self.capture_mode)
        except Exception as exc:
            self._record(M_CAPTURE_START, f"raised {_first_line(exc)}")
            if not self._continuous:
                self.logger.info("captureStart raised '%s': the capture is continuous and is left running", _first_line(exc))
            self._continuous = True
        self._capturing = True

    def _stop_capture(self) -> None:
        if not self._capturing:
            return
        self._capturing = False
        if self.module_id is None or not self._api.has(M_CAPTURE_STOP):
            return
        self._rc(M_CAPTURE_STOP, self.module_id)
        self._wait_capture_idle()

    def _wait_capture_idle(self) -> None:
        """captureStop is asynchronous: wait until getCaptureStatus is back to its pre-start value."""
        if self._status_idle is None or not self._api.has(M_CAPTURE_STATUS):
            return
        deadline = time.monotonic() + min(self._frame_timeout(), self._frame_period_s() + 0.5)
        while time.monotonic() < deadline:
            if self._rc(M_CAPTURE_STATUS, self.module_id).result == self._status_idle:
                return
            time.sleep(0.02)
        self.logger.info("getCaptureStatus still %s after captureStop", self.diagnostics.get(M_CAPTURE_STATUS))

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

    def _is_new_frame(self, counts: np.ndarray, stamp: int) -> bool:
        """A frame counts as new when it has data and either a fresh timestamp or changed pixels."""
        if counts.size == 0 or not np.any(counts):
            return False
        if self._last_counts is None:
            return True
        if stamp != 0 or self._last_stamp:
            return stamp != self._last_stamp
        return not np.array_equal(counts, self._last_counts)

    def _note_frame(self, counts: np.ndarray, stamp: int) -> None:
        now = time.monotonic()
        if self._last_frame_at is not None:
            interval = now - self._last_frame_at
            self.frame_period_s = interval if self.frame_period_s is None else 0.7 * self.frame_period_s + 0.3 * interval
        self._last_frame_at = now
        self._last_stamp = stamp
        self._last_counts = counts

    def _acquire_frame(self) -> Optional[np.ndarray]:
        """Start (if needed) and wait for the next new frame (None when aborted)."""
        self._start_capture()
        timeout = self._frame_timeout()
        it_s = (self.it_ms or 10.0) / 1000.0
        poll = min(POLL_MAX_S, max(POLL_MIN_S, it_s / 4.0))
        deadline = time.monotonic() + timeout
        codes: Dict[Any, int] = {}
        last_error: Optional[str] = None
        while True:
            if self._abort.is_set():
                return None
            try:
                call = self._rc(M_IMAGE, self.module_id, Buf(self.npix_active), Buf(TIMESTAMP_SLOTS))
                codes[call.result] = codes.get(call.result, 0) + 1
                counts, stamp = self._frame_from(call)
            except Exception as exc:  # the assembly throws while no frame is available
                last_error = _first_line(exc)
                counts, stamp = np.array([], dtype=float), 0
            if self._is_new_frame(counts, stamp):
                self._note_frame(counts, stamp)
                if self._frames_to_discard > 0:
                    self._frames_to_discard -= 1
                    deadline = time.monotonic() + timeout
                    continue
                return counts
            if time.monotonic() > deadline:
                status = self._rc(M_CAPTURE_STATUS, self.module_id).result if self._api.has(M_CAPTURE_STATUS) else "n/a"
                raise RuntimeError(
                    f"No new frame within {timeout:.1f} s at IT {self.it_ms} ms (getImageData codes {codes}, "
                    f"captureStart={self.diagnostics.get(M_CAPTURE_START)}, captureStatus={status}, capture mode "
                    f"{self.capture_mode} of supported {self.capture_modes_supported}, cycle limits {self.cycle_limits_s}"
                    f"{', last error: ' + last_error if last_error else ''}). "
                    f"Run tools/diagnose_c13016.py on this PC and send its output.")
            time.sleep(poll)

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
            # Frames arrive once per exposure cycle, which may be far longer than the integration time.
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
