from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import pandas as pd
except Exception:  # pragma: no cover - runtime dependency on target machine
    pd = None

try:
    from PIL import Image, ImageTk
except Exception:  # pragma: no cover
    Image = None
    ImageTk = None

from domain.measurement import MeasurementData
from hardware.controllers import FilterWheelController, LaserController
from services.analysis_service import AnalysisService

try:
    from stage.stage_controller import StageController
except ImportError:
    StageController = None


LOGGER = logging.getLogger(__name__)


def get_resource_path(relative_path: str) -> str:
    """Return a bundled-resource path that also works for frozen apps."""
    base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return str((base_dir / relative_path).resolve())


def get_writable_path(relative_path: str) -> str:
    """Return a writable path near the app, falling back to the user home."""
    preferred_base = Path(__file__).resolve().parent
    try:
        preferred_base.mkdir(parents=True, exist_ok=True)
        test_path = preferred_base / ".write_test"
        test_path.write_text("ok", encoding="utf-8")
        test_path.unlink(missing_ok=True)
        return str((preferred_base / relative_path).resolve())
    except Exception:
        fallback_base = Path.home() / ".head-scilab"
        fallback_base.mkdir(parents=True, exist_ok=True)
        return str((fallback_base / relative_path).resolve())


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
    IT_STEP_UP = 0.3
    IT_STEP_DOWN = 0.1
    MAX_IT_ADJUST_ITERS = 1000
    SAT_THRESH = 65400

    SETTINGS_FILE = get_writable_path("spectro_gui_settings.json")
    RESULTS_ROOT = Path(get_writable_path("results"))

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

    def __init__(self):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        super().__init__()

        self.title("SciGlob Spectrometer Characterization System")
        self._configure_initial_geometry()

        try:
            self.iconbitmap(get_resource_path("sciglob_symbol.ico"))
        except Exception:
            pass

        # Apply centralized UI theme
        from tabs.theme import apply_theme
        apply_theme(self)

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
        self.analysis_service = AnalysisService()
        self.data = MeasurementData(npix=self.npix, serial_number=self.sn)
        self.available_lasers = list(self.DEFAULT_ALL_LASERS)
        self.laser_configs = self._build_default_laser_configs()

        self.lasers = LaserController(self.hw.com_ports)
        self.filterwheel = FilterWheelController(self.hw.com_ports["HEADSENSOR"])

        self.stage = StageController() if StageController else None
        self.stage_config_path = ""

        self.live_running = threading.Event()
        self.measure_running = threading.Event()
        self._pending_it = None
        self._it_updating = False
        self.it_history: List[Tuple[float, float]] = []
        self._pending_live_plot = None
        self._live_plot_redraw_id = None
        self._pending_measurement_plot = None
        self._measurement_plot_redraw_id = None

        self.analysis_artifacts: List = []
        self.analysis_summary_lines: List[str] = []
        self.analysis_measurement_tabs: Dict[str, object] = {}
        self.analysis_measurement_counter = 0
        self.reference_csv_paths: List[str] = []
        self._latest_results_dir: Optional[Path] = None
        self._latest_csv_path: Optional[Path] = None
        self._latest_results_timestamp: Optional[str] = None
        self._analysis_images: List[object] = []
        self._analysis_metrics = None
        self._auto_it_redraw_id = None
        self._pending_auto_it_plot = None

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=6, pady=(6, 4))

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
        self._built_tabs = {str(self.setup_tab), str(self.measure_tab), str(self.analysis_tab)}
        self._current_tab_id = str(self.setup_tab)
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        if hasattr(self, "load_settings_into_ui"):
            try:
                self.load_settings_into_ui()
            except Exception:
                LOGGER.exception("Failed to load saved settings")

        self.protocol("WM_DELETE_WINDOW", self._on_closing)

    def _build_live_view_tab(self) -> None:
        from tabs import live_view_tab
        live_view_tab.build(self)

    def _build_check_resolution_tab(self) -> None:
        from tabs import check_resolution_tab
        check_resolution_tab.build(self)

    def _build_eeprom_tab(self) -> None:
        from tabs import eeprom_tab
        eeprom_tab.build(self)

    def _on_tab_changed(self, _event=None) -> None:
        try:
            selected = self.nb.select()
        except Exception:
            return
        if not selected:
            return
        if selected not in self._built_tabs:
            builder = self._tab_builders.get(selected)
            if builder is not None:
                try:
                    builder()
                except Exception:
                    LOGGER.exception("Failed to build tab %s", selected)
                self._built_tabs.add(selected)
        self._current_tab_id = selected

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
        tb = "".join(traceback.format_exception(type(ex), ex, ex.__traceback__))
        LOGGER.error("[%s] %s\n%s", title, ex, tb)
        try:
            self.after(0, lambda: messagebox.showerror(title, str(ex)))
        except Exception:
            pass

    def _on_closing(self) -> None:
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
        self._analysis_images = []
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
        if pd is None:
            raise RuntimeError("pandas is required to save measurement data.")

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
        if pd is None:
            raise RuntimeError("pandas is required to run analysis.")

        if csv_path:
            df = pd.read_csv(csv_path)
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

        if hasattr(self, "export_plots_btn"):
            self.export_plots_btn.state(["!disabled"] if self.analysis_artifacts else ["disabled"])
        if hasattr(self, "open_folder_btn"):
            self.open_folder_btn.state(["!disabled"] if self.analysis_artifacts else ["disabled"])

        paths = [getattr(art, "path", "") for art in self.analysis_artifacts]
        LOGGER.info("Analysis complete. %d plots generated to: %s", len(paths), plots_dir)
        return paths

    # ------------------------------------------------------------------
    # Check Spectrometer
    # ------------------------------------------------------------------

    def run_check_spectrometer(self) -> None:
        """Launch Check Spectrometer in a background thread."""
        if not self.spec:
            messagebox.showwarning("Check Spectrometer", "No spectrometer connected.")
            return
        if self.measure_running.is_set():
            messagebox.showwarning(
                "Check Spectrometer", "A measurement is already running. Please stop it first."
            )
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
                    f"  Measure IT           : 3000.0 ms",
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

                self.after(0, _show)

            except Exception as exc:
                self._post_error("Check Spectrometer", exc)
            finally:
                if btn:
                    self.after(0, lambda: btn.configure(state="normal"))

        threading.Thread(target=_run, daemon=True).start()

    def refresh_analysis_view(self):
        """Add a new run tab to the analysis notebook with saved plot previews."""
        if not self.analysis_artifacts:
            if self._latest_csv_path and Path(self._latest_csv_path).is_file():
                self.run_analysis_and_save_plots(str(self._latest_csv_path))
            elif self.data.rows:
                csv_path = self.save_measurement_data()
                if csv_path:
                    self.run_analysis_and_save_plots(csv_path)
            else:
                messagebox.showwarning("Analysis", "No measurement data available.")
                return

        if not self.analysis_artifacts:
            messagebox.showwarning("Analysis", "No analysis plots were generated.")
            return

        notebook = getattr(self, "analysis_measurements_notebook", None)
        if notebook is None:
            return

        self.analysis_measurement_counter += 1
        timestamp = getattr(self, "_latest_results_timestamp", None) or datetime.now().strftime("%Y%m%d_%H%M%S")
        tab_name = f"Run #{self.analysis_measurement_counter} ({timestamp})"

        # Remove welcome tab if it exists
        if hasattr(self, "analysis_welcome_tab"):
            try:
                notebook.forget(self.analysis_welcome_tab)
            except Exception:
                pass

        # Create new tab for this measurement run
        measurement_tab = ttk.Frame(notebook)
        notebook.add(measurement_tab, text=tab_name)

        # Scrollable canvas for plots
        canvas_container = tk.Canvas(measurement_tab, highlightthickness=0)
        scrollbar_y = ttk.Scrollbar(measurement_tab, orient="vertical", command=canvas_container.yview)
        scrollbar_x = ttk.Scrollbar(measurement_tab, orient="horizontal", command=canvas_container.xview)
        scrollable_frame = ttk.Frame(canvas_container)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas_container.configure(scrollregion=canvas_container.bbox("all")),
        )
        canvas_container.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas_container.configure(yscrollcommand=scrollbar_y.set, xscrollcommand=scrollbar_x.set)

        def _on_mousewheel(event):
            canvas_container.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas_container.bind("<Enter>", lambda e: canvas_container.bind_all("<MouseWheel>", _on_mousewheel))
        canvas_container.bind("<Leave>", lambda e: canvas_container.unbind_all("<MouseWheel>"))

        canvas_container.grid(row=0, column=0, sticky="nsew")
        scrollbar_y.grid(row=0, column=1, sticky="ns")
        scrollbar_x.grid(row=1, column=0, sticky="ew")
        measurement_tab.grid_rowconfigure(0, weight=1)
        measurement_tab.grid_columnconfigure(0, weight=1)

        # Arrange plots in a 2-column grid
        num_plots = len(self.analysis_artifacts)
        cols = 2 if num_plots > 1 else 1
        tab_images = []

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
                foreground="#6c757d",
            ).pack(side="left")
            ttk.Button(
                actions_frame,
                text="Open Image",
                command=lambda p=art_path: self._open_path_with_default_app(p, "Analysis Plot"),
                width=12,
            ).pack(side="right")

            preview_label = ttk.Label(plot_frame, text="Preview unavailable", anchor="center")
            preview_label.pack(fill="both", expand=True, padx=5, pady=5)

            preview = self._build_analysis_preview_image(art_path)
            if preview is not None:
                preview_label.configure(image=preview, text="")
                preview_label.image = preview
                preview_label.bind("<Button-1>", lambda _event, p=art_path: self._open_path_with_default_app(p, "Analysis Plot"))
                tab_images.append(preview)
            elif art_path.is_file():
                preview_label.configure(text=f"Saved plot:\n{art_path.name}")
            else:
                preview_label.configure(text="Plot file missing.")

        for c in range(cols):
            scrollable_frame.grid_columnconfigure(c, weight=1, uniform="plots")

        # Summary section at the bottom
        summary_row = (num_plots + cols - 1) // cols
        summary_frame = ttk.LabelFrame(scrollable_frame, text="Analysis Summary", padding=12)
        summary_frame.grid(row=summary_row, column=0, columnspan=cols, padx=10, pady=10, sticky="ew")

        summary_text_widget = tk.Text(
            summary_frame, height=10, wrap="word",
            font=("TkDefaultFont", 9), relief="flat",
            bg="#f8f9fa", fg="#212529", padx=10, pady=10,
        )
        summary_text = "\n".join(self.analysis_summary_lines) if self.analysis_summary_lines else "No summary available."
        summary_text_widget.insert("1.0", summary_text)
        summary_text_widget.configure(state="disabled")
        summary_text_widget.pack(fill="both", expand=True)

        # Store tab reference
        self.analysis_measurement_tabs[tab_name] = {
            "tab": measurement_tab,
            "timestamp": timestamp,
            "csv_path": str(self._latest_csv_path) if self._latest_csv_path else None,
            "images": tab_images,
        }

        if hasattr(self, "export_plots_btn"):
            self.export_plots_btn.state(["!disabled"])
        if hasattr(self, "open_folder_btn"):
            self.open_folder_btn.state(["!disabled"])

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

    def _build_analysis_preview_image(self, image_path: Path):
        if Image is None or ImageTk is None or not image_path.is_file():
            return None
        try:
            with Image.open(image_path) as raw_image:
                preview = raw_image.copy()
            resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
            preview.thumbnail((720, 440), resampling)
            return ImageTk.PhotoImage(preview)
        except Exception:
            LOGGER.exception("Unable to build analysis preview for %s", image_path)
            return None

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

    def _update_auto_it_plot(self, tag, spectrum, it_ms, peak):
        """Update measurement plot during Auto-IT adjustment without flooding the UI thread."""
        self._pending_auto_it_plot = (str(tag), np.asarray(spectrum, dtype=float), float(it_ms), float(peak))
        if self._auto_it_redraw_id is not None:
            return

        def _flush_auto_it_plot():
            self._auto_it_redraw_id = None
            pending = self._pending_auto_it_plot
            if pending is None:
                return
            plot_tag, plot_spectrum, plot_it_ms, plot_peak = pending
            try:
                xs = np.arange(len(plot_spectrum))
                self.meas_sig_line.set_data(xs, plot_spectrum)
                self.meas_ax.set_xlim(0, max(10, len(plot_spectrum) - 1))
                self.meas_ax.set_ylim(0, 65000)
                self.meas_ax.set_title(
                    f"Auto-IT: {plot_tag} nm | IT={plot_it_ms:.2f} ms | Peak={plot_peak:.0f}",
                    fontsize=13, fontweight="bold", pad=8,
                )

                # Auto-IT inset is currently disabled; skip inset updates
                if False and getattr(self, "meas_inset", None) is not None:
                    steps = list(getattr(self, "it_history", []))
                    if steps:
                        st = np.arange(len(steps))
                        peaks = [value for (_, value) in steps]
                        its = [value for (value, _) in steps]
                        self.inset_peak_line.set_data(st, peaks)
                        self.inset_it_line.set_data(st, its)
                        self.meas_inset.set_xlim(-0.5, max(0.5, len(st) - 0.5))
                        self.meas_inset.relim()
                        self.meas_inset.autoscale_view()
                        self.meas_inset2.relim()
                        self.meas_inset2.autoscale_view()

                if self._is_tab_visible(self.measure_tab):
                    self.meas_canvas.draw_idle()
            except Exception:
                LOGGER.debug("Auto-IT plot update skipped (UI not ready)")

        self._auto_it_redraw_id = self.after(16, _flush_auto_it_plot)

    def _finalize_measurement_run(self):
        return None
