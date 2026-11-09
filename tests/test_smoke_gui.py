"""GUI smoke test: build the whole app once and drive it against the simulated backend.

Skipped automatically when the interpreter has no ``_tkinter`` (the default
pyenv Python on the dev Mac). Run it with the Tk-enabled venv:

    .venv/bin/python -m pytest tests/test_smoke_gui.py -q

One ``SpectroApp`` is built per test session: Tk + matplotlib's TkAgg backend
do not reliably survive destroying and recreating the root inside one process,
which also mirrors how the real application runs.
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")
pytest.importorskip("matplotlib")

pytestmark = pytest.mark.gui


@pytest.fixture(scope="session")
def tk_app():
    from tkinter import messagebox

    import app as app_module

    tmp = Path(tempfile.mkdtemp(prefix="scilab-gui-"))
    app_module.SpectroApp.SETTINGS_FILE = str(tmp / "settings.json")
    app_module.SpectroApp.RESULTS_ROOT = tmp / "results"

    dialogs = []
    originals = {}
    for name in ("showinfo", "showwarning", "showerror"):
        originals[name] = getattr(messagebox, name)
        setattr(messagebox, name, lambda title, message="", *a, _n=name, **k: dialogs.append((_n, title, message)) or "ok")

    try:
        app = app_module.SpectroApp()
    except tk.TclError as exc:  # no display
        pytest.skip(f"Tk display unavailable: {exc}")
    app.withdraw()
    app.dialogs = dialogs
    yield app
    for name, fn in originals.items():
        setattr(messagebox, name, fn)
    try:
        app._on_closing()
    except tk.TclError:
        pass


def pump_until(app, predicate, timeout=5.0):
    """Run the real Tk event loop until ``predicate()`` holds or ``timeout`` passes.

    A bare ``update()`` polling loop cannot be used while an acquisition is
    streaming: the UI pump re-arms its timer every tick and matplotlib queues
    idle draws, so ``update()`` (which returns only when Tk is idle) would
    never come back. ``mainloop()`` plus a scheduled check is how Tk is meant
    to be driven.
    """
    deadline = time.time() + timeout
    result = {"ok": False, "error": None}

    def _check():
        # A predicate that raises must fail the test, not leave mainloop
        # running with nothing scheduled to end it.
        try:
            if predicate():
                result["ok"] = True
                app.quit()
                return
        except Exception as exc:  # noqa: BLE001 - re-raised below
            result["error"] = exc
            app.quit()
            return
        if time.time() >= deadline:
            app.quit()
            return
        app.after(20, _check)

    app.after(0, _check)
    app.mainloop()
    if result["error"] is not None:
        raise result["error"]
    return result["ok"]


def errors(app):
    return [d for d in app.dialogs if d[0] == "showerror"]


def test_check_resolution_before_live_view_has_laser_controls(tk_app):
    """Regression: Check Resolution used to depend on Live View having been built first."""
    tk_app.nb.select(tk_app.check_res_tab)
    tk_app.update()
    assert str(tk_app.check_res_tab) in tk_app._built_tabs
    assert "405" in tk_app.laser_vars
    assert not errors(tk_app), tk_app.dialogs


def test_builds_every_tab(tk_app):
    for tab_id in tk_app.nb.tabs():
        tk_app.nb.select(tab_id)
        tk_app.update()
    assert set(tk_app._built_tabs) == set(tk_app.nb.tabs())
    assert not errors(tk_app), tk_app.dialogs


def test_simulated_backend_streams_live_frames(tk_app):
    tk_app.spec_type_var.set("Simulated")
    tk_app.dll_entry.delete(0, "end")
    tk_app.connect_spectrometer()  # discovery + connect run on a worker thread
    assert pump_until(tk_app, lambda: tk_app.spec_backend == "Simulated", timeout=10.0), tk_app.dialogs
    assert tk_app.npix == tk_app.spec.npix_active
    assert "Connected" in tk_app.status.message.cget("text")

    tk_app.nb.select(tk_app.live_tab)
    tk_app.update()
    tk_app.spec.set_active_source("445")
    assert tk_app.live_empty_text.get_visible()
    tk_app.start_live()
    assert tk_app.acq.owner == "Live View"
    assert str(tk_app.live_start_btn.cget("state")) == "disabled"
    assert str(tk_app.live_stop_btn.cget("state")) == "normal"
    assert pump_until(tk_app, lambda: len(tk_app.live_line.get_xdata()) == tk_app.npix, timeout=8.0)
    assert not tk_app.live_empty_text.get_visible()
    # Single-frame shot noise can move the argmax by a pixel either side of the line centre.
    assert abs(int(tk_app.live_line.get_ydata().argmax()) - tk_app.spec.pixel_of(445.0)) <= 1.5

    # The readout card beside the plot follows the frames and the stream state.
    assert pump_until(tk_app, lambda: tk_app.live_readout["peak"].cget("text") not in ("", "—"), timeout=5.0)
    assert tk_app.live_readout["status"].cget("text") == "Streaming"
    assert tk_app.live_readout["sat"].cget("text") == "OK"
    assert tk_app.live_readout["it"].cget("text").endswith(" ms")
    assert tk_app.live_readout["scale"].cget("text").endswith(" %")

    # A second acquisition must be refused while Live View owns the spectrometer.
    tk_app.run_all_selected()
    assert any("Live View is running" in d[2] for d in tk_app.dialogs if d[0] == "showwarning")

    tk_app.stop_live()
    assert pump_until(tk_app, lambda: not tk_app.live_running.is_set() and tk_app.acq.owner is None, timeout=5.0)
    assert pump_until(tk_app, lambda: tk_app.live_readout["status"].cget("text") == "Stopped", timeout=5.0)
    assert pump_until(tk_app, lambda: str(tk_app.live_stop_btn.cget("state")) == "disabled", timeout=5.0)
    assert str(tk_app.live_start_btn.cget("state")) == "normal"
    assert not errors(tk_app), tk_app.dialogs


def test_live_chart_settings_invert_and_log(tk_app):
    """The toolbar's sliders button opens the chart settings; invert and log apply at once and hold across frames.

    Invert mirrors the spectrum; the pixel axis keeps reading 0 -> end.
    """
    import numpy as np

    tk_app.nb.select(tk_app.live_tab)
    tk_app.update()
    tk_app._live_reset_view()
    ax = tk_app.live_ax

    window = tk_app.live_toolbar.configure_subplots()
    tk_app.update()
    assert window is tk_app.live_chart_settings and window.winfo_exists()
    assert tk_app.live_toolbar.configure_subplots() is window       # one window, raised again
    options = tk_app.live_chart_options

    npix = 2048
    x = np.arange(npix, dtype=float)
    y = np.full(npix, 800.0)
    y[:5] = 0.0                                                      # zeros must not upset the log axis
    y[1000] = 40000.0

    mirrored_peak = npix - 1 - 1000                                  # where pixel 1000 is drawn when inverted

    def show(peak):
        frame = y.copy()
        frame[1000] = peak
        drawn_at = mirrored_peak if tk_app.live_invert_x.get() else 1000
        tk_app._update_live_plot(x, frame)
        assert pump_until(tk_app, lambda: tk_app.live_line.get_ydata()[drawn_at] == peak, timeout=5.0)

    show(40000.0)
    options["invert_x"].invoke()
    assert tk_app.live_invert_x.get()
    # The trace already on the plot is mirrored at once; the axis is not turned round.
    assert not ax.xaxis_inverted() and ax.get_xlim() == (0, npix - 1)
    assert tk_app.live_line.get_ydata()[mirrored_peak] == 40000.0
    assert not np.any(tk_app.live_line.get_ydata()[-5:])             # the zero pixels 0-4 are now at the right end
    time.sleep(0.15)                                                 # let the throttled readout refresh on the next frame
    show(30000.0)
    assert ax.get_xlim() == (0, npix - 1)
    assert tk_app.live_readout["pixel"].cget("text") == str(mirrored_peak)  # the readout follows the plot

    options["log_y"].invoke()
    assert ax.get_yscale() == "log"
    bottom, top = ax.get_ylim()
    assert bottom == 100.0 and top >= 30000.0                         # the decade below the 800-count floor
    tk_app.live_canvas.draw()
    labels = [label.get_text() for label in ax.get_yticklabels(which="both") if label.get_visible()]
    assert {"100", "200", "500", "1000"} <= set(labels)              # plain counts, not powers of ten

    # Zoomed in on the peak: un-inverting moves the zoom with the peak, and frames do not undo the zoom.
    ax.set_xlim(mirrored_peak - 50, mirrored_peak + 50)
    tk_app.live_limits_locked = True
    options["invert_x"].invoke()
    assert ax.get_xlim() == (950, 1050)
    assert tk_app.live_line.get_ydata()[1000] == 30000.0
    show(40000.0)
    assert ax.get_xlim() == (950, 1050)
    # Reset Zoom brings the whole trace back (it used to leave a toolbar zoom in place).
    tk_app._live_reset_view()
    assert not tk_app.live_limits_locked and ax.get_xlim() == (0, npix - 1)

    options["log_y"].invoke()
    assert ax.get_yscale() == "linear" and ax.get_ylim()[0] == 0.0
    assert type(ax.yaxis.get_major_formatter()).__name__ == "ScalarFormatter"
    window.destroy()
    tk_app.update()
    assert not errors(tk_app), tk_app.dialogs


def test_check_resolution_fits_simulated_line(tk_app):
    tk_app.nb.select(tk_app.check_res_tab)
    tk_app.update()
    tk_app.spec.set_active_source("532")
    tk_app.check_res_it_entry.delete(0, "end")
    tk_app.check_res_it_entry.insert(0, "30")
    assert tk_app.check_res_empty_text.get_visible()
    tk_app.check_res_start_btn.invoke()
    assert tk_app.acq.owner == "Check Resolution"
    assert str(tk_app.check_res_start_btn.cget("state")) == "disabled"
    assert pump_until(tk_app, lambda: len(tk_app.check_res_ax.lines[3].get_xdata()) > 0, timeout=8.0)
    assert not tk_app.check_res_empty_text.get_visible()
    tk_app.check_res_stop_btn.invoke()
    assert pump_until(tk_app, lambda: tk_app.acq.owner is None, timeout=5.0)
    assert pump_until(tk_app, lambda: str(tk_app.check_res_stop_btn.cget("state")) == "disabled", timeout=5.0)
    assert not errors(tk_app), tk_app.dialogs


def _find_widgets(root, cls_name, text=None):
    """Widgets of a Tk class under ``root`` (depth first), optionally with a given text."""
    found = []
    for child in root.winfo_children():
        if child.winfo_class() == cls_name and (text is None or str(child.cget("text")) == text):
            found.append(child)
        found.extend(_find_widgets(child, cls_name, text))
    return found


def test_light_sources_card_sets_power_and_follows_add_remove(tk_app, monkeypatch):
    """Setup's source table: a setpoint reaches the (simulated) OBIS at once, a refused one is reported,
    and adding or removing a source updates every tab's list and the settings file."""
    import json

    tk_app.nb.select(tk_app.setup_tab)
    tk_app.update()
    tree = tk_app.sources_tree
    assert list(tree.get_children()) == tk_app.laser_tags()
    assert tree.set("405", "controller") == "OBIS ch 5" and tree.set("405", "setpoint") == "50.0 mW"
    assert tree.set("532", "setpoint") == "—"                     # relay sources only switch

    tree.selection_set("405")
    tk_app.update()
    assert tk_app.source_setpoint_entry.get() == "50.0"
    tk_app.source_setpoint_entry.delete(0, "end")
    tk_app.source_setpoint_entry.insert(0, "20")
    tk_app.source_apply_btn.invoke()
    assert pump_until(tk_app, lambda: tk_app.laser_configs["405"]["power_mw"] == 20.0, timeout=5.0), tk_app.dialogs
    assert tk_app.lasers.obis_setpoint_w(5) == 0.02                # 20 mW went out as 0.02 W
    assert pump_until(tk_app, lambda: tree.set("405", "setpoint") == "20.0 mW", timeout=5.0)

    # The simulated box refuses 500 mW (ERR-220): the operator is told and the table keeps 20 mW.
    tk_app.source_setpoint_entry.delete(0, "end")
    tk_app.source_setpoint_entry.insert(0, "500")
    tk_app.source_apply_btn.invoke()
    assert pump_until(tk_app, lambda: any("Setpoint 405 nm" in d[1] for d in tk_app.dialogs), timeout=5.0)
    assert "ERR-220" in [d for d in tk_app.dialogs if "Setpoint 405 nm" in d[1]][0][2]
    assert tk_app.laser_configs["405"]["power_mw"] == 20.0
    tk_app.dialogs.clear()

    tk_app.source_read_btn.invoke()
    assert pump_until(tk_app, lambda: tk_app._source_reports.get("405", {}).get("max_mw") == 100.0, timeout=5.0)
    assert "set 20.0" in tree.set("405", "reports")

    # Add 561 nm on OBIS slot 1 through the dialog.
    def _fill_dialog():
        dialog = tk_app.add_source_dialog
        entries = _find_widgets(dialog, "TEntry")
        entries[0].insert(0, "561")                                 # wavelength
        entries[1].delete(0, "end"); entries[1].insert(0, "15")     # setpoint
        _find_widgets(dialog, "TButton", "Add")[0].invoke()

    tk_app.after(200, _fill_dialog)
    tk_app.add_source()
    assert tk_app.laser_configs["561"] == {"type": "OBIS", "channel": 1, "power_mw": 15.0}
    assert "561" in tree.get_children() and "561" in tk_app.measure_vars and tk_app.measure_vars["561"].get()
    assert _find_widgets(tk_app.live_tab, "TCheckbutton", "561 nm") and _find_widgets(tk_app.check_res_tab, "TCheckbutton", "561 nm")
    with open(tk_app.SETTINGS_FILE, encoding="utf-8") as handle:
        saved = json.load(handle)
    assert saved["laser_configs_version"] == 2 and saved["laser_configs"]["561"]["power_mw"] == 15.0
    assert saved["laser_configs"]["405"]["power_mw"] == 20.0

    # Remove it again; the lamp cannot be removed.
    monkeypatch.setattr("tkinter.messagebox.askyesno", lambda *a, **k: True)
    tree.selection_set("561")
    tk_app.remove_source()
    assert "561" not in tk_app.laser_configs and "561" not in tree.get_children() and "561" not in tk_app.measure_vars
    assert not _find_widgets(tk_app.live_tab, "TCheckbutton", "561 nm")
    tree.selection_set("Hg_Ar")
    tk_app.remove_source()
    assert "Hg_Ar" in tk_app.laser_configs and any("cannot be removed" in d[2] for d in tk_app.dialogs)
    tk_app.dialogs.clear()
    assert not errors(tk_app), tk_app.dialogs


