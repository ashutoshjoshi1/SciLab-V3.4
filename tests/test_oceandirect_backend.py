"""Hardware-free tests for the OceanDirect (OcOpt1) backend and its loader wiring.

The real DLL is never loaded here; the backend's simulation mode and the pure
helpers are exercised instead.  A live check against the Flame-S is in
tools/oceandirect_smoke_test.py.
"""
from __future__ import annotations

import numpy as np
import pytest

import spectrometer_loader as loader
from domain.spectrometer import validate_spectrometer_backend
from spectrometers import oceandirect_spectrometer as od


def test_loader_lists_ocopt1_and_normalizes_case():
    assert "OcOpt1" in loader.SPECTROMETER_TYPE_OPTIONS
    assert loader.normalize_spec_type("ocopt1") == "OcOpt1"
    assert loader.normalize_spec_type("OCOPT1") == "OcOpt1"


def test_loader_infers_ocopt1_from_dll_name():
    assert loader.infer_spec_type_from_dll_path(r"C:\Program Files\Ocean Optics\OceanDirect SDK\lib\OceanDirect.dll") == "OcOpt1"
    assert loader.infer_spec_type_from_dll_path("oceandirect.DLL") == "OcOpt1"
    assert loader.infer_spec_type_from_dll_path("avaspecx64.dll") == "Ava1"


def test_auto_mode_includes_ocopt1_last():
    candidates = loader._candidate_types("Auto", None)
    assert candidates[-1] == "OcOpt1"
    assert candidates[:5] == ["Ava1", "Hama4", "Hama3", "Hama2", "MiniSpec"]


def test_explicit_ocopt1_only_tries_ocopt1():
    assert loader._candidate_types("OcOpt1", None) == ["OcOpt1"]


def test_import_backend_class():
    cls = loader._import_backend_class("OcOpt1")
    assert cls is od.OceanDirect_Spectrometer


def test_find_dll_returns_string(monkeypatch, tmp_path):
    # Point OCEANDIRECT_HOME at a fake SDK layout and check the lookup order works.
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "OceanDirect.dll").write_bytes(b"MZ")
    monkeypatch.setenv(od.OCEANDIRECT_HOME_ENV, str(tmp_path))
    assert od.find_oceandirect_dll().lower() == str(lib / "OceanDirect.dll").lower()
    assert loader.suggest_default_dll_path("OcOpt1").lower() == str(lib / "OceanDirect.dll").lower()


def test_simulated_backend_satisfies_contract_and_measures():
    spec = od.OceanDirect_Spectrometer()
    spec.simulation_mode = True
    spec.alias = "1"
    spec.initialize_spec_logger()

    assert validate_spectrometer_backend(spec) == []
    assert spec.spec_type == "OcOpt1"
    assert spec.connect() == "OK"
    assert spec.sn
    assert len(spec.wl) == spec.npix_active

    assert spec.set_it(5.0) == "OK"
    assert spec.it_ms == 5.0
    # Clamping to the device range
    assert spec.set_it(0.0001) == "OK"
    assert spec.it_ms == spec.min_it_ms

    assert spec.measure(ncy=4) == "OK"
    assert spec.wait_for_measurement() == "OK"
    assert spec.ncy_read == 4
    assert spec.rcm.shape == (spec.npix_active,)
    assert spec.rcs.shape == (spec.npix_active,)
    assert spec.rcl.shape == (spec.npix_active,)
    assert np.all(np.isfinite(spec.rcm))
    assert np.all(spec.rcs >= 0)
    assert spec.disconnect() == "OK"
    assert not spec.is_open


def test_frame_stats_mean_std_and_spread():
    stats = od._FrameStats(3)
    stats.add(np.array([1.0, 2.0, 3.0]))
    stats.add(np.array([3.0, 2.0, 1.0]))
    mean, std, spread = stats.result()
    assert np.allclose(mean, [2.0, 2.0, 2.0])
    assert np.allclose(std, [1.0, 0.0, 1.0])
    assert np.allclose(spread, [1.0, 0.0, 1.0])


