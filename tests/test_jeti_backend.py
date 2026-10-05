"""Hardware-free tests for the JETI (Jeti1) backend and its loader wiring.

The real SDK is never called here: a fake object stands in for both DLLs and
mimics the entry points the backend uses (ctypes arguments in, results written
through the pointers), so the backend's real call sequence runs end to end.
A live check against a PE60_2 is in tools/jeti_smoke_test.py.
"""
from __future__ import annotations

import threading

import numpy as np
import pytest

import spectrometer_loader as loader
from domain.spectrometer import validate_spectrometer_backend
from spectrometers import jeti_spectrometer as jeti


class FakeJetiSdk:
    """Both JETI DLLs in one object; records every call as (name, args)."""

    def __init__(self, devices=(("15105", "2005184", ""),), npix=1024):
        self.devices = list(devices)
        self.npix = npix
        self.device_type = 4
        self.adc_bits = 16
        self.min_tint = 0.5
        self.sdelay = 0
        self.fit = (300.0, 0.4, 0.0, 0.0, 0.0)
        self.busy_polls = 2               # status answers "busy" this many times per frame
        self.always_busy = False
        self.start_rc = 0
        self.status_rc = 0
        # Like the PE60_2 (firmware 0.66.0): a break is not acknowledged and the device
        # stays busy (status busy, starts refused with JETI_BUSY) until the exposure ends.
        self.break_rc = 0x08
        self.break_stuck_polls = 0
        self.stuck_polls = 0
        self.calls = []
        self.open_handles = set()
        self.frames = 0
        self.pending = None
        self.on_status = None             # hook: called on every status poll
        self.frame_fn = lambda tint, aver: np.full(self.npix, 1000.0 + 10.0 * tint)

    def _log(self, name, *args):
        self.calls.append((name, args))

    def started(self):
        return [args for name, args in self.calls if name == "JETI_StartLightEx"]

    # --- jeti_spectro_ex ------------------------------------------------------
    def JETI_GetNumSpectroEx(self, p_count):
        self._log("JETI_GetNumSpectroEx")
        p_count._obj.value = len(self.devices)
        return 0

    def JETI_GetSerialSpectroEx(self, index, board, spec, device):
        self._log("JETI_GetSerialSpectroEx", index.value)
        b, s, d = self.devices[index.value]
        board.value, spec.value, device.value = b.encode(), s.encode(), d.encode()
        return 0

    def JETI_OpenSpectroEx(self, index, p_handle):
        self._log("JETI_OpenSpectroEx", index.value)
        handle = 0x7F000000 + index.value
        p_handle._obj.value = handle
        self.open_handles.add(handle)
        return 0

    def JETI_CloseSpectroEx(self, handle):
        self._log("JETI_CloseSpectroEx", handle)
        self.open_handles.discard(handle)
        return 0

    def JETI_PixelCountEx(self, handle, p_pixels):
        p_pixels._obj.value = self.npix
        return 0

    def JETI_StartLightEx(self, handle, tint, aver):
        self._log("JETI_StartLightEx", round(tint.value, 4), aver.value)
        if self.stuck_polls > 0:
            return 0x0E
        self.pending = (tint.value, aver.value)
        self.polls_left = self.busy_polls
        return self.start_rc

    def JETI_SpectroStatusEx(self, handle, p_busy):
        if self.on_status is not None:
            self.on_status()
        if self.status_rc:
            return self.status_rc
        if self.stuck_polls > 0:
            self.stuck_polls -= 1
            p_busy._obj.value = 1
            return 0
        busy = self.always_busy or self.polls_left > 0
        self.polls_left -= 1
        p_busy._obj.value = 1 if busy else 0
        return 0

    def JETI_SpectroBreakEx(self, handle):
        self._log("JETI_SpectroBreakEx", handle)
        self.stuck_polls = self.break_stuck_polls
        return self.break_rc

    def JETI_LightPixEx(self, handle, buf):
        self.frames += 1
        np.ctypeslib.as_array(buf)[:] = np.asarray(self.frame_fn(*self.pending), dtype=np.int32)
        return 0

    # --- jeti_core --------------------------------------------------------------
    def JETI_GetDeviceType(self, handle, p_type):
        p_type._obj.value = self.device_type
        return 0

    def JETI_GetFirmwareVersion(self, handle, buf):
        buf.value = b"SPECFIRM_PE60 VERSION 1.2.3"
        return 0

    def JETI_GetFit(self, handle, arr):
        for i, c in enumerate(self.fit):
            arr[i] = c
        return 0

    def JETI_GetADCRes(self, handle, p_bits):
        p_bits._obj.value = self.adc_bits
        return 0

    def JETI_GetMinTintConf(self, handle, p_tint):
        p_tint._obj.value = self.min_tint
        return 0

    def JETI_GetSDelay(self, handle, p_delay):
        p_delay._obj.value = self.sdelay
        return 0