def test_auto_it_per_source_dialog_saves_start_and_step(tk_app):
    """Measurements › Auto-IT: a start IT and step per source, validated on the form and saved with the sources."""
    import json

    tk_app.nb.select(tk_app.measure_tab)
    tk_app.update()
    assert tk_app.auto_it_summary_label.cget("text").startswith("Defaults")

    def _fill_bad_then_good():
        start, step = tk_app.auto_it_entries["405"]
        start.insert(0, "200")
        step.insert(0, "lots")
        _find_widgets(tk_app.auto_it_dialog, "TButton", "Save")[0].invoke()
        assert tk_app.auto_it_dialog.winfo_exists()                 # refused: the dialog stays open
        step.delete(0, "end")
        step.insert(0, "5")
        start2, step2 = tk_app.auto_it_entries["445"]
        start2.insert(0, "100")
        step2.insert(0, "3")
        _find_widgets(tk_app.auto_it_dialog, "TButton", "Save")[0].invoke()

    tk_app.after(200, _fill_bad_then_good)
    tk_app.open_auto_it_settings()
    assert tk_app.laser_configs["405"]["start_it_ms"] == 200.0 and tk_app.laser_configs["405"]["it_step_ms"] == 5.0
    assert tk_app.laser_configs["445"]["start_it_ms"] == 100.0 and tk_app.laser_configs["445"]["it_step_ms"] == 3.0
    assert "start_it_ms" not in tk_app.laser_configs["488"]
    assert "405 nm, 445 nm" in tk_app.auto_it_summary_label.cget("text")
    with open(tk_app.SETTINGS_FILE, encoding="utf-8") as handle:
        saved = json.load(handle)
    assert saved["laser_configs"]["405"]["start_it_ms"] == 200.0 and saved["laser_configs"]["445"]["it_step_ms"] == 3.0

    # Reset to defaults clears the overrides again.
    def _reset():
        _find_widgets(tk_app.auto_it_dialog, "TButton", "Reset to defaults")[0].invoke()
        _find_widgets(tk_app.auto_it_dialog, "TButton", "Save")[0].invoke()

    tk_app.after(200, _reset)
    tk_app.open_auto_it_settings()
    assert "start_it_ms" not in tk_app.laser_configs["405"] and "it_step_ms" not in tk_app.laser_configs["445"]
    assert tk_app.auto_it_summary_label.cget("text").startswith("Defaults")
    assert not errors(tk_app), tk_app.dialogs


