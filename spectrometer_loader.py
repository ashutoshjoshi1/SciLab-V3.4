from __future__ import annotations

import importlib
import logging
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from domain.spectrometer import assert_spectrometer_backend


LOGGER = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
DLLS_DIR = PROJECT_ROOT / "spectrometers dll files"
SPECTROMETER_TYPE_OPTIONS = ["Auto", "Ava1", "Hama2", "Hama3", "Hama4", "MiniSpec"]


def normalize_spec_type(spec_type: Optional[str]) -> str:
    value = (spec_type or "Auto").strip()
    for option in SPECTROMETER_TYPE_OPTIONS:
        if option.lower() == value.lower():
            return option
    raise ValueError(f"Unsupported spectrometer type '{spec_type}'.")


def infer_spec_type_from_dll_path(dll_path: Optional[str]) -> Optional[str]:
    if not dll_path:
        return None
    name = Path(dll_path).name.lower()
    if name.startswith("avaspec") and name.endswith(".dll"):
        return "Ava1"
    if name == "dcicusb.dll":
        return "Hama3"
    if name == "hiasapi.dll":
        return "Hama4"
    if name == "dcamapi.dll":
        return "Hama2"
    if name == "dcamusb.dll":
        return "MiniSpec"
    return None


def suggest_default_dll_path(spec_type: Optional[str]) -> str:
    normalized = normalize_spec_type(spec_type)
    suggestions = {
        "Ava1": DLLS_DIR / "avaspecx64.dll",
        "Hama3": DLLS_DIR / "DcIcUSB.dll",
        "Hama4": DLLS_DIR / "HiasApi.dll",
        "MiniSpec": DLLS_DIR / "DCamUSB.dll",
    }
    path = suggestions.get(normalized)
    return str(path.resolve()) if path and path.exists() else ""


def supports_eeprom_type(spec_type: Optional[str]) -> bool:
    try:
        return normalize_spec_type(spec_type) == "Ava1"
    except Exception:
        return False


def supports_eeprom(spec) -> bool:
    return supports_eeprom_type(getattr(spec, "spec_type", ""))


def _clean_text(value) -> str:
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="ignore")
    else:
        text = str(value)
    text = text.strip()
    if text.startswith("b'") and text.endswith("'"):
        text = text[2:-1]
    if text.startswith('b"') and text.endswith('"'):
        text = text[2:-1]
    return text.strip()


def _candidate_types(spec_type: Optional[str], dll_path: Optional[str]) -> List[str]:
    normalized = normalize_spec_type(spec_type)
    if normalized != "Auto":
        return [normalized]

    inferred = infer_spec_type_from_dll_path(dll_path)
    if inferred:
        return [inferred]
    return ["Ava1", "Hama4", "Hama3", "Hama2", "MiniSpec"]


def _candidate_dll_path(candidate: str, requested_type: Optional[str], dll_path: Optional[str]) -> str:
    explicit = (dll_path or "").strip()
    if explicit and normalize_spec_type(requested_type) != "Auto":
        return _resolve_dll_path(candidate, explicit)
    if explicit and infer_spec_type_from_dll_path(explicit) == candidate:
        return _resolve_dll_path(candidate, explicit)
    return _resolve_dll_path(candidate, None)


def _prepare_hama2_import(dll_path: Optional[str]) -> None:
    if dll_path:
        normalized = Path(dll_path)
        if normalized.is_dir():
            normalized = normalized / "dcamapi.dll"
        os.environ["DCAMAPI_DLL_PATH"] = str(normalized)

    for module_name in [
        "dcamapi4",
        "spectrometers.hama2_spectrometer",
        "spectrometers.spec_hama2.Hamamatsu_DCAMSDK4_v25056964.dcam",
        "spectrometers.spec_hama2.Hamamatsu_DCAMSDK4_v25056964.dcamapi4",
    ]:
        sys.modules.pop(module_name, None)


def _import_backend_class(spec_type: str, dll_path: Optional[str] = None):
    normalized = normalize_spec_type(spec_type)
    if normalized == "Ava1":
        module = importlib.import_module("spectrometers.ava1_spectrometer")
        return module.Avantes_Spectrometer
    if normalized == "Hama2":
        _prepare_hama2_import(dll_path)
        module = importlib.import_module("spectrometers.hama2_spectrometer")
        return module.Hama2_Spectrometer
    if normalized == "Hama3":
        module = importlib.import_module("spectrometers.hama3_spectrometer")
        return module.Hama3_Spectrometer
    if normalized == "Hama4":
        module = importlib.import_module("spectrometers.hama4_spectrometer")
        return module.Hama4_Spectrometer
    if normalized == "MiniSpec":
        module = importlib.import_module("spectrometers.minispec_spectrometer")
        return module.MiniSpec_Spectrometer
    raise ValueError(f"Unsupported spectrometer type '{spec_type}'.")


