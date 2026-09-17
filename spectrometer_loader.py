from __future__ import annotations

import importlib
import logging
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

from domain.spectrometer import assert_spectrometer_backend


LOGGER = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
DLLS_DIR = PROJECT_ROOT / "spectrometers dll files"
# "Hama5" = Hamamatsu mini-spectrometers on the specu1b USB protocol (C10785, TM/TG/RC
# series) via pyusb; no vendor DLL. "Simulated" is explicit opt-in only: Auto never
# falls back to fake data.
SPECTROMETER_TYPE_OPTIONS = ["Auto", "Ava1", "Hama2", "Hama3", "Hama4", "Hama5", "MiniSpec", "Simulated"]


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


# Default Auto probe order. Hama5 only claims Hamamatsu USB devices that are not
# DCamUSB units, so it is safe to try last.
AUTO_PROBE_ORDER = ["Ava1", "Hama4", "Hama3", "Hama2", "MiniSpec", "Hama5"]

# USB vendor IDs each backend can possibly talk to. In Auto mode a backend is not
# even loaded when no device of its vendor is attached: each skipped probe saves a
# DLL load, a runtime initialisation and a USB enumeration (seconds each).
AVANTES_VENDOR_ID = 0x1992
HAMAMATSU_VENDOR_ID = 0x0661
BACKEND_USB_VENDORS: Dict[str, frozenset] = {
    "Ava1": frozenset({AVANTES_VENDOR_ID}),
    "Hama2": frozenset({HAMAMATSU_VENDOR_ID}),
    "Hama3": frozenset({HAMAMATSU_VENDOR_ID}),
    "Hama4": frozenset({HAMAMATSU_VENDOR_ID}),
    "MiniSpec": frozenset({HAMAMATSU_VENDOR_ID}),
    "Hama5": frozenset({HAMAMATSU_VENDOR_ID}),
}


def present_usb_vendor_ids() -> Optional[set]:
    """Vendor IDs of every attached USB device, or None when that cannot be determined.

    Uses pyusb/libusb, which can list descriptors of devices bound to vendor
    drivers even though it cannot open them. None (no libusb, or an error) means
    "unknown": the caller then probes every backend as before.
    """
    try:
        import usb.core

        from spectrometers.hama5_spectrometer import _libusb_backend

        backend = _libusb_backend()
        devices = usb.core.find(find_all=True, backend=backend) if backend is not None else usb.core.find(find_all=True)
        return {int(device.idVendor) for device in (devices or [])}
    except Exception as exc:
        LOGGER.debug("USB pre-scan unavailable (%s); probing every backend", exc)
        return None


def _candidate_types(spec_type: Optional[str], dll_path: Optional[str], preferred_type: Optional[str] = None) -> List[str]:
    normalized = normalize_spec_type(spec_type)
    if normalized != "Auto":
        return [normalized]

    inferred = infer_spec_type_from_dll_path(dll_path)
    if inferred:
        return [inferred]

    order = list(AUTO_PROBE_ORDER)
    # The type that connected last time is by far the most likely one now.
    try:
        preferred = normalize_spec_type(preferred_type) if preferred_type else None
    except ValueError:
        preferred = None
    if preferred in order:
        order.remove(preferred)
        order.insert(0, preferred)
    return order


def _release_probe(spec) -> None:
    """Free the DLL session a discovery probe opened (v3.3 leaked one per probe)."""
    handler = getattr(spec, "dll_handler", None)
    if handler is not None and hasattr(handler, "DcIc_Terminate") and not getattr(spec, "spec_id", None):
        # Hama3 (DcIcUSB) only frees its session through DcIc_Terminate; the probe
        # never connected a device, so there is no handle to disconnect first.
        try:
            handler.DcIc_Terminate()
        except Exception:
            LOGGER.debug("DcIc_Terminate after probe failed", exc_info=True)
        return
    for method_name in ("close_spec_dll", "_finalize_dll"):
        method = getattr(spec, method_name, None)
        if method is None:
            continue
        try:
            method(ignore_errors=True)
        except TypeError:
            try:
                method()
            except Exception:
                LOGGER.debug("Probe cleanup %s failed", method_name, exc_info=True)
        except Exception:
            LOGGER.debug("Probe cleanup %s failed", method_name, exc_info=True)
        return


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
    if normalized == "Hama5":
        module = importlib.import_module("spectrometers.hama5_spectrometer")
        return module.Hama5_Spectrometer
    if normalized == "Simulated":
        module = importlib.import_module("spectrometers.simulated_spectrometer")
        return module.SimulatedSpectrometer
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