def test_disconnect_restores_hardware_lasers(tk_app):
    from hardware.controllers import LaserController

    tk_app.disconnect_spectrometer()
    tk_app.update()
    assert tk_app.spec is None
    assert isinstance(tk_app.lasers, LaserController)
    assert not errors(tk_app), tk_app.dialogs


def test_laser_chip_tracks_sources(tk_app):
    tk_app.note_source_state("532", True)
    tk_app.update()
    assert tk_app._laser_chip.text.cget("text") == "532 nm on"
    assert tk_app.laser_vars["532"].get() is True
    assert tk_app._laser_all_off_btn.winfo_manager() == "pack"

    tk_app.note_source_state("Hg_Ar", True)
    tk_app.update()
    assert tk_app._laser_chip.text.cget("text") == "532 nm, Hg-Ar on"

    tk_app.note_all_sources_off()
    tk_app.update()
    assert tk_app._laser_chip.text.cget("text") == "Sources off"
    assert not tk_app.laser_vars["532"].get() and not tk_app.laser_vars["Hg_Ar"].get()
    assert tk_app._laser_all_off_btn.winfo_manager() == ""
    assert not errors(tk_app), tk_app.dialogs


def test_run_progress_panel_and_chip_follow_the_model(tk_app):
    model = tk_app.run_progress
    panel = tk_app.run_progress_panel
    chip = tk_app._run_chip.chip

    model.start(["640", "405", "532"])
    model.set_sequence(["405", "532", "640"])      # as the orchestrator reorders it
    model.source_started("405")
    model.frame("405", 2.7, 1200.0)
    assert pump_until(tk_app, lambda: "Measuring 405 nm" in panel.status_label.cget("text"), timeout=5.0)
    assert [row["name"].cget("text") for row in panel._rows.values()] == ["405 nm", "532 nm", "640 nm"]
    assert chip.winfo_manager() == "pack"
    assert pump_until(tk_app, lambda: "2.70 ms" in chip.text.cget("text"), timeout=5.0)
    assert "1,200 cts" in panel._rows["405"]["value"].cget("text")

    model.source_completed("405")
    model.source_started("532")
    model.source_failed("532", "auto-IT did not reach the target band")
    model.finish(stopped_early=True)
    assert pump_until(tk_app, lambda: panel.status_label.cget("text").startswith("Stopped"), timeout=5.0)
    rows = panel._rows
    assert rows["405"]["glyph"].cget("text") == "✓"
    assert rows["532"]["glyph"].cget("text") == "✕" and "failed" in rows["532"]["value"].cget("text")
    assert rows["532"]["tip"].text == "auto-IT did not reach the target band"
    assert rows["640"]["value"].cget("text") == "not run"
    assert chip.text.cget("text").startswith("Stopped")
    tk_app._run_chip.hide()
    assert not errors(tk_app), tk_app.dialogs


