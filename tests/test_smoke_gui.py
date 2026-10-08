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
    result = {"ok": False}

    def _check():
        if predicate():
            result["ok"] = True
            app.quit()
            return
        if time.time() >= deadline:
            app.quit()
            return
        app.after(20, _check)

    app.after(0, _check)
    app.mainloop()
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
    tk_app.start_live()
    assert tk_app.acq.owner == "Live View"
    assert pump_until(tk_app, lambda: len(tk_app.live_line.get_xdata()) == tk_app.npix, timeout=8.0)
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
    assert not errors(tk_app), tk_app.dialogs


def test_check_resolution_fits_simulated_line(tk_app):
    tk_app.nb.select(tk_app.check_res_tab)
    tk_app.update()
    tk_app.spec.set_active_source("532")
    tk_app.check_res_it_entry.delete(0, "end")
    tk_app.check_res_it_entry.insert(0, "30")
    tk_app.check_res_start_btn.invoke()
    assert tk_app.acq.owner == "Check Resolution"
    assert pump_until(tk_app, lambda: len(tk_app.check_res_ax.lines[3].get_xdata()) > 0, timeout=8.0)
    tk_app.check_res_stop_btn.invoke()
    assert pump_until(tk_app, lambda: tk_app.acq.owner is None, timeout=5.0)
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