def _require_dll(spec, backend: str, dll_name: str) -> None:
    if not getattr(spec, "dll_path", ""):
        raise RuntimeError(f"{backend} requires a valid {dll_name} path.")
    if not Path(spec.dll_path).is_file():
        raise RuntimeError(f"{backend} dll not found: {spec.dll_path}")


def _check(result, action: str = "") -> None:
    if result != "OK":
        raise RuntimeError(f"{action}: {result}" if action else str(result))


def _discover_ava1(dll_path: Optional[str]) -> List[Dict[str, str]]:
    spec = _new_spec_instance("Ava1", dll_path, serial=None, debug_mode=0)
    _require_dll(spec, "Ava1", "avaspecx64.dll")
    try:
        _check(spec.load_spec_dll(), "load DLL")
        _check(spec.initialize_dll(), "initialise DLL")
        res, count = spec.get_number_of_devices()
        _check(res, "count devices")
        res, info = spec.get_all_devices_info(count)
        _check(res, "read device info")

        devices = []
        for index in range(count):
            ident = getattr(info, f"a{index}")
            serial_number = _clean_text(getattr(ident, "SerialNumber", "")) or f"Ava1-{index + 1}"
            devices.append({"serial": serial_number, "label": f"{serial_number} [Ava1]", "type": "Ava1"})
        return devices
    finally:
        _release_probe(spec)


def _discover_hama2(dll_path: Optional[str]) -> List[Dict[str, str]]:
    spec = _new_spec_instance("Hama2", dll_path, serial=None, debug_mode=0)
    try:
        _check(spec.load_and_init_hama2_dll(), "load DCAM-API")
        res, count = spec.get_number_of_devices()
        _check(res, "count devices")
        res, devices_info = spec.get_all_devices_info(count)
        _check(res, "read device info")

        devices = []
        for index, dev_info in devices_info.items():
            serial_number = _clean_text(dev_info.get("id", "")) or f"Hama2-{index + 1}"
            model = _clean_text(dev_info.get("model", "Hamamatsu"))
            devices.append(
                {"serial": serial_number, "label": f"{serial_number} ({model}) [Hama2]", "type": "Hama2"}
            )
        return devices
    finally:
        _release_probe(spec)


def _discover_hama3(dll_path: Optional[str]) -> List[Dict[str, str]]:
    spec = _new_spec_instance("Hama3", dll_path, serial=None, debug_mode=0)
    _require_dll(spec, "Hama3", "DcIcUSB.dll")
    try:
        _check(spec.load_spec_dll(), "load DLL")
        _check(spec.initialize_dll(), "initialise DLL")
        res, count = spec.get_number_of_devices()
        _check(res, "count devices")

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
    finally:
        _release_probe(spec)


def _discover_hama4(dll_path: Optional[str]) -> List[Dict[str, str]]:
    spec = _new_spec_instance("Hama4", dll_path, serial=None, debug_mode=0)
    _require_dll(spec, "Hama4", "HiasApi.dll")
    try:
        _check(spec.load_spec_dll(), "load DLL")
        _check(spec.initialize_dll(), "initialise DLL")
        res, count = spec.get_number_of_devices()
        _check(res, "count devices")
        res, devices_info = spec.get_all_devices_info(count)
        _check(res, "read device info")

        devices = []
        for index, dev_info in devices_info.items():
            serial_number = _clean_text(dev_info.get("id", "")) or f"Hama4-{index + 1}"
            name = _clean_text(dev_info.get("name", "Hamamatsu"))
            devices.append(
                {"serial": serial_number, "label": f"{serial_number} ({name}) [Hama4]", "type": "Hama4"}
            )
        return devices
    finally:
        _release_probe(spec)


def _discover_minispec(dll_path: Optional[str]) -> List[Dict[str, str]]:
    spec = _new_spec_instance("MiniSpec", dll_path, serial=None, debug_mode=0)
    _require_dll(spec, "MiniSpec", "DCamUSB.dll")
    _check(spec.load_spec_dll(), "load DLL")
    _check(spec.initialize_dll(), "initialise DLL")
    res, count = spec.get_number_of_devices()
    _check(res, "count devices")
    if count == 0:
        return []
    res, devices_info = spec.get_all_devices_info(count)
    _check(res, "read device info")

    devices = []
    for index, dev_info in devices_info.items():
        serial_number = _clean_text(dev_info.get("id", "")) or f"MiniSpec-{index + 1}"
        model = _clean_text(dev_info.get("model", "Hamamatsu MiniSpec"))
        devices.append(
            {"serial": serial_number, "label": f"{serial_number} ({model}) [MiniSpec]", "type": "MiniSpec"}
        )
    return devices


