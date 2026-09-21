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
# The SciGlob mark is deep blue on transparent, so it belongs on a light card:
# on the old near-black splash the "Glob" half almost disappeared.
SPLASH_BG = "#ffffff"
SPLASH_EDGE = "#c4cedd"
SPLASH_RULE = "#e2e8f0"
SPLASH_ACCENT = "#2563eb"
SPLASH_TEXT = "#0f172a"
SPLASH_MUTED = "#64748b"
SPLASH_FAINT = "#94a3b8"
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
    width = min(620, int(screen_width * 0.42))
    height = min(320, int(screen_height * 0.38))
    splash.geometry(f"{width}x{height}+{(screen_width - width) // 2}+{(screen_height - height) // 2}")
    splash.configure(bg=SPLASH_EDGE)

    # 1 px frame drawn by the outer background, so the card has a crisp edge on
    # any desktop wallpaper without a window border.
    # Same typography resolution as the main window (Segoe UI Variable where
    # available), so the splash is not the one screen in another typeface.
    try:
        from tabs.theme import Fonts, gradient_rail, resolve_fonts

        resolve_fonts(splash)
        fonts = (Fonts.H2, Fonts.BODY_SMALL, Fonts.CAPTION, Fonts.H0)
    except Exception:
        gradient_rail = None
        fonts = (("Segoe UI", 13, "bold"), ("Segoe UI", 9), ("Segoe UI", 8),
                 ("Segoe UI", 24, "bold"))
    title_font, sub_font, status_font, mark_font = fonts

    card = tk.Frame(splash, bg=SPLASH_BG, bd=0, highlightthickness=0)
    card.pack(fill="both", expand=True, padx=1, pady=1)
    if gradient_rail is not None:
        gradient_rail(card, height=3, start="#1e3a8a", end="#22d3ee",
                      background=SPLASH_BG).pack(fill="x", side="top")
    else:
        tk.Frame(card, bg=SPLASH_ACCENT, height=3, bd=0,
                 highlightthickness=0).pack(fill="x", side="top")

    body = tk.Frame(card, bg=SPLASH_BG, bd=0, highlightthickness=0)
    body.pack(fill="both", expand=True, padx=34, pady=(26, 0))

    try:
        from PIL import Image, ImageTk  # light (~30 ms); the logo is worth it

        image = Image.open(os.path.join(APP_DIR, "sciglob_logoRGB.png"))
        image.thumbnail((int(width * 0.55), int(height * 0.34)), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(image, master=splash)
        logo = tk.Label(body, image=photo, bg=SPLASH_BG, borderwidth=0, highlightthickness=0)
        logo.image = photo  # keep a reference
        logo.pack(anchor="w")
    except Exception as exc:
        LOGGER.warning("Splash logo unavailable: %s", exc)
        tk.Label(
            body,
            text="SciGlob",
            font=mark_font,
            bg=SPLASH_BG,
            fg=SPLASH_ACCENT,
            borderwidth=0,
            highlightthickness=0,
        ).pack(anchor="w")

    tk.Frame(body, bg=SPLASH_RULE, height=1, bd=0,
             highlightthickness=0).pack(fill="x", pady=(20, 16))
    tk.Label(
        body, text="Spectrometer Characterization System",
        font=title_font, bg=SPLASH_BG, fg=SPLASH_TEXT,
    ).pack(anchor="w")
    tk.Label(
        body, text="SciLab v3.4", font=sub_font,
        bg=SPLASH_BG, fg=SPLASH_MUTED,
    ).pack(anchor="w", pady=(2, 0))

    footer = tk.Frame(card, bg=SPLASH_BG, bd=0, highlightthickness=0)
    footer.pack(fill="x", padx=34, pady=(0, 22))
    status = tk.Label(footer, text="Loading…", font=status_font,
                      bg=SPLASH_BG, fg=SPLASH_FAINT, anchor="w")
    status.pack(fill="x")
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
