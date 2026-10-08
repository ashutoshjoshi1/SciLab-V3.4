from __future__ import annotations

import functools
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from domain.measurement import MeasurementData
from scilab_version import APP_NAME, DISPLAY_VERSION, PRODUCT_NAME
from hardware.controllers import FilterWheelController, LaserController
from services.acquisition_guard import AcquisitionGuard
from tabs.ui_pump import ThrottledRedraw, UiPump

try:
    from stage.stage_controller import StageController
except ImportError:
    StageController = None


from services.run_progress import RunProgress, source_label

LOGGER = logging.getLogger(__name__)

# Analysis-tab previews retained before the oldest run tab is destroyed. Each run
# holds ~10 MB of PhotoImage data, which used to accumulate for the whole session.
MAX_ANALYSIS_RUN_TABS = 5
PREVIEW_MAX_SIZE = (720, 440)


def _pandas():
    """pandas is only needed to save/load measurement CSVs; import it on first use."""
    try:
        import pandas as pd
    except Exception as exc:  # pragma: no cover - runtime dependency on target machine
        raise RuntimeError("pandas is required for this operation.") from exc
    return pd


def get_resource_path(relative_path: str) -> str:
    """Return a bundled-resource path that also works for frozen apps."""
    base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return str((base_dir / relative_path).resolve())


@functools.lru_cache(maxsize=1)
def _writable_base() -> Path:
    """Directory the app may write to for settings and results.

    An installed build lives under Program Files, which is read-only for a
    normal user, so it writes to ``%LOCALAPPDATA%\\SciLab`` -- the conventional
    place for per-user application data on Windows, and somewhere Explorer can
    actually reach. Running from a source checkout keeps writing next to the
    code, so a developer's ``spectro_gui_settings.json`` and ``results/`` stay
    where they have always been.
    """
    candidates = []
    if getattr(sys, "frozen", False):
        app_data = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if app_data:
            candidates.append(Path(app_data) / "SciLab")
        candidates.append(Path(sys.executable).resolve().parent)
    else:
        candidates.append(Path(__file__).resolve().parent)
    candidates.append(Path.home() / ".head-scilab")

    for base in candidates:
        try:
            base.mkdir(parents=True, exist_ok=True)
            test_path = base / ".write_test"
            test_path.write_text("ok", encoding="utf-8")
            test_path.unlink(missing_ok=True)
            return base
        except Exception:
            continue

    # Last resort: the temp directory always exists and is always writable.
    fallback = Path(tempfile.gettempdir()) / "SciLab"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def get_writable_path(relative_path: str) -> str:
    """Return a writable path near the app, falling back to the user home."""
    return str((_writable_base() / relative_path).resolve())


def _clean_text(value: object) -> str:
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="ignore")
    else:
        text = str(value)
    text = text.strip()
    if text.startswith("b'") and text.endswith("'"):
        text = text[2:-1]
    if text.startswith('b"') and text.endswith('"'):
        text = text[2:-1]
    return text.strip()


@dataclass
class HardwareState:
    dll_path: str = ""
    spectrometer_type: str = "Auto"
    # Backend that connected most recently; Auto probes it first.
    last_connected_type: str = ""
    com_ports: Dict[str, str] = field(default_factory=dict)
    laser_power: Dict[str, float] = field(default_factory=dict)


