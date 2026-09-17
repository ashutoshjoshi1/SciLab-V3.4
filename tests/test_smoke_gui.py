"""GUI smoke test: build the whole app against the simulated backend.

Skipped automatically when the interpreter has no ``_tkinter`` (the default
pyenv Python on the dev Mac). Run it with the Tk-enabled venv:

    .venv/bin/python -m pytest tests/test_smoke_gui.py -q
"""
from __future__ import annotations

import time

import pytest

tk = pytest.importorskip("tkinter")
pytest.importorskip("matplotlib")


@pytest.fixture
def tk_app(tmp_path, monkeypatch):
    from tkinter import messagebox

    import app as app_module

    monkeypatch.setattr(app_module.SpectroApp, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(app_module.SpectroApp, "RESULTS_ROOT", tmp_path / "results")

    dialogs = []
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(messagebox, name, lambda title, message="", *a, _n=name, **k: dialogs.append((_n, title, message)) or "ok")

    try:
        app = app_module.SpectroApp()
    except tk.TclError as exc:  # no display
        pytest.skip(f"Tk display unavailable: {exc}")
    app.withdraw()
    app.dialogs = dialogs
    yield app
    try:
        app._on_closing()
    except tk.TclError:
        pass


def pump_until(app, predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.update()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_builds_every_tab_and_closes(tk_app):
    for tab_id in tk_app.nb.tabs():
        tk_app.nb.select(tab_id)
        tk_app.update()
    assert set(tk_app._built_tabs) == set(tk_app.nb.tabs())
    assert not [d for d in tk_app.dialogs if d[0] == "showerror"], tk_app.dialogs


def test_check_resolution_before_live_view_has_laser_controls(tk_app):
    """Regression: Check Resolution used to depend on Live View having been built first."""
    tk_app.nb.select(tk_app.check_res_tab)
    tk_app.update()
    assert str(tk_app.check_res_tab) in tk_app._built_tabs
    assert "405" in tk_app.laser_vars
    assert not [d for d in tk_app.dialogs if d[0] == "showerror"], tk_app.dialogs


def test_simulated_backend_streams_live_frames(tk_app):
    tk_app.spec_type_var.set("Simulated")
    tk_app.dll_entry.delete(0, "end")
    tk_app.connect_spectrometer()
    assert tk_app.spec_backend == "Simulated", tk_app.dialogs
    assert tk_app.npix == tk_app.spec.npix_active

    tk_app.nb.select(tk_app.live_tab)
    tk_app.update()
    tk_app.spec.set_active_source("445")
    tk_app.start_live()
    assert pump_until(tk_app, lambda: len(tk_app.live_line.get_xdata()) == tk_app.npix, timeout=8.0)
    tk_app.stop_live()
    assert pump_until(tk_app, lambda: not tk_app.live_running.is_set(), timeout=5.0)
    tk_app.disconnect_spectrometer()
    assert tk_app.spec is None
