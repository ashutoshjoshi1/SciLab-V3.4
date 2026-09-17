"""SciLab entry point.

Startup order matters for perceived speed:

1. A tiny Tk root with the SciGlob logo appears within ~100 ms.
2. The heavy scientific stack (numpy, matplotlib, scipy, pandas) and the tab
   modules are imported *while the logo is visible*.
3. The splash is closed and the main window is built (~0.4 s of widgets).

v3.3 did these in the opposite order: ~2.5 s of imports with nothing on screen,
then a fixed 2.5 s splash, then the build.
"""
from __future__ import annotations

import logging
import os
import sys
import time
import tkinter as tk

LOGGER = logging.getLogger(__name__)

MIN_SPLASH_MS = 800
SPLASH_BG = "#0f172a"  # deep slate, matches the logo background
APP_DIR = os.path.dirname(os.path.abspath(__file__))


def enable_windows_dpi_awareness() -> None:
    """Ask Windows for real device pixels so Tk text is not bitmap-scaled (blurry) at 125-150 %."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system-DPI aware
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception as exc:  # pragma: no cover - Windows only
        LOGGER.debug("DPI awareness not set: %s", exc)


def build_splash() -> tk.Tk:
    splash = tk.Tk()
    splash.title("SciGlob")
    splash.overrideredirect(True)

    screen_width = splash.winfo_screenwidth()
    screen_height = splash.winfo_screenheight()
    width = min(680, int(screen_width * 0.45))
    height = min(420, int(screen_height * 0.45))
    splash.geometry(f"{width}x{height}+{(screen_width - width) // 2}+{(screen_height - height) // 2}")
    splash.configure(bg=SPLASH_BG)

    try:
        from PIL import Image, ImageTk  # light (~30 ms); the logo is worth it

        image = Image.open(os.path.join(APP_DIR, "sciglob_logoRGB.png"))
        image.thumbnail((int(width * 0.82), int(height * 0.72)), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(image, master=splash)
        logo = tk.Label(splash, image=photo, bg=SPLASH_BG, borderwidth=0, highlightthickness=0)
        logo.image = photo  # keep a reference
        logo.pack(expand=True, pady=(20, 4))
    except Exception as exc:
        LOGGER.warning("Splash logo unavailable: %s", exc)
        tk.Label(
            splash,
            text="SciGlob\nSpectrometer Characterization System",
            font=("Segoe UI", 20, "bold"),
            bg=SPLASH_BG,
            fg="#60a5fa",
            borderwidth=0,
            highlightthickness=0,
        ).pack(expand=True)

    tk.Label(
        splash, text="Spectrometer Characterization System", font=("Segoe UI", 10), bg=SPLASH_BG, fg="#64748b"
    ).pack(pady=(0, 4))
    status = tk.Label(splash, text="Loading…", font=("Segoe UI", 9), bg=SPLASH_BG, fg="#94a3b8")
    status.pack(pady=(0, 14))
    splash.status_label = status
    splash.update()
    return splash


def preload_heavy_modules(report) -> None:
    """Import everything expensive while the splash is on screen."""
    steps = (
        ("Loading numerics…", ("numpy",)),
        ("Loading plotting…", ("matplotlib.figure", "matplotlib.backends.backend_tkagg")),
        ("Loading analysis…", ("pandas", "scipy.optimize", "scipy.signal")),
        ("Loading application…", ("app", "tabs.setup_tab", "tabs.measurements_tab", "tabs.analysis_tab")),
    )
    import importlib

    for label, modules in steps:
        report(label)
        for name in modules:
            try:
                importlib.import_module(name)
            except Exception as exc:
                # A missing optional package is reported by the app itself later.
                LOGGER.warning("Preload of %s failed: %s", name, exc)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    enable_windows_dpi_awareness()

    started = time.perf_counter()
    splash = build_splash()

    def report(text: str) -> None:
        try:
            splash.status_label.configure(text=text)
            splash.update()
        except tk.TclError:
            pass

    preload_heavy_modules(report)
    from app import SpectroApp  # already imported above; cheap

    report("Starting…")
    remaining_ms = MIN_SPLASH_MS - int((time.perf_counter() - started) * 1000)
    if remaining_ms > 0:
        splash.after(remaining_ms, splash.quit)
        splash.mainloop()
    splash.destroy()
    LOGGER.info("Startup preload finished in %.2f s", time.perf_counter() - started)

    app = SpectroApp()
    app.mainloop()


if __name__ == "__main__":
    main()