class SpectroApp(tk.Tk):
    DEFAULT_SPECTROMETER_TYPE = "Auto"
    DEFAULT_ALL_LASERS = ["377", "405", "445", "488", "517", "532", "640", "685", "Hg_Ar"]
    DEFAULT_COM_PORTS = {"OBIS": "COM10", "CUBE": "COM1", "RELAY": "COM11", "HEADSENSOR": "", "STAGE": ""}
    DEFAULT_LASER_POWERS = {
        "377": 12.0,
        "405": 0.05,
        "445": 0.03,
        "488": 0.03,
        "517": 30.0,
        "532": 30.0,
        "640": 0.05,
        "685": 0.03,
        "Hg_Ar": 0.0,
    }
    DEFAULT_START_IT = {"532": 20.0, "517": 80.0, "Hg_Ar": 20.0, "default": 2.4}

    N_SIG = 50
    N_DARK = 50
    N_SIG_640 = 10
    N_DARK_640 = 10

    TARGET_LOW = 60000
    TARGET_HIGH = 65000
    TARGET_MID = 62500

    IT_MIN = 0.2
    IT_MAX = 3000.0
    # Auto-IT walks toward the target band in additive steps of at most these
    # amounts per single-frame capture, so a dim source can take hundreds of frames.
    IT_STEP_UP = 0.3
    IT_STEP_DOWN = 0.1
    MAX_IT_ADJUST_ITERS = 1000
    SAT_THRESH = 65400
    # Bench-validated speed options, persisted in the settings file (see setup tab).
    DEFAULT_MEASUREMENT_FLAGS = {"smart_source_off": False, "cube_prearm": False, "framed_serial_reads": False}

    # Resolved on first instantiation (not at import time, which used to touch the
    # filesystem twice before the splash even appeared). Tests may override these.
    SETTINGS_FILE: Optional[str] = None
    RESULTS_ROOT: Optional[Path] = None

    def _configure_initial_geometry(self) -> None:
        screen_width = max(800, self.winfo_screenwidth())
        screen_height = max(600, self.winfo_screenheight())

        target_width = min(1500, int(screen_width * 0.96), max(820, screen_width - 80))
        target_height = min(950, int(screen_height * 0.92), max(620, screen_height - 80))

        min_width = min(target_width, 960 if screen_width >= 1100 else max(720, int(screen_width * 0.78)))
        min_height = min(target_height, 680 if screen_height >= 760 else max(540, int(screen_height * 0.8)))

        self.minsize(min_width, min_height)

        offset_x = max(0, (screen_width - target_width) // 2)
        offset_y = max(0, (screen_height - target_height) // 2)
        self.geometry(f"{target_width}x{target_height}+{offset_x}+{offset_y}")

    def _adopt_default_root(self) -> None:
        """Make this window Tkinter's default root while the splash still exists.

        main.py keeps the splash (its own ``tk.Tk``) on screen until this
        window is ready. Tkinter binds master-less variables, images and the
        standard dialogs to the *first* root, which would be the splash, and
        they would die with it. Pointing the default at this window keeps
        them here.
        """
        if getattr(tk, "_support_default_root", True):
            tk._default_root = self

    def _show_window(self) -> None:
        """Map the finished window, maximised.

        The zoomed state is applied while the window is still withdrawn so it
        maps straight into it; deiconifying first and zooming afterwards shows
        the restore-size window for a frame. The restore size itself comes
        from ``_configure_initial_geometry``, for when the operator
        un-maximises.
        """
        self.update_idletasks()
        self.maximize()
        self.deiconify()
        try:
            if self.state() != "zoomed":  # X11: the zoomed attribute needs a mapped window
                self.maximize()
        except tk.TclError:
            pass

    def maximize(self) -> None:
        try:
            self.state("zoomed")
            return
        except tk.TclError:
            pass
        try:
            self.attributes("-zoomed", True)
        except tk.TclError:
            self.geometry(f"{self.winfo_screenwidth()}x{self.winfo_screenheight()}+0+0")

    def __init__(self):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        super().__init__()
        # Built out of sight and shown once, maximised and complete (see
        # _show_window): nobody watches the window assemble itself.
        self.withdraw()
        self._adopt_default_root()

        if self.SETTINGS_FILE is None:
            self.SETTINGS_FILE = get_writable_path("spectro_gui_settings.json")
        if self.RESULTS_ROOT is None:
            self.RESULTS_ROOT = Path(get_writable_path("results"))

        self.title(f"{PRODUCT_NAME} {DISPLAY_VERSION}")
        self._configure_initial_geometry()

        # Multi-resolution icon first (16-256 px, generated from the SciGlob
        # mark); the old 32 px-only file is the fallback.
        for icon_name in ("packaging/scilab.ico", "sciglob_symbol.ico"):
            try:
                self.iconbitmap(get_resource_path(icon_name))
                break
            except Exception:
                continue

        # Apply centralized UI theme
        from tabs.theme import apply_theme
        apply_theme(self)

        # Every worker -> UI hand-off goes through this queue; it is drained by
        # one after() loop on the Tk thread, so no other thread ever calls Tk.
        self.pump = UiPump(self)
        self.pump.start()
        # Single-owner token for the spectrometer handle (Live View, Measurement,
        # Check Resolution, Check Spectrometer, Apply IT, EEPROM).
        self.acq = AcquisitionGuard()

        self.npix = 2048
        self.sn = "Unknown"
        self.spec = None
        self.spec_backend = None

        self.hw = HardwareState(
            dll_path="",
            spectrometer_type=self.DEFAULT_SPECTROMETER_TYPE,
            com_ports=dict(self.DEFAULT_COM_PORTS),
            laser_power=dict(self.DEFAULT_LASER_POWERS),
        )
        self.data = MeasurementData(npix=self.npix, serial_number=self.sn)
        self.available_lasers = list(self.DEFAULT_ALL_LASERS)
        self.laser_configs = self._build_default_laser_configs()
        self.measurement_flags: Dict[str, bool] = dict(self.DEFAULT_MEASUREMENT_FLAGS)

        self.lasers = LaserController(self.hw.com_ports)
        self.filterwheel = FilterWheelController(self.hw.com_ports["HEADSENSOR"])

        self.stage = StageController() if StageController else None
        self.stage_config_path = ""

        # Shared laser checkbox state. Created here (not inside a lazily built
        # tab) so Live View, Check Resolution and Measurements can all bind to
        # the same variables regardless of which tab is opened first.
        self.laser_vars: Dict[str, tk.BooleanVar] = {}
        # Sources currently emitting, as reported by every switching path (the
        # checkboxes, the measurement run, All off): drives the app-bar chip.
        self.lit_sources: set = set()
        # Progress of the measurement run: written by the orchestrator's
        # callbacks, read by the Measurements card and the app-bar chip.
        self.run_progress = RunProgress()

        self.live_running = threading.Event()
        self.measure_running = threading.Event()
        self._pending_it = None
        self._it_updating = False
        self.it_history: List[Tuple[float, float]] = []

        self.analysis_artifacts: List = []
        self.analysis_summary_lines: List[str] = []
        self.analysis_measurement_tabs: Dict[str, object] = {}
        self.analysis_measurement_counter = 0
        self.reference_csv_paths: List[str] = []
        self._latest_results_dir: Optional[Path] = None
        self._latest_csv_path: Optional[Path] = None
        self._latest_results_timestamp: Optional[str] = None
        self._analysis_metrics = None

        from tabs.ui_utils import StatusBar

        # Branded title strip: logo, what this application is, and the live
        # instrument state. Packed first so it owns the top of the window.
        self._build_app_bar()

        # Persistent status strip (messages, progress, toasts) replaces the ad-hoc
        # window-title messages and most informational pop-ups.
        self.status = StatusBar(self)
        self.status.pack(fill="x", side="bottom")

        # Event log drawer above the status bar, toggled from the bar or F12.
        # Every logger's records reach it through the pump, from any thread.
        from tabs.log_drawer import LogDrawer, install_log_handler

        self.log_drawer = LogDrawer(self, toggle_host=self.status.actions)
        self._log_handler = install_log_handler(self.pump.post, self.log_drawer.append)

        from tabs.theme import logical_px

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=logical_px(self, 10),
                     pady=(logical_px(self, 4), logical_px(self, 6)))

        self.setup_tab = ttk.Frame(self.nb)
        self.live_tab = ttk.Frame(self.nb)
        self.measure_tab = ttk.Frame(self.nb)
        self.check_res_tab = ttk.Frame(self.nb)
        self.analysis_tab = ttk.Frame(self.nb)
        self.eeprom_tab = ttk.Frame(self.nb)

        self.nb.add(self.setup_tab, text="  Setup  ")
        self.nb.add(self.live_tab, text="  Live View  ")
        self.nb.add(self.measure_tab, text="  Measurements  ")
        self.nb.add(self.check_res_tab, text="  Check Resolution  ")
        self.nb.add(self.analysis_tab, text="  Analysis  ")
        self.nb.add(self.eeprom_tab, text="  EEPROM  ")

        # Lazy-build the heavy independent tabs on first activation.
        # Setup/Measurements/Analysis are eager because the Run All flow
        # crosses between them at startup-time before the user clicks.
        from tabs import analysis_tab, measurements_tab, setup_tab

        setup_tab.build(self)
        measurements_tab.build(self)
        analysis_tab.build(self)

        self._tab_builders = {
            str(self.live_tab): self._build_live_view_tab,
            str(self.check_res_tab): self._build_check_resolution_tab,
            str(self.eeprom_tab): self._build_eeprom_tab,
        }
        # One-shot hooks run the first time an already-built tab is shown (e.g. the
        # Measurements plot is created only when the tab is first opened).
        self._tab_show_hooks: Dict[str, object] = getattr(self, "_tab_show_hooks", {})
        self._built_tabs = {str(self.setup_tab), str(self.measure_tab), str(self.analysis_tab)}
        self._current_tab_id = str(self.setup_tab)
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        if hasattr(self, "load_settings_into_ui"):
            try:
                self.load_settings_into_ui()
            except Exception:
                LOGGER.exception("Failed to load saved settings")

        # The eager tabs were built before the NotebookTabChanged binding
        # existed, so tint them here; lazily built tabs are handled on show.
        for tab in (self.setup_tab, self.measure_tab, self.analysis_tab):
            self._harmonize_tab(str(tab))
        self.refresh_instrument_chip()

        self._bind_shortcuts()
        self._add_tooltips()
        self.protocol("WM_DELETE_WINDOW", self._on_closing)
        self.status.set("Ready. Connect a spectrometer in Setup (Ctrl+1) to begin.")

        self._warm_up_tabs()
        self._show_window()

    # ------------------------------------------------------------------
    # Application title strip
    # ------------------------------------------------------------------

    APP_VERSION = DISPLAY_VERSION

    def _build_app_bar(self) -> None:
        """White title strip: SciGlob mark, product name, live instrument chip."""
        from tabs.run_progress import RunChip
        from tabs.theme import Colors, Fonts, gradient_rail, logical_px, make_chip, make_flat_button
        from tabs.ui_utils import Tooltip

        bar = ttk.Frame(self, style="Surface.TFrame")
        bar.pack(fill="x", side="top")
        inner = ttk.Frame(bar, style="Surface.TFrame")
        inner.pack(fill="x", padx=logical_px(self, 14), pady=(logical_px(self, 9), logical_px(self, 9)))

        # -- Left: logo + product name ---------------------------------
        self._logo_image = None
        logo_height = max(24, logical_px(self, 26))
        try:
            from PIL import Image, ImageTk

            image = Image.open(get_resource_path("sciglob_logoRGB.png"))
            width = max(1, int(image.width * logo_height / max(1, image.height)))
            image = image.resize((width, logo_height), Image.Resampling.LANCZOS)
            self._logo_image = ImageTk.PhotoImage(image, master=self)
            tk.Label(inner, image=self._logo_image, bg=Colors.BG_SURFACE,
                     bd=0, highlightthickness=0).pack(side="left")
        except Exception as exc:  # pragma: no cover - Pillow / asset missing
            LOGGER.debug("App-bar logo unavailable: %s", exc)
            ttk.Label(inner, text="SciGlob", style="Display.TLabel",
                      background=Colors.BG_SURFACE,
                      foreground=Colors.ACCENT_DEEP).pack(side="left")

        tk.Frame(inner, bg=Colors.BORDER_LIGHT, width=1).pack(
            side="left", fill="y", padx=14, pady=2)

        titles = ttk.Frame(inner, style="Surface.TFrame")
        titles.pack(side="left")
        ttk.Label(titles, text="Spectrometer Characterization System",
                  font=Fonts.H2, background=Colors.BG_SURFACE,
                  foreground=Colors.TEXT_PRIMARY).pack(anchor="w")
        ttk.Label(titles, text=f"{APP_NAME} {self.APP_VERSION}", font=Fonts.CAPTION,
                  background=Colors.BG_SURFACE,
                  foreground=Colors.TEXT_MUTED).pack(anchor="w")

        # -- Right: instrument, lit-source and run chips ------------------
        chip_pad = (logical_px(self, 12), 0)
        chip = make_chip(inner, "No spectrometer")
        chip.pack(side="right", padx=chip_pad)
        self._chip_dot, self._chip_text = chip.dot, chip.text

        # A laser left on is the state an operator most needs to see from any
        # tab, and this is the one place that switches everything off.
        self._laser_chip = make_chip(inner, "Sources off")
        self._laser_chip.pack(side="right", padx=chip_pad)
        self._laser_all_off_btn = make_flat_button(self._laser_chip, "All off", self.all_sources_off,
                                                   fg=Colors.DANGER, bg=Colors.BG_SUBTLE)
        self._laser_all_off_shown = False
        Tooltip(self._laser_all_off_btn,
                "Switch every laser and the lamp off. A running measurement is stopped first.")
        self._refresh_laser_chip()

        # The measurement run: hidden until one starts; a click opens Measurements.
        self._run_chip = RunChip(make_chip(inner), self.run_progress, post=self.pump.post,
                                 on_click=lambda: self.nb.select(self.measure_tab),
                                 pack_kwargs={"side": "right", "padx": chip_pad})

        gradient_rail(self, height=3, start=Colors.ACCENT_DEEP, end="#22d3ee",
                      background=Colors.BG_SURFACE).pack(fill="x", side="top")

    def refresh_instrument_chip(self) -> None:
        """Mirror the spectrometer state into the title strip."""
        from tabs.theme import Colors

        dot = getattr(self, "_chip_dot", None)
        text = getattr(self, "_chip_text", None)
        if dot is None or text is None:
            return
        if self.spec is not None:
            backend = self.spec_backend or "Spectrometer"
            serial = _clean_text(self.sn) or "Unknown"
            label, color = f"{backend}  ·  S/N {serial}", Colors.CONNECTED
        else:
            label, color = "No spectrometer", Colors.IDLE
        try:
            dot.configure(fg=color)
            text.configure(text=label)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Lit sources. Every path that switches a laser or the lamp reports here
    # (set_source_state for the checkboxes, the orchestrator's callbacks for a
    # run, All off), so the chip is right no matter which tab did the switching.
    # ------------------------------------------------------------------

    def _refresh_laser_chip(self) -> None:
        from tabs.theme import Colors

        chip = getattr(self, "_laser_chip", None)
        if chip is None:
            return
        ordered = [tag for tag in self.laser_tags() if tag in self.lit_sources]
        ordered += sorted(tag for tag in self.lit_sources if tag not in ordered)
        button = self._laser_all_off_btn
        try:
            if ordered:
                chip.dot.configure(fg=Colors.WARNING)
                chip.text.configure(text=f"{', '.join(source_label(tag) for tag in ordered)} on",
                                    fg=Colors.TEXT_PRIMARY)
                if not self._laser_all_off_shown:
                    button.pack(side="left", padx=(0, 6), pady=3)
                    self._laser_all_off_shown = True
            else:
                chip.dot.configure(fg=Colors.IDLE)
                chip.text.configure(text="Sources off", fg=Colors.TEXT_SECONDARY)
                if self._laser_all_off_shown:
                    button.pack_forget()
                    self._laser_all_off_shown = False
        except tk.TclError:
            pass

    def note_source_state(self, tag: str, is_on: bool) -> None:
        """Record that a source was switched. Safe from any thread."""
        if threading.current_thread() is not threading.main_thread():
            self.pump.post(self.note_source_state, tag, is_on)
            return
        tag = str(tag)
        if is_on:
            self.lit_sources.add(tag)
        else:
            self.lit_sources.discard(tag)
        try:
            # The Live View / Check Resolution checkboxes mirror the real state,
            # including the switching a measurement run does behind them.
            self.laser_var(tag).set(bool(is_on))
        except tk.TclError:
            pass
        self._refresh_laser_chip()

    def note_all_sources_off(self) -> None:
        """Record that every source was switched off. Safe from any thread."""
        if threading.current_thread() is not threading.main_thread():
            self.pump.post(self.note_all_sources_off)
            return
        self.lit_sources.clear()
        for var in self.laser_vars.values():
            try:
                var.set(False)
            except tk.TclError:
                pass
        self._refresh_laser_chip()

    def all_sources_off(self) -> None:
        """App-bar 'All off': stop a running measurement, then switch every source off."""
        from tabs.ui_utils import run_in_worker

        if self.measure_running.is_set():
            self.stop_measure()

        def _work():
            worker = getattr(self, "measure_thread", None)
            if worker is not None and worker.is_alive():
                worker.join(timeout=10.0)   # it switches its own source off on the way out
            if hasattr(self.lasers, "all_off"):
                return list(self.lasers.all_off() or [])
            for tag in list(self.lit_sources):
                self.set_source_state(tag, False)
            return []

        def _done(failed):
            self.note_all_sources_off()
            if failed:
                self.status.progress_end(f"All off: no reply from {', '.join(failed)}", kind="warning")
            else:
                self.status.progress_end("All sources off", kind="success")

        def _failed(exc):
            self.status.progress_end("All off failed", kind="error")
            self._post_error("All off", exc)

        self.status.progress_begin("Switching every source off…")
        run_in_worker(self, _work, on_done=_done, on_error=_failed, name="all-off")

    # ------------------------------------------------------------------
    # Keyboard shortcuts / tooltips
    # ------------------------------------------------------------------

    def _bind_shortcuts(self) -> None:
        tabs = [self.setup_tab, self.live_tab, self.measure_tab, self.check_res_tab, self.analysis_tab, self.eeprom_tab]
        for index, tab in enumerate(tabs):
            self.bind_all(f"<Control-Key-{index + 1}>", lambda _event, t=tab: self.nb.select(t))
        self.bind_all("<F5>", lambda _event: self._shortcut(getattr(self, "refresh_ports", None)))
        self.bind_all("<Control-s>", lambda _event: self._shortcut(self._save_settings_quietly))
        self.bind_all("<Control-l>", lambda _event: self._shortcut(self._toggle_live))
        self.bind_all("<F12>", lambda _event: self._shortcut(self.log_drawer.toggle))

    def _shortcut(self, action) -> str:
        if callable(action):
            try:
                action()
            except Exception as exc:
                self._post_error("Shortcut", exc)
        return "break"

    def _save_settings_quietly(self) -> None:
        saver = getattr(self, "save_settings_silently", None)
        if callable(saver):
            saver()
            self.status.toast("Settings saved")

    def _toggle_live(self) -> None:
        if self.live_running.is_set():
            self.stop_live()
        else:
            self.nb.select(self.live_tab)
            starter = getattr(self, "start_live", None)
            if callable(starter):
                starter()

    def _add_tooltips(self) -> None:
        from tabs.ui_utils import Tooltip

        tips = {
            "dll_entry": "Vendor DLL for DLL-backed backends (Ava1, Hama2/3/4, Hama6, MiniSpec, OcOpt1, Jeti1). "
                         "Leave empty to use the bundled or installed default; not used by Hama5 (USB) or the "
                         "Simulated backend.",
            "auto_it_entry": "Starting integration time (ms) for the auto-exposure search. Leave empty to use the "
                             "per-source defaults; the controller then jumps to the target band in a few frames.",
            "spec_type_combo": "Auto probes each hardware backend in turn. Pick Simulated to exercise the whole "
                               "workflow without instruments.",
        }
        for attribute, text in tips.items():
            widget = getattr(self, attribute, None)
            if widget is not None:
                Tooltip(widget, text)

    def laser_tags(self) -> List[str]:
        """Configured sources in ascending wavelength order, Hg-Ar lamp last."""
        def _key(tag: str):
            try:
                return (0, float(tag))
            except ValueError:
                return (1, tag)
        tags = set(self.laser_configs) | {"Hg_Ar"}
        return sorted(tags, key=_key)

    def _build_live_view_tab(self) -> None:
        from tabs import live_view_tab
        live_view_tab.build(self)

    def _build_check_resolution_tab(self) -> None:
        from tabs import check_resolution_tab
        check_resolution_tab.build(self)

    def _build_eeprom_tab(self) -> None:
        from tabs import eeprom_tab
        eeprom_tab.build(self)

    def _build_tab(self, tab_id: str) -> None:
        """Build a lazily created tab (once) and run its one-shot show hook."""
        if tab_id not in self._built_tabs:
            builder = self._tab_builders.get(tab_id)
            if builder is not None:
                try:
                    builder()
                except Exception:
                    LOGGER.exception("Failed to build tab %s", tab_id)
            self._built_tabs.add(tab_id)
        hook = self._tab_show_hooks.pop(tab_id, None)
        if hook is not None:
            try:
                hook()
            except Exception:
                LOGGER.exception("Tab show hook failed for %s", tab_id)

    def _warm_up_tabs(self) -> None:
        """Build, lay out and draw every tab at its final size before the window is shown.

        The window is still withdrawn but already zoomed, so each tab can be
        selected in turn without anything reaching the screen: its layout
        settles at the real pane size and its matplotlib figure is rendered
        once, into the canvas it will be shown in. Live View, Check Resolution
        and EEPROM used to be built on the first click, and the Measurements
        figure on first show, which cost a figure, a canvas and a full-size
        draw (0.3-1 s at 4K) at the moment the operator wanted the page. Now
        a tab switch is only the switch.
        """
        try:
            current = self.nb.select()
            for tab_id in self.nb.tabs():
                self._build_tab(tab_id)
                self.nb.select(tab_id)
                self.update()          # geometry passes, map/configure bindings, idle draws
            self.nb.select(current)
            self.update()
        except tk.TclError:
            LOGGER.exception("Tab warm-up interrupted")

    def _on_tab_changed(self, _event=None) -> None:
        try:
            selected = self.nb.select()
        except Exception:
            return
        if not selected:
            return
        self._build_tab(selected)
        self._current_tab_id = selected
        self._harmonize_tab(selected)

    def _harmonize_tab(self, tab_id) -> None:
        """Re-tint labels/frames to their container background.

        Run on every activation rather than once at build time: several panels
        (laser rows, stage slots, analysis runs) create widgets long after the
        tab was built, and they must not come back as grey blocks on the cards.
        """
        from tabs.theme import harmonize

        try:
            widget = self.nametowidget(tab_id)
        except Exception:
            return
        try:
            harmonize(widget)
        except Exception:
            LOGGER.debug("harmonize failed for %s", tab_id, exc_info=True)

    @property
    def analysis_service(self):
        """Created on first use: importing the analysis stack costs ~2 s and is not needed at startup."""
        service = self.__dict__.get("_analysis_service")
        if service is None:
            from services.analysis_service import AnalysisService

            service = AnalysisService()
            self._analysis_service = service
        return service

    def _is_tab_visible(self, tab_widget) -> bool:
        try:
            return getattr(self, "_current_tab_id", None) == str(tab_widget)
        except Exception:
            return True

    def _build_default_laser_configs(self) -> Dict[str, Dict[str, float]]:
        configs = {}
        for laser in self.DEFAULT_ALL_LASERS:
            if laser == "377":
                laser_type = "CUBE"
            elif laser in {"517", "532", "Hg_Ar"}:
                laser_type = "RELAY"
            else:
                laser_type = "OBIS"
            configs[laser] = {"type": laser_type, "power": self.DEFAULT_LASER_POWERS.get(laser, 0.01)}
        return configs

    def _post_error(self, title: str, ex: Exception) -> None:
        """Log an exception and show it to the operator. Safe from any thread."""
        tb = "".join(traceback.format_exception(type(ex), ex, ex.__traceback__))
        LOGGER.error("[%s] %s\n%s", title, ex, tb)
        self.pump.post(messagebox.showerror, title, str(ex))

    # ------------------------------------------------------------------
    # Spectrometer ownership. Every Start path claims the device first so two
    # threads can never drive one handle; Stop asks the backend to wake any
    # blocked wait immediately.
    # ------------------------------------------------------------------

    def claim_spectrometer(self, owner: str, title: Optional[str] = None) -> bool:
        if not self.spec:
            messagebox.showwarning(title or owner, "No spectrometer connected.")
            return False
        if not self.acq.try_acquire(owner):
            messagebox.showwarning(title or owner, self.acq.busy_reason() or "Spectrometer is busy.")
            return False
        return True

    def release_spectrometer(self, owner: str) -> None:
        self.acq.release(owner)

    def request_spectrometer_stop(self) -> None:
        spec = self.spec
        if spec is None:
            return
        try:
            spec.request_stop()
        except Exception:
            LOGGER.warning("Spectrometer stop request failed", exc_info=True)

    # ------------------------------------------------------------------
    # Defaults that lazily built tabs may override. Having them on the class
    # means Disconnect / on_close / Check Resolution work even if Live View or
    # Measurements were never opened in this session.
    # ------------------------------------------------------------------

    def stop_live(self) -> None:
        self.live_running.clear()
        self.request_spectrometer_stop()

    def stop_measure(self) -> None:
        self.measure_running.clear()
        self.request_spectrometer_stop()

    def laser_var(self, tag: str) -> tk.BooleanVar:
        """Return the shared BooleanVar for a source, creating it on first use."""
        var = self.laser_vars.get(tag)
        if var is None:
            var = tk.BooleanVar(master=self, value=False)
            self.laser_vars[tag] = var
        return var

    RELAY_CHANNEL_MAP = {"517": 2, "532": 1, "Hg_Ar": 4}

    def apply_measurement_flags(self) -> None:
        """Push the persisted bench options to the objects that consume them."""
        framed = bool(self.measurement_flags.get("framed_serial_reads", False))
        for controller in (self.lasers, getattr(self, "_hardware_lasers", None)):
            if controller is not None and hasattr(controller, "framed_reads"):
                controller.framed_reads = framed
        enabled = [name for name, value in self.measurement_flags.items() if value]
        if enabled:
            LOGGER.info("Bench-validated measurement options enabled: %s", ", ".join(enabled))

    def install_laser_controller_for_backend(self, backend_type: Optional[str]) -> None:
        """Pair the simulated spectrometer with the simulated light bench; real serial lasers otherwise."""
        if backend_type == "Simulated" and self.spec is not None:
            from spectrometers.simulated_spectrometer import SimulatedLaserController

            if not isinstance(self.lasers, SimulatedLaserController):
                self._hardware_lasers = self.lasers
                self.lasers = SimulatedLaserController(self.spec.bench)
            return
        hardware = getattr(self, "_hardware_lasers", None)
        if hardware is not None:
            self.lasers = hardware
            self._hardware_lasers = None

    def set_source_state(self, tag: str, turn_on: bool) -> None:
        """Turn one light source on or off. Blocking serial I/O; not for the Tk thread."""
        self.lasers.ensure_open_for_tag(tag)
        obis_map = self.lasers.OBIS_MAP
        if tag in obis_map:
            channel = obis_map[tag]
            if turn_on:
                self.lasers.obis_set_power(channel, float(self._get_power(tag)))
                self.lasers.obis_on(channel)
            else:
                self.lasers.obis_off(channel)
        elif tag == "377":
            if turn_on:
                value = float(self._get_power(tag))
                milliwatts = value * 1000.0 if value <= 0.3 else value
                self.lasers.cube_on(power_mw=milliwatts)
            else:
                self.lasers.cube_off()
        elif tag in self.RELAY_CHANNEL_MAP:
            relay = self.RELAY_CHANNEL_MAP[tag]
            if turn_on:
                self.lasers.relay_on(relay)
            else:
                self.lasers.relay_off(relay)
        else:
            raise RuntimeError(f"Unsupported source '{tag}'.")
        self.note_source_state(tag, turn_on)

    def toggle_laser(self, tag: str, turn_on: bool) -> None:
        """Checkbox handler shared by Live View and Check Resolution.

        The serial commands (up to ~6 s of fixed waits for the CUBE) run on a
        worker thread; the checkbox is reverted if the device refuses.
        """
        from tabs.ui_utils import run_in_worker

        try:
            self._update_ports_from_ui()  # reads Tk entries: main thread only
        except Exception as exc:
            self._post_error(f"Laser {tag}", exc)
            return

        def _revert(exc):
            try:
                self.laser_var(tag).set(not turn_on)
            except Exception:
                pass
            self.status.progress_end(f"{tag}: switching failed", kind="error")
            self._post_error(f"Laser {tag}", exc)

        def _done(_result):
            self.status.progress_end(f"{source_label(tag)} {'on' if turn_on else 'off'}", kind="success")

        self.status.progress_begin(f"Switching {tag} {'on' if turn_on else 'off'}…")
        run_in_worker(self, lambda: self.set_source_state(tag, turn_on), on_done=_done, on_error=_revert, name=f"laser-{tag}")

    def _on_closing(self) -> None:
        # Stop marshalling first: nothing posted from a worker may touch Tk
        # once teardown has started.
        self.pump.stop()
        handler = getattr(self, "on_close", None)
        if callable(handler):
            try:
                handler()
                return
            except Exception:
                LOGGER.exception("Error while closing application")
        self.destroy()

    def _live_reset_view(self) -> None:
        try:
            self.live_limits_locked = False
            if hasattr(self, "live_ax"):
                self.live_ax.relim()
                self.live_ax.autoscale_view()
            blit = getattr(self, "_live_blit", None)
            if blit is not None:
                blit.request_full()
            if hasattr(self, "live_canvas"):
                self.live_canvas.draw_idle()
        except Exception as exc:
            self._post_error("Reset Zoom", exc)

    def rebuild_laser_ui(self) -> None:
        self.available_lasers = list(self.laser_configs.keys())
        if "Hg_Ar" not in self.available_lasers:
            self.available_lasers.append("Hg_Ar")

    def update_target_peak(self, value) -> None:
        try:
            target = int(value)
        except Exception:
            return
        self.TARGET_MID = target
        self.TARGET_LOW = max(0, target - 2500)
        self.TARGET_HIGH = target + 2500
        if hasattr(self, "target_band_label"):
            self.target_band_label.config(text=f"Target window: {self.TARGET_LOW}-{self.TARGET_HIGH}")

    def _clear_analysis_window(self) -> None:
        """Reset current-run analysis state. Previous run tabs are preserved."""
        self.analysis_artifacts = []
        self.analysis_summary_lines = []
        self._analysis_metrics = None
        self._latest_results_dir = None
        self._latest_csv_path = None
        self._latest_results_timestamp = None

    def _ensure_results_dir(self) -> Path:
        self.RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
        if self._latest_results_dir is None:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            serial_number = _clean_text(self.data.serial_number or self.sn or "Unknown")
            serial_number = serial_number.replace(" ", "_")
            self._latest_results_dir = self.RESULTS_ROOT / f"{serial_number}_{stamp}"
            self._latest_results_dir.mkdir(parents=True, exist_ok=True)
        return self._latest_results_dir

    def save_measurement_data(self) -> Optional[str]:
        if not self.data.rows:
            return None

        output_dir = self._ensure_results_dir()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        serial_number = _clean_text(self.data.serial_number or self.sn or "Unknown")
        path = output_dir / f"Measurements_{serial_number}_{timestamp}.csv"
        df = self.data.to_dataframe()
        df.to_csv(path, index=False)
        self._latest_csv_path = path
        return str(path)

    def run_analysis_and_save_plots(self, csv_path: Optional[str] = None):
        """Run comprehensive analysis using the analysis service and save all plots."""
        if csv_path:
            df = _pandas().read_csv(csv_path)
            results_dir = Path(csv_path).resolve().parent
            self._latest_csv_path = Path(csv_path).resolve()
            self._latest_results_dir = results_dir
        else:
            if not self.data.rows:
                return []
            df = self.data.to_dataframe()
            results_dir = self._ensure_results_dir()
            if self._latest_csv_path is None:
                saved_path = self.save_measurement_data()
                if saved_path:
                    self._latest_csv_path = Path(saved_path)

        plots_dir = results_dir / "plots"
        plots_dir.mkdir(parents=True, exist_ok=True)

        if "Wavelength" in df.columns:
            df["Wavelength"] = df["Wavelength"].astype(str)

        sn = _clean_text(self.data.serial_number or self.sn or "Unknown")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        reference_csv_paths = getattr(self, "reference_csv_paths", [])

        LOGGER.info("Starting comprehensive analysis via AnalysisService...")
        try:
            result = self.analysis_service.analyze(
                df=df,
                serial_number=sn,
                output_dir=str(plots_dir),
                timestamp=timestamp,
                reference_csv_paths=reference_csv_paths or None,
            )
        except Exception as exc:
            LOGGER.exception("AnalysisService raised an exception")
            self.analysis_artifacts = []
            self.analysis_summary_lines = [f"Analysis error: {exc}"]
            self._analysis_metrics = None
            self._latest_results_timestamp = timestamp
            return []

        self.analysis_artifacts = result.artifacts if result else []
        self.analysis_summary_lines = result.summary_lines if result else []
        self._analysis_metrics = result.metrics if result else None
        self._latest_results_timestamp = timestamp

        # This runs on the measurement worker; widget state changes go through the pump.
        button_state = ["!disabled"] if self.analysis_artifacts else ["disabled"]
        for name in ("export_plots_btn", "open_folder_btn", "clear_all_btn"):
            button = getattr(self, name, None)
            if button is not None:
                self.pump.post(button.state, button_state)

        paths = [getattr(art, "path", "") for art in self.analysis_artifacts]
        LOGGER.info("Analysis complete. %d plots generated to: %s", len(paths), plots_dir)
        return paths

    # ------------------------------------------------------------------
    # Check Spectrometer
    # ------------------------------------------------------------------

    def run_check_spectrometer(self) -> None:
        """Launch Check Spectrometer in a background thread."""
        if not self.claim_spectrometer("Check Spectrometer"):
            return

        btn = getattr(self, "check_spec_btn", None)
        if btn:
            btn.configure(state="disabled")

        def _run():
            try:
                from services.check_spectrometer_service import CheckSpectrometerService

                output_dir = self._ensure_results_dir()
                sn = _clean_text(self.data.serial_number or self.sn or "Unknown")
                instrument_name = _clean_text(
                    getattr(self.spec, "name", "") or getattr(self.spec, "sn", "") or sn
                )
                location = _clean_text(getattr(self, "location", "") or "")

                service = CheckSpectrometerService(
                    output_dir=output_dir,
                    instrument_name=instrument_name,
                    location=location,
                )
                result = service.run(self.spec)

                # Build an AnalysisArtifact so the standard display path works
                from analysis.models import AnalysisArtifact

                artifact = AnalysisArtifact(
                    name="Check Spectrometer — Strongest Line Fit",
                    path=result.plot_path,
                )

                # Compose summary
                summary_lines = [
                    "=== Check Spectrometer Results ===",
                    f"  Auto-IT (settled)    : {result.auto_it_ms:.4f} ms",
                    "  Measure IT           : 3000.0 ms",
                    f"  Center pixel (xcen)  : {result.xcen:.2f}",
                    f"  Width (resolfit)     : {result.resolfit:.3f} px",
                    f"  Shape exponent (n)   : {result.shape_exponent:.4f}",
                    f"  Fit RMS              : {result.rms:.4f}",
                    f"  Fit status           : {'OK' if result.fit_err == 0 else 'Iteration limit reached'}",
                    f"  CSV saved            : {result.csv_path}",
                    f"  Plot saved           : {result.plot_path}",
                ] + (result.warnings if result.warnings else [])

                self._latest_results_dir = output_dir

                def _show():
                    # Inject into the standard analysis display
                    self.analysis_artifacts = [artifact]
                    self.analysis_summary_lines = summary_lines
                    self._latest_results_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    self.refresh_analysis_view()

                self.pump.post(_show)

            except Exception as exc:
                self._post_error("Check Spectrometer", exc)
            finally:
                self.release_spectrometer("Check Spectrometer")
                if btn:
                    self.pump.post(btn.configure, {"state": "normal"})

        threading.Thread(target=_run, daemon=True, name="check-spectrometer").start()

    def _start_background_analysis(self, csv_path: str) -> bool:
        """Run the analysis pipeline on a worker and show the result when it lands."""
        worker = getattr(self, "_analysis_thread", None)
        if worker is not None and worker.is_alive():
            messagebox.showinfo("Analysis", "An analysis is already running.")
            return False

        def _work():
            try:
                self.run_analysis_and_save_plots(csv_path)
            except Exception as exc:
                self._post_error("Analysis", exc)
                return
            self.pump.post(self.refresh_analysis_view)

        self._analysis_thread = threading.Thread(target=_work, daemon=True, name="analysis")
        self._analysis_thread.start()
        return True

    def refresh_analysis_view(self):
        """Add a new run tab to the analysis notebook with saved plot previews."""
        if not self.analysis_artifacts:
            csv_path = None
            if self._latest_csv_path and Path(self._latest_csv_path).is_file():
                csv_path = str(self._latest_csv_path)
            elif self.data.rows:
                csv_path = self.save_measurement_data()
            if not csv_path:
                messagebox.showwarning("Analysis", "No measurement data available.")
                return
            if getattr(self, "_analysis_view_pending", False):
                # The background run finished with nothing to show.
                self._analysis_view_pending = False
                messagebox.showwarning("Analysis", "No analysis plots were generated.")
                return
            # Several seconds of matplotlib rendering: never on the Tk thread.
            self._analysis_view_pending = self._start_background_analysis(csv_path)
            return
        self._analysis_view_pending = False

        notebook = getattr(self, "analysis_measurements_notebook", None)
        if notebook is None:
            return

        from tabs.theme import Colors, logical_px, style_text
        from tabs.ui_utils import ScrollableFrame

        self.analysis_measurement_counter += 1
        timestamp = getattr(self, "_latest_results_timestamp", None) or datetime.now().strftime("%Y%m%d_%H%M%S")
        tab_name = f"Run #{self.analysis_measurement_counter} ({timestamp})"

        # Remove welcome tab if it exists (destroy, not just forget, so Tk frees it)
        welcome = getattr(self, "analysis_welcome_tab", None)
        if welcome is not None:
            try:
                notebook.forget(welcome)
                welcome.destroy()
            except Exception:
                pass
            self.analysis_welcome_tab = None

        # Bound memory: each run keeps ~10 MB of preview images alive.
        self._prune_analysis_tabs(notebook, keep=MAX_ANALYSIS_RUN_TABS - 1)

        # Create new tab for this measurement run
        measurement_tab = ttk.Frame(notebook)
        notebook.add(measurement_tab, text=tab_name)

        scroll = ScrollableFrame(measurement_tab, x_scroll=True, y_scroll=True, background=Colors.BG_PRIMARY)
        scroll.pack(fill="both", expand=True)
        scrollable_frame = scroll.content

        # Preview grid: as many columns as the width allows, previews sized to fill them.
        num_plots = len(self.analysis_artifacts)
        notebook_width = max(600, notebook.winfo_width() or self.winfo_width() or 0)
        cols = 1 if num_plots == 1 else min(num_plots, max(2, notebook_width // logical_px(self, 760)))
        preview_width = max(420, min(1400, notebook_width // cols - logical_px(self, 70)))
        preview_size = (preview_width, int(preview_width * PREVIEW_MAX_SIZE[1] / PREVIEW_MAX_SIZE[0]))
        preview_targets = []

        for idx, artifact in enumerate(self.analysis_artifacts):
            row = idx // cols
            col = idx % cols

            art_name = getattr(artifact, "name", f"Plot {idx + 1}")
            art_path = Path(str(getattr(artifact, "path", "")))

            plot_frame = ttk.LabelFrame(scrollable_frame, text=art_name, padding=12)
            plot_frame.grid(row=row, column=col, padx=10, pady=10, sticky="nsew")

            actions_frame = ttk.Frame(plot_frame)
            actions_frame.pack(fill="x", pady=(0, 6))
            ttk.Label(
                actions_frame,
                text=art_path.name if art_path.name else "Plot file unavailable",
                style="Secondary.TLabel",
            ).pack(side="left")
            ttk.Button(
                actions_frame,
                text="Open Image",
                command=lambda p=art_path: self._open_path_with_default_app(p, "Analysis Plot"),
                width=12,
            ).pack(side="right")

            preview_label = ttk.Label(plot_frame, text="Loading preview…", anchor="center", style="Muted.TLabel")
            preview_label.pack(fill="both", expand=True, padx=5, pady=5)
            if art_path.is_file():
                preview_targets.append((preview_label, art_path))
            else:
                preview_label.configure(text="Plot file missing.")

        for c in range(cols):
            scrollable_frame.grid_columnconfigure(c, weight=1, uniform="plots")

        # Summary section at the bottom
        summary_row = (num_plots + cols - 1) // cols
        summary_frame = ttk.LabelFrame(scrollable_frame, text="Analysis Summary", padding=12)
        summary_frame.grid(row=summary_row, column=0, columnspan=cols, padx=10, pady=10, sticky="ew")

        summary_text_widget = tk.Text(summary_frame, height=10, wrap="word")
        style_text(summary_text_widget)
        summary_text = "\n".join(self.analysis_summary_lines) if self.analysis_summary_lines else "No summary available."
        summary_text_widget.insert("1.0", summary_text)
        summary_text_widget.configure(state="disabled")
        summary_text_widget.pack(fill="both", expand=True)

        # Store tab reference
        record = {
            "tab": measurement_tab,
            "timestamp": timestamp,
            "csv_path": str(self._latest_csv_path) if self._latest_csv_path else None,
            "images": [],
        }
        self.analysis_measurement_tabs[tab_name] = record
        # Decode + thumbnail the PNGs on a worker; only the PhotoImage creation touches Tk.
        self._load_analysis_previews_async(preview_targets, record, preview_size)

        for name in ("export_plots_btn", "open_folder_btn", "clear_all_btn"):
            button = getattr(self, name, None)
            if button is not None:
                button.state(["!disabled"])

        # Switch to the new tab and the Analysis page
        notebook.select(measurement_tab)
        try:
            self.nb.select(self.analysis_tab)
        except Exception:
            pass

    def export_analysis_plots(self):
        if not self.analysis_artifacts:
            messagebox.showwarning("Export Plots", "No analysis plots available.")
            return
        destination = filedialog.askdirectory(title="Select folder to export analysis plots")
        if not destination:
            return
        dest_dir = Path(destination)
        copied = 0
        for artifact in self.analysis_artifacts:
            src = Path(str(getattr(artifact, "path", "")))
            if not src.is_file():
                continue
            shutil.copy2(src, dest_dir / src.name)
            copied += 1
        messagebox.showinfo("Export Plots", f"Exported {copied} plot(s) to:\n{dest_dir}")

    def export_analysis_summary(self):
        if not self.analysis_summary_lines:
            messagebox.showwarning("Export Summary", "No analysis summary available.")
            return
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = filedialog.asksaveasfilename(
            title="Save Analysis Summary",
            defaultextension=".txt",
            initialfile=f"analysis_summary_{timestamp}.txt",
            filetypes=[("Text", "*.txt"), ("All Files", "*.*")],
        )
        if not path:
            return
        Path(path).write_text("\n".join(self.analysis_summary_lines) + "\n", encoding="utf-8")
        messagebox.showinfo("Export Summary", f"Saved summary to:\n{path}")

    def open_results_folder(self):
        if self._latest_results_dir is None or not self._latest_results_dir.exists():
            messagebox.showwarning("Results Folder", "No results folder available yet.")
            return
        folder = self._latest_results_dir.resolve()
        if not folder.is_dir():
            messagebox.showwarning("Results Folder", "Path is not a valid directory.")
            return
        results_root = self.RESULTS_ROOT.resolve()
        if not str(folder).startswith(str(results_root)):
            messagebox.showerror("Results Folder", "Path is outside the results directory.")
            return
        self._open_path_with_default_app(folder, "Results Folder")

    def _prune_analysis_tabs(self, notebook, keep: int) -> None:
        """Destroy the oldest run tabs (and release their images) beyond ``keep``."""
        while len(self.analysis_measurement_tabs) > max(0, keep):
            oldest_name = next(iter(self.analysis_measurement_tabs))
            record = self.analysis_measurement_tabs.pop(oldest_name)
            record["images"] = []
            widget = record.get("tab")
            if widget is not None:
                try:
                    notebook.forget(widget)
                except Exception:
                    pass
                try:
                    widget.destroy()
                except Exception:
                    pass
            LOGGER.info("Released analysis tab %s to bound memory use", oldest_name)

    def _load_analysis_previews_async(self, targets, record, size=PREVIEW_MAX_SIZE) -> None:
        """Decode and thumbnail plot PNGs off the Tk thread, then attach them via the pump."""
        if not targets:
            return

        def _decode():
            try:
                from PIL import Image
            except Exception as exc:
                LOGGER.warning("Pillow unavailable; analysis previews disabled: %s", exc)
                return
            resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
            decoded = []
            for label, path in targets:
                try:
                    with Image.open(path) as raw_image:
                        preview = raw_image.convert("RGB")
                    preview.thumbnail(size, resampling)
                    decoded.append((label, path, preview))
                except Exception:
                    LOGGER.exception("Unable to build analysis preview for %s", path)
                    decoded.append((label, path, None))
            self.pump.post(self._attach_analysis_previews, decoded, record)

        threading.Thread(target=_decode, daemon=True, name="analysis-previews").start()

    def _attach_analysis_previews(self, decoded, record) -> None:
        """Main thread: turn decoded previews into PhotoImages on their labels."""
        try:
            from PIL import ImageTk
        except Exception:
            return
        for label, path, preview in decoded:
            try:
                if not label.winfo_exists():
                    continue
                if preview is None:
                    label.configure(text=f"Saved plot:\n{path.name}")
                    continue
                photo = ImageTk.PhotoImage(preview, master=self)
                label.configure(image=photo, text="")
                label.image = photo
                label.bind("<Button-1>", lambda _event, p=path: self._open_path_with_default_app(p, "Analysis Plot"))
                record["images"].append(photo)
            except tk.TclError:
                continue

    def _open_path_with_default_app(self, path: Path, title: str) -> None:
        if not path.exists():
            messagebox.showwarning(title, f"Path not found:\n{path}")
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "--", str(path)])  # noqa: S603
            else:
                subprocess.Popen(["xdg-open", "--", str(path)])  # noqa: S603
        except Exception as exc:
            self._post_error(title, exc)

    def _render_auto_it_plot(self, payload) -> None:
        """Main thread: draw the latest Auto-IT frame on the Measurements plot."""
        plot_tag, plot_spectrum, plot_it_ms, plot_peak = payload
        hint = getattr(self, "meas_empty_text", None)
        if hint is not None and hint.get_visible():
            hint.set_visible(False)
        xs = np.arange(len(plot_spectrum))
        self.meas_sig_line.set_data(xs, plot_spectrum)
        self.meas_ax.set_xlim(0, max(10, len(plot_spectrum) - 1))
        self.meas_ax.set_ylim(0, 65000)
        from tabs.theme import plot_title_font

        self.meas_ax.set_title(
            f"Auto-IT: {plot_tag} nm | IT={plot_it_ms:.2f} ms | Peak={plot_peak:.0f}",
            **plot_title_font(pad=8),
        )

    def _update_auto_it_plot(self, tag, spectrum, it_ms, peak):
        """Worker thread: queue the newest Auto-IT frame; at most one redraw per pump tick."""
        redraw = getattr(self, "_auto_it_redraw", None)
        if redraw is None:
            if getattr(self, "meas_canvas", None) is None:
                return  # Measurements plot not built yet
            redraw = ThrottledRedraw(
                self.pump,
                self._render_auto_it_plot,
                draw=self.meas_canvas.draw_idle,
                visible=lambda: self._is_tab_visible(self.measure_tab),
            )
            self._auto_it_redraw = redraw
        redraw.submit((str(tag), np.asarray(spectrum, dtype=float), float(it_ms), float(peak)))