def test_idle_buttons_spinboxes_and_measure_empty_state(tk_app):
    """Stop buttons are greyed while nothing runs; IT fields step; the plot hint goes with the first frame."""
    import numpy as np
    from tkinter import ttk

    assert isinstance(tk_app.it_entry, ttk.Spinbox) and isinstance(tk_app.check_res_it_entry, ttk.Spinbox)
    assert float(tk_app.it_entry.get()) == 2.4
    assert str(tk_app.live_stop_btn.cget("state")) == "disabled"
    assert str(tk_app.check_res_stop_btn.cget("state")) == "disabled"
    assert tk_app.stop_all_btn.instate(["disabled"])
    assert tk_app.clear_all_btn.instate(["disabled"])

    tk_app._ensure_measure_figure()
    assert tk_app.meas_empty_text.get_visible()
    tk_app._render_auto_it_plot(("405", np.zeros(64), 2.4, 100.0))
    assert not tk_app.meas_empty_text.get_visible()
    assert not errors(tk_app), tk_app.dialogs


def test_setup_status_words_dll_row_and_advanced_card(tk_app):
    assert tk_app.obis_status.cget("text").endswith("not tested")
    tk_app.set_port_status(tk_app.obis_status, "failed", "COM10 could not be opened")
    assert tk_app.obis_status.cget("text").endswith("not found")
    tk_app.set_port_status(tk_app.obis_status, "open")
    assert tk_app.obis_status.cget("text").endswith("open")

    tk_app.dll_entry.delete(0, "end")
    tk_app.spec_type_var.set("Auto")
    tk_app._update_dll_row_visibility()
    assert tk_app.dll_entry.winfo_manager() == ""
    tk_app.spec_type_var.set("OcOpt1")
    tk_app._update_dll_row_visibility()
    assert tk_app.dll_entry.winfo_manager() == "grid"
    tk_app.spec_type_var.set("Auto")
    tk_app._update_dll_row_visibility()

    assert tk_app.advanced_group.winfo_manager() == ""       # collapsed: no bench option is on
    tk_app._show_advanced(True)
    assert tk_app.advanced_group.winfo_manager() == "pack"
    tk_app._show_advanced(False)
    assert not errors(tk_app), tk_app.dialogs