@pytest.fixture
def sdk(monkeypatch):
    fake = FakeJetiSdk()
    monkeypatch.setitem(jeti._sdk, "spectro", fake)
    monkeypatch.setitem(jeti._sdk, "core", fake)
    monkeypatch.setitem(jeti._sdk, "path", r"C:\fake\jeti_spectro_ex64.dll")
    monkeypatch.setattr(jeti.Jeti_Spectrometer, "poll_interval_s", 0.0)
    return fake


@pytest.fixture
def spec(sdk):
    instance = jeti.Jeti_Spectrometer()
    assert instance.connect() == "OK"
    yield instance
    instance.disconnect()


# --- loader wiring --------------------------------------------------------------

def test_loader_lists_jeti1_and_normalizes_case():
    assert "Jeti1" in loader.SPECTROMETER_TYPE_OPTIONS
    assert loader.normalize_spec_type("jeti1") == "Jeti1"
    assert loader._import_backend_class("Jeti1") is jeti.Jeti_Spectrometer


@pytest.mark.parametrize("name", ["jeti_spectro_ex64.dll", r"C:\SDK\Win64\JETI_CORE64.DLL", "jeti_spectro_ex.dll"])
def test_loader_infers_jeti1_from_dll_name(name):
    assert loader.infer_spec_type_from_dll_path(name) == "Jeti1"


def test_auto_order_puts_jeti1_before_ocopt1_and_explicit_is_exclusive():
    order = loader._candidate_types("Auto", None)
    assert order.index("Jeti1") == order.index("OcOpt1") - 1
    assert loader._candidate_types("Jeti1", None) == ["Jeti1"]


@pytest.fixture
def probe_log(monkeypatch):
    log = {"probed": []}

    def fake_discover(candidate, _dll):
        log["probed"].append(candidate)
        return []

    monkeypatch.setattr(loader, "_discover_for_type", fake_discover)
    monkeypatch.setattr(loader, "_candidate_dll_path", lambda candidate, requested, dll: "")
    return log


def test_auto_probes_jeti1_only_when_a_jeti_usb_device_is_seen(monkeypatch, probe_log):
    monkeypatch.setattr(loader, "present_usb_vendor_ids", lambda: {loader.JETI_VENDOR_ID, 0x046D})
    with pytest.raises(RuntimeError):
        loader.discover_spectrometers("Auto")
    assert "Jeti1" in probe_log["probed"]

    probe_log["probed"].clear()
    monkeypatch.setattr(loader, "present_usb_vendor_ids", lambda: {loader.HAMAMATSU_VENDOR_ID})
    with pytest.raises(RuntimeError) as excinfo:
        loader.discover_spectrometers("Auto")
    assert "Jeti1" not in probe_log["probed"]
    message = str(excinfo.value)
    assert "Jeti1" in message and "select Jeti1" in message and "JETI = 0C6C" in message


def test_auto_never_runs_the_com_port_search_on_an_unknown_usb_scan(monkeypatch, probe_log):
    monkeypatch.setattr(loader, "present_usb_vendor_ids", lambda: None)
    assert loader.discover_spectrometers("Auto") == []
    assert "Jeti1" not in probe_log["probed"]
    assert "Ava1" in probe_log["probed"]               # everything else is still probed


def test_explicit_jeti1_always_searches(monkeypatch, probe_log):
    monkeypatch.setattr(loader, "present_usb_vendor_ids", lambda: pytest.fail("no pre-scan for explicit types"))
    assert loader.discover_spectrometers("Jeti1") == []
    assert probe_log["probed"] == ["Jeti1"]


def test_connect_in_auto_without_discovery_respects_the_gate(monkeypatch):
    tried = []
    monkeypatch.setattr(loader, "present_usb_vendor_ids", lambda: None)
    monkeypatch.setattr(loader, "_candidate_dll_path", lambda candidate, requested, dll: "")
    monkeypatch.setattr(loader, "_discover_for_type", lambda c, d: tried.append(c) or [])
    with pytest.raises(RuntimeError):
        loader.connect_spectrometer("Auto")
    assert "Jeti1" not in tried and "Ava1" in tried


