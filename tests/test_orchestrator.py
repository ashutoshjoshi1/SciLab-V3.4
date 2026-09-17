"""MeasurementOrchestrator end-to-end against the simulated spectrometer and bench."""
from __future__ import annotations

from typing import Dict, List, Tuple

import numpy as np
import pytest

from domain.measurement import MeasurementData
from services.measurement_orchestrator import (
    MeasurementOrchestrator,
    MeasurementOrchestratorCallbacks,
    MeasurementOrchestratorConfig,
)
from spectrometers.simulated_spectrometer import SimulatedBench, SimulatedLaserController, SimulatedSpectrometer

ALL_TAGS = ["377", "405", "445", "488", "517", "532", "640", "685", "Hg_Ar"]

# Literal copy of the SpectroApp tunables (app.py); they live on the Tk class and
# cannot be imported without _tkinter.
APP_CONFIG = dict(
    default_start_it={"532": 20.0, "517": 80.0, "Hg_Ar": 20.0, "default": 2.4},
    target_low=60000,
    target_high=65000,
    target_mid=62500,
    it_min=0.2,
    it_max=3000.0,
    it_step_up=0.3,
    it_step_down=0.1,
    max_it_adjust_iters=1000,
    sat_thresh=65400,
    n_sig=50,
    n_dark=50,
    n_sig_640=10,
    n_dark_640=10,
)


@pytest.fixture
def bench() -> SimulatedBench:
    return SimulatedBench()


@pytest.fixture
def sim_spec(bench):
    spec = SimulatedSpectrometer(bench=bench, time_scale=0.0, seed=1234)
    assert spec.connect() == "OK"
    spec.abort_on_saturation = False  # mirrors setup_tab after connect
    yield spec
    spec.disconnect()


@pytest.fixture
def sim_lasers(bench) -> SimulatedLaserController:
    return SimulatedLaserController(bench=bench)


@pytest.fixture
def make_orchestrator(sim_spec, sim_lasers):
    def _factory(**overrides):
        config = MeasurementOrchestratorConfig(**{**APP_CONFIG, **overrides})
        data = MeasurementData(npix=sim_spec.npix_active, serial_number=sim_spec.sn)
        auto_it_log: List[Tuple[str, float, float]] = []
        completed: List[str] = []
        callbacks = MeasurementOrchestratorCallbacks(
            prepare_devices=lambda: None,
            power_lookup=lambda tag: 0.01,
            auto_it_update=lambda tag, spectrum, it_ms, peak: auto_it_log.append((tag, float(it_ms), float(peak))),
            measurement_completed=lambda tag: completed.append(tag),
            countdown=lambda *_args: None,
            error_handler=lambda *_args: None,
        )
        orchestrator = MeasurementOrchestrator(
            spectrometer=sim_spec,
            laser_controller=sim_lasers,
            measurement_data=data,
            config=config,
            callbacks=callbacks,
            sleep_fn=lambda _s: None,
        )
        return orchestrator, data, auto_it_log, completed

    return _factory


def _rows_by_tag(data: MeasurementData) -> Dict[str, List[list]]:
    grouped: Dict[str, List[list]] = {}
    for row in data.rows:
        grouped.setdefault(str(row[1]), []).append(row)
    return grouped


def _frames_per_tag(auto_it_log) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for tag, _it, _peak in auto_it_log:
        counts[tag] = counts.get(tag, 0) + 1
    return counts


def test_full_run_writes_all_rows_and_turns_sources_off(make_orchestrator, bench):
    orchestrator, data, _log, completed = make_orchestrator()
    result = orchestrator.run(ALL_TAGS)

    assert result.errors == []
    assert result.completed_tags == ALL_TAGS[:6] + ["685", "Hg_Ar", "640"]  # 640 always runs last
    assert set(completed) == set(ALL_TAGS)
    # 8 auto-IT sources x (signal + dark) + 640 nm x (3 signal + 3 dark)
    assert result.rows_written == 8 * 2 + 3 + 3
    assert bench.active() == {}
    grouped = _rows_by_tag(data)
    assert sorted(grouped) == sorted(ALL_TAGS + [f"{t}_dark" for t in ALL_TAGS])