def test_log_drawer_collects_records(tk_app):
    import logging

    assert not tk_app.log_drawer.shown
    logging.getLogger("tests.drawer").warning("drawer smoke warning")
    assert pump_until(tk_app, lambda: "drawer smoke warning" in tk_app.log_drawer.contents(), timeout=5.0)
    assert "\u26a0" in tk_app.log_drawer.toggle_btn.cget("text")     # counted while closed
    tk_app.log_drawer.toggle()
    assert tk_app.log_drawer.shown and tk_app.log_drawer.winfo_manager() == "pack"
    assert tk_app.log_drawer.toggle_btn.cget("text") == "Log"
    tk_app.log_drawer.toggle()
    assert not tk_app.log_drawer.shown
    assert not errors(tk_app), tk_app.dialogs


def test_live_frame_averaging_dark_and_save(tk_app, monkeypatch, tmp_path):
    """Live View: a rolling average, a captured dark, the peak mark and the frame CSV."""
    import csv

    import numpy as np

    tk_app.nb.select(tk_app.live_tab)
    tk_app.update()
    tk_app.live_invert_x.set(False)
    tk_app.live_mark_peak.set(True)
    tk_app.live_subtract_dark.set(False)
    tk_app._live_processing["dark"] = None
    tk_app.live_limits_locked = False

    npix = 2048
    x = np.arange(npix, dtype=float)
    rng = np.random.default_rng(1)

    def show(frame):
        tk_app._update_live_plot(x, frame)
        assert pump_until(tk_app, lambda: tk_app._live_processing["last_raw"] is not None
                          and np.array_equal(tk_app._live_processing["last_raw"], frame), timeout=5.0)

    # Four frames averaged: the trace is their mean, not the last one.
    tk_app.live_avg_var.set(4)
    frames = [600.0 + rng.normal(0.0, 20.0, npix) for _ in range(4)]
    for frame in frames:
        frame[700] += 30000.0
        show(frame)
    expected = np.mean(frames, axis=0)
    assert np.allclose(tk_app.live_line.get_ydata(), expected)
    assert tk_app.live_peak_marker.get_visible() and tk_app.live_peak_marker.get_xdata()[0] == 700
    assert tk_app.live_peak_text.get_text() == "px 700"

    # Capture that as the dark; the same frames again come out flat at zero.
    tk_app.live_capture_dark()
    assert tk_app.live_subtract_dark.get() and not tk_app.live_dark_check.instate(["disabled"])
    assert tk_app.live_dark_caption.cget("text").startswith("Dark: 2048 px")
    for frame in frames:
        show(frame)
    assert np.allclose(tk_app.live_line.get_ydata(), 0.0, atol=1e-6)
    assert tk_app.live_ax.get_ylim()[0] <= 0.0

    # Save the frame: header lines, then one row per pixel with the processed and the raw value.
    target = tmp_path / "frame.csv"
    monkeypatch.setattr("tkinter.filedialog.asksaveasfilename", lambda *a, **k: str(target))
    tk_app.live_save_frame()
    text = target.read_text(encoding="utf-8")
    assert "# dark_subtracted: yes" in text and "# averaged_frames: 4" in text
    rows = [row for row in csv.reader(line for line in text.splitlines() if not line.startswith("#"))]
    assert rows[0] == ["pixel", "counts", "raw_counts"] and len(rows) == npix + 1
    assert rows[701][0] == "700" and abs(float(rows[701][1])) < 1e-3 and float(rows[701][2]) > 30000.0

    # The peak mark can be switched off; averaging back to single frames.
    tk_app.live_mark_peak.set(False)
    tk_app.live_subtract_dark.set(False)
    tk_app.live_avg_var.set(1)
    single = frames[0].copy()
    show(single)
    assert not tk_app.live_peak_marker.get_visible()
    assert np.array_equal(tk_app.live_line.get_ydata(), single)
    tk_app.live_mark_peak.set(True)
    assert not errors(tk_app), tk_app.dialogs