def test_loader_discovery_uses_the_backend_enumeration(sdk):
    devices = loader.discover_spectrometers("Jeti1")
    assert devices == [{
        "serial": "2005184",
        "label": "2005184 (JETI board 15105, spectrometer 2005184) [Jeti1]",
        "type": "Jeti1",
        "board": "15105",
        "spectrometer": "2005184",
        "device": "",
    }]


# --- DLL lookup -----------------------------------------------------------------

def _sdk_tree(root, folder="Win64"):
    win = root / folder
    win.mkdir(parents=True)
    for name in (jeti.JETI_SPECTRO_DLL_NAME, jeti.JETI_CORE_DLL_NAME):
        (win / name).write_bytes(b"MZ")
    return win


def test_find_dll_prefers_jeti_sdk_home_then_bundled(monkeypatch, tmp_path):
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    for name in (jeti.JETI_SPECTRO_DLL_NAME, jeti.JETI_CORE_DLL_NAME):
        (bundled / name).write_bytes(b"MZ")
    monkeypatch.setattr(jeti, "JETI_BUNDLED_DIR", str(bundled))
    monkeypatch.delenv(jeti.JETI_SDK_HOME_ENV, raising=False)
    assert jeti.find_jeti_dll().lower() == str(bundled / jeti.JETI_SPECTRO_DLL_NAME).lower()

    win = _sdk_tree(tmp_path / "sdk")
    monkeypatch.setenv(jeti.JETI_SDK_HOME_ENV, str(tmp_path / "sdk"))
    assert jeti.find_jeti_dll().lower() == str(win / jeti.JETI_SPECTRO_DLL_NAME).lower()
    assert loader.suggest_default_dll_path("Jeti1").lower() == str(win / jeti.JETI_SPECTRO_DLL_NAME).lower()


def test_find_dll_needs_the_core_dll_next_to_it(monkeypatch, tmp_path):
    (tmp_path / jeti.JETI_SPECTRO_DLL_NAME).write_bytes(b"MZ")
    monkeypatch.setattr(jeti, "JETI_BUNDLED_DIR", str(tmp_path))
    monkeypatch.delenv(jeti.JETI_SDK_HOME_ENV, raising=False)
    assert jeti.find_jeti_dll() == ""


@pytest.mark.skipif(not jeti.IS_64BIT, reason="redirect applies to a 64-bit process")
def test_resolve_accepts_folder_core_dll_and_redirects_32bit_choice(tmp_path):
    win64 = _sdk_tree(tmp_path)
    (tmp_path / "Win32").mkdir()
    (tmp_path / "Win32" / "jeti_spectro_ex.dll").write_bytes(b"MZ")
    expected = (str(win64 / jeti.JETI_SPECTRO_DLL_NAME), str(win64 / jeti.JETI_CORE_DLL_NAME))
    assert jeti.resolve_dll_paths(str(tmp_path)) == expected                        # SDK root
    assert jeti.resolve_dll_paths(str(win64)) == expected                           # Win64 folder
    assert jeti.resolve_dll_paths(str(win64 / jeti.JETI_CORE_DLL_NAME)) == expected  # the core DLL
    assert jeti.resolve_dll_paths(str(tmp_path / "Win32" / "jeti_spectro_ex.dll")) == expected


def test_resolve_reports_a_missing_core_dll(tmp_path):
    (tmp_path / jeti.JETI_SPECTRO_DLL_NAME).write_bytes(b"MZ")
    with pytest.raises(RuntimeError, match=jeti.JETI_CORE_DLL_NAME):
        jeti.resolve_dll_paths(str(tmp_path / jeti.JETI_SPECTRO_DLL_NAME))


# --- helpers --------------------------------------------------------------------

def test_error_text_names_the_code_and_gives_hints():
    assert jeti.error_text(0) == "OK"
    assert jeti.error_text(0x0E) == "device busy (JETI error 0x0E JETI_BUSY)"
    assert "re-plug" in jeti.error_text(0xFF)
    assert "VersaSpec" in jeti.error_text(0x02)
    assert jeti.error_text(0x2711).startswith("Windows sockets error 10001")
    assert "unknown error" in jeti.error_text(0x77)


def test_wavelength_fit_is_used_only_when_plausible():
    wl = jeti.wavelengths_from_fit([300.0, 0.4, 0.0, 0.0, 0.0], 1024)
    assert wl[0] == pytest.approx(300.0) and wl[-1] == pytest.approx(300.0 + 0.4 * 1023)
    assert jeti.wavelengths_from_fit([800.0, -0.3, 0.0, 0.0, 0.0], 1024)[-1] < 800.0   # reversed readout is fine
    assert jeti.wavelengths_from_fit([0.0] * 5, 1024).size == 0                       # no fit stored
    assert jeti.wavelengths_from_fit([300.0, 0.4, -0.01, 0.0, 0.0], 1024).size == 0   # not monotonic
    assert jeti.wavelengths_from_fit([5000.0, 1.0, 0.0, 0.0, 0.0], 1024).size == 0    # out of range