def test_saturation_aborts_cycle_loop(monkeypatch):
    spec = od.OceanDirect_Spectrometer()
    spec.simulation_mode = True
    assert spec.connect() == "OK"
    spec.abort_on_saturation = True

    saturated = np.full(spec.npix_active, spec.eff_saturation_limit, dtype=np.float64)
    monkeypatch.setattr(spec, "_simulated_frame", lambda: saturated)
    assert spec.measure(ncy=5) == "OK"
    assert spec.ncy_saturated == 1
    assert spec.ncy_read == 1          # loop stopped after the first saturated frame

    spec.abort_on_saturation = False
    assert spec.measure(ncy=3) == "OK"
    assert spec.ncy_read == 3
    assert spec.ncy_saturated == 3


def test_it_change_discards_one_frame(monkeypatch):
    spec = od.OceanDirect_Spectrometer()
    spec.simulation_mode = True
    assert spec.connect() == "OK"
    calls = {"n": 0}

    def fake_frame():
        calls["n"] += 1
        return np.zeros(spec.npix_active)

    monkeypatch.setattr(spec, "_simulated_frame", fake_frame)
    spec.measure(ncy=2)
    first = calls["n"]                 # connect() left the discard flag set -> 1 + 2
    spec.measure(ncy=2)
    assert calls["n"] - first == 2     # no IT change -> no discard
    spec.set_it(20.0)
    spec.measure(ncy=2)
    assert calls["n"] - first == 5     # IT change -> one extra discarded frame


def test_measure_rejects_reentry():
    spec = od.OceanDirect_Spectrometer()
    spec.simulation_mode = True
    assert spec.connect() == "OK"
    assert spec._meas_lock.acquire(blocking=False)
    try:
        assert spec.measure(ncy=1) == "Measurement already in progress"
    finally:
        spec._meas_lock.release()


@pytest.mark.parametrize("code,expected", [(0, "OK")])
def test_get_error_ok(code, expected):
    spec = od.OceanDirect_Spectrometer()
    assert spec.get_error(code) == expected
    assert spec.get_error(5).startswith("OceanDirect error 5")  # no DLL loaded -> numeric fallback


def test_request_stop_breaks_cycle_loop_and_reports_user_stop():
    """The UI Stop button must end the run early and not look like a clean finish."""
    spec = od.OceanDirect_Spectrometer()
    spec.simulation_mode = True
    spec.alias = "1"
    spec.initialize_spec_logger()
    assert spec.connect() == "OK"
    assert spec.set_it(5.0) == "OK"
    assert spec.measure(ncy=1) == "OK"        # consumes the discard frame set_it armed

    real_frame = spec._simulated_frame

    def stop_during_first_cycle():
        spec.request_stop()
        return real_frame()

    spec._simulated_frame = stop_during_first_cycle
    assert spec.measure(ncy=10) == "OK"
    assert spec.wait_for_measurement() == "Measurement stopped by user"
    assert spec.ncy_read == 1                 # broke out instead of running all 10
    assert spec.rcm.shape == (spec.npix_active,)

    # The flag is consumed, so the next run reports its own status.
    spec._simulated_frame = real_frame
    assert spec.measure(ncy=2) == "OK"
    assert spec.wait_for_measurement() == "OK"
    assert spec.ncy_read == 2
    assert spec.disconnect() == "OK"


def test_request_stop_wakes_waiters_and_ends_cycle_loop():
    spec = od.OceanDirect_Spectrometer()
    spec.simulation_mode = True
    assert spec.connect() == "OK"
    assert spec.request_stop() == "OK"
    assert spec._abort_requested is True
    # a new measurement clears the request and runs normally
    assert spec.measure(ncy=2) == "OK"
    assert spec.ncy_read == 2
    assert spec.wait_for_measurement(timeout=1.0) == "OK"


def test_wait_for_measurement_times_out_when_no_measurement_finishes():
    spec = od.OceanDirect_Spectrometer()
    spec.simulation_mode = True
    assert spec.connect() == "OK"
    spec._meas_done_event.clear()          # pretend a frame is stuck
    res = spec.wait_for_measurement(timeout=0.05)
    assert "timed out" in res
    spec._meas_done_event.set()