def test_setup_port_dropdowns_list_detected_ports(tk_app, monkeypatch):
    """The COM port fields are dropdowns of the ports Windows sees; Refresh does not overwrite them."""
    import types

    serial_ports = pytest.importorskip("serial.tools.list_ports")
    fake = [types.SimpleNamespace(device="COM4", description="USB Serial Device (COM4)"),
            types.SimpleNamespace(device="COM12", description="Prolific USB-to-Serial (COM12)"),
            types.SimpleNamespace(device="COM3", description="")]
    monkeypatch.setattr(serial_ports, "comports", lambda: list(fake))

    tk_app.nb.select(tk_app.setup_tab)
    tk_app.update()
    tk_app.obis_entry.delete(0, "end")
    tk_app.obis_entry.insert(0, "COM10")
    tk_app.refresh_ports()
    assert pump_until(tk_app, lambda: len(tk_app._serial_ports) == 3, timeout=5.0)
    assert tk_app._serial_ports[0] == ("COM3", "")                     # numeric order, "(COMn)" stripped
    values = list(tk_app.obis_entry.cget("values"))
    assert values == ["COM3", "COM4  ·  USB Serial Device", "COM12  ·  Prolific USB-to-Serial"]
    assert list(tk_app.stage_com_entry.cget("values")) == values
    assert tk_app.obis_entry.get() == "COM10"                         # not overwritten

    # Picking an entry keeps only the port name.
    tk_app.obis_entry.set(values[1])
    tk_app.obis_entry.event_generate("<<ComboboxSelected>>")
    tk_app.update()
    assert tk_app.obis_entry.get() == "COM4"
    tk_app.obis_entry.delete(0, "end")
    tk_app.obis_entry.insert(0, "COM10")
    assert "3 serial ports" in tk_app.status.message.cget("text")
    assert not errors(tk_app), tk_app.dialogs


