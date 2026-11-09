"""Measurements tab - automated laser measurement sequences with Auto-IT."""
import logging
import os
import threading
from datetime import datetime
from types import MethodType
from typing import Dict

import numpy as np
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from services.measurement_orchestrator import (
    MeasurementOrchestrator,
    MeasurementOrchestratorCallbacks,
    MeasurementOrchestratorConfig,
)
from services import light_sources
from services.run_progress import source_label
from .run_progress import RunProgressPanel
from .ui_utils import ScrollableFrame, ThrottledRedraw, Tooltip, bind_debounced_configure, center_over

from .theme import (Colors, Fonts, Spacing, bind_figure_margins, configure_matplotlib_style,
                    control_strip_height, fit_strip_to_content, logical_px, make_run_button,
                    plot_title_font, relax_canvas_size, source_style, style_legend, style_listbox,
                    empty_state_text)

LOGGER = logging.getLogger(__name__)

def build(app):
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
            app.reference_csv_count_label.config(text=f"{len(app.reference_csv_paths)} files selected")
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
            app.reference_csv_count_label.config(text=f"{len(app.reference_csv_paths)} files selected")
            LOGGER.info("Reference CSV(s) removed. %d remaining.", len(app.reference_csv_paths))
        except Exception as e:
            messagebox.showerror("Remove Error", f"Error removing file:\n{e}")

    def _clear_all_reference_csvs(app):
        """Clear all reference CSVs."""
        app.reference_csv_listbox.delete(0, tk.END)
        app.reference_csv_paths.clear()
        app.reference_csv_count_label.config(text="0 files selected")
        LOGGER.info("All reference CSVs cleared.")

    def _build_measure_tab():
        # Create main container with minimal padding for maximum space
        main_frame = ttk.Frame(app.measure_tab)
        main_frame.pack(fill="both", expand=True, padx=Spacing.PAD_SM, pady=Spacing.PAD_SM)

        # Top section - Live measurement plot (maximize space). Packed *after*
        # the control panel below, so that panel keeps its slice of the window:
        # pack hands the whole remaining cavity to the first expanding slave.
        plot_frame = ttk.Frame(main_frame)
        plot_frame.pack_propagate(False)

        def _ensure_measure_figure():
            """Create the matplotlib figure the first time the tab is shown.

            The Measurements tab itself is built eagerly (its controls are needed by
            the Run flow), but a 14x7 in figure + canvas + first draw is ~0.3 s of
            startup that nobody sees until this tab is opened.
            """
            if getattr(app, "meas_canvas", None) is not None:
                return
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            from matplotlib.figure import Figure

            from .theme import screen_dpi

            npix = int(getattr(app, "npix", 2048) or 2048)
            app.measure_fig = Figure(figsize=(14, 7), dpi=screen_dpi(app))
            app.measure_fig.subplots_adjust(left=0.06, right=0.985, top=0.92, bottom=0.09)
            app.measure_ax = app.measure_fig.add_subplot(111)
            configure_matplotlib_style(app.measure_fig, app.measure_ax, title="Live Measurement")
            app.measure_ax.set_xticks(np.arange(0, npix, 100))
            app.measure_ax.set_xlim(0, max(10, npix - 1))
            app.measure_ax.set_ylim(0, 65000)

            # Initialize empty plot lines - signal (blue) and dark (black dashed)
            app.measure_line, = app.measure_ax.plot([], [], lw=1.5, color=Colors.ACCENT, label='Signal (Laser ON)')
            app.measure_dark_line, = app.measure_ax.plot([], [], lw=1.5, color=Colors.TEXT_PRIMARY, linestyle='--', label='Dark (Laser OFF)')
            # Saturation overlay lines – hidden until data exceeds clamp
            app.meas_sig_sat_line, = app.measure_ax.plot([], [], lw=2, color=Colors.DANGER, label='Saturated Signal')
            app.meas_dark_sat_line, = app.measure_ax.plot([], [], lw=2, color=Colors.DANGER, linestyle='--', label='Saturated Dark')
            app.meas_sig_sat_line.set_visible(False)
            app.meas_dark_sat_line.set_visible(False)
            style_legend(app.measure_ax, loc='upper right')
            app.meas_empty_text = empty_state_text(app.measure_ax, "Select sources and press Run Selected")

            canvas = FigureCanvasTkAgg(app.measure_fig, plot_frame)
            relax_canvas_size(canvas, 640, 340, dpi=screen_dpi(app))
            bind_figure_margins(canvas, app.measure_fig)
            canvas.get_tk_widget().pack(fill="both", expand=True)
            app.meas_canvas = canvas
            app.meas_ax = app.measure_ax
            app.meas_sig_line = app.measure_line
            app.meas_dark_line = app.measure_dark_line
            app.meas_inset = None
            app.meas_inset2 = None
            app.inset_peak_line = None
            app.inset_it_line = None
            canvas.draw_idle()

        app.meas_canvas = None
        app._ensure_measure_figure = _ensure_measure_figure
        hooks = getattr(app, "_tab_show_hooks", None)
        if hooks is None:
            hooks = app._tab_show_hooks = {}
        hooks[str(app.measure_tab)] = _ensure_measure_figure

        # Bottom section - responsive, scrollable controls
        controls_host = ttk.Frame(main_frame, relief="flat", height=300)
        controls_host.pack(side="bottom", fill="x", pady=(0, 0))
        controls_host.pack_propagate(False)
        plot_frame.pack(side="top", fill="both", expand=True, pady=(0, Spacing.PAD_SM))

        # The control strip is capped at a laptop-screen width and centred
        # under the plot, so its cards keep their proportions on a 4K screen.
        controls_scroll = ScrollableFrame(controls_host, y_scroll=True, background=Colors.BG_PRIMARY,
                                          max_content_width=logical_px(app, 1720))
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
            text="Select all",
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
            width=12,
            command=lambda: app.preset_var.set("Quick Scan") if hasattr(app, 'preset_var') else None,
        )
        quick_scan_btn.grid(row=0, column=2, sticky="w", padx=2)
        full_spectrum_btn = ttk.Button(
            select_all_frame,
            text="Full Spectrum",
            width=14,
            command=lambda: app.preset_var.set("Full Spectrum") if hasattr(app, 'preset_var') else None,
        )
        full_spectrum_btn.grid(row=0, column=3, sticky="w", padx=2)

        # Add separator
        laser_separator = ttk.Separator(laser_frame, orient="horizontal")
        laser_separator.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(0, 4))

        app.measure_vars = {}
        measure_checkbuttons = []
        layout_state = {"panel_width": None}

        preset_state = {"applying": False}

        def _preset_tags(preset):
            """The tags a preset selects; None in the preset means every configured source."""
            tags = preset.get("lasers")
            return list(app.measure_vars) if tags is None else list(tags)

        def on_laser_change(*args):
            """When user manually changes lasers, switch to Custom preset."""
            if preset_state["applying"]:
                return
            if hasattr(app, 'preset_var') and app.preset_var.get() != "Custom":
                # Check if current selection matches the active preset
                preset = app.measurement_presets.get(app.preset_var.get(), {})
                selected_lasers = _preset_tags(preset)

                # Get currently selected lasers
                current_selection = [tag for tag, var in app.measure_vars.items() if var.get()]

                # If they don't match, switch to Custom
                if set(current_selection) != set(selected_lasers):
                    app.preset_var.set("Custom")

        def _build_source_checks():
            """One checkbox per configured source, in wavelength order.

            Rebuilt when Setup adds or removes a source (and after the settings
            file is read at startup): a source removed from the bench must not
            stay ticked here and drive an empty channel. Ticks survive for the
            sources that remain; a new source starts ticked like the others.
            """
            previous = {tag: var.get() for tag, var in app.measure_vars.items()}
            for chk in measure_checkbuttons:
                chk.destroy()
            measure_checkbuttons.clear()
            app.measure_vars.clear()
            for i, tag in enumerate(app.laser_tags()):
                v = tk.BooleanVar(master=app, value=previous.get(tag, True))
                chk = ttk.Checkbutton(laser_frame, text=source_label(tag), variable=v,
                                      style=source_style(laser_frame, tag, 'Large.TCheckbutton'))
                chk.grid(row=(i // 4) + 2, column=i % 4, padx=8, pady=3, sticky="w")
                app.measure_vars[tag] = v
                measure_checkbuttons.append(chk)
                v.trace_add("write", on_laser_change)
            if layout_state["panel_width"]:
                _layout_laser_checkbuttons(layout_state["panel_width"])
            refresh_summary = getattr(app, "_refresh_auto_it_summary", None)
            if callable(refresh_summary):
                refresh_summary()

        _build_source_checks()
        app._laser_ui_rebuilders.append(_build_source_checks)


        # === MIDDLE LEFT: Settings ===
        settings_frame = ttk.LabelFrame(controls_frame, text="Settings", padding=(10, 6, 10, 6))
        settings_frame.grid(row=0, column=1, sticky="nsew", padx=6)

        # Grid layout for settings
        settings_grid = ttk.Frame(settings_frame)
        settings_grid.pack(fill="both", expand=True)
        # One control per row, label above it: a side-by-side label plus field
        # asked for more width than this column is ever given, which clipped
        # the preset dropdown and the target-count field.
        settings_grid.columnconfigure(0, weight=1)

        # ===== MEASUREMENT PRESETS =====
        ttk.Label(settings_grid, text="Preset", font=Fonts.BODY_SMALL_BOLD).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 2))

        # Define presets
        app.measurement_presets = {
            "Quick Scan": {
                "lasers": ["405", "445"],
                "reference_files": [],
                "description": "Fast scan with 405nm and 445nm lasers"
            },
            "Full Spectrum": {
                "lasers": None,            # every source configured in Setup
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
                                    width=12, font=Fonts.BODY_SMALL, state="readonly")
        preset_combo.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 2))

        def apply_preset(*args):
            """Apply the selected measurement preset."""
            preset_name = app.preset_var.get()
            if preset_name == "Custom":
                return  # Don't change anything for custom

            preset = app.measurement_presets.get(preset_name, {})
            selected_lasers = _preset_tags(preset)

            # Update laser checkboxes without letting on_laser_change see a
            # half-applied selection (which would snap the preset back to Custom).
            preset_state["applying"] = True
            try:
                for laser, var in app.measure_vars.items():
                    var.set(laser in selected_lasers)

                # Update select all checkbox state
                all_selected = all(var.get() for var in app.measure_vars.values())
                none_selected = not any(var.get() for var in app.measure_vars.values())
                if all_selected:
                    app.select_all_lasers_var.set(True)
                elif none_selected:
                    app.select_all_lasers_var.set(False)
            finally:
                preset_state["applying"] = False

            # Show info
            description = preset.get("description", "")
            if description:
                # Update a status label if we have one, or just print
                LOGGER.info("Applied preset: %s - %s", preset_name, description)

        app.preset_var.trace_add("write", apply_preset)

        # Preset description label
        app.preset_desc_label = ttk.Label(settings_grid, text="",
                                         font=Fonts.CAPTION, foreground=Colors.TEXT_MUTED)
        app.preset_desc_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 4))

        def update_preset_description(*args):
            """Update the description label when preset changes."""
            preset_name = app.preset_var.get()
            preset = app.measurement_presets.get(preset_name, {})
            description = preset.get("description", "")
            app.preset_desc_label.config(text=description)

        app.preset_var.trace_add("write", update_preset_description)
        update_preset_description()  # Set initial description

        ttk.Separator(settings_grid, orient="horizontal").grid(row=3, column=0, columnspan=2, sticky="ew", pady=6)

        # Target Peak Count - Larger font
        ttk.Label(settings_grid, text="Target peak (mid)", font=Fonts.BODY_SMALL).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(0, 2))
        try:
            default_mid = app.TARGET_MID if hasattr(app, "TARGET_MID") else 62500
        except Exception:
            default_mid = 62500
        app.target_peak_var = tk.IntVar(value=default_mid)
        app.target_peak_entry = ttk.Entry(settings_grid, width=12, textvariable=app.target_peak_var,
                                         font=Fonts.BODY_SMALL)
        app.target_peak_entry.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(0, 2))

        # Target window display - Larger font
        try:
            app.target_band_label = ttk.Label(settings_grid,
                text=f"Target window: {getattr(app, 'TARGET_LOW', default_mid-2500)}–{getattr(app, 'TARGET_HIGH', default_mid+2500)}",
                font=Fonts.CAPTION, foreground=Colors.TEXT_MUTED)
            app.target_band_label.grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 4))
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

        # Auto-IT: where the exposure search starts and how far it may move per
        # frame, per source (a strong 405 nm line and a weak 445 nm line want
        # different values). Stored in the source table with the setpoints.
        ttk.Label(settings_grid, text="Auto-IT", font=Fonts.BODY_SMALL_BOLD).grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(0, 2))
        app.auto_it_button = ttk.Button(settings_grid, text="Start and step per source…",
                                        command=lambda: open_auto_it_dialog())
        app.auto_it_button.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(0, 2))
        app.auto_it_summary_label = ttk.Label(settings_grid, text="", font=Fonts.CAPTION,
                                              foreground=Colors.TEXT_MUTED, justify="left",
                                              wraplength=logical_px(app, 210))
        app.auto_it_summary_label.grid(row=9, column=0, columnspan=2, sticky="w", pady=(0, 2))

        def _default_start(tag):
            return float(app.DEFAULT_START_IT.get(tag, app.DEFAULT_START_IT["default"]))

        def _refresh_auto_it_summary():
            custom = [tag for tag in app.laser_tags()
                      if light_sources.has_auto_it_override(app.laser_configs.get(tag, {}))]
            if custom:
                text = "Custom start/step: " + ", ".join(source_label(tag) for tag in custom)
            else:
                text = (f"Defaults: start {app.DEFAULT_START_IT['default']:g} ms, "
                        f"step {app.IT_STEP_UP:g} ms up / {app.IT_STEP_DOWN:g} ms down")
            app.auto_it_summary_label.configure(text=text)

        app._refresh_auto_it_summary = _refresh_auto_it_summary

        def open_auto_it_dialog():
            """One row per source: start IT and the largest IT step per frame; blank = default."""
            from .theme import harmonize, source_color

            dialog = tk.Toplevel(app)
            dialog.title("Auto-IT per source")
            dialog.transient(app)
            dialog.resizable(False, False)
            dialog.grab_set()
            try:
                from app import get_resource_path
                dialog.iconbitmap(get_resource_path("sciglob_symbol.ico"))
            except Exception:
                pass
            body = ttk.Frame(dialog, padding=Spacing.PAD_XL, style="Surface.TFrame")
            body.pack(fill="both", expand=True)
            ttk.Label(body, text="Auto-IT per source", font=Fonts.H3).grid(
                row=0, column=0, columnspan=5, sticky="w", pady=(0, 4))
            ttk.Label(body, font=Fonts.BODY_SMALL, foreground=Colors.TEXT_SECONDARY, justify="left",
                      wraplength=logical_px(app, 460),
                      text="Start is the first frame's integration time; step is the most the exposure "
                           "may change per frame while it walks to the target band. Leave a field blank "
                           "for the default. 640 nm uses its fixed 100 / 500 / 1000 ms ladder.").grid(
                row=1, column=0, columnspan=5, sticky="w", pady=(0, 10))
            for column, heading in enumerate(("Source", "Start (ms)", "", "Step (ms)", "")):
                ttk.Label(body, text=heading, font=Fonts.BODY_SMALL_BOLD,
                          foreground=Colors.TEXT_SECONDARY).grid(row=2, column=column, sticky="w",
                                                                  padx=(0, 10), pady=(0, 4))
            app.auto_it_entries = {}
            row = 3
            for tag in app.laser_tags():
                config = app.laser_configs.get(tag, {})
                ttk.Label(body, text=source_label(tag), font=Fonts.BODY_BOLD,
                          foreground=source_color(tag)).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=2)
                if tag == "640":
                    ttk.Label(body, text="fixed 100 / 500 / 1000 ms ladder, no auto-IT",
                              font=Fonts.CAPTION, foreground=Colors.TEXT_MUTED).grid(
                        row=row, column=1, columnspan=4, sticky="w", pady=2)
                    row += 1
                    continue
                start_entry = ttk.Entry(body, width=8, font=Fonts.BODY, justify="right")
                start_value = light_sources.auto_it_value(config, "start_it_ms")
                if start_value is not None:
                    start_entry.insert(0, f"{start_value:g}")
                start_entry.grid(row=row, column=1, sticky="w", pady=2)
                ttk.Label(body, text=f"default {_default_start(tag):g}", font=Fonts.CAPTION,
                          foreground=Colors.TEXT_MUTED).grid(row=row, column=2, sticky="w", padx=(6, 16))
                step_entry = ttk.Entry(body, width=8, font=Fonts.BODY, justify="right")
                step_value = light_sources.auto_it_value(config, "it_step_ms")
                if step_value is not None:
                    step_entry.insert(0, f"{step_value:g}")
                step_entry.grid(row=row, column=3, sticky="w", pady=2)
                ttk.Label(body, text=f"default {app.IT_STEP_UP:g} up / {app.IT_STEP_DOWN:g} down",
                          font=Fonts.CAPTION, foreground=Colors.TEXT_MUTED).grid(row=row, column=4, sticky="w", padx=(6, 0))
                app.auto_it_entries[tag] = (start_entry, step_entry)
                row += 1

            problem = ttk.Label(body, text="", foreground=Colors.DANGER, font=Fonts.BODY_SMALL,
                                wraplength=logical_px(app, 460), justify="left")
            problem.grid(row=row, column=0, columnspan=5, sticky="w", pady=(8, 0))

            def on_save(*_event):
                problems = []
                values = {}
                for tag, (start_entry, step_entry) in app.auto_it_entries.items():
                    start_ms, step_ms, errors = light_sources.validate_auto_it(start_entry.get(), step_entry.get())
                    problems.extend(f"{source_label(tag)}: {error}" for error in errors)
                    values[tag] = (start_ms, step_ms)
                if problems:
                    problem.configure(text="\n".join(problems))
                    return
                for tag, (start_ms, step_ms) in values.items():
                    config = app.laser_configs.get(tag)
                    if config is None:
                        continue
                    for key, value in (("start_it_ms", start_ms), ("it_step_ms", step_ms)):
                        if value is None:
                            config.pop(key, None)
                        else:
                            config[key] = float(value)
                saver = getattr(app, "save_settings_silently", None)
                if callable(saver):
                    saver()
                _refresh_auto_it_summary()
                dialog.destroy()
                app.status.toast("Auto-IT settings saved")

            def on_reset():
                for start_entry, step_entry in app.auto_it_entries.values():
                    start_entry.delete(0, "end")
                    step_entry.delete(0, "end")
                problem.configure(text="")

            buttons = ttk.Frame(body, style="Surface.TFrame")
            buttons.grid(row=row + 1, column=0, columnspan=5, sticky="ew", pady=(16, 0))
            ttk.Button(buttons, text="Reset to defaults", command=on_reset, width=16).pack(side="left")
            ttk.Button(buttons, text="Cancel", command=dialog.destroy, width=10).pack(side="right")
            ttk.Button(buttons, text="Save", command=on_save, style="Accent.TButton", width=10).pack(
                side="right", padx=(0, 8))
            dialog.bind("<Return>", on_save)
            dialog.bind("<Escape>", lambda _event: dialog.destroy())
            dialog.configure(bg=Colors.BG_SURFACE)
            harmonize(dialog, Colors.BG_SURFACE)
            center_over(dialog, app, min_width=logical_px(app, 520))
            app.auto_it_dialog = dialog
            app.wait_window(dialog)
            app.auto_it_dialog = None

        app.open_auto_it_settings = open_auto_it_dialog
        _refresh_auto_it_summary()

        # === MIDDLE CENTER: Reference CSV Files ===
        reference_frame = ttk.LabelFrame(controls_frame, text="Reference CSV Files", padding=(10, 6, 10, 6))
        reference_frame.grid(row=0, column=2, sticky="nsew", padx=6)

        # Header with file count
        ref_header_frame = ttk.Frame(reference_frame)
        ref_header_frame.pack(fill="x", pady=(0, 4))
        app.reference_csv_count_label = ttk.Label(ref_header_frame, text="0 files selected",
                                                  foreground=Colors.TEXT_MUTED, font=Fonts.BODY_SMALL)
        app.reference_csv_count_label.pack(side="left")

        # Listbox with scrollbar - Larger
        listbox_frame = ttk.Frame(reference_frame)
        listbox_frame.pack(fill="both", expand=True, pady=(0, 6))

        scrollbar = ttk.Scrollbar(listbox_frame, orient="vertical")
        app.reference_csv_listbox = tk.Listbox(listbox_frame, height=3,
                                               yscrollcommand=scrollbar.set,
                                               selectmode=tk.MULTIPLE)
        style_listbox(app.reference_csv_listbox, mono=False)
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

        # No fixed widths: the buttons stretch with sticky="ew" below, and a
        # character width only inflates the minimum this column demands.
        button_style = {}

        app.run_all_btn = make_run_button(actions_buttons_frame, text="Run Selected",
                                          command=app.run_all_selected, width=12)

        app.stop_all_btn = ttk.Button(actions_buttons_frame, text="Stop",
                                    command=app.stop_measure, **button_style)
        app.stop_all_btn.state(["disabled"])

        app.save_csv_btn = ttk.Button(actions_buttons_frame, text="Save CSV",
                                    command=app.save_csv, **button_style)

        app.start_analysis_btn = ttk.Button(actions_buttons_frame, text="Analyse last run",
                                          command=app.refresh_analysis_view, **button_style)
        Tooltip(app.start_analysis_btn, "Analyse the last measurement (or re-open its plots) in the Analysis tab.")

        action_buttons = [
            app.run_all_btn,
            app.stop_all_btn,
            app.save_csv_btn,
            app.start_analysis_btn,
        ]
        for col in range(4):
            actions_buttons_frame.columnconfigure(col, weight=1)

        # The run in flight, one row per source: state, IT, peak, timing, failures.
        app.run_progress_panel = RunProgressPanel(actions_frame, app.run_progress, post=app.pump.post)
        app.run_progress_panel.pack(fill="both", expand=True, pady=(Spacing.PAD_SM, 0))

        def _layout_quick_controls(panel_width):
            if panel_width < logical_px(app, 520):
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
            # As many per row as fit at ~112 logical px each: one row of nine
            # on a wide screen, two or three rows on a laptop.
            layout_state["panel_width"] = panel_width
            count = len(measure_checkbuttons)
            fit = max(2, min(count, panel_width // logical_px(app, 112)))
            rows = -(-count // fit)          # ceil: how many rows that many per row needs
            columns = -(-count // rows)      # then spread the sources evenly over them
            for col in range(max(4, len(measure_checkbuttons))):
                laser_frame.columnconfigure(col, weight=0)
            for col in range(columns):
                laser_frame.columnconfigure(col, weight=1)

            select_all_frame.grid_configure(columnspan=columns)
            laser_separator.grid_configure(columnspan=columns)

            for idx, chk in enumerate(measure_checkbuttons):
                chk.grid_configure(row=(idx // columns) + 2, column=idx % columns)

        def _layout_measure_controls(width=None, height=None):
            # Bound to the tab's own frame, whose size arrives before its
            # children are arranged: the strip height is then known on the
            # first geometry pass and the plot canvas is sized (and drawn) once.
            host_width = width or main_frame.winfo_width()
            main_height = height or main_frame.winfo_height()
            if not host_width or host_width <= 1:
                return
            # The scroll frame caps and centres the strip; lay out for that width.
            available_width = min(host_width, logical_px(app, 1720))
            narrow = available_width < logical_px(app, 900)

            # The plot keeps a working height; the strip takes what is left,
            # scrolls on a short window, and shrinks to its cards on a tall one.
            controls_height = control_strip_height(app, main_height, share=0.5 if narrow else 0.38,
                                                   preferred=230, cap=360, floor=150, plot_min=300)
            controls_host.configure(height=controls_height)

            for col in range(4):
                controls_frame.columnconfigure(col, weight=0)
            for row in range(4):
                controls_frame.rowconfigure(row, weight=0)

            if available_width >= logical_px(app, 1180):
                # One row of four cards: stacking Settings over Reference used to
                # push Reference below the visible strip on a 1500 px window.
                layouts = (
                    (laser_frame, 0, 0, {"padx": (0, 6)}),
                    (settings_frame, 0, 1, {"padx": 6}),
                    (reference_frame, 0, 2, {"padx": 6}),
                    (actions_frame, 0, 3, {"padx": (6, 0)}),
                )
                # Actions holds the progress checklist, so it gets as much as Reference.
                for col, weight in enumerate((3, 1, 2, 2)):
                    controls_frame.columnconfigure(col, weight=weight)
                laser_panel_width = int(available_width * 3 / 8) - 2 * Spacing.GROUP_PAD
            elif not narrow:
                # Stacked, the strip scrolls: Actions (Run, Stop, progress) sit
                # right under the sources, not below the fold.
                layouts = (
                    (laser_frame, 0, 0, {"columnspan": 2, "padx": 0}),
                    (actions_frame, 1, 0, {"columnspan": 2, "padx": 0}),
                    (settings_frame, 2, 0, {"padx": (0, 6)}),
                    (reference_frame, 2, 1, {"padx": (6, 0)}),
                )
                for col in range(2):
                    controls_frame.columnconfigure(col, weight=1)
                laser_panel_width = available_width
            else:
                layouts = (
                    (laser_frame, 0, 0, {"padx": 0}),
                    (actions_frame, 1, 0, {"padx": 0}),
                    (settings_frame, 2, 0, {"padx": 0}),
                    (reference_frame, 3, 0, {"padx": 0}),
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

            if available_width >= logical_px(app, 1180):
                # 2x2 block: a single row of four buttons demanded ~580 px, which
                # is what squeezed the Settings and Reference cards.
                for col in range(4):
                    actions_buttons_frame.columnconfigure(col, weight=1 if col < 2 else 0)
                for idx, button in enumerate(action_buttons):
                    button.grid_configure(row=idx // 2, column=idx % 2, sticky="ew",
                                          padx=(0 if idx % 2 == 0 else 6, 0),
                                          pady=(0 if idx < 2 else 6, 0))
            elif not narrow:
                for col in range(4):
                    actions_buttons_frame.columnconfigure(col, weight=1)
                for idx, button in enumerate(action_buttons):
                    button.grid_configure(row=0, column=idx, sticky="ew", padx=(0 if idx == 0 else 6, 0), pady=0)
            else:
                for col in range(4):
                    actions_buttons_frame.columnconfigure(col, weight=1 if col == 0 else 0)
                for idx, button in enumerate(action_buttons):
                    button.grid_configure(row=idx, column=0, sticky="ew", padx=0, pady=(0, 6 if idx < len(action_buttons) - 1 else 0))

            _layout_quick_controls(laser_panel_width)
            _layout_laser_checkbuttons(laser_panel_width)
            fit_strip_to_content(controls_host, controls_scroll, controls_height)

        bind_debounced_configure(main_frame, _layout_measure_controls)



    def run_all_selected():
        if app.measure_running.is_set():
            return
        tags = [t for t, v in app.measure_vars.items() if v.get()]
        if not tags:
            messagebox.showwarning("Run", "No lasers selected.")
            return
        try:
            target = int(str(app.target_peak_entry.get()).strip())
            if not 1000 <= target <= 65535:
                raise ValueError
        except (ValueError, tk.TclError):
            # The field applies per keystroke, so a half-typed or empty target
            # (e.g. 6, from backspacing 62500) could otherwise drive the run.
            messagebox.showerror("Run", "Enter a target peak between 1000 and 65535 counts.")
            return
        app.update_target_peak(target)
        if not app.claim_spectrometer("Measurement"):
            return
        app._ensure_measure_figure()
        # Per-source start and step come with the source table (see the Auto-IT dialog).
        start_it_override = None

        app._update_ports_from_ui()
        # Setpoints (mW) and routing as they are now: the run keeps them even if Setup is edited meanwhile.
        power_snapshot = {tag: float(app.source_power_mw(tag)) for tag in app.measure_vars.keys()}

        # Clear previous data
        app.data.clear()

        # Clear analysis window when new measurement starts
        app._clear_analysis_window()

        # Change button to light red (running state)
        app.run_all_btn.configure(bg=Colors.WARNING, activebackground=Colors.WARNING)

        app.status.progress_begin("Measurement starting…", total=len(tags))
        app.run_progress.start(tags)
        app.stop_all_btn.state(["!disabled"])
        # Save and Analyse act on the run in progress, half done: not until it ends.
        app.save_csv_btn.state(["disabled"])
        app.start_analysis_btn.state(["disabled"])
        app.measure_running.set()
        app.measure_thread = threading.Thread(
            target=run_measurement_with_analysis,
            args=(tags, start_it_override, power_snapshot),
            daemon=True,
        )
        app.measure_thread.start()

    def _show_countdown_modal_threadsafe(seconds: int, title: str, message: str):
        """Block the worker thread while a non-blocking countdown runs on the Tk thread."""
        done = threading.Event()
        state = {"error": None, "top": None}

        def _show():
            try:
                state["top"] = app._countdown_modal(seconds, title, message, on_done=done.set)
            except Exception as exc:  # pragma: no cover - UI error path
                state["error"] = exc
                done.set()

        app.pump.post(_show)
        # Wake periodically so a Stop request ends the wait promptly.
        while not done.wait(0.25):
            if not app.measure_running.is_set():
                top = state["top"]
                if top is not None:
                    def _close_top(widget=top):
                        try:
                            widget.destroy()
                        except tk.TclError:
                            pass
                    app.pump.post(_close_top)
                break
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
            smart_source_off=bool(app.measurement_flags.get("smart_source_off", False)),
            cube_prearm=bool(app.measurement_flags.get("cube_prearm", False)),
            sources={tag: dict(cfg) for tag, cfg in app.laser_configs.items()},
        )
        progress = app.run_progress

        def _auto_it_update(tag, spectrum, it_ms, peak):
            progress.frame(tag, it_ms, peak)
            app._update_auto_it_plot(tag, spectrum, it_ms, peak)

        def _measurement_completed(tag):
            progress.source_completed(tag)
            app._update_last_plots(tag)

        def _source_started(tag, index, total):
            progress.source_started(tag)
            app.pump.post(app.status.progress_update, index - 1,
                          f"Measuring {source_label(tag)}…", f"{index}/{total}")

        callbacks = MeasurementOrchestratorCallbacks(
            prepare_devices=lambda: None,
            power_lookup=lambda tag: power_snapshot.get(tag, 0.0),
            auto_it_update=_auto_it_update,
            measurement_completed=_measurement_completed,
            countdown=_show_countdown_modal_threadsafe,
            error_handler=app._post_error,
            source_started=_source_started,
            run_started=progress.set_sequence,
            source_failed=progress.source_failed,
            source_state_changed=app.note_source_state,
            all_sources_off=app.note_all_sources_off,
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
        end_text, end_kind = "Measurement finished", "success"
        run_result = None
        try:
            orchestrator = _build_measurement_orchestrator(power_snapshot)
            run_result = orchestrator.run(
                tags,
                start_it_override,
                should_continue=app.measure_running.is_set,
            )
            if run_result.stopped_early:
                end_text, end_kind = "Measurement stopped", "warning"
            elif run_result.errors:
                end_text, end_kind = f"Measurement finished with {len(run_result.errors)} warning(s)", "warning"

            # Save data to CSV
            csv_path = app.save_measurement_data()
            if csv_path:
                # Generate analysis plots
                app.pump.post(app.status.progress_update, len(tags), "Analysing results…", "")
                plot_paths = app.run_analysis_and_save_plots(csv_path)
                app.pump.post(app.refresh_analysis_view)
                # The outcome goes to the status bar and the run chip, and the
                # Analysis tab opens on the plots: a dialog on top of that only
                # had to be dismissed (and sat there when the operator was away).
                end_text = (f"{end_text}: {os.path.basename(csv_path)} saved, "
                            f"{len(plot_paths)} plot{'s' if len(plot_paths) != 1 else ''} in Analysis")
            elif run_result.stopped_early:
                end_text = "Measurement stopped before any data was saved"
        except Exception as e:
            end_text, end_kind = "Measurement failed", "error"
            app._post_error("Measurement", e)
        finally:
            app.run_progress.finish(stopped_early=run_result is None or run_result.stopped_early)
            app.measure_running.clear()
            app.release_spectrometer("Measurement")
            app.pump.post(app.status.progress_end, end_text, end_kind)
            # Change button back to green (idle state)
            app.pump.post(app.run_all_btn.configure, {"bg": Colors.SUCCESS, "activebackground": "#15803d"})
            app.pump.post(app.stop_all_btn.state, ["disabled"])
            app.pump.post(app.save_csv_btn.state, ["!disabled"])
            app.pump.post(app.start_analysis_btn.state, ["!disabled"])

    def stop_measure():
        was_running = app.measure_running.is_set()
        app.measure_running.clear()
        if was_running:   # never abort another owner's capture
            app.request_spectrometer_stop()
        app.stop_all_btn.state(["disabled"])
        # Change button back to green (idle state)
        app.run_all_btn.configure(bg=Colors.SUCCESS, activebackground='#15803d')

    def _countdown_modal(self, seconds: int, title: str, message: str, on_done=None):
        """Non-blocking countdown dialog driven by ``after``; Enter/Escape skips.

        Runs for the full ``seconds`` (the previous implementation used a
        ``time.sleep(0.2)`` loop with a nested ``update()`` and lasted only
        ~0.2 s per second requested). Calls ``on_done`` exactly once when the
        countdown ends, is skipped, or the dialog is closed. Returns the
        Toplevel so a caller can destroy it early.
        """
        from .theme import harmonize

        top = tk.Toplevel(self)
        top.title(title)
        top.transient(self)
        top.resizable(False, False)
        top.configure(bg=Colors.BG_SURFACE)
        tk.Frame(top, bg=Colors.ACCENT, height=3, bd=0,
                 highlightthickness=0).pack(fill="x", side="top")
        body = ttk.Frame(top, style="Surface.TFrame")
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=message, wraplength=460, justify="center",
                  anchor="center").pack(fill="x", padx=20, pady=(16, 6))
        lbl = ttk.Label(body, text=f"{int(seconds)} sec", font=Fonts.H0,
                        foreground=Colors.ACCENT_DEEP, anchor="center")
        lbl.pack(fill="x", pady=(4, 6))
        ttk.Label(body, text="Press Enter to continue now", style="Muted.TLabel",
                  anchor="center").pack(fill="x", pady=(0, 14))
        harmonize(top, Colors.BG_SURFACE)

        center_over(top, self, min_width=logical_px(self, 500), min_height=logical_px(self, 160))

        state = {"remaining": int(seconds), "after_id": None, "finished": False}

        def _finish(_event=None):
            if state["finished"]:
                return
            state["finished"] = True
            if state["after_id"] is not None:
                try:
                    top.after_cancel(state["after_id"])
                except tk.TclError:
                    pass
            try:
                top.destroy()
            except tk.TclError:
                pass
            if on_done is not None:
                on_done()

        def _tick():
            state["after_id"] = None
            if state["finished"]:
                return
            if state["remaining"] <= 0:
                _finish()
                return
            lbl.config(text=f"{state['remaining']} sec")
            state["remaining"] -= 1
            state["after_id"] = top.after(1000, _tick)

        top.bind("<Return>", _finish)
        top.bind("<Escape>", _finish)
        top.protocol("WM_DELETE_WINDOW", _finish)
        top.focus_set()
        _tick()
        return top

    def _update_last_plots(self, tag: str):
        """Worker thread: queue the latest signal/dark pair for the Measurements plot."""
        sig, dark = app.data.last_vectors_for(tag)
        if getattr(app, "meas_canvas", None) is None:
            return  # figure not created yet (tab never shown); nothing to draw on
        redraw = getattr(app, "_meas_redraw", None)
        if redraw is None:
            redraw = ThrottledRedraw(
                app.pump,
                _render_last_plots,
                draw=app.meas_canvas.draw_idle,
                visible=lambda: app._is_tab_visible(app.measure_tab),
            )
            app._meas_redraw = redraw
        redraw.submit((sig, dark))

    def _render_last_plots(pending):
        CLAMP = 65000  # counts ceiling for display

        sig_pending, dark_pending = pending
        hint = getattr(app, "meas_empty_text", None)
        if hint is not None and hint.get_visible():
            hint.set_visible(False)
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
        app.meas_ax.set_title(title, **plot_title_font(pad=8, color=Colors.DANGER if any_sat else Colors.TEXT_PRIMARY))



    def save_csv():
        if not app.data.rows:
            messagebox.showwarning("Save CSV", "No data collected yet.")
            return
        df = app.data.to_dataframe()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        default = f"Measurements_{app.data.serial_number}_{ts}.csv"
        try:
            app.RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
            initial_dir = str(app.RESULTS_ROOT)
        except Exception:
            initial_dir = None
        path = filedialog.asksaveasfilename(
            title="Save CSV", defaultextension=".csv", initialdir=initial_dir,
            initialfile=default, filetypes=[("CSV", "*.csv"), ("All", "*.*")])
        if not path:
            return
        try:
            df.to_csv(path, index=False)
            app.status.toast(f"Saved {os.path.basename(path)}")
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
