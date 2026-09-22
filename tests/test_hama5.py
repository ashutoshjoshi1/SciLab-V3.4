"""Hama5 (Hamamatsu mini-spectrometer USB protocol) against a fake USB device.

The fake reproduces the wire format documented in spectrometers/hama5_spectrometer.py
(taken from the reference open-source driver), so these tests pin the protocol
encoding even though no hardware is available on the development machine.
"""
from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from domain.spectrometer import validate_spectrometer_backend
from spectrometers import hama5_spectrometer as h5


class FakeEndpoint:
    def __init__(self, device):
        self.device = device

    def read(self, nbytes, timeout=None):
        return self.device.next_bulk(nbytes)


class FakeHamamatsuUsb:
    """Emulates control transfers + the free-running bulk frame stream."""

    def __init__(self, serial="510C2114", sensor="S10420-1106", coefficients=(320.0, 0.38, 0.0, 0.0, 0.0, 0.0),
                 active_pixels=2048, total_pixels=2068, start_index=16, product_id=0x2909, peak_pixel=700):
        self.idVendor = h5.HAMAMATSU_VENDOR_ID
        self.idProduct = product_id
        self.eeprom = {
            h5.EEPROM_SERIAL: serial.encode().ljust(8, b"\x00"),
            h5.EEPROM_SENSOR: sensor.encode().ljust(16, b"\x00"),
            h5.EEPROM_UNIT_ID: b"UNIT0001",
        }
        for order, value in enumerate(coefficients):
            self.eeprom[h5.EEPROM_COEFF_BASE + 0x10 * order] = f"{value:.8g}".encode().ljust(16, b"\x00")
        self.integration_us = 10000
        self.gain = 0
        self.active_pixels = active_pixels
        self.total_pixels = total_pixels
        self.start_index = start_index
        self.pixel_offset = h5.sensor_pixel_offset(sensor)
        self.counter = 100
        self.frame_index = 0
        self.not_ready_reads = 0     # how many all-zero reads to return before each frame
        self.pending_not_ready = 0
        self.peak_pixel = peak_pixel
        self.counts_per_us = 5.0     # peak counts per microsecond of IT
        self.rng = np.random.default_rng(3)
        self.set_calls = []
        self.configured = False
        self.disposed = False
        self.garbage_prefix_words = 0  # simulate an out-of-sync stream once

    # pyusb device surface -------------------------------------------------
    def set_configuration(self):
        self.configured = True

    def get_active_configuration(self):
        return {(0, 0): "interface0"}

    def ctrl_transfer(self, bmRequestType, bRequest, wValue=0, wIndex=0, data_or_wLength=None, timeout=None):
        if bmRequestType == h5.BM_VENDOR_IN:
            if bRequest == h5.REQ_EEPROM_READ:
                payload = self.eeprom.get(wValue, b"\x00" * 16)
                return list(payload[: int(data_or_wLength)])
            if bRequest == h5.REQ_GET and wValue == h5.PARAM_INTEGRATION:
                v = self.integration_us
                return [(v >> 24) & 0xFF, (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF]
            if bRequest == h5.REQ_GET and wValue == h5.PARAM_GAIN:
                return [self.gain]
            if bRequest == h5.REQ_TEMPERATURE:
                return [25, 128]  # 25.5 C
            raise AssertionError(f"unexpected IN request {bRequest:#x}/{wValue:#x}")
        if bmRequestType == h5.BM_VENDOR_OUT and bRequest == h5.REQ_SET:
            if wValue == h5.PARAM_INTEGRATION:
                b = list(data_or_wLength)
                # Decode exactly the way the device does (mirror of encode_integration_us).
                self.integration_us = b[2] | (b[3] << 8) | (b[0] << 16) | (b[1] << 24)
                self.set_calls.append(self.integration_us)
                return 4
            if wValue == h5.PARAM_GAIN:
                self.gain = int(wIndex)
                return 0
        raise AssertionError(f"unexpected request {bmRequestType:#x}/{bRequest:#x}")

    # bulk stream -------------------------------------------------------------
    def spectrum(self):
        px = np.arange(self.active_pixels, dtype=float)
        signal = self.counts_per_us * self.integration_us * np.exp(-0.5 * ((px - self.peak_pixel) / 4.0) ** 2)
        frame = 600.0 + signal + self.rng.normal(0, 3.0, self.active_pixels)
        return np.clip(np.rint(frame), 0, 65535).astype(">u2")

    def next_bulk(self, nbytes):
        if self.pending_not_ready > 0:
            self.pending_not_ready -= 1
            return bytes(nbytes)
        words = np.zeros(h5.FRAME_WORDS, dtype=">u2")
        header = [h5.STATUS_FRAME, self.frame_index % 3, self.active_pixels, self.start_index,
                  self.start_index + self.total_pixels, self.total_pixels, 0, 0, self.counter]
        words[: len(header)] = header
        start = self.start_index + self.pixel_offset
        words[start: start + self.active_pixels] = self.spectrum()
        self.counter += 1
        self.frame_index += 1
        self.pending_not_ready = self.not_ready_reads
        raw = words.tobytes()
        if self.garbage_prefix_words:
            raw = bytes(2 * self.garbage_prefix_words) + raw
            self.garbage_prefix_words = 0
        return raw[:nbytes] if nbytes < len(raw) else raw


class FakeTransport(h5.UsbTransport):
    def configure(self):
        self._dev.set_configuration()
        self._endpoint = FakeEndpoint(self._dev)

    def close(self):
        self._dev.disposed = True


@pytest.fixture
def fake_device():
    return FakeHamamatsuUsb()


@pytest.fixture
def spec(fake_device):
    backend = h5.Hama5_Spectrometer(transport=FakeTransport(fake_device))
    assert backend.connect() == "OK"
    yield backend
    backend.disconnect()


# --------------------------------------------------------------------------- pure protocol helpers

def test_integration_time_encoding_matches_reference_driver():
    assert h5.encode_integration_us(10000) == bytes([0x00, 0x00, 0x10, 0x27])
    assert h5.encode_integration_us(0x01020304) == bytes([0x02, 0x01, 0x04, 0x03])
    assert h5.decode_integration_us([0x00, 0x00, 0x27, 0x10]) == 10000
    with pytest.raises(ValueError):
        h5.decode_integration_us([1, 2, 3])


def test_sensor_pixel_offset_and_wavelengths():
    assert h5.sensor_pixel_offset("S10420-1106") == 10
    assert h5.sensor_pixel_offset("S8378-256N") == 0
    wl = h5.wavelengths_from_coefficients((300.0, 0.5, 0.0, 0.0, 0.0, 0.0), 4)
    assert wl.tolist() == [300.5, 301.0, 301.5, 302.0]
    assert h5.decode_temperature_c([25, 128]) == pytest.approx(25.5)


def test_parse_frame_not_ready_and_resync():
    zeros = np.zeros(h5.FRAME_WORDS, dtype=np.int64)
    assert h5.parse_frame(zeros, 0) == (None, None, 0)
    words = np.zeros(h5.FRAME_WORDS, dtype=np.int64)
    words[3:12] = [h5.STATUS_FRAME, 1, 5, 20, 30, 10, 0, 0, 7]   # header shifted by 3 words
    header, counts, realign = h5.parse_frame(words, 0)
    assert header is None and counts is None and realign == 3
    words = np.zeros(h5.FRAME_WORDS, dtype=np.int64)
    words[0:9] = [h5.STATUS_NO_POWER, 0, 5, 20, 30, 10, 0, 0, 7]
    with pytest.raises(RuntimeError, match="no power"):
        h5.parse_frame(words, 0)


# --------------------------------------------------------------------------- backend behaviour

def test_connect_reads_identity_and_geometry(spec, fake_device):
    assert spec.sn == "510C2114"
    assert spec.sensor_name == "S10420-1106"
    assert spec.pixel_offset == 10
    assert spec.npix_active == 2048
    assert spec.it_ms == pytest.approx(10.0)
    assert spec.wavelengths_nm[0] == pytest.approx(320.38)
    assert spec.product_id == 0x2909
    assert validate_spectrometer_backend(spec) == []


def test_set_it_clamps_to_device_range_and_verifies_readback(spec, fake_device):
    assert spec.set_it(2.4) == "OK"          # below the 10 ms floor
    assert spec.it_ms == pytest.approx(10.0)
    assert spec.set_it(250.0) == "OK"
    assert fake_device.integration_us == 250000 and spec.it_ms == pytest.approx(250.0)
    assert spec.set_it(99999.0) == "OK"
    assert spec.it_ms == pytest.approx(h5.DEFAULT_MAX_IT_MS)


def test_measure_averages_fresh_frames(spec, fake_device):
    spec.abort_on_saturation = False
    fake_device.not_ready_reads = 2          # device reports "not ready" twice per frame
    fake_device.garbage_prefix_words = 3     # and the stream starts out of sync
    fake_device.counts_per_us = 2.0          # 40,600 counts at 20 ms: bright but unsaturated
    assert spec.set_it(20.0) == "OK"
    assert spec.measure(ncy=8) == "OK"
    assert spec.wait_for_measurement() == "OK"
    assert spec.ncy_handled == 8
    assert spec.rcm.shape == (2048,) and spec.rcs.shape == (2048,)
    assert int(np.argmax(spec.rcm)) == fake_device.peak_pixel
    expected_peak = 600 + 2.0 * 20000
    assert spec.rcm.max() == pytest.approx(expected_peak, rel=0.01)
    assert 1.0 < spec.rcs.mean() < 6.0     # frame noise sigma = 3 counts


def test_saturation_aborts_when_requested(spec, fake_device):
    fake_device.counts_per_us = 1e4          # saturates every frame
    spec.abort_on_saturation = True
    assert spec.set_it(20.0) == "OK"
    spec.measure(ncy=5)
    assert spec.wait_for_measurement() == "OK"
    assert spec.ncy_saturated == 1 and spec.ncy_handled == 1
    assert spec.rcm.max() == 65535.0


def test_request_stop_unblocks_waiter(spec, fake_device):
    fake_device.not_ready_reads = 10_000    # frames never arrive
    spec.set_it(1000.0)
    spec.measure(ncy=50)
    threading.Timer(0.2, spec.request_stop).start()
    t0 = time.monotonic()
    result = spec.wait_for_measurement()
    assert time.monotonic() - t0 < 5.0
    assert result == "Measurement stopped by user"
    assert not spec.measuring


def test_temperature_and_gain(spec, fake_device):
    res, temperature = spec.read_aux_sensor()
    assert res == "OK" and temperature == pytest.approx(25.5)
    assert spec.set_gain(True) == "OK" and fake_device.gain == 1


def test_enumerate_devices_uses_eeprom_identity(monkeypatch, fake_device):
    monkeypatch.setattr(h5, "find_usb_devices", lambda vendor_id=h5.HAMAMATSU_VENDOR_ID: [fake_device])
    found = h5.enumerate_devices(transport_factory=FakeTransport)
    assert len(found) == 1
    entry = found[0]
    assert entry["serial"] == "510C2114" and entry["type"] == "Hama5"
    assert "S10420-1106" in entry["label"] and "TM series" in entry["label"]
    assert fake_device.disposed


def test_unknown_hamamatsu_pid_is_still_described():
    assert "PID 0x1A2B" in h5.describe_product(0x1A2B)
    assert 0x4608 in h5.DCAMUSB_PRODUCT_IDS  # MiniSpec units are never claimed by Hama5