# --- connect / measure ----------------------------------------------------------

def test_connect_reads_identity_limits_and_satisfies_the_contract(spec, sdk):
    assert validate_spectrometer_backend(spec) == []
    assert spec.is_open and spec.handle in sdk.open_handles
    assert spec.sn == "2005184"                       # no device serial: spectrometer serial
    assert spec.board_sn == "15105"
    assert spec.npix_active == 1024 and spec.rcm.shape == (1024,)
    assert spec.model == "SDCM4 / PE60_2 / PE65" and spec.device_type_code == 4
    assert spec.fw_version == "SPECFIRM_PE60 VERSION 1.2.3"
    assert spec.eff_saturation_limit == 65535.0
    assert spec.min_it_ms == pytest.approx(0.5) and spec.max_it_ms == 60000.0
    assert spec.wl.size == 1024 and spec.wl[0] == pytest.approx(300.0)
    assert spec.it_ms == 10.0
    assert sdk.started() == [(10.0, 1)]                # one test frame at connect
    assert spec.frame_overhead_ms is not None
    assert spec.get_device_info()["model"] == "SDCM4 / PE60_2 / PE65"


def test_connect_selects_the_requested_serial_from_any_of_the_three(sdk):
    sdk.devices = [("111", "222", "333"), ("444", "555", "")]
    spec = jeti.Jeti_Spectrometer()
    spec.sn = "444"                                    # the board serial of the second unit
    assert spec.connect() == "OK"
    assert ("JETI_OpenSpectroEx", (1,)) in sdk.calls
    assert spec.sn == "555"
    spec.disconnect()

    spec = jeti.Jeti_Spectrometer()
    spec.sn = "999"
    res = spec.connect()
    assert "'999' not found" in res and "333" in res and "555" in res
    assert not spec.is_open


def test_connect_reports_no_device_with_operator_hints(sdk):
    sdk.devices = []
    res = jeti.Jeti_Spectrometer().connect()
    assert res.startswith("No JETI spectrometer found") and "COM" in res


def test_set_it_clamps_and_never_sends_zero(spec, sdk):
    assert spec.set_it(0) == "OK" and spec.it_ms == pytest.approx(0.5)   # 0 would mean "adapt"
    assert spec.set_it(1e9) == "OK" and spec.it_ms == 60000.0
    assert spec.set_it("x").startswith("Invalid integration time")
    assert spec.set_it(float("nan")).startswith("Invalid integration time")
    assert spec.set_it(2.5) == "OK"
    assert spec.measure(ncy=1) == "OK"
    assert sdk.started()[-1] == (2.5, 1)


def test_measure_averages_single_scan_frames_in_float(spec, sdk):
    values = iter([100.0, 101.0, 103.0, 104.0])
    sdk.frame_fn = lambda tint, aver: np.full(sdk.npix, next(values))
    sdk.calls.clear()
    assert spec.measure(ncy=4) == "OK"
    assert spec.wait_for_measurement() == "OK"
    assert sdk.started() == [(10.0, 1)] * 4            # one firmware scan per cycle
    assert spec.ncy_read == 4
    assert np.allclose(spec.rcm, 102.0)                # float mean, not truncated
    assert np.allclose(spec.rcs, np.std([100, 101, 103, 104]))
    assert np.allclose(spec.rcl, 2.0)


def test_device_averaging_uses_one_firmware_measurement(spec, sdk):
    spec.device_averaging = True
    sdk.calls.clear()
    assert spec.measure(ncy=50) == "OK"
    assert sdk.started() == [(10.0, 50)]
    assert spec.ncy_read == 50
    assert np.allclose(spec.rcm, 1100.0) and not spec.rcs.any()


def test_saturation_detection_and_abort(spec, sdk):
    sdk.frame_fn = lambda tint, aver: np.full(sdk.npix, 65535.0)
    spec.abort_on_saturation = True
    assert spec.measure(ncy=5) == "OK"
    assert spec.ncy_read == 1 and spec.ncy_saturated == 1
    spec.abort_on_saturation = False                   # what the Setup tab sets after connecting
    assert spec.measure(ncy=3) == "OK"
    assert spec.ncy_read == 3 and spec.ncy_saturated == 3


