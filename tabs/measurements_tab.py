"""Measurements tab - automated laser measurement sequences with Auto-IT."""
import logging
import os
import threading
import time
from datetime import datetime
from types import MethodType
from typing import Dict

import numpy as np
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from services.measurement_orchestrator import (
    MeasurementOrchestrator,
    MeasurementOrchestratorCallbacks,
    MeasurementOrchestratorConfig,
)
from .ui_utils import ScrollableFrame, bind_debounced_configure

LOGGER = logging.getLogger(__name__)

def build(app):
    from .theme import Colors, Fonts, Spacing, configure_matplotlib_style, make_run_button

    # Import constants from app
    DEFAULT_ALL_LASERS = app.DEFAULT_ALL_LASERS
    def _add_reference_csv(app):
        """Add reference CSV files for analysis (supports multiple selection)."""
        try:
            file_paths = filedialog.askopenfilenames(
                title="Select Reference CSV File(s)",
                filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
                initialdir=os.getcwd()
            )

            # filedialog.askopenfilenames returns a tuple of strings (or empty tuple)
            # Convert to list for consistency
            if file_paths:
                file_paths_list = list(file_paths)
            else:
                return

            # Validate each CSV structure
            import pandas as pd
            required_columns = ['Wavelength_nm', 'WavelengthOffset_nm', 'LSF_Normalized']
            
            for file_path in file_paths_list:
                # Ensure file_path is a string
                if not isinstance(file_path, str):
                    LOGGER.warning("Skipping invalid path type: %s", type(file_path))
                    continue
                
                try:
                    # Check if file exists
                    if not os.path.isfile(file_path):
                        messagebox.showerror("File Not Found", f"File does not exist:\n{file_path}")
                        continue
                    
                    df = pd.read_csv(file_path)
                    if all(col in df.columns for col in required_columns):
                        filename = os.path.basename(file_path)
                        # Add to list if not already present
                        if file_path not in app.reference_csv_paths:
                            app.reference_csv_paths.append(file_path)
                            app.reference_csv_listbox.insert(tk.END, filename)
                            LOGGER.info("Reference CSV added: %s", file_path)
                    else:
                        messagebox.showerror("Invalid CSV",
                                           f"{os.path.basename(file_path)}\n\nCSV must contain columns: {', '.join(required_columns)}")
                except Exception as e:
                    messagebox.showerror("CSV Error", f"Error reading {os.path.basename(file_path)}:\n{str(e)}")
            
            # Update count label
            app.reference_csv_count_label.config(text=f"({len(app.reference_csv_paths)} file(s) selected)")
        except Exception as e:
            LOGGER.exception("Error in _add_reference_csv")
            messagebox.showerror("Upload Error", f"Error uploading file:\n{str(e)}")
    
    def _remove_selected_reference_csv(app):
        """Remove selected reference CSV from the list."""
        try:
            selection = app.reference_csv_listbox.curselection()
            if not selection:
                messagebox.showwarning("Remove CSV", "Please select a CSV file to remove.")
                return
            
            # Remove from listbox (in reverse to maintain indices)
            for idx in reversed(selection):
                app.reference_csv_listbox.delete(idx)
                app.reference_csv_paths.pop(idx)
            
            # Update count label
            app.reference_csv_count_label.config(text=f"({len(app.reference_csv_paths)} file(s) selected)")
            LOGGER.info("Reference CSV(s) removed. %d remaining.", len(app.reference_csv_paths))
        except Exception as e:
            messagebox.showerror("Remove Error", f"Error removing file:\n{e}")
    
    def _clear_all_reference_csvs(app):
        """Clear all reference CSVs."""
        app.reference_csv_listbox.delete(0, tk.END)
        app.reference_csv_paths.clear()
        app.reference_csv_count_label.config(text="(0 file(s) selected)")
        LOGGER.info("All reference CSVs cleared.")

    def _build_measure_tab():
        # Create main container with minimal padding for maximum space
        main_frame = ttk.Frame(app.measure_tab)
        main_frame.pack(fill="both", expand=True, padx=4, pady=4)

        # Top section - Live measurement plot (maximize space)
        plot_frame = ttk.Frame(main_frame)
        plot_frame.pack(fill="both", expand=True, pady=(0, 4))

        # Add matplotlib plot
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        from matplotlib.figure import Figure
        import numpy as np

        app.measure_fig = Figure(figsize=(14, 7), dpi=100, constrained_layout=True)
        app.measure_ax = app.measure_fig.add_subplot(111)
        configure_matplotlib_style(app.measure_fig, app.measure_ax, title="Live Measurement")
        app.measure_ax.set_xticks(np.arange(0, 2048, 100))
        app.measure_ax.set_ylim(0, 65000)

        # Initialize empty plot lines - signal (blue) and dark (black dashed)
        app.measure_line, = app.measure_ax.plot(np.zeros(2048), lw=1.5, color='#2563eb', label='Signal (Laser ON)')
        app.measure_dark_line, = app.measure_ax.plot(np.zeros(2048), lw=1.5, color='#1e293b', linestyle='--', label='Dark (Laser OFF)')
        # Saturation overlay lines – hidden until data exceeds clamp
        app.meas_sig_sat_line, = app.measure_ax.plot([], [], lw=2, color=Colors.DANGER, label='Saturated Signal')
        app.meas_dark_sat_line, = app.measure_ax.plot([], [], lw=2, color=Colors.DANGER, linestyle='--', label='Saturated Dark')
        app.meas_sig_sat_line.set_visible(False)
        app.meas_dark_sat_line.set_visible(False)
        app.measure_ax.legend(loc='upper right')

        # Add canvas to plot frame
        canvas = FigureCanvasTkAgg(app.measure_fig, plot_frame)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both", expand=True)
        app.meas_canvas = canvas
        app.meas_ax = app.measure_ax
        app.meas_sig_line = app.measure_line
        app.meas_dark_line = app.measure_dark_line
        # app.meas_inset = app.measure_ax.inset_axes([0.60, 0.55, 0.34, 0.34])
        # app.meas_inset.set_title("Auto-IT", fontsize=8)
        # app.meas_inset.grid(True, alpha=0.25)
        # app.inset_peak_line, = app.meas_inset.plot([], [], color="#d62728", marker="o", label="Peak")
        # app.meas_inset2 = app.meas_inset.twinx()
        # app.inset_it_line, = app.meas_inset2.plot([], [], color="#2ca02c", marker="s", label="IT")
        app.meas_inset = None
        app.meas_inset2 = None
        app.inset_peak_line = None
        app.inset_it_line = None

        # Bottom section - responsive, scrollable controls
        controls_host = ttk.Frame(main_frame, relief="flat")
        controls_host.pack(fill="both", pady=(0, 0))
        controls_host.pack_propagate(False)

        controls_scroll = ScrollableFrame(controls_host, y_scroll=True, background=Colors.BG_PRIMARY)
        controls_scroll.pack(fill="both", expand=True)

        controls_frame = ttk.Frame(controls_scroll.content)
        controls_frame.pack(fill="both", expand=True)

        # === LEFT: Laser Selection ===
        laser_frame = ttk.LabelFrame(controls_frame, text="Laser Selection", padding=(10, 6, 10, 6))
        laser_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        # Select All / Deselect All checkbox
        select_all_frame = ttk.Frame(laser_frame)
        select_all_frame.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))
        select_all_frame.columnconfigure(0, weight=1)
        
        app.select_all_lasers_var = tk.BooleanVar(value=True)
        
        def toggle_all_lasers():
            """Toggle all laser checkboxes on/off."""
            state = app.select_all_lasers_var.get()
            for var in app.measure_vars.values():
                var.set(state)
            # Switch to custom preset when manually toggling
            if hasattr(app, 'preset_var'):
                app.preset_var.set("Custom")
        
        select_all_chk = ttk.Checkbutton(
            select_all_frame, 
            text="Select All / Deselect All", 
            variable=app.select_all_lasers_var,
            command=toggle_all_lasers,
            style='Large.TCheckbutton'
        )
        select_all_chk.grid(row=0, column=0, sticky="w")
        
        # Add quick preset buttons
        quick_label = ttk.Label(select_all_frame, text="Quick:", font=Fonts.BODY_SMALL)
        quick_label.grid(row=0, column=1, sticky="w", padx=(8, 4))
        quick_scan_btn = ttk.Button(
            select_all_frame,
            text="Quick Scan",
            width=16,
            command=lambda: app.preset_var.set("Quick Scan") if hasattr(app, 'preset_var') else None,
        )
        quick_scan_btn.grid(row=0, column=2, sticky="w", padx=2)
        full_spectrum_btn = ttk.Button(
            select_all_frame,
            text="Full Spectrum",
            width=18,
            command=lambda: app.preset_var.set("Full Spectrum") if hasattr(app, 'preset_var') else None,
        )
        full_spectrum_btn.grid(row=0, column=3, sticky="w", padx=2)
        
        # Add separator
        laser_separator = ttk.Separator(laser_frame, orient="horizontal")
        laser_separator.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(0, 4))

        app.measure_vars = {}
        measure_checkbuttons = []
        # All lasers in ascending order (including Hg_Ar)
        all_lasers = ["377", "405", "445", "488", "517", "532", "640", "685", "Hg_Ar"]

        def on_laser_change(*args):
            """When user manually changes lasers, switch to Custom preset."""
            if hasattr(app, 'preset_var') and app.preset_var.get() != "Custom":
                # Check if current selection matches the active preset
                preset = app.measurement_presets.get(app.preset_var.get(), {})
                selected_lasers = preset.get("lasers", [])
                
                # Get currently selected lasers
                current_selection = [tag for tag, var in app.measure_vars.items() if var.get()]
                
                # If they don't match, switch to Custom
                if set(current_selection) != set(selected_lasers):
                    app.preset_var.set("Custom")

        # Create horizontal layout for laser checkboxes (2 rows) - starting from row 2
        for i, tag in enumerate(all_lasers):
            v = tk.BooleanVar(value=True)  # Default all selected
            label_text = f"{tag} nm" if tag != "Hg_Ar" else "Hg_Ar"
            chk = ttk.Checkbutton(laser_frame, text=label_text, variable=v, 
                                 style='Large.TCheckbutton')
            chk.grid(row=(i // 4) + 2, column=i % 4, padx=8, pady=3, sticky="w")
            app.measure_vars[tag] = v
            measure_checkbuttons.append(chk)
            # Trace changes to laser checkboxes
            v.trace_add("write", on_laser_change)


        # === MIDDLE LEFT: Settings ===
        settings_frame = ttk.LabelFrame(controls_frame, text="Settings", padding=(10, 6, 10, 6))
        settings_frame.grid(row=0, column=1, sticky="nsew", padx=6)

        # Grid layout for settings
        settings_grid = ttk.Frame(settings_frame)
        settings_grid.pack(fill="both", expand=True)
        
        # ===== MEASUREMENT PRESETS =====
        ttk.Label(settings_grid, text="Measurement Preset:", font=Fonts.BODY_SMALL_BOLD).grid(
            row=0, column=0, sticky="w", pady=3)
        
        # Define presets
        app.measurement_presets = {
            "Quick Scan": {
                "lasers": ["405", "445"],
                "reference_files": [],
                "description": "Fast scan with 405nm and 445nm lasers"
            },
            "Full Spectrum": {
                "lasers": ["377", "405", "445", "488", "517", "532", "640", "685", "Hg_Ar"],
                "reference_files": [],
                "description": "All lasers, no reference"
            },
            "Custom": {
                "lasers": [],
                "reference_files": [],
                "description": "Manual configuration"
            }
        }
        
        # Preset dropdown
        app.preset_var = tk.StringVar(value="Custom")
        preset_combo = ttk.Combobox(settings_grid, textvariable=app.preset_var, 
                                    values=list(app.measurement_presets.keys()),
                                    width=15, font=Fonts.BODY_SMALL, state="readonly")
        preset_combo.grid(row=0, column=1, sticky="w", pady=3, padx=(6, 0))
        
        def apply_preset(*args):
            """Apply the selected measurement preset."""
            preset_name = app.preset_var.get()
            if preset_name == "Custom":
                return  # Don't change anything for custom
            
            preset = app.measurement_presets.get(preset_name, {})
            selected_lasers = preset.get("lasers", [])
            
            # Update laser checkboxes
            for laser, var in app.measure_vars.items():
                if laser in selected_lasers:
                    var.set(True)
                else:
                    var.set(False)
            
            # Update select all checkbox state
            all_selected = all(var.get() for var in app.measure_vars.values())
            none_selected = not any(var.get() for var in app.measure_vars.values())
            if all_selected:
                app.select_all_lasers_var.set(True)
            elif none_selected:
                app.select_all_lasers_var.set(False)
            
            # Clear reference files
            _clear_all_reference_csvs(app)
            
            # Show info
            description = preset.get("description", "")
            if description:
                # Update a status label if we have one, or just print
                LOGGER.info("Applied preset: %s - %s", preset_name, description)
        
        app.preset_var.trace_add("write", apply_preset)
        
        # Preset description label
        app.preset_desc_label = ttk.Label(settings_grid, text="", 
                                         font=Fonts.CAPTION, foreground="gray")
        app.preset_desc_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 5))
        
        def update_preset_description(*args):
            """Update the description label when preset changes."""
            preset_name = app.preset_var.get()
            preset = app.measurement_presets.get(preset_name, {})
            description = preset.get("description", "")
            app.preset_desc_label.config(text=description)
        
        app.preset_var.trace_add("write", update_preset_description)
        update_preset_description()  # Set initial description
        
        ttk.Separator(settings_grid, orient="horizontal").grid(row=2, column=0, columnspan=2, sticky="ew", pady=5)
        
        # Target Peak Count - Larger font
        ttk.Label(settings_grid, text="Target Peak Count (mid):", font=Fonts.BODY_SMALL).grid(
            row=3, column=0, sticky="w", pady=3)
        try:
            default_mid = app.TARGET_MID if hasattr(app, "TARGET_MID") else 62500
        except Exception:
            default_mid = 62500
        app.target_peak_var = tk.IntVar(value=default_mid)
        app.target_peak_entry = ttk.Entry(settings_grid, width=12, textvariable=app.target_peak_var, 
                                         font=Fonts.BODY_SMALL)
        app.target_peak_entry.grid(row=3, column=1, sticky="w", pady=3, padx=(6, 0))

        # Target window display - Larger font
        try:
            app.target_band_label = ttk.Label(settings_grid, 
                text=f"Target window: {getattr(app, 'TARGET_LOW', default_mid-2500)}–{getattr(app, 'TARGET_HIGH', default_mid+2500)}",
                font=Fonts.CAPTION, foreground="gray")
            app.target_band_label.grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 5))
        except Exception:
            pass

        # Update target window callback
        def _on_target_peak_change(*args):
            """Callback when target peak value changes."""
            try:
                if hasattr(app, 'update_target_peak'):
                    app.update_target_peak(app.target_peak_var.get())
            except Exception as e:
                LOGGER.warning("Error updating target peak: %s", e)
        
        app.target_peak_var.trace_add("write", _on_target_peak_change)

        # Auto-IT start - Larger font
        ttk.Label(settings_grid, text="Auto-IT start (ms):", font=Fonts.BODY_SMALL).grid(
            row=5, column=0, sticky="w", pady=3)
        app.auto_it_entry = ttk.Entry(settings_grid, width=12, font=Fonts.BODY_SMALL)
        app.auto_it_entry.insert(0, "")
        app.auto_it_entry.grid(row=5, column=1, sticky="w", pady=3, padx=(6, 0))
        
        ttk.Label(settings_grid, text="(Leave blank for defaults)", 
                 font=Fonts.CAPTION, foreground="gray").grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(0, 5))

        # === MIDDLE CENTER: Reference CSV Files ===
        reference_frame = ttk.LabelFrame(controls_frame, text="Reference CSV Files", padding=(10, 6, 10, 6))
        reference_frame.grid(row=0, column=2, sticky="nsew", padx=6)
        
        # Header with file count
        ref_header_frame = ttk.Frame(reference_frame)
        ref_header_frame.pack(fill="x", pady=(0, 4))
        app.reference_csv_count_label = ttk.Label(ref_header_frame, text="(0 file(s) selected)", 
                                                  foreground="gray", font=Fonts.BODY_SMALL)
        app.reference_csv_count_label.pack(side="left")

        # Listbox with scrollbar - Larger
        listbox_frame = ttk.Frame(reference_frame)
        listbox_frame.pack(fill="both", expand=True, pady=(0, 6))
        
        scrollbar = ttk.Scrollbar(listbox_frame, orient="vertical")
        app.reference_csv_listbox = tk.Listbox(listbox_frame, height=3, 
                                               yscrollcommand=scrollbar.set,
                                               selectmode=tk.MULTIPLE,
                                               font=Fonts.BODY_SMALL)
        scrollbar.config(command=app.reference_csv_listbox.yview)
        app.reference_csv_listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        app.reference_csv_paths = []
        
        # Buttons - Larger and more visible
        ref_buttons_frame = ttk.Frame(reference_frame)
        ref_buttons_frame.pack(fill="x")
        
        ttk.Button(ref_buttons_frame, text="+ Add", width=8,
                  command=lambda: _add_reference_csv(app)).pack(side="top", fill="x", pady=(0, 3))
        ttk.Button(ref_buttons_frame, text="- Remove", width=8,
                  command=lambda: _remove_selected_reference_csv(app)).pack(side="top", fill="x", pady=(0, 3))
        ttk.Button(ref_buttons_frame, text="Clear All", width=8,
                  command=lambda: _clear_all_reference_csvs(app)).pack(side="top", fill="x")

        # === RIGHT: Action Buttons ===
        actions_frame = ttk.LabelFrame(controls_frame, text="Actions", padding=(12, 8, 12, 8))
        actions_frame.grid(row=0, column=3, sticky="nsew", padx=(6, 0))
        actions_buttons_frame = ttk.Frame(actions_frame)
        actions_buttons_frame.pack(fill="x")

        button_style = {"width": 15}  # Wider buttons

        app.run_all_btn = make_run_button(actions_buttons_frame, text="Run Selected",
                                          command=app.run_all_selected, width=15)

        app.stop_all_btn = ttk.Button(actions_buttons_frame, text="Stop",
                                    command=app.stop_measure, **button_style)

        app.save_csv_btn = ttk.Button(actions_buttons_frame, text="Save CSV",
                                    command=app.save_csv, **button_style)

        app.start_analysis_btn = ttk.Button(actions_buttons_frame, text="Analysis",
                                          command=app.refresh_analysis_view, **button_style)

        action_buttons = [
            app.run_all_btn,
            app.stop_all_btn,
            app.save_csv_btn,
            app.start_analysis_btn,
        ]
        for col in range(4):
            actions_buttons_frame.columnconfigure(col, weight=1)

        def _layout_quick_controls(panel_width):
            if panel_width < 520:
                select_all_chk.grid_configure(row=0, column=0, columnspan=3, pady=(0, 4))
                quick_label.grid_configure(row=1, column=0, padx=(0, 4))
                quick_scan_btn.grid_configure(row=1, column=1, padx=2)
                full_spectrum_btn.grid_configure(row=1, column=2, padx=2)
            else:
                select_all_chk.grid_configure(row=0, column=0, columnspan=1, pady=0)
                quick_label.grid_configure(row=0, column=1, padx=(8, 4))
                quick_scan_btn.grid_configure(row=0, column=2, padx=2)
                full_spectrum_btn.grid_configure(row=0, column=3, padx=2)

        def _layout_laser_checkbuttons(panel_width):
            columns = 4 if panel_width >= 700 else 3 if panel_width >= 520 else 2
            for col in range(4):
                laser_frame.columnconfigure(col, weight=0)
            for col in range(columns):
                laser_frame.columnconfigure(col, weight=1)

            select_all_frame.grid_configure(columnspan=columns)
            laser_separator.grid_configure(columnspan=columns)

            for idx, chk in enumerate(measure_checkbuttons):
                chk.grid_configure(row=(idx // columns) + 2, column=idx % columns)

        def _layout_measure_controls(width=None, _height=None):
            available_width = width or controls_host.winfo_width()
            main_height = main_frame.winfo_height() or controls_host.winfo_height()
            if not available_width:
                return

            controls_height = max(220, min(430, int(main_height * (0.5 if available_width < 1100 else 0.4))))
            controls_host.configure(height=controls_height)

            for col in range(4):
                controls_frame.columnconfigure(col, weight=0)
            for row in range(4):
                controls_frame.rowconfigure(row, weight=0)

            if available_width >= 1450:
                layouts = (
                    (laser_frame, 0, 0, {"rowspan": 2, "padx": (0, 6)}),
                    (settings_frame, 0, 1, {"padx": 6}),
                    (reference_frame, 1, 1, {"padx": 6}),
                    (actions_frame, 0, 2, {"rowspan": 2, "padx": (6, 0)}),
                )
                for col in range(3):
                    controls_frame.columnconfigure(col, weight=1)
                laser_panel_width = max(420, int(available_width * 0.44))
            elif available_width >= 1100:
                layouts = (
                    (laser_frame, 0, 0, {"columnspan": 2, "padx": 0}),
                    (settings_frame, 1, 0, {"padx": (0, 6)}),
                    (reference_frame, 1, 1, {"padx": (6, 0)}),
                    (actions_frame, 2, 0, {"columnspan": 2, "padx": 0}),
                )
                for col in range(2):
                    controls_frame.columnconfigure(col, weight=1)
                laser_panel_width = available_width
            else:
                layouts = (
                    (laser_frame, 0, 0, {"padx": 0}),
                    (settings_frame, 1, 0, {"padx": 0}),
                    (reference_frame, 2, 0, {"padx": 0}),
                    (actions_frame, 3, 0, {"padx": 0}),
                )
                controls_frame.columnconfigure(0, weight=1)
                laser_panel_width = available_width

            for widget, row, column, options in layouts:
                widget.grid_configure(
                    row=row,
                    column=column,
                    sticky="nsew",
                    padx=options.get("padx", 0),
                    pady=6,
                    rowspan=options.get("rowspan", 1),
                    columnspan=options.get("columnspan", 1),
                )

            if available_width >= 1100:
                for idx, button in enumerate(action_buttons):
                    button.grid_configure(row=0, column=idx, sticky="ew", padx=(0 if idx == 0 else 6, 0), pady=0)
            else:
                for idx, button in enumerate(action_buttons):
                    button.grid_configure(row=idx, column=0, sticky="ew", padx=0, pady=(0, 6 if idx < len(action_buttons) - 1 else 0))

            _layout_quick_controls(laser_panel_width)
            _layout_laser_checkbuttons(laser_panel_width)

        bind_debounced_configure(controls_host, _layout_measure_controls)



    def run_all_selected():
        if not app.spec:
            messagebox.showwarning("Spectrometer", "Not connected.")
            return
        if app.measure_running.is_set():
            return
        tags = [t for t, v in app.measure_vars.items() if v.get()]
        if not tags:
            messagebox.showwarning("Run", "No lasers selected.")
            return
        start_it_override = None
        try:
            txt = app.auto_it_entry.get().strip()
            if txt:
                start_it_override = float(txt)
        except (ValueError, AttributeError):
            start_it_override = None

        app._update_ports_from_ui()
        power_snapshot = {tag: float(app._get_power(tag)) for tag in app.measure_vars.keys()}

        # Clear previous data
        app.data.clear()
        
        # Clear analysis window when new measurement starts
        app.after(0, lambda: app._clear_analysis_window())

        # Change button to light red (running state)
        app.run_all_btn.configure(bg=Colors.WARNING, activebackground=Colors.WARNING)

        app.measure_running.set()
        app.measure_thread = threading.Thread(
            target=run_measurement_with_analysis,
            args=(tags, start_it_override, power_snapshot),
            daemon=True,
        )
        app.measure_thread.start()

    def _show_countdown_modal_threadsafe(seconds: int, title: str, message: str):
        done = threading.Event()
        state = {"error": None}

        def _show():
            try:
                app._countdown_modal(seconds, title, message)
            except Exception as exc:  # pragma: no cover - UI error path
                state["error"] = exc
            finally:
                done.set()

        app.after(0, _show)
        done.wait()
        if state["error"] is not None:
            raise state["error"]

    def _build_measurement_orchestrator(power_snapshot: Dict[str, float]):
        config = MeasurementOrchestratorConfig(
            default_start_it=app.DEFAULT_START_IT,
            target_low=app.TARGET_LOW,
            target_high=app.TARGET_HIGH,
            target_mid=app.TARGET_MID,
            it_min=app.IT_MIN,
            it_max=app.IT_MAX,
            it_step_up=app.IT_STEP_UP,
            it_step_down=app.IT_STEP_DOWN,
            max_it_adjust_iters=app.MAX_IT_ADJUST_ITERS,
            sat_thresh=app.SAT_THRESH,
            n_sig=app.N_SIG,
            n_dark=app.N_DARK,
            n_sig_640=app.N_SIG_640,
            n_dark_640=app.N_DARK_640,
        )
        callbacks = MeasurementOrchestratorCallbacks(
            prepare_devices=lambda: None,
            power_lookup=lambda tag: power_snapshot.get(tag, 0.0),
            auto_it_update=lambda tag, spectrum, it_ms, peak: app._update_auto_it_plot(tag, spectrum, it_ms, peak),
            measurement_completed=lambda tag: app._update_last_plots(tag),
            countdown=_show_countdown_modal_threadsafe,
            error_handler=app._post_error,
        )
        return MeasurementOrchestrator(
            spectrometer=app.spec,
            laser_controller=app.lasers,
            measurement_data=app.data,
            config=config,
            callbacks=callbacks,
            it_history=app.it_history,
        )

    def run_measurement_with_analysis(tags, start_it_override, power_snapshot):
        """Run measurement sequence and automatically generate analysis."""
        try:
            orchestrator = _build_measurement_orchestrator(power_snapshot)
            run_result = orchestrator.run(
                tags,
                start_it_override,
                should_continue=app.measure_running.is_set,
            )

            # Save data to CSV
            csv_path = app.save_measurement_data()
            if csv_path:
                # Generate analysis plots
                plot_paths = app.run_analysis_and_save_plots(csv_path)
                app.after(0, app.refresh_analysis_view)

                # Show completion message
                status_label = "Measurement and analysis complete!"
                if run_result.stopped_early:
                    status_label = "Measurement stopped early; saved partial results."
                if run_result.errors:
                    status_label += f"\n\nCompleted with {len(run_result.errors)} warning(s)."
                app.after(0, lambda: messagebox.showinfo(
                    "Measurement Complete",
                    f"{status_label}\n\n"
                    f"Data saved to: {os.path.basename(csv_path)}\n"
                    f"Generated {len(plot_paths)} analysis plots.\n\n"
                    f"Check the Analysis tab for results."
                ))
            elif run_result.stopped_early:
                app.after(0, lambda: messagebox.showinfo("Measurement", "Measurement stopped before any data was saved."))
        except Exception as e:
            app._post_error("Measurement", e)
        finally:
            app.measure_running.clear()
            # Change button back to green (idle state)
            app.after(0, lambda: app.run_all_btn.configure(bg=Colors.SUCCESS, activebackground='#15803d'))

    def stop_measure():
        app.measure_running.clear()
        # Change button back to green (idle state)
        app.run_all_btn.configure(bg=Colors.SUCCESS, activebackground='#15803d')

    def _countdown_modal(self, seconds: int, title: str, message: str):
        """Blocking modal with countdown; Enter key to skip."""
        top = tk.Toplevel(self)
        top.title(title)
        top.geometry("500x180+200+200")
        ttk.Label(top, text=message, wraplength=460).pack(pady=8)
        lbl = ttk.Label(top, text="", font=Fonts.H1)
        lbl.pack(pady=10)

        skip = {"flag": False}
        def on_key(ev):
            skip["flag"] = True
        top.bind("<Return>", on_key)

        for s in range(seconds, -1, -1):
            if skip["flag"]:
                break
            lbl.config(text=f"{s} sec")
            top.update()
            time.sleep(0.2)
        top.destroy()

    def _update_last_plots(self, tag: str):
        sig, dark = app.data.last_vectors_for(tag)
        app._pending_measurement_plot = (sig, dark)
        if getattr(app, "_measurement_plot_redraw_id", None) is not None:
            return

        CLAMP = 65000  # counts ceiling for display

        def update():
            app._measurement_plot_redraw_id = None
            pending = getattr(app, "_pending_measurement_plot", None)
            if pending is None:
                return
            sig_pending, dark_pending = pending
            # main overlay
            xmax = 10
            any_sat = False

            if sig_pending is not None:
                xs = np.arange(len(sig_pending))
                sig_sat = np.any(sig_pending > CLAMP)
                sig_display = np.clip(sig_pending, None, CLAMP)
                app.meas_sig_line.set_data(xs, sig_display)
                xmax = max(xmax, len(sig_pending)-1)
                if sig_sat:
                    any_sat = True
                    y_sat = np.where(sig_pending > CLAMP, CLAMP, np.nan)
                    app.meas_sig_sat_line.set_data(xs, y_sat)
                    app.meas_sig_sat_line.set_visible(True)
                else:
                    app.meas_sig_sat_line.set_visible(False)
            else:
                app.meas_sig_sat_line.set_visible(False)

            if dark_pending is not None:
                xd = np.arange(len(dark_pending))
                dark_sat = np.any(dark_pending > CLAMP)
                dark_display = np.clip(dark_pending, None, CLAMP)
                app.meas_dark_line.set_data(xd, dark_display)
                xmax = max(xmax, len(dark_pending)-1)
                if dark_sat:
                    any_sat = True
                    y_sat = np.where(dark_pending > CLAMP, CLAMP, np.nan)
                    app.meas_dark_sat_line.set_data(xd, y_sat)
                    app.meas_dark_sat_line.set_visible(True)
                else:
                    app.meas_dark_sat_line.set_visible(False)
            else:
                app.meas_dark_sat_line.set_visible(False)

            app.meas_ax.set_xlim(0, xmax)
            app.meas_ax.set_ylim(0, 65000)

            # Visual saturation indicator in title
            title = "Live Measurement"
            if any_sat:
                title += "  [SATURATED]"
            app.meas_ax.set_title(title, fontsize=13, fontweight="bold",
                                  pad=8, color="red" if any_sat else "black")

            # inset: Auto-IT step history (peaks & IT) — commented out
            # if app.meas_inset is not None:
            #     steps = list(getattr(app, "it_history", []))
            #     if steps:
            #         st = np.arange(len(steps))
            #         peaks = [p for (_, p) in steps]
            #         its   = [it for (it, _) in steps]
            #         app.meas_inset.set_xlim(-0.5, len(st)-0.5 if len(st) else 0.5)
            #         app.inset_peak_line.set_data(st, peaks)
            #         app.inset_it_line.set_data(st, its)
            #         app.meas_inset.relim();  app.meas_inset.autoscale_view()
            #         app.meas_inset2.relim(); app.meas_inset2.autoscale_view()
            #     else:
            #         app.inset_peak_line.set_data([], [])
            #         app.inset_it_line.set_data([], [])
            #         app.meas_inset.relim();  app.meas_inset.autoscale_view()
            #         app.meas_inset2.relim(); app.meas_inset2.autoscale_view()

            if getattr(app, "_is_tab_visible", lambda _w: True)(app.measure_tab):
                app.meas_canvas.draw_idle()

        app._measurement_plot_redraw_id = app.after(16, update)


    def save_csv():
        if not app.data.rows:
            messagebox.showwarning("Save CSV", "No data collected yet.")
            return
        df = app.data.to_dataframe()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        default = f"Measurements_{app.data.serial_number}_{ts}.csv"
        path = filedialog.asksaveasfilename(
            title="Save CSV", defaultextension=".csv",
            initialfile=default, filetypes=[("CSV", "*.csv"), ("All", "*.*")])
        if not path:
            return
        try:
            df.to_csv(path, index=False)
            messagebox.showinfo("Save CSV", f"Saved: {path}")
        except Exception as e:
            messagebox.showerror("Save CSV", str(e))


    # Bind functions to app object
    app.run_all_selected = run_all_selected
    app.stop_measure = stop_measure
    app.save_csv = save_csv
    app._countdown_modal = MethodType(_countdown_modal, app)
    app._update_last_plots = MethodType(_update_last_plots, app)

    # Call the UI builder
    _build_measure_tab()
