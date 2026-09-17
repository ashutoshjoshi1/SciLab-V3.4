"""Hamamatsu mini-spectrometer USB backend ("Hama5") for supported TM / TG / RC models.

The known product table covers C9407MA, C10082CA/C10083CA, and C11007MA.
These devices use the USB protocol implemented by the open-source drivers:

    "Linux python libraries for spectrometers", C. Frank / W. Schoenfeld,
    https://sourceforge.net/projects/usbspecdrivers/  (GPL-2, kernel driver
    usbhspec.c + HamamatsuMiniSpectrometer.py). Verified against C10082/C10083
    and C9407 hardware by its authors.

This backend re-implements that protocol on top of pyusb and libusb; Windows
requires a compatible WinUSB binding. SciLab's implementation still requires
physical bench validation.

C10785 belongs on the MiniSpec/DCamUSB path: the bundled DCamUSB.dll explicitly
checks that model name, and Hama5 excludes its reported product ID 0x3400.
Keep the working vendor driver and use its matching DCamUSB.dll for C10785.

Protocol summary (vendor requests on endpoint 0; all multi-byte values are
described exactly as the reference implementation sends/reads them):

    EEPROM read   : bmRequestType 0xC0, bRequest 0x04, wValue = byte offset
                    0x000..0x050 (step 0x10): a0..a5 wavelength-calibration
                    coefficients as 16-byte ASCII floats
                    0x1C8 unit id (8 bytes), 0x1D4 serial (8 bytes),
                    0x1DC sensor name (16 bytes)
    Temperature   : 0xC0, 0x06, 0, 0, 2 bytes -> high + low*1000/256  [deg C]
    Gain set/get  : 0x40, 0x0A, wValue 0x02, wIndex 0/1  |  0xC0, 0x0B, 0x02, 0, 1 byte
    IT set        : 0x40, 0x0A, wValue 0x01, data = 4 bytes (see encode_integration_us)
    IT get        : 0xC0, 0x0B, wValue 0x01, 4 bytes big-endian, microseconds
    Frame         : bulk IN, 0x1040 bytes = 2080 big-endian 16-bit words:
                    [0] status (2 = frame, 3 = no external power), [1] 0..2 index,
                    [2] active pixels, [3] start index, [4] end index,
                    [5] total pixels, [8] running frame counter,
                    pixels at start_index + sensor_pixel_offset for `active pixels` words.
                    All-zero words mean "not ready yet".

Wavelength of pixel p (1-based): a0 + a1*p + a2*p^2 + a3*p^3 + a4*p^4 + a5*p^5.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from spectrometers.spec_xfus import measurement_timeout_s, spec_clock

LOGGER = logging.getLogger(__name__)

HAMAMATSU_VENDOR_ID = 0x0661

# Product IDs from the reference driver's device table and udev rules.
# An unknown PID is not evidence of protocol compatibility; the DCamUSB product
# IDs below, including the reported C10785 PID, must remain excluded.
KNOWN_PRODUCTS: Dict[int, str] = {
    0x2906: "C9407MA (TG series)",
    0x2909: "C10082CA / C10083CA (TM series)",
    0x1300: "C11007MA (RC series)",
}
# Handled by the DCamUSB-based MiniSpec backend; never claim them here.
DCAMUSB_PRODUCT_IDS = {0x3400, 0x4607, 0x4608, 0x4609, 0x460A, 0x460B, 0x460C}

FRAME_BYTES = 0x1040
FRAME_WORDS = FRAME_BYTES // 2
HEADER_WORDS = 16
STATUS_FRAME = 2
STATUS_NO_POWER = 3

DEFAULT_MIN_IT_MS = 10.0        # 10,000 us: the sensor's shortest exposure in the reference driver
DEFAULT_MAX_IT_MS = 10_000.0    # 10 s
DEFAULT_SATURATION = 2 ** 16 - 1
NOT_READY_POLL_S = 0.002

# Vendor request codes
REQ_EEPROM_READ = 0x04
REQ_TEMPERATURE = 0x06
REQ_SET = 0x0A
REQ_GET = 0x0B
PARAM_INTEGRATION = 0x01
PARAM_GAIN = 0x02
EEPROM_COEFF_BASE = 0x000
EEPROM_UNIT_ID = 0x1C8
EEPROM_SERIAL = 0x1D4
EEPROM_SENSOR = 0x1DC

BM_VENDOR_IN = 0xC0
BM_VENDOR_OUT = 0x40


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------


def encode_integration_us(integration_us: int) -> bytes:
    """4-byte payload for the IT set request, exactly as the reference driver sends it.

    The device expects the high 16-bit word first and each word little-endian:
    [b2, b3, b0, b1] where b0 is the least-significant byte.
    """
    value = int(integration_us) & 0xFFFFFFFF
    return bytes([(value >> 16) & 0xFF, (value >> 24) & 0xFF, value & 0xFF, (value >> 8) & 0xFF])


def decode_integration_us(payload: Sequence[int]) -> int:
    """Integration time from the 4-byte IT get reply (big-endian)."""
    if len(payload) != 4:
        raise ValueError(f"IT reply must be 4 bytes, got {len(payload)}")
    b0, b1, b2, b3 = (int(x) & 0xFF for x in payload)
    return b3 + (b2 << 8) + (b1 << 16) + (b0 << 24)


def decode_temperature_c(payload: Sequence[int]) -> float:
    if len(payload) < 2:
        raise ValueError("temperature reply must be 2 bytes")
    return float(payload[0]) + float(payload[1]) * 1000.0 / 256.0 / 1000.0


def ascii_field(payload: Sequence[int]) -> str:
    return bytes(int(x) & 0xFF for x in payload).split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()


def sensor_pixel_offset(sensor_name: str) -> int:
    """First-pixel offset inside the frame for the sensor family (reference driver)."""
    name = (sensor_name or "").upper()
    if name.startswith("S10420"):
        return 10
    return 0


def wavelengths_from_coefficients(coefficients: Sequence[float], npix: int) -> np.ndarray:
    """a0 + a1*p + ... + a5*p^5 with p = 1..npix."""
    pixels = np.arange(1, int(npix) + 1, dtype=float)
    result = np.zeros_like(pixels)
    for power, coefficient in enumerate(coefficients):
        result += float(coefficient) * pixels ** power
    return result


def words_from_frame(raw: Sequence[int]) -> np.ndarray:
    data = np.frombuffer(bytes(bytearray(raw)), dtype=">u2")
    return data.astype(np.int64)


class FrameHeader:
    __slots__ = ("status", "index", "active_pixels", "start_index", "end_index", "total_pixels", "counter")

    def __init__(self, words: np.ndarray) -> None:
        self.status = int(words[0])
        self.index = int(words[1])
        self.active_pixels = int(words[2])
        self.start_index = int(words[3])
        self.end_index = int(words[4])
        self.total_pixels = int(words[5])
        self.counter = int(words[8]) if words.size > 8 else 0

    def is_consistent(self) -> bool:
        return (
            self.status == STATUS_FRAME
            and 0 <= self.index <= 2
            and self.active_pixels > 0
            and (self.total_pixels == self.end_index - self.start_index or self.total_pixels == self.end_index)
        )


def parse_frame(words: np.ndarray, pixel_offset: int) -> Tuple[Optional[FrameHeader], Optional[np.ndarray], int]:
    """Return (header, pixel counts, realign_words).

    ``realign_words`` > 0 means the stream is out of sync and that many words
    should be discarded from the endpoint before the next read (reference
    behaviour). ``header`` is None when the device has no frame ready yet.
    """
    if words.size < HEADER_WORDS or int(words.max()) == 0:
        return None, None, 0
    header = FrameHeader(words)
    if header.status == STATUS_NO_POWER:
        raise RuntimeError("Spectrometer reports no power (USB connected, external supply missing).")
    if not header.is_consistent():
        for offset in range(1, words.size - HEADER_WORDS):
            if int(words[offset]) == STATUS_FRAME:
                candidate = FrameHeader(words[offset:])
                if candidate.is_consistent():
                    return None, None, offset
        return None, None, 0
    start = header.start_index + int(pixel_offset)
    stop = start + header.active_pixels
    if stop > words.size:
        return None, None, 0
    return header, words[start:stop].astype(np.float64), 0


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class UsbTransport:
    """Thin wrapper over a pyusb device: control transfers + bulk reads."""

    def __init__(self, device, timeout_ms: int = 2000) -> None:
        self._dev = device
        self.timeout_ms = int(timeout_ms)
        self._endpoint = None

    @property
    def device(self):
        return self._dev

    def configure(self) -> None:
        import usb.util

        dev = self._dev
        dev.set_configuration()
        configuration = dev.get_active_configuration()
        interface = configuration[(0, 0)]
        self._endpoint = usb.util.find_descriptor(
            interface,
            custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN,
        )
        if self._endpoint is None:
            raise RuntimeError("No bulk IN endpoint on the spectrometer interface.")

    def ctrl_in(self, request: int, value: int, length: int, index: int = 0) -> bytes:
        data = self._dev.ctrl_transfer(BM_VENDOR_IN, request, value, index, length, self.timeout_ms)
        return bytes(bytearray(data))

    def ctrl_out(self, request: int, value: int, index: int = 0, data: Optional[bytes] = None) -> None:
        self._dev.ctrl_transfer(BM_VENDOR_OUT, request, value, index, data if data is not None else None, self.timeout_ms)

    def bulk_read(self, nbytes: int, timeout_ms: Optional[int] = None) -> bytes:
        if self._endpoint is None:
            raise RuntimeError("Transport not configured.")
        data = self._endpoint.read(int(nbytes), self.timeout_ms if timeout_ms is None else int(timeout_ms))
        return bytes(bytearray(data))

    def close(self) -> None:
        try:
            import usb.util

            usb.util.dispose_resources(self._dev)
        except Exception:
            pass


def _libusb_backend():
    """pyusb backend, falling back to the bundled libusb from ``libusb-package`` on Windows."""
    try:
        import usb.backend.libusb1 as libusb1

        backend = libusb1.get_backend()
        if backend is not None:
            return backend
    except Exception:
        pass
    try:
        import libusb_package

        return libusb_package.get_libusb1_backend()
    except Exception:
        return None


def find_usb_devices(vendor_id: int = HAMAMATSU_VENDOR_ID):
    """All Hamamatsu USB devices that are not DCamUSB units (pyusb device objects)."""
    try:
        import usb.core
    except ImportError as exc:
        raise RuntimeError("pyusb is not installed. Install with: pip install pyusb libusb-package") from exc
    backend = _libusb_backend()
    if backend is None:
        raise RuntimeError(
            "No libusb backend found. Install with: pip install libusb-package "
            "(and on Windows bind the spectrometer to the WinUSB driver with Zadig)."
        )
    devices = list(usb.core.find(find_all=True, idVendor=vendor_id, backend=backend) or [])
    return [dev for dev in devices if int(dev.idProduct) not in DCAMUSB_PRODUCT_IDS]


def describe_product(product_id: int) -> str:
    return KNOWN_PRODUCTS.get(int(product_id), f"Hamamatsu mini-spectrometer (PID 0x{int(product_id):04X})")


def enumerate_devices(transport_factory: Optional[Callable[[object], UsbTransport]] = None) -> List[Dict[str, str]]:
    """Discovery for the loader: one dict per spectrometer with its EEPROM identity."""
    factory = transport_factory or UsbTransport
    found: List[Dict[str, str]] = []
    for dev in find_usb_devices():
        transport = factory(dev)
        try:
            transport.configure()
            serial = ascii_field(transport.ctrl_in(REQ_EEPROM_READ, EEPROM_SERIAL, 8))
            sensor = ascii_field(transport.ctrl_in(REQ_EEPROM_READ, EEPROM_SENSOR, 16))
        except Exception as exc:
            LOGGER.warning("Hamamatsu USB device PID 0x%04X did not answer the identity request: %s", int(dev.idProduct), exc)
            continue
        finally:
            transport.close()
        product = describe_product(int(dev.idProduct))
        found.append(
            {
                "serial": serial or f"Hama5-{len(found) + 1}",
                "label": f"{serial} ({product}, sensor {sensor}) [Hama5]",
                "type": "Hama5",
                "product_id": f"0x{int(dev.idProduct):04X}",
                "sensor": sensor,
            }
        )
    return found


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class Hama5_Spectrometer:
    """SciLab backend for Hamamatsu mini-spectrometers on the specu1b USB protocol."""

    spec_type = "Hama5"

    def __init__(self, transport: Optional[UsbTransport] = None, transport_factory=None):
        # --- contract attributes ---
        self.sn = ""
        self.alias = "1"
        self.debug_mode = 0
        self.name = "Hamamatsu mini-spectrometer (USB)"
        self.npix_active = 2048  # replaced by the frame header's active-pixel count on connect
        self.npix_blind_left = 0
        self.npix_blind_right = 0
        self.nbits = 16
        self.eff_saturation_limit = float(DEFAULT_SATURATION)
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

        # --- device identity (filled by connect) ---
        self.sensor_name = ""
        self.unit_id = ""
        self.product_id: Optional[int] = None
        self.calibration_coefficients: List[float] = []
        self.wavelengths_nm = np.array([], dtype=float)
        self.pixel_offset = 0
        self.min_it_ms = DEFAULT_MIN_IT_MS
        self.max_it_ms = DEFAULT_MAX_IT_MS
        self.frame_counter: Optional[int] = None

        # --- internals ---
        self._transport = transport
        self._transport_factory = transport_factory or UsbTransport
        self._io_lock = threading.Lock()
        self._done = threading.Event()
        self._done.set()
        self._stop_requested = False
        self._abort = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self.logger = LOGGER

    def initialize_spec_logger(self) -> None:
        self.logger = logging.getLogger(f"spec{self.alias}.hama5")

    # -- connection ------------------------------------------------------------

    def _open_transport(self) -> UsbTransport:
        if self._transport is not None:
            return self._transport
        candidates = find_usb_devices()
        if not candidates:
            raise RuntimeError("No Hamamatsu mini-spectrometer (USB VID 0x0661) found.")
        chosen = None
        for dev in candidates:
            transport = self._transport_factory(dev)
            try:
                transport.configure()
                serial = ascii_field(transport.ctrl_in(REQ_EEPROM_READ, EEPROM_SERIAL, 8))
            except Exception:
                transport.close()
                continue
            if not self.sn or serial == self.sn:
                chosen = transport
                break
            transport.close()
        if chosen is None:
            raise RuntimeError(f"Hamamatsu mini-spectrometer with serial '{self.sn}' not found.")
        return chosen

    def connect(self) -> str:
        try:
            transport = self._open_transport()
            if self._transport is None:
                self._transport = transport
            elif getattr(transport, "_endpoint", None) is None:
                transport.configure()
            self._read_identity()
            self.set_it(self.it_ms if self.it_ms is not None else self.min_it_ms)
            self._probe_frame_geometry()
            self.error = "OK"
            self.logger.info(
                "Connected Hama5 %s: sensor %s, %d active pixels, %.1f-%.1f nm",
                self.sn, self.sensor_name, self.npix_active,
                float(self.wavelengths_nm[0]) if self.wavelengths_nm.size else float("nan"),
                float(self.wavelengths_nm[-1]) if self.wavelengths_nm.size else float("nan"),
            )
            return "OK"
        except Exception as exc:
            self.error = f"Could not connect to the Hamamatsu mini-spectrometer: {exc}"
            self.logger.error(self.error)
            return self.error

    def _read_identity(self) -> None:
        transport = self._transport
        with self._io_lock:
            serial = ascii_field(transport.ctrl_in(REQ_EEPROM_READ, EEPROM_SERIAL, 8))
            self.sensor_name = ascii_field(transport.ctrl_in(REQ_EEPROM_READ, EEPROM_SENSOR, 16))
            self.unit_id = ascii_field(transport.ctrl_in(REQ_EEPROM_READ, EEPROM_UNIT_ID, 8))
            coefficients = []
            for order in range(6):
                text = ascii_field(transport.ctrl_in(REQ_EEPROM_READ, EEPROM_COEFF_BASE + 0x10 * order, 16))
                try:
                    coefficients.append(float(text))
                except ValueError:
                    coefficients.append(0.0)
        if serial:
            self.sn = serial
        device = getattr(transport, "device", None)
        self.product_id = int(getattr(device, "idProduct", 0) or 0) or None
        self.calibration_coefficients = coefficients
        self.pixel_offset = sensor_pixel_offset(self.sensor_name)

    def _probe_frame_geometry(self) -> None:
        header, counts = self._read_frame(timeout_s=max(2.0, 8.0 * (self.it_ms or 10.0) / 1000.0))
        self.npix_active = int(header.active_pixels)
        self.wavelengths_nm = wavelengths_from_coefficients(self.calibration_coefficients, self.npix_active)
        self.frame_counter = header.counter
        self.rcm = counts.copy()
        self.rcs = np.zeros_like(counts)

    def disconnect(self, *args, **kwargs) -> str:
        self.abort(ignore_errors=True)
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()
        return "OK"

    # -- parameters --------------------------------------------------------------

    def set_it(self, it_ms: float) -> str:
        if self._transport is None:
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
        integration_us = int(round(clamped * 1000.0))
        try:
            with self._io_lock:
                deadline = time.monotonic() + 1.0
                while True:
                    self._transport.ctrl_out(REQ_SET, PARAM_INTEGRATION, 0, encode_integration_us(integration_us))
                    time.sleep(0.01)
                    readback = decode_integration_us(self._transport.ctrl_in(REQ_GET, PARAM_INTEGRATION, 4))
                    if readback == integration_us:
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError(f"device reports {readback} us after requesting {integration_us} us")
        except Exception as exc:
            self.error = f"Could not set integration time: {exc}"
            self.logger.error(self.error)
            return self.error
        self.it_ms = integration_us / 1000.0
        self.frame_counter = None  # frames exposed with the old IT must be skipped
        self.error = "OK"
        return "OK"

    def set_gain(self, high: bool) -> str:
        if self._transport is None:
            return "Spectrometer not connected"
        try:
            with self._io_lock:
                self._transport.ctrl_out(REQ_SET, PARAM_GAIN, 1 if high else 0, None)
            return "OK"
        except Exception as exc:
            return f"Could not set gain: {exc}"

    def read_aux_sensor(self, sname: str = "detector"):
        if self._transport is None:
            return "Spectrometer not connected", float("nan")
        try:
            with self._io_lock:
                payload = self._transport.ctrl_in(REQ_TEMPERATURE, 0, 2)
            return "OK", decode_temperature_c(payload)
        except Exception as exc:
            return f"Could not read temperature: {exc}", float("nan")

    # -- acquisition -------------------------------------------------------------

    def _read_frame(self, timeout_s: float) -> Tuple[FrameHeader, np.ndarray]:
        """Block until a consistent frame arrives (device free-runs), resyncing if needed."""
        deadline = time.monotonic() + max(0.05, float(timeout_s))
        while True:
            if self._abort.is_set():
                raise RuntimeError("Measurement aborted")
            with self._io_lock:
                raw = self._transport.bulk_read(FRAME_BYTES, timeout_ms=int(min(10000, max(500, timeout_s * 1000))))
            words = words_from_frame(raw)
            header, counts, realign = parse_frame(words, self.pixel_offset)
            if realign:
                with self._io_lock:
                    self._transport.bulk_read(realign * 2)
                continue
            if header is not None:
                return header, counts
            if time.monotonic() > deadline:
                raise RuntimeError("Timeout waiting for a spectrum frame")
            time.sleep(NOT_READY_POLL_S)

    def _read_new_frame(self, timeout_s: float) -> Tuple[FrameHeader, np.ndarray]:
        """Skip frames already seen (or exposed before the last IT change)."""
        deadline = time.monotonic() + max(0.05, float(timeout_s))
        skip_until = self.frame_counter
        while True:
            header, counts = self._read_frame(max(0.05, deadline - time.monotonic()))
            if skip_until is None:
                # Unknown history (fresh connect / IT change): the first frame may have
                # been exposed with old settings, drop exactly one.
                skip_until = header.counter
                continue
            if header.counter != skip_until:
                self.frame_counter = header.counter
                return header, counts
            if time.monotonic() > deadline:
                raise RuntimeError("Timeout waiting for a new spectrum frame")
            time.sleep(NOT_READY_POLL_S)

    def measure(self, ncy: int = 1) -> str:
        if self._transport is None:
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
        self.measuring = True
        self._done.clear()
        self.meas_start_time = spec_clock.now()
        self._worker = threading.Thread(target=self._acquire, name=f"hama5-acquire-{self.alias}", daemon=True)
        self._worker.start()
        return "OK"

    def _acquire(self) -> None:
        per_frame_timeout = max(2.0, 8.0 * float(self.it_ms) / 1000.0)
        shift = None
        sum_y = None
        sum_yy = None
        handled = 0
        saturated = 0
        try:
            for _ in range(self.ncy_requested):
                if self._abort.is_set():
                    break
                _header, counts = self._read_new_frame(per_frame_timeout)
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
                        # Keep the saturated frame as the result (auto-IT needs to see the
                        # clipped peak) but do not spend the remaining cycles on it.
                        break
            if handled > 0:
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
        finally:
            self.measuring = False
            self._done.set()

    def wait_for_measurement(self, timeout: Optional[float] = None) -> str:
        if timeout is None:
            timeout = measurement_timeout_s(self.ncy_requested, self.it_ms)
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
        self.measuring = False
        self._done.set()
        return "OK"

    def request_stop(self) -> str:
        self._stop_requested = True
        return self.abort(ignore_errors=True)

    # -- loader-facing helpers -----------------------------------------------------

    def get_wavelengths(self) -> np.ndarray:
        return self.wavelengths_nm.copy()
