"""Auto-probe behaviour of spectrometer_loader.discover_spectrometers (no hardware)."""
from __future__ import annotations

import pytest

import spectrometer_loader as loader


@pytest.fixture
def probe_log(monkeypatch):
    """Record which backends get probed; behaviour per backend is configurable."""
    log = {"probed": [], "results": {}, "errors": {}}

    def fake_discover(candidate, _dll):
        log["probed"].append(candidate)
        if candidate in log["errors"]:
            raise RuntimeError(log["errors"][candidate])
        return log["results"].get(candidate, [])

    monkeypatch.setattr(loader, "_discover_for_type", fake_discover)
    monkeypatch.setattr(loader, "_candidate_dll_path", lambda candidate, requested, dll: "")
    return log


def test_candidate_order_prefers_last_connected_type():
    assert loader._candidate_types("Auto", None) == loader.AUTO_PROBE_ORDER
    assert loader._candidate_types("Auto", None, preferred_type="MiniSpec")[0] == "MiniSpec"
    assert loader._candidate_types("Auto", None, preferred_type="Simulated") == loader.AUTO_PROBE_ORDER
    assert loader._candidate_types("Hama3", None, preferred_type="Ava1") == ["Hama3"]


def test_auto_skips_backends_without_their_usb_vendor(monkeypatch, probe_log):
    monkeypatch.setattr(loader, "present_usb_vendor_ids", lambda: {loader.HAMAMATSU_VENDOR_ID})
    probe_log["results"]["Hama3"] = [{"serial": "H3", "label": "H3 [Hama3]", "type": "Hama3"}]
    progress = []

    devices = loader.discover_spectrometers("Auto", progress=progress.append)

    assert devices[0]["type"] == "Hama3"
    assert "Ava1" not in probe_log["probed"]          # no Avantes device attached: DLL never loaded
    assert probe_log["probed"] == ["Hama4", "Hama3"]  # stops at the first backend that answers
    assert progress == ["Probing Hama4…", "Probing Hama3…"]


def test_auto_probes_everything_when_usb_scan_unavailable(monkeypatch, probe_log):
    monkeypatch.setattr(loader, "present_usb_vendor_ids", lambda: None)
    # Nothing attached and no backend error: an empty list (the Setup tab reports it).
    assert loader.discover_spectrometers("Auto") == []
    assert probe_log["probed"] == loader.AUTO_PROBE_ORDER


def test_empty_usb_scan_is_treated_as_unknown(monkeypatch, probe_log):
    """libusb returning no devices at all means the scan failed, not that the bus is empty."""
    monkeypatch.setattr(loader, "present_usb_vendor_ids", lambda: set())
    assert loader.discover_spectrometers("Auto") == []
    assert probe_log["probed"] == loader.AUTO_PROBE_ORDER


def test_auto_prefers_last_connected_backend(monkeypatch, probe_log):
    monkeypatch.setattr(loader, "present_usb_vendor_ids", lambda: None)
    probe_log["results"]["MiniSpec"] = [{"serial": "M1", "label": "M1 [MiniSpec]", "type": "MiniSpec"}]
    devices = loader.discover_spectrometers("Auto", preferred_type="MiniSpec")
    assert devices[0]["type"] == "MiniSpec"
    assert probe_log["probed"] == ["MiniSpec"]        # one probe instead of five


def test_failure_message_lists_probed_skipped_and_vendors(monkeypatch, probe_log):
    monkeypatch.setattr(loader, "present_usb_vendor_ids", lambda: {loader.AVANTES_VENDOR_ID, 0x046D})
    probe_log["errors"]["Ava1"] = "load DLL: avaspecx64.dll not found"
    with pytest.raises(RuntimeError) as excinfo:
        loader.discover_spectrometers("Auto")
    message = str(excinfo.value)
    assert message.startswith("No spectrometer detected.")
    assert "Ava1: load DLL: avaspecx64.dll not found" in message
    assert "Skipped (no matching USB device attached): Hama4, Hama3, Hama2, MiniSpec, Hama5" in message
    assert "046D" in message and "1992" in message
    assert " | " not in message


def test_explicit_type_never_uses_the_usb_prescan(monkeypatch, probe_log):
    calls = []
    monkeypatch.setattr(loader, "present_usb_vendor_ids", lambda: calls.append(1) or set())
    probe_log["results"]["Hama4"] = [{"serial": "H4", "label": "H4", "type": "Hama4"}]
    assert loader.discover_spectrometers("Hama4")[0]["type"] == "Hama4"
    assert calls == [] and probe_log["probed"] == ["Hama4"]


def test_failed_auto_discovery_includes_backends_that_returned_no_devices(monkeypatch, probe_log):
    monkeypatch.setattr(loader, "present_usb_vendor_ids", lambda: {loader.HAMAMATSU_VENDOR_ID})
    probe_log["errors"]["MiniSpec"] = "count devices: No driver was found"
    with pytest.raises(RuntimeError) as excinfo:
        loader.discover_spectrometers("Auto")
    message = str(excinfo.value)
    assert "MiniSpec: count devices: No driver was found" in message
    assert "Hama3: no compatible devices returned" in message
    assert "Hama5: no compatible devices returned" in message


def test_connect_only_tries_backends_of_the_discovered_devices(monkeypatch):
    attempted = []

    class FakeSpec:
        sn = "X1"
        npix_active = 2048
        rcm = []
        rcs = []
        it_ms = 1.0
        eff_saturation_limit = 65535.0
        abort_on_saturation = True

        def connect(self):
            return "OK"

        def disconnect(self):
            return "OK"

        def set_it(self, it):
            return "OK"

        def measure(self, ncy=1):
            return "OK"

        def wait_for_measurement(self, timeout=None):
            return "OK"

        def abort(self, ignore_errors=False):
            return "OK"

        def request_stop(self):
            return "OK"

    def fake_new_instance(spec_type, dll_path, serial, debug_mode):
        attempted.append(spec_type)
        return FakeSpec()

    monkeypatch.setattr(loader, "_new_spec_instance", fake_new_instance)
    monkeypatch.setattr(loader, "_candidate_dll_path", lambda candidate, requested, dll: "")
    monkeypatch.setattr(loader, "_discover_for_type", lambda c, d: pytest.fail("must reuse discovered devices"))
    devices = [{"serial": "X1", "label": "X1", "type": "MiniSpec"}]
    backend_type, spec = loader.connect_spectrometer("Auto", devices=devices)
    assert backend_type == "MiniSpec" and attempted == ["MiniSpec"]