def test_analysis_run_tab_key_results_dedupe_and_viewer(tk_app, tmp_path):
    """A run tab shows the key numbers and a grid of previews; the same results are not added twice;
    a preview opens the in-app viewer."""
    import json

    from PIL import Image

    from analysis.models import AnalysisArtifact
    from tests.test_analysis_results import METRICS

    plots = tmp_path / "plots"
    plots.mkdir()
    artifacts = []
    for index, name in enumerate(("Normalized LSFs", "Dispersion Fit")):
        path = plots / f"plot_{index}.png"
        Image.new("RGB", (640, 400), (20 * index + 10, 60, 120)).save(path)
        artifacts.append(AnalysisArtifact(name=name, path=str(path)))
    metrics_path = plots / "metrics_SIM-0001_20260101_120000.json"
    metrics_path.write_text(json.dumps(METRICS), encoding="utf-8")

    notebook = tk_app.analysis_measurements_notebook
    tk_app.analysis_artifacts = list(artifacts)
    tk_app.analysis_summary_lines = ["Dispersion Polynomial: test"]
    tk_app._analysis_metrics_path = str(metrics_path)
    tk_app._latest_results_timestamp = "20260101_120000"
    tk_app.refresh_analysis_view()
    tk_app.update()
    new_tab_text = notebook.tab(notebook.select(), "text")
    assert new_tab_text.startswith("Run ") and new_tab_text.endswith("12:00:00")
    assert tk_app.nb.select() == str(tk_app.analysis_tab)
    record = tk_app.analysis_measurement_tabs[new_tab_text]
    assert [title for title, _path in record["items"]] == ["Normalized LSFs", "Dispersion Fit"]
    assert _find_widgets(record["tab"], "TLabelframe", "Key results")
    assert pump_until(tk_app, lambda: len(record["images"]) == 2, timeout=8.0)

    # Analyse last run pressed again: the existing tab is selected, nothing is added.
    count = len(notebook.tabs())
    tk_app.nb.select(tk_app.measure_tab)
    tk_app.refresh_analysis_view()
    tk_app.update()
    assert len(notebook.tabs()) == count and notebook.select() == str(record["tab"])
    assert tk_app.nb.select() == str(tk_app.analysis_tab)

    # The in-app viewer steps through the run's plots.
    tk_app.open_analysis_image(record, 1)
    tk_app.update()
    viewer = tk_app.image_viewer
    assert viewer.winfo_exists() and viewer.index == 1 and viewer.current_path.name == "plot_1.png"
    assert "2 of 2" in viewer._caption.cget("text")
    viewer.show_previous()
    assert viewer.index == 0 and viewer._prev_btn.instate(["disabled"])
    viewer.show_next()
    assert viewer.index == 1 and viewer._next_btn.instate(["disabled"])
    tk_app.open_analysis_image(record, 0)                 # the one window is reused
    assert tk_app.image_viewer is viewer and viewer.index == 0
    viewer.destroy()
    tk_app.update()
    assert not errors(tk_app), tk_app.dialogs