def test_auto_it_converges_into_target_band(make_orchestrator):
    orchestrator, data, log, _completed = make_orchestrator()
    tags = ["377", "405", "488", "532"]
    result = orchestrator.run(tags)
    assert result.errors == []

    last_peak_by_tag = {}
    for tag, _it, peak in log:
        last_peak_by_tag[tag] = peak
    for tag in tags:
        assert APP_CONFIG["target_low"] <= last_peak_by_tag[tag] <= APP_CONFIG["target_high"], tag

    for tag, rows in _rows_by_tag(data).items():
        if tag.endswith("_dark"):
            continue
        counts = np.asarray(rows[-1][4:], dtype=float)
        # 50-cycle mean vs. single-frame decision: allow shot-noise slack around the band.
        assert APP_CONFIG["target_low"] - 1500 <= counts.max() <= APP_CONFIG["target_high"] + 1500, tag


@pytest.mark.xfail(strict=True, reason="v3.3 additive auto-IT needs hundreds of frames; proportional controller lands in Phase 3")
def test_auto_it_iteration_budget(make_orchestrator):
    orchestrator, _data, log, _completed = make_orchestrator()
    orchestrator.run(["377", "405", "445", "488", "532", "685", "Hg_Ar"])
    frames = _frames_per_tag(log)
    assert frames, "no auto-IT frames recorded"
    assert max(frames.values()) <= 8, frames


def test_saturated_start_backs_off_then_settles(make_orchestrator):
    orchestrator, _data, log, _completed = make_orchestrator()
    result = orchestrator.run(["488"], start_it_override=50.0)
    assert result.errors == []
    peaks = [peak for tag, _it, peak in log if tag == "488"]
    its = [it for tag, it, _peak in log if tag == "488"]
    assert peaks[0] >= APP_CONFIG["sat_thresh"]          # started saturated
    assert its[1] == pytest.approx(its[0] * 0.7)          # backed off by 30 %
    assert APP_CONFIG["target_low"] <= peaks[-1] <= APP_CONFIG["target_high"]


def test_stop_after_first_tag(make_orchestrator, bench):
    orchestrator, data, _log, completed = make_orchestrator()
    result = orchestrator.run(["377", "405", "445"], should_continue=lambda: len(completed) == 0)
    assert result.stopped_early is True
    assert result.completed_tags == ["377"]
    assert result.rows_written == 2
    assert bench.active() == {}


def test_backend_error_is_recorded_and_run_continues(make_orchestrator, sim_spec):
    orchestrator, _data, _log, completed = make_orchestrator()
    sim_spec.inject_error["measure"] = "boom"
    result = orchestrator.run(["377", "405"])
    assert result.errors and result.errors[0].startswith("377: measure failed: boom")
    assert completed == ["405"]


def test_640_uses_fixed_integration_ladder(make_orchestrator, sim_spec):
    orchestrator, data, _log, _completed = make_orchestrator()
    result = orchestrator.run(["640"])
    assert result.errors == []
    grouped = _rows_by_tag(data)
    assert [row[2] for row in grouped["640"]] == [100.0, 500.0, 1000.0]
    assert [row[2] for row in grouped["640_dark"]] == [100.0, 500.0, 1000.0]
    longest = np.asarray(grouped["640"][-1][4:], dtype=float)
    assert longest.max() == sim_spec.eff_saturation_limit


def test_set_it_is_skipped_when_unchanged(make_orchestrator, sim_spec):
    """Signal and dark captures reuse the settled IT; set_it must not be re-sent for the same value."""
    orchestrator, _data, log, _completed = make_orchestrator()
    orchestrator.run(["517"])  # starts in band (80 ms x 800 counts/ms)
    frames = _frames_per_tag(log)["517"]
    # Expected DLL round trips: one set_it per auto-IT frame that changed IT, none for signal/dark reuse.
    assert sim_spec.set_it_calls <= frames + 1, (sim_spec.set_it_calls, frames)
