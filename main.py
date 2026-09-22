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
from pathlib import Path

from scilab_version import APP_NAME, DISPLAY_VERSION, PRODUCT_NAME

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
        body, text=f"{APP_NAME} {DISPLAY_VERSION}", font=sub_font,
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


SINGLE_INSTANCE_MUTEX = "SciLab.SingleInstance.Mutex"
_INSTANCE_HANDLE = None


def acquire_single_instance() -> bool:
    """Claim the app-wide mutex; False means another copy is already running.

    Two copies on one bench is not a cosmetic problem: they would both try to
    open the same USB spectrometer, and the second one's failure looks like
    broken hardware. The installer watches for this same mutex name, so it can
    refuse to overwrite a running installation.
    """
    global _INSTANCE_HANDLE
    if not sys.platform.startswith("win"):
        return True
    try:
        import ctypes
        from ctypes import wintypes

        ERROR_ALREADY_EXISTS = 183
        # use_last_error=True is not optional: it makes ctypes capture
        # GetLastError() immediately after the call. Reading
        # ctypes.get_last_error() without it returns a stale value, which
        # reported "already running" on a machine with nothing running.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX)
        error = ctypes.get_last_error()
        if not handle:
            return True  # cannot tell; let the app start rather than block it
        if error == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        _INSTANCE_HANDLE = handle  # released when the process exits
        return True
    except Exception as exc:  # pragma: no cover - Windows only
        LOGGER.debug("Single-instance check unavailable: %s", exc)
        return True


def focus_running_instance() -> None:
    """Raise the window of the copy that is already running, if it can be found."""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        handle = user32.FindWindowW(None, f"{PRODUCT_NAME} {DISPLAY_VERSION}")
        if handle:
            SW_RESTORE = 9
            user32.ShowWindow(handle, SW_RESTORE)
            user32.SetForegroundWindow(handle)
            return
    except Exception:
        pass
    try:
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(APP_NAME, f"{APP_NAME} is already running.")
        root.destroy()
    except Exception:
        pass


def self_test() -> int:
    """Check that this build can do its job, without opening a window.

    A frozen bundle that silently lost a hardware backend or a vendor DLL looks
    perfectly healthy until an operator tries to connect an instrument, and
    then reports "No spectrometer detected". This is what the build step runs
    before wrapping the bundle in an installer, and what to ask a site to run
    when an installed copy misbehaves:

        "C:\\Program Files\\SciLab\\SciLab.exe" --self-test
    """
    import importlib

    lines: list[str] = []
    failures: list[str] = []

    def check(label, fn, *, critical=True):
        try:
            detail = fn()
            lines.append(f"  OK    {label}{f': {detail}' if detail else ''}")
        except Exception as exc:
            lines.append(f"  {'FAIL ' if critical else 'warn '} {label}: {exc}")
            if critical:
                failures.append(label)

    frozen = getattr(sys, "frozen", False)
    lines.append(f"{APP_NAME} {DISPLAY_VERSION} self-test")
    lines.append(f"  build     : {'frozen' if frozen else 'source'}  python {sys.version.split()[0]}")
    lines.append(f"  executable: {sys.executable}")

    for module in ("numpy", "matplotlib", "matplotlib.backends.backend_tkagg",
                   "pandas", "scipy.optimize", "scipy.signal", "PIL", "serial",
                   "serial.tools.list_ports", "tkinter"):
        check(f"import {module}", lambda m=module: importlib.import_module(m) and "")

    # Optional hardware stacks: absence only limits which instruments work.
    for module in ("clr", "usb.core", "libusb_package", "pymodbus"):
        check(f"import {module}", lambda m=module: importlib.import_module(m) and "",
              critical=False)

    for backend in ("ava1", "hama2", "hama3", "hama4", "hama5", "hama6",
                    "minispec", "simulated"):
        name = f"spectrometers.{backend}_spectrometer"
        check(f"backend {backend}", lambda m=name: importlib.import_module(m) and "")

    def _dlls():
        from spectrometer_loader import DLLS_DIR

        if not DLLS_DIR.is_dir():
            raise RuntimeError(f"missing directory {DLLS_DIR}")
        found = sorted(p.name for p in DLLS_DIR.glob("*.dll"))
        if not found:
            raise RuntimeError(f"no DLLs in {DLLS_DIR}")
        return ", ".join(found)

    check("vendor DLLs", _dlls)

    def _writable():
        from app import get_writable_path

        probe = Path(get_writable_path(".self_test"))
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return str(probe.parent)

    check("writable data dir", _writable)

    def _assets():
        from app import get_resource_path

        missing = [name for name in ("sciglob_logoRGB.png", "packaging/scilab.ico")
                   if not Path(get_resource_path(name)).is_file()]
        if missing:
            raise RuntimeError("missing " + ", ".join(missing))
        return "logo, icon"

    check("bundled assets", _assets)

    lines.append(f"RESULT: {'FAIL - ' + ', '.join(failures) if failures else 'PASS'}")
    report = "\n".join(lines)

    # A windowed build launched from Explorer has no stdout at all, so the
    # report is also written next to the app's data where support can find it.
    try:
        print(report)
    except Exception:
        pass
    try:
        from app import get_writable_path

        Path(get_writable_path("self-test.log")).write_text(report + "\n", encoding="utf-8")
    except Exception:
        pass
    return 1 if failures else 0


def main() -> None:
    if "--self-test" in sys.argv:
        raise SystemExit(self_test())
    if "--version" in sys.argv:
        try:
            print(f"{APP_NAME} {DISPLAY_VERSION}")
        except Exception:
            pass
        raise SystemExit(0)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # Before anything touches hardware or draws a splash.
    if not acquire_single_instance():
        LOGGER.info("Another instance is already running; focusing it")
        focus_running_instance()
        raise SystemExit(0)

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