def test_help_window_switch_guard_and_eta(tk_app):
    tk_app.show_help()
    tk_app.update()
    window = tk_app._help_window
    assert window.winfo_exists()
    tk_app.show_help()
    assert tk_app._help_window is window
    window.destroy()
    tk_app.update()

    # A click on a source that is still switching is ignored and the box shows the real state.
    tk_app._switching_sources.add("405")
    tk_app.laser_var("405").set(True)
    tk_app.toggle_laser("405", True)
    assert tk_app.laser_var("405").get() is False
    assert "still switching" in tk_app.status.message.cget("text")
    tk_app._switching_sources.discard("405")

    # Once a source has finished, the checklist and the chip estimate the time left.
    model = tk_app.run_progress
    model.start(["405", "445", "488"])
    model.source_started("405")
    model.source_completed("405")
    model.source_started("445")
    assert pump_until(tk_app, lambda: "left" in tk_app.run_progress_panel.elapsed_label.cget("text"), timeout=5.0)
    assert "left" in tk_app._run_chip.chip.text.cget("text")
    model.finish(stopped_early=True)
    assert pump_until(tk_app, lambda: "left" not in tk_app.run_progress_panel.elapsed_label.cget("text"), timeout=5.0)
    tk_app._run_chip.hide()
    assert not errors(tk_app), tk_app.dialogs