def _resolve_dll_path(spec_type: str, dll_path: Optional[str]) -> str:
    explicit = (dll_path or "").strip()
    if explicit:
        return explicit
    return suggest_default_dll_path(spec_type)


def _new_spec_instance(spec_type: str, dll_path: Optional[str], serial: Optional[str], debug_mode: int):
    cls = _import_backend_class(spec_type, dll_path)
    spec = cls()
    spec.alias = "1"
    spec.debug_mode = debug_mode
    if hasattr(spec, "initialize_spec_logger"):
        spec.initialize_spec_logger()

    resolved_dll = _resolve_dll_path(spec_type, dll_path)
    if hasattr(spec, "dll_path") and resolved_dll:
        spec.dll_path = resolved_dll
    if serial:
        spec.sn = serial
    return spec


def _discover_ava1(dll_path: Optional[str]) -> List[Dict[str, str]]:
    spec = _new_spec_instance("Ava1", dll_path, serial=None, debug_mode=0)
    if not getattr(spec, "dll_path", ""):
        raise RuntimeError("Ava1 requires a valid avaspecx64.dll path.")
    if not Path(spec.dll_path).is_file():
        raise RuntimeError(f"Ava1 dll not found: {spec.dll_path}")

    res = spec.load_spec_dll()
    if res != "OK":
        raise RuntimeError(res)
    res = spec.initialize_dll()
    if res != "OK":
        raise RuntimeError(res)
    res, count = spec.get_number_of_devices()
    if res != "OK":
        raise RuntimeError(res)
    res, info = spec.get_all_devices_info(count)
    if res != "OK":
        raise RuntimeError(res)

    devices = []
    for index in range(count):
        ident = getattr(info, f"a{index}")
        serial_number = _clean_text(getattr(ident, "SerialNumber", "")) or f"Ava1-{index + 1}"
        devices.append({"serial": serial_number, "label": f"{serial_number} [Ava1]", "type": "Ava1"})
    return devices


def _discover_hama2(dll_path: Optional[str]) -> List[Dict[str, str]]:
    spec = _new_spec_instance("Hama2", dll_path, serial=None, debug_mode=0)
    res = spec.load_and_init_hama2_dll()
    if res != "OK":
        raise RuntimeError(res)
    res, count = spec.get_number_of_devices()
    if res != "OK":
        raise RuntimeError(res)
    res, devices_info = spec.get_all_devices_info(count)
    if res != "OK":
        raise RuntimeError(res)

    devices = []
    for index, dev_info in devices_info.items():
        serial_number = _clean_text(dev_info.get("id", "")) or f"Hama2-{index + 1}"
        model = _clean_text(dev_info.get("model", "Hamamatsu"))
        devices.append(
            {"serial": serial_number, "label": f"{serial_number} ({model}) [Hama2]", "type": "Hama2"}
        )
    return devices


def _discover_hama3(dll_path: Optional[str]) -> List[Dict[str, str]]:
    spec = _new_spec_instance("Hama3", dll_path, serial=None, debug_mode=0)
    if not getattr(spec, "dll_path", ""):
        raise RuntimeError("Hama3 requires a valid DcIcUSB.dll path.")
    if not Path(spec.dll_path).is_file():
        raise RuntimeError(f"Hama3 dll not found: {spec.dll_path}")

    res = spec.load_spec_dll()
    if res != "OK":
        raise RuntimeError(res)
    res = spec.initialize_dll()
    if res != "OK":
        raise RuntimeError(res)
    res, count = spec.get_number_of_devices()
    if res != "OK":
        raise RuntimeError(res)

    from ctypes import c_uint

    devices = []
    for index in range(count):
        device_handle = spec.dll_handler.DcIc_Connect(c_uint(index))
        if int(device_handle) <= 0:
            continue
        try:
            res, dev_info = spec.get_dev_info(device_handle)
            if res != "OK":
                continue
            serial_number = _clean_text(dev_info.get("sn", "")) or f"Hama3-{index + 1}"
            model = _clean_text(dev_info.get("dev_type", "Hamamatsu"))
            devices.append(
                {"serial": serial_number, "label": f"{serial_number} ({model}) [Hama3]", "type": "Hama3"}
            )
        finally:
            try:
                spec.dll_handler.DcIc_Disconnect(device_handle)
            except Exception:
                pass
    return devices


