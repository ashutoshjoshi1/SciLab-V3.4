"""Build SciLab against the simulated backend and screenshot every tab.

A visual regression aid for layout work: it connects the Simulated
spectrometer, streams Live View, runs Check Resolution, optionally runs a
Quick Scan measurement (so the Analysis page has a run), and saves one PNG
per page into the folder given. Settings and results go to a temporary
folder, so the bench's own settings file is left alone.

    python tools/screenshot_tabs.py out/shots            # maximised
    python tools/screenshot_tabs.py out/shots 1366x768   # a laptop-sized window
    python tools/screenshot_tabs.py out/shots --run      # include a Quick Scan run

Needs a display and Pillow (for the screen grab); Windows only for the grab.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    size = next((a for a in sys.argv[2:] if re.match(r"^\d+x\d+$", a)), None)
    run = "--run" in sys.argv

    from tkinter import messagebox

    from PIL import ImageGrab

    import main as main_module

    main_module.enable_windows_dpi_awareness()

    import app as app_module

    tmp = Path(tempfile.mkdtemp(prefix="scilab-shots-"))
    app_module.SpectroApp.SETTINGS_FILE = str(tmp / "settings.json")
    app_module.SpectroApp.RESULTS_ROOT = tmp / "results"

    dialogs: list = []
    for name in ("showinfo", "showwarning", "showerror"):
        setattr(messagebox, name,
                lambda title, message="", *a, _n=name, **k: dialogs.append((_n, title, message)) or "ok")
    messagebox.askyesno = lambda *a, **k: True

    app = app_module.SpectroApp()
    if size:
        width, height = size.split("x")
        app.state("normal")
        app.geometry(f"{width}x{height}+0+0")
    app.update()

    def pump(seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            app.update()
            time.sleep(0.02)

    def pump_until(predicate, timeout: float = 10.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            app.update()
            try:
                if predicate():
                    return True
            except Exception:
                pass
            time.sleep(0.03)
        return False

    def grab(window, name: str) -> None:
        try:
            window.attributes("-topmost", True)
            window.lift()
        except Exception:
            pass
        app.update_idletasks()
        app.update()
        pump(0.25)
        x, y = window.winfo_rootx(), window.winfo_rooty()
        width, height = window.winfo_width(), window.winfo_height()
        ImageGrab.grab(bbox=(x, y, x + width, y + height)).save(out / f"{name}.png")
        print("saved", name, width, height)

    def shot(name: str) -> None:
        grab(app, name)

    app.nb.select(app.setup_tab)
    shot("01_setup_disconnected")

    app.spec_type_var.set("Simulated")
    app.dll_entry.delete(0, "end")
    app.connect_spectrometer()
    print("connected", pump_until(lambda: app.spec_backend == "Simulated", 15))
    shot("02_setup_connected")

    app.nb.select(app.live_tab)
    pump(0.3)
    shot("03_live_idle")
    app.spec.set_active_source("445")
    app.note_source_state("445", True)
    app.start_live()
    pump_until(lambda: len(app.live_line.get_xdata()) > 0, 8)
    pump(1.0)
    shot("04_live_streaming")
    app.stop_live()
    pump_until(lambda: app.acq.owner is None, 5)

    app.nb.select(app.check_res_tab)
    pump(0.3)
    shot("05_checkres_idle")
    app.spec.set_active_source("532")
    app.check_res_it_entry.delete(0, "end")
    app.check_res_it_entry.insert(0, "30")
    app.check_res_start_btn.invoke()
    pump_until(lambda: len(app.check_res_ax.lines[3].get_xdata()) > 0, 8)
    pump(1.0)
    shot("06_checkres_running")
    app.check_res_stop_btn.invoke()
    pump_until(lambda: app.acq.owner is None, 5)
    app.note_all_sources_off()

    app.nb.select(app.measure_tab)
    pump(0.3)
    shot("07_measure_idle")

    if run:
        app.preset_var.set("Quick Scan")
        pump(0.2)
        app.run_all_selected()
        pump_until(lambda: app.run_progress.snapshot().current is not None, 10)
        pump(2.0)
        shot("08_measure_running")
        print("run finished", pump_until(lambda: not app.measure_running.is_set(), 240))
        pump(3.0)
        shot("09_measure_done")
        app.nb.select(app.analysis_tab)
        pump_until(lambda: any(record.get("images") for record in app.analysis_measurement_tabs.values()), 20)
        pump(1.0)
        shot("10_analysis_results")
        records = list(app.analysis_measurement_tabs.values())
        if records:
            app.open_analysis_image(records[-1], 0)
            pump(0.8)
            grab(app.image_viewer, "10b_image_viewer")
            app.image_viewer.destroy()
            pump(0.2)
    else:
        app.nb.select(app.analysis_tab)
        pump(0.3)
        shot("10_analysis_empty")

    app.nb.select(app.eeprom_tab)
    pump(0.3)
    shot("11_eeprom")

    app.nb.select(app.setup_tab)
    app.log_drawer.show()
    pump(0.3)
    shot("12_setup_logdrawer")
    app.log_drawer.hide()

    app.show_help()
    pump(0.6)
    grab(app._help_window, "13_help")
    app._help_window.destroy()

    print("dialogs:", dialogs)
    app._on_closing()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