def test_request_stop_cancels_the_frame_in_flight(spec, sdk):
    sdk.always_busy = True
    polls = {"n": 0}

    def stop_on_third_poll():
        polls["n"] += 1
        if polls["n"] == 3:
            spec.request_stop()

    sdk.on_status = stop_on_third_poll
    sdk.calls.clear()
    res = spec.measure(ncy=10)
    assert res == "Measurement stopped by user"
    assert any(name == "JETI_SpectroBreakEx" for name, _ in sdk.calls)
    assert len(sdk.started()) == 1
    spec.wait_for_measurement()

    sdk.always_busy = False                            # the next run is clean again
    sdk.on_status = None
    assert spec.measure(ncy=2) == "OK"
    assert spec.wait_for_measurement() == "OK"


def test_next_start_waits_until_the_cancelled_exposure_has_ended(spec, sdk):
    """The PE60_2 ignores the break: starting again at once would answer JETI_BUSY."""
    sdk.always_busy = True
    sdk.break_stuck_polls = 4
    sdk.on_status = lambda: spec.request_stop()
    assert spec.measure(ncy=1) == "Measurement stopped by user"
    assert spec._busy_until is not None
    sdk.always_busy = False
    sdk.on_status = None
    sdk.calls.clear()
    assert spec.measure(ncy=2) == "OK"
    assert sdk.started() == [(10.0, 1)] * 2            # no start was refused
    assert sdk.stuck_polls == 0 and spec._busy_until is None


def test_busy_at_start_is_waited_out_and_started_again(spec, sdk):
    sdk.stuck_polls = 3                                # e.g. reconnect right after a cancelled long exposure
    sdk.calls.clear()
    assert spec.measure(ncy=1) == "OK"
    assert sdk.started() == [(10.0, 1)] * 2            # refused once, then started


def test_user_stop_after_some_frames_keeps_them_but_reports_the_stop(spec, sdk):
    real = sdk.frame_fn

    def stop_after_second(tint, aver):
        if sdk.frames == 2:
            spec.request_stop()
        return real(tint, aver)

    sdk.frame_fn = stop_after_second
    sdk.frames = 0
    assert spec.measure(ncy=10) == "OK"
    assert spec.ncy_read == 2
    assert spec.wait_for_measurement() == "Measurement stopped by user"
    assert spec.wait_for_measurement() == "Measurement stopped by user"   # error persists, flag consumed
    assert spec.measure(ncy=1) == "OK" and spec.wait_for_measurement() == "OK"


def test_stuck_frame_is_cancelled_after_the_deadline(spec, sdk, monkeypatch):
    monkeypatch.setattr(spec, "frame_timeout_margin_s", 0.05)
    sdk.always_busy = True
    res = spec.measure(ncy=1)
    assert res.startswith("Cycle 1/1 failed: no frame within")
    assert any(name == "JETI_SpectroBreakEx" for name, _ in sdk.calls)


def test_dll_errors_are_reported_with_their_names(spec, sdk):
    sdk.start_rc = 0x0E
    assert spec.measure(ncy=1) == "Cycle 1/1 failed: JETI_StartLightEx: device busy (JETI error 0x0E JETI_BUSY)"
    sdk.start_rc = 0
    sdk.status_rc = 0xFF
    res = spec.measure(ncy=1)
    assert "fatal communication error" in res and "re-plug" in res


def test_measure_rejects_reentry_and_requires_connection(spec, sdk):
    assert spec._meas_lock.acquire(blocking=False)
    try:
        assert spec.measure(ncy=1) == "Measurement already in progress"
    finally:
        spec._meas_lock.release()
    spec.disconnect()
    assert spec.measure(ncy=1) == "JETI spectrometer not connected"
    assert spec.set_it(5.0) == "JETI spectrometer not connected"


def test_disconnect_closes_the_handle_and_reconnect_works(spec, sdk):
    handle = spec.handle
    assert spec.disconnect() == "OK"
    assert handle not in sdk.open_handles and not spec.is_open
    assert spec.disconnect() == "OK"                   # idempotent
    assert spec.connect() == "OK"                      # abort flag from disconnect does not leak
    assert spec.measure(ncy=2) == "OK"


def test_disconnect_from_another_thread_ends_a_running_measurement(spec, sdk):
    sdk.always_busy = True
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault("res", spec.measure(ncy=3)))
    worker.start()
    while not sdk.started()[1:]:                       # wait until the run's first frame started
        pass
    assert spec.disconnect() == "OK"
    worker.join(5.0)
    assert not worker.is_alive()
    assert result["res"] in ("No frames acquired", "OK")
    assert not sdk.open_handles