def _discover_hama4(dll_path: Optional[str]) -> List[Dict[str, str]]:
    spec = _new_spec_instance("Hama4", dll_path, serial=None, debug_mode=0)
    if not getattr(spec, "dll_path", ""):
        raise RuntimeError("Hama4 requires a valid HiasApi.dll path.")
    if not Path(spec.dll_path).is_file():
        raise RuntimeError(f"Hama4 dll not found: {spec.dll_path}")

    res = spec.load_spec_dll()
    if res != "OK":
        raise RuntimeError(res)
    res = spec.initialize_dll()
    if res != "OK":
        raise RuntimeError(res)
    res, count = spec.get_number_of_devices()
    if res != "OK":
        raise RuntimeError(res)
    res, devices_info = spec.get_all_devices_info(count)
    if res != "OK":
        raise RuntimeError(res)

    devices = []
    for index, dev_info in devices_info.items():
        serial_number = _clean_text(dev_info.get("id", "")) or f"Hama4-{index + 1}"
        name = _clean_text(dev_info.get("name", "Hamamatsu"))
        devices.append(
            {"serial": serial_number, "label": f"{serial_number} ({name}) [Hama4]", "type": "Hama4"}
        )
    return devices


def _discover_minispec(dll_path: Optional[str]) -> List[Dict[str, str]]:
    spec = _new_spec_instance("MiniSpec", dll_path, serial=None, debug_mode=0)
    if not getattr(spec, "dll_path", ""):
        raise RuntimeError("MiniSpec requires a valid DCamUSB.dll path.")
    if not Path(spec.dll_path).is_file():
        raise RuntimeError(f"MiniSpec dll not found: {spec.dll_path}")

    res = spec.load_spec_dll()
    if res != "OK":
        raise RuntimeError(res)
    res = spec.initialize_dll()
    if res != "OK":
        raise RuntimeError(res)
    res, count = spec.get_number_of_devices()
    if res != "OK":
        raise RuntimeError(res)
    if count == 0:
        return []
    res, devices_info = spec.get_all_devices_info(count)
    if res != "OK":
        raise RuntimeError(res)

    devices = []
    for index, dev_info in devices_info.items():
        serial_number = _clean_text(dev_info.get("id", "")) or f"MiniSpec-{index + 1}"
        model = _clean_text(dev_info.get("model", "Hamamatsu MiniSpec"))
        devices.append(
            {"serial": serial_number, "label": f"{serial_number} ({model}) [MiniSpec]", "type": "MiniSpec"}
        )
    return devices


def _discover_for_type(spec_type: str, dll_path: Optional[str]) -> List[Dict[str, str]]:
    normalized = normalize_spec_type(spec_type)
    if normalized == "Ava1":
        return _discover_ava1(dll_path)
    if normalized == "Hama2":
        return _discover_hama2(dll_path)
    if normalized == "Hama3":
        return _discover_hama3(dll_path)
    if normalized == "Hama4":
        return _discover_hama4(dll_path)
    if normalized == "MiniSpec":
        return _discover_minispec(dll_path)
    raise ValueError(f"Unsupported spectrometer type '{spec_type}'.")


def discover_spectrometers(spec_type: Optional[str], dll_path: Optional[str] = None) -> List[Dict[str, str]]:
    errors = []
    for candidate in _candidate_types(spec_type, dll_path):
        candidate_dll = _candidate_dll_path(candidate, spec_type, dll_path)
        try:
            devices = _discover_for_type(candidate, candidate_dll)
        except Exception as exc:
            errors.append(f"{candidate}: {exc}")
            LOGGER.exception("Unable to discover %s spectrometers", candidate)
            continue
        if devices:
            return devices

    if errors:
        raise RuntimeError(" | ".join(errors))
    return []


def connect_spectrometer(
    spec_type: Optional[str],
    dll_path: Optional[str] = None,
    serial: Optional[str] = None,
    debug_mode: int = 1,
):
    candidates = _candidate_types(spec_type, dll_path)
    last_error = None

    for candidate in candidates:
        candidate_dll = _candidate_dll_path(candidate, spec_type, dll_path)
        try:
            devices = _discover_for_type(candidate, candidate_dll)
            if not devices:
                continue
            selected_serial = serial or devices[0]["serial"]
            spec = _new_spec_instance(candidate, candidate_dll, selected_serial, debug_mode=debug_mode)
            res = spec.connect()
            if res != "OK":
                raise RuntimeError(res)
            spec.spec_type = candidate
            return candidate, assert_spectrometer_backend(spec)
        except Exception as exc:
            last_error = exc
            LOGGER.exception("Unable to connect %s spectrometer", candidate)

    if last_error is not None:
        raise RuntimeError(str(last_error))
    raise RuntimeError("No compatible spectrometers detected.")