def _discover_hama5(_dll_path: Optional[str]) -> List[Dict[str, str]]:
    module = importlib.import_module("spectrometers.hama5_spectrometer")
    return module.enumerate_devices()


def _discover_simulated(_dll_path: Optional[str]) -> List[Dict[str, str]]:
    return [{"serial": "SIM-0001", "label": "SIM-0001 (Simulated spectrometer) [Simulated]", "type": "Simulated"}]


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
    if normalized == "Hama5":
        return _discover_hama5(dll_path)
    if normalized == "Simulated":
        return _discover_simulated(dll_path)
    raise ValueError(f"Unsupported spectrometer type '{spec_type}'.")


def _format_discovery_failure(spec_type: str, errors: List[str], skipped: List[str], vendors: Optional[set]) -> str:
    lines = [f"No {spec_type} spectrometer detected." if spec_type != "Auto" else "No spectrometer detected."]
    if errors:
        lines.append("Backends that were probed:")
        lines.extend(f"  • {entry}" for entry in errors)
    if skipped:
        lines.append(f"Skipped (no matching USB device attached): {', '.join(skipped)}")
    if vendors is not None:
        seen = ", ".join(f"{vendor:04X}" for vendor in sorted(vendors)) or "none"
        lines.append(f"USB vendor IDs present: {seen}  (Avantes = 1992, Hamamatsu = 0661)")
    return "\n".join(lines)


def discover_spectrometers(
    spec_type: Optional[str],
    dll_path: Optional[str] = None,
    *,
    preferred_type: Optional[str] = None,
    progress=None,
    skip_absent_usb: bool = True,
) -> List[Dict[str, str]]:
    """Enumerate spectrometers of ``spec_type`` ("Auto" probes every backend).

    Auto mode is optimised three ways: the type that connected last time
    (``preferred_type``) is probed first; backends whose USB vendor is not
    attached are skipped without loading their DLL; and every probe releases
    its DLL session afterwards. ``progress(text)`` is called before each probe.
    """
    normalized = normalize_spec_type(spec_type)
    candidates = _candidate_types(spec_type, dll_path, preferred_type)
    vendors = present_usb_vendor_ids() if (normalized == "Auto" and skip_absent_usb and len(candidates) > 1) else None
    if vendors is not None and not vendors:
        # A bench PC always has some USB device (hub, mouse); an empty list means the
        # scan itself failed, so fall back to probing every backend.
        LOGGER.debug("USB pre-scan returned no devices at all; probing every backend")
        vendors = None

    errors: List[str] = []
    skipped: List[str] = []
    for candidate in candidates:
        wanted = BACKEND_USB_VENDORS.get(candidate)
        if vendors is not None and wanted and not (wanted & vendors):
            skipped.append(candidate)
            continue
        if progress is not None:
            try:
                progress(f"Probing {candidate}…")
            except Exception:
                pass
        candidate_dll = _candidate_dll_path(candidate, spec_type, dll_path)
        try:
            devices = _discover_for_type(candidate, candidate_dll)
        except Exception as exc:
            errors.append(f"{candidate}: {exc}")
            LOGGER.warning("Unable to discover %s spectrometers: %s", candidate, exc)
            continue
        if devices:
            if skipped:
                LOGGER.info("Auto probe skipped %s (no matching USB vendor attached)", ", ".join(skipped))
            return devices

    if errors or skipped:
        raise RuntimeError(_format_discovery_failure(normalized, errors, skipped, vendors))
    return []


def connect_spectrometer(
    spec_type: Optional[str],
    dll_path: Optional[str] = None,
    serial: Optional[str] = None,
    debug_mode: int = 1,
    devices: Optional[List[Dict[str, str]]] = None,
):
    """Connect to a spectrometer of ``spec_type`` (or the first that responds for "Auto").

    Pass ``devices`` (the list returned by ``discover_spectrometers``) to reuse an
    enumeration the caller already performed; otherwise the hardware is
    enumerated again here, which for some DLLs means a second full init cycle.
    """
    known_types = [device.get("type") for device in (devices or []) if device.get("type")]
    # When the caller already discovered devices, only their backend types are candidates.
    candidates = _candidate_types(spec_type, dll_path, preferred_type=known_types[0] if known_types else None)
    if known_types:
        candidates = [candidate for candidate in candidates if candidate in known_types] or candidates
    last_error = None

    for candidate in candidates:
        candidate_dll = _candidate_dll_path(candidate, spec_type, dll_path)
        try:
            known = [device for device in (devices or []) if device.get("type", candidate) == candidate]
            found = known or _discover_for_type(candidate, candidate_dll)
            if not found:
                continue
            selected_serial = serial or found[0]["serial"]
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
