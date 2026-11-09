"""Live View tab - real-time spectrum display with laser and filter wheel controls."""
import csv
import logging
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from types import MethodType

import numpy as np
import tkinter as tk
from tkinter import filedialog, ttk, messagebox
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

from services.run_progress import source_label
from .theme import (Colors, Fonts, Spacing, attach_hover, bind_figure_margins,
                    configure_matplotlib_style, control_strip_height, empty_state_text, fit_strip_to_content,
                    harmonize, logical_px, make_action_button, matplotlib_font_family,
                    mount_toolbar, plot_title_font, relax_canvas_size, scaled_pt, screen_dpi,
                    set_action_enabled, set_counts_scale, source_style, style_legend)
from .ui_utils import BlitRenderer, ScrollableFrame, ThrottledRedraw, Tooltip, bind_debounced_configure, run_in_worker

LOGGER = logging.getLogger(__name__)

_LIVE_CLAMP = 65000        # counts ceiling for display
_LIVE_MIN_FRAME_S = 0.04   # cap the acquisition loop at ~25 frames/s
_FULL_SCALE = 65535.0      # 16-bit detector full scale, for the readout's percentage
_READOUT_MIN_S = 0.1       # the numeric readout refreshes at most 10x per second
_MAX_AVERAGE_FRAMES = 64   # rolling average depth the operator may ask for


class _LiveToolbar(NavigationToolbar2Tk):
    """matplotlib's toolbar, whose sliders button opens the Live View chart settings."""

    toolitems = tuple(
        (text, "Chart settings: invert the spectrum, logarithmic counts, margins", image, callback)
        if callback == "configure_subplots" else (text, tip, image, callback)
        for text, tip, image, callback in NavigationToolbar2Tk.toolitems
    )
    open_settings = None  # set by the tab once the toolbar is mounted

    def configure_subplots(self, *args):
        if self.open_settings is not None:
            return self.open_settings()
        return super().configure_subplots(*args)


def build(app):
    IT_MIN = 0.2
    IT_MAX = 3000.0

    def _build_live_tab():
        main_frame = ttk.Frame(app.live_tab)
        main_frame.pack(fill="both", expand=True)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(0, weight=1)

        plot_container = ttk.Frame(main_frame)
        plot_container.grid(row=0, column=0, sticky="nsew", padx=Spacing.PAD_SM, pady=Spacing.PAD_SM)
        # A Tk-Agg canvas re-requests its full pixel size after every resize
        # (figsize * dpi, ~1200x950 px here), which otherwise starves the
        # control panel beside it. Propagation is per *inner* geometry manager,
        # and the canvas is packed, so it is pack_propagate that must be off.
        plot_container.pack_propagate(False)

        controls_host = ttk.Frame(main_frame)
        controls_host.grid(row=0, column=1, sticky="nsew", padx=(0, Spacing.PAD_SM), pady=Spacing.PAD_SM)
        controls_host.grid_propagate(False)
        controls_host.pack_propagate(False)

        controls_scroll = ScrollableFrame(controls_host, y_scroll=True, background=Colors.BG_PRIMARY)
        controls_scroll.pack(fill="both", expand=True)
        controls_container = controls_scroll.content
        controls_grid = ttk.Frame(controls_container)
        controls_grid.pack(fill="both", expand=True)

        # Matplotlib figure. No constrained_layout: its solver re-runs on every
        # draw and dominated the per-frame cost; one fixed margin set instead.
        app.live_fig = Figure(figsize=(12, 7), dpi=screen_dpi(app))
        app.live_fig.subplots_adjust(left=0.07, right=0.985, top=0.92, bottom=0.09)
        app.live_ax = app.live_fig.add_subplot(111)
        configure_matplotlib_style(app.live_fig, app.live_ax, title="Live Spectrum")

        # Plot lines
        app.live_line, = app.live_ax.plot([], [], lw=1.8, color=Colors.ACCENT, label="Signal", alpha=0.9)
        app.live_sat_line, = app.live_ax.plot([], [], lw=2.2, color=Colors.DANGER, label="Saturated", alpha=0.95)
        app.live_sat_line.set_visible(False)
        # The brightest pixel, marked on the trace so it can be found at a
        # glance when zoomed in or when the line is one pixel wide.
        from matplotlib.transforms import offset_copy

        # The marker floats a few points above the peak so it never covers the
        # tip of the line; the label sits above the marker.
        app.live_peak_marker, = app.live_ax.plot([], [], linestyle="none", marker="v", markersize=scaled_pt(6),
                                                 color=Colors.ACCENT_DEEP, alpha=0.9, zorder=5, clip_on=False)
        app.live_peak_marker.set_transform(offset_copy(app.live_ax.transData, fig=app.live_fig,
                                                       x=0, y=scaled_pt(5), units="points"))
        app.live_peak_text = app.live_ax.annotate("", xy=(0, 0), xytext=(0, scaled_pt(11)), textcoords="offset points",
                                                  ha="center", va="bottom", fontsize=scaled_pt(8.5),
                                                  color=Colors.ACCENT_DEEP, fontfamily=matplotlib_font_family(),
                                                  annotation_clip=False, zorder=5)
        app.live_peak_marker.set_visible(False)
        app.live_peak_text.set_visible(False)

        style_legend(app.live_ax, loc="upper right")
        app.live_empty_text = empty_state_text(app.live_ax, "Connect a spectrometer in Setup, then press Start Live")
        # Sensible idle axes; the first frame sets its own limits.
        app.live_ax.set_xlim(0, 2048)
        app.live_ax.set_ylim(0, 65535)

        # Chart settings (the toolbar's sliders button), for this session only.
        # live_invert_x mirrors the trace; the pixel axis itself always reads 0 -> end.
        app.live_invert_x = tk.BooleanVar(master=app, value=False)
        app.live_log_y = tk.BooleanVar(master=app, value=False)
        app.live_mark_peak = tk.BooleanVar(master=app, value=True)
        # Frame processing: a rolling average over the last N frames and an
        # optional dark frame subtracted from what is shown and saved.
        app.live_avg_var = tk.IntVar(master=app, value=1)
        app.live_subtract_dark = tk.BooleanVar(master=app, value=False)

        app.live_canvas = FigureCanvasTkAgg(app.live_fig, master=plot_container)
        relax_canvas_size(app.live_canvas, 620, 340, dpi=screen_dpi(app))
        bind_figure_margins(app.live_canvas, app.live_fig)
        # Toolbar first, canvas second: see mount_toolbar.
        app.live_toolbar = mount_toolbar(app.live_canvas, plot_container, _LiveToolbar)
        app.live_toolbar.open_settings = _open_chart_settings
        app.live_canvas.get_tk_widget().pack(side="top", fill="both", expand=True)

        # Blit only the trace lines and the peak mark over a cached background; a
        # full draw is requested when limits/title change, on resize, or after a toolbar zoom.
        app._live_blit = BlitRenderer(app.live_canvas, app.live_ax,
                                      [app.live_line, app.live_sat_line, app.live_peak_marker, app.live_peak_text])
        app.live_canvas.draw()

        # Acquisition thread submits frames; only the newest is rendered, once
        # per UI-pump tick, and only drawn while this tab is visible.
        app._live_redraw = ThrottledRedraw(
            app.pump,
            _render_live_frame,
            draw=app._live_blit.draw,
            visible=lambda: app._is_tab_visible(app.live_tab),
        )

        # track zoom/pan interactions
        app.live_limits_locked = False
        app._live_mouse_down = False

        def _on_press(event):
            app._live_mouse_down = True

        def _on_release(event):
            # When user releases after an interaction on axes, lock current limits
            if event.inaxes is not None:
                app.live_limits_locked = True
            app._live_mouse_down = False

        app.live_canvas.mpl_connect("button_press_event", _on_press)
        app.live_canvas.mpl_connect("button_release_event", _on_release)

        # === Spectrum Controls Section ===
        spectrum_frame = ttk.LabelFrame(controls_grid, text="Spectrum Controls", padding=8)
        spectrum_frame.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

        ttk.Label(spectrum_frame, text="Integration Time (ms):").pack(anchor="w")
        # A spinbox: Up/Down and the arrows step 0.1 ms and apply at once while
        # a spectrometer is connected; Enter applies a typed value.
        app.it_entry = ttk.Spinbox(spectrum_frame, width=12, from_=IT_MIN, to=IT_MAX, increment=0.1,
                                   format="%.2f", command=lambda: app.apply_it(quiet=True))
        app.it_entry.insert(0, "2.4")
        app.it_entry.pack(anchor="w", pady=(2, 6))
        app.it_entry.bind("<Return>", lambda _event: app.apply_it())

        btn_frame = ttk.Frame(spectrum_frame)
        btn_frame.pack(anchor="w", fill="x")
        app.apply_it_btn = ttk.Button(btn_frame, text="Apply IT", command=app.apply_it)
        app.apply_it_btn.pack(side="left", padx=(0, 4))
        Tooltip(app.it_entry, "Detector integration (exposure) time in milliseconds. Up/Down steps 0.1 ms "
                              "and applies at once; Enter applies a typed value. While Live View runs the "
                              "change is applied between frames.")
        ttk.Button(btn_frame, text="Reset Zoom", command=app._live_reset_view).pack(side="left")

        # Frame processing: averaging and dark subtraction act on what is
        # shown, read out and saved; the detector itself is not touched.
        ttk.Separator(spectrum_frame, orient="horizontal").pack(fill="x", pady=(Spacing.PAD_SM, Spacing.PAD_XS))
        avg_row = ttk.Frame(spectrum_frame)
        avg_row.pack(anchor="w", fill="x", pady=(2, 4))
        ttk.Label(avg_row, text="Average frames:").pack(side="left", padx=(0, 6))
        app.live_avg_spin = ttk.Spinbox(avg_row, width=5, from_=1, to=_MAX_AVERAGE_FRAMES, increment=1,
                                        textvariable=app.live_avg_var, command=lambda: _re_present())
        app.live_avg_spin.pack(side="left")
        app.live_avg_spin.bind("<Return>", lambda _event: _re_present())
        Tooltip(app.live_avg_spin, "Rolling average over the last N frames (1 = every frame as it comes). "
                                   "Lowers the noise on the trace and the readout; saturation is still "
                                   "judged on each raw frame.")
        dark_row = ttk.Frame(spectrum_frame)
        dark_row.pack(anchor="w", fill="x", pady=(0, 2))
        app.live_dark_btn = ttk.Button(dark_row, text="Capture dark", command=lambda: _capture_dark())
        app.live_dark_btn.pack(side="left", padx=(0, 6))
        Tooltip(app.live_dark_btn, "Keep the frame on the plot (averaged as set) as the dark and subtract it "
                                   "from the frames that follow. Cover the input or switch the sources off "
                                   "first; take it again after changing the integration time.")
        app.live_dark_check = ttk.Checkbutton(dark_row, text="Subtract dark", variable=app.live_subtract_dark,
                                              command=lambda: _re_present())
        app.live_dark_check.pack(side="left")
        app.live_dark_check.state(["disabled"])
        app.live_dark_caption = ttk.Label(spectrum_frame, text="No dark captured", style="Muted.TLabel")
        app.live_dark_caption.pack(anchor="w", pady=(0, 4))
        app.live_save_frame_btn = ttk.Button(spectrum_frame, text="Save frame as CSV…", command=lambda: _save_live_frame())
        app.live_save_frame_btn.pack(anchor="w", fill="x", pady=(2, 0))
        Tooltip(app.live_save_frame_btn, "Write the spectrum on the plot (averaged and dark-corrected as set) "
                                         "and the raw frame to a CSV file, with the serial number and "
                                         "integration time in the header.")

        # === Laser Controls Section ===
        laser_frame = ttk.LabelFrame(controls_grid, text="Laser Controls", padding=8)
        laser_frame.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)

        # All sources in ascending order; variables are shared app-wide. Two
        # columns: the list is half as tall and the column stays scroll-free.
        laser_grid = ttk.Frame(laser_frame)
        laser_grid.pack(fill="x")
        laser_grid.columnconfigure(0, weight=1)
        laser_grid.columnconfigure(1, weight=1)

        def _build_laser_checks():
            """One checkbox per configured source; rebuilt when Setup adds or removes one."""
            for child in laser_grid.winfo_children():
                child.destroy()
            for index, tag in enumerate(app.laser_tags()):
                var = app.laser_var(tag)
                btn = ttk.Checkbutton(
                    laser_grid,
                    text=source_label(tag),
                    variable=var,
                    style=source_style(laser_grid, tag, "Bold.TCheckbutton"),
                    command=lambda t=tag, v=var: app.toggle_laser(t, v.get())
                )
                btn.grid(row=index // 2, column=index % 2, sticky="w", padx=(0, Spacing.PAD_SM), pady=2)
            harmonize(laser_grid)

        _build_laser_checks()
        app._laser_ui_rebuilders.append(_build_laser_checks)

        # === Live Control Section ===
        live_control_frame = ttk.LabelFrame(controls_grid, text="Live View Control", padding=8)
        live_control_frame.grid(row=0, column=1, sticky="nsew", padx=4, pady=4)

        app.live_start_btn = make_action_button(live_control_frame, text="Start Live",
                                                command=app.start_live, width=18)
        app.live_start_btn.pack(pady=(0, 6), fill="x")

        app.live_stop_btn = make_action_button(live_control_frame, text="Stop Live",
                                               command=app.stop_live, danger=True, width=18)
        app.live_stop_btn.pack(fill="x")
        set_action_enabled(app.live_stop_btn, False)

        # Lives with the acquisitions, not the laser switches: it claims the
        # spectrometer like Start Live does and its result lands in Analysis.
        ttk.Separator(live_control_frame, orient="horizontal").pack(fill="x", pady=(Spacing.PAD_SM, Spacing.PAD_XS))
        app.check_spec_btn = ttk.Button(live_control_frame, text="Check spectrometer (line fit)",
                                        command=app.run_check_spectrometer)
        app.check_spec_btn.pack(fill="x", pady=(2, 0))
        Tooltip(app.check_spec_btn, "Switch one source on first. Auto-exposes on the brightest line, fits a "
                                    "super-Gaussian and reports centre, width and shape in the Analysis tab; "
                                    "saves a CSV, a plot and a .ddf next to the results.")

        # === Readout Section ===
        # The numbers an operator otherwise reads off the plot while tuning
        # the exposure; same styling as Check Resolution's Fit Readout, in
        # two columns so the control column still fits a 1080p screen.
        readout_frame = ttk.LabelFrame(controls_grid, text="Readout", padding=8)
        readout_frame.grid(row=2, column=0, sticky="nsew", padx=4, pady=4)
        readout_frame.columnconfigure(1, weight=1)
        readout_frame.columnconfigure(3, weight=1)

        def _readout_cell(row, column, label_text):
            ttk.Label(readout_frame, text=label_text, anchor="w", style="FieldLabel.TLabel").grid(
                row=row, column=column * 2, sticky="w", padx=(0, 4), pady=1)
            value = ttk.Label(readout_frame, text="—", anchor="w", style="MetricSmall.TLabel")
            value.grid(row=row, column=column * 2 + 1, sticky="ew", padx=(0, Spacing.PAD_SM), pady=1)
            return value

        app.live_readout = {
            "peak": _readout_cell(0, 0, "Peak:"),
            "pixel": _readout_cell(0, 1, "Pixel:"),
            "scale": _readout_cell(1, 0, "Full scale:"),
            "sat": _readout_cell(1, 1, "Saturation:"),
            "it": _readout_cell(2, 0, "Integration:"),
            "fps": _readout_cell(2, 1, "Frame rate:"),
            "status": _readout_cell(3, 0, "Status:"),
        }
        app.live_readout["status"].configure(text="Idle", foreground=Colors.TEXT_MUTED)

        # === Head Sensor Section ===
        headsensor_frame = ttk.LabelFrame(controls_grid, text="Head Sensor", padding=8)
        headsensor_frame.grid(row=1, column=1, sticky="nsew", padx=4, pady=4)

        # On/Off switch
        headsensor_header = ttk.Frame(headsensor_frame)
        headsensor_header.pack(anchor="w", pady=(0, 4), fill="x")

        app.headsensor_enabled = tk.BooleanVar(value=False)
        app.headsensor_toggle = ttk.Checkbutton(
            headsensor_header,
            text="Enable Head Sensor",
            variable=app.headsensor_enabled,
            command=lambda: toggle_headsensor()
        )
        app.headsensor_toggle.pack(side="left")

        # Status label (shows device ID or error)
        app.headsensor_status_label = ttk.Label(headsensor_frame, text="Disabled", foreground=Colors.TEXT_MUTED, font=Fonts.BODY_SMALL_BOLD)
        app.headsensor_status_label.pack(anchor="w", pady=(0, 6))

        # Frame to hold all filter wheel controls (initially hidden)
        app.headsensor_controls_frame = ttk.Frame(headsensor_frame)

        # Separator
        ttk.Separator(app.headsensor_controls_frame, orient="horizontal").pack(fill="x", pady=(0, 8))

        # Filter wheel selection using radio buttons
        fw_selection_label = ttk.Label(app.headsensor_controls_frame, text="Target Filter Wheel:", font=Fonts.BODY_SMALL_BOLD)
        fw_selection_label.pack(anchor="w", pady=(0, 4))

        app.filterwheel_var = tk.IntVar(value=1)  # Default to Filter Wheel 1
        fw_radio_frame = ttk.Frame(app.headsensor_controls_frame)
        fw_radio_frame.pack(anchor="w", pady=(0, 8))

        app.fw1_radio = ttk.Radiobutton(
            fw_radio_frame,
            text="FW 1",
            variable=app.filterwheel_var,
            value=1
        )
        app.fw1_radio.pack(side="left", padx=(0, 15))

        app.fw2_radio = ttk.Radiobutton(
            fw_radio_frame,
            text="FW 2",
            variable=app.filterwheel_var,
            value=2
        )
        app.fw2_radio.pack(side="left")

        # Commands dropdown
        cmd_label = ttk.Label(app.headsensor_controls_frame, text="Command:", font=Fonts.BODY_SMALL_BOLD)
        cmd_label.pack(anchor="w", pady=(0, 4))

        app.filterwheel_cmd_var = tk.StringVar(value="")

        # Build command list: Position 1-9, Test, Reset
        command_options = [f"Position {i}" for i in range(1, 10)] + ["Test", "Reset"]

        app.filterwheel_cmd_combo = ttk.Combobox(
            app.headsensor_controls_frame,
            textvariable=app.filterwheel_cmd_var,
            values=command_options,
            state="readonly",
            width=20
        )
        app.filterwheel_cmd_combo.pack(anchor="w", pady=(0, 10))

        # Position status section
        ttk.Separator(app.headsensor_controls_frame, orient="horizontal").pack(fill="x", pady=(0, 8))

        status_label = ttk.Label(app.headsensor_controls_frame, text="Position Status:", font=Fonts.BODY_SMALL_BOLD)
        status_label.pack(anchor="w", pady=(0, 4))

        # Filter Wheel 1 position status
        app.fw1_status_label = ttk.Label(
            app.headsensor_controls_frame,
            text="FW 1: Uncertain",
            foreground=Colors.TEXT_MUTED,
            font=Fonts.BODY_SMALL_BOLD
        )
        app.fw1_status_label.pack(anchor="w", pady=(0, 3))

        # Filter Wheel 2 position status
        app.fw2_status_label = ttk.Label(
            app.headsensor_controls_frame,
            text="FW 2: Uncertain",
            foreground=Colors.TEXT_MUTED,
            font=Fonts.BODY_SMALL_BOLD
        )
        app.fw2_status_label.pack(anchor="w")

        # === Stage Slots Section ===
        stage_frame = ttk.LabelFrame(controls_grid, text="Stage Slots", padding=8)
        stage_frame.grid(row=2, column=1, sticky="nsew", padx=4, pady=4)

        app.stage_slots_container = ttk.Frame(stage_frame)
        app.stage_slots_container.pack(fill="both", expand=True)

        app.stage_move_status = ttk.Label(stage_frame, text="", foreground=Colors.TEXT_MUTED,
                                          font=Fonts.BODY_SMALL)
        app.stage_move_status.pack(anchor="w", pady=(4, 0))

        # No-slots placeholder
        app.stage_no_slots_label = ttk.Label(
            app.stage_slots_container,
            text="No stage config loaded.\nSet path in Setup tab.",
            foreground=Colors.TEXT_MUTED, font=Fonts.BODY_SMALL,
        )
        app.stage_no_slots_label.pack(anchor="w")

        def _on_slot_done(success, message):
            """Callback from stage move thread — schedule UI update on main thread."""
            def _update():
                if success:
                    app.stage_move_status.config(text=message, foreground=Colors.SUCCESS)
                else:
                    app.stage_move_status.config(text=message, foreground=Colors.DANGER)
                # Re-enable all slot buttons
                for btn in getattr(app, '_stage_slot_buttons', []):
                    btn.config(state="normal")
                if hasattr(app, '_stage_stop_btn'):
                    app._stage_stop_btn.config(state="disabled")
            app.pump.post(_update)

        def _goto_slot(index):
            if app.stage is None:
                messagebox.showerror("Stage", "Stage module not available.\nInstall pymodbus.")
                return
            if not app.stage.config.loaded:
                messagebox.showwarning("Stage", "Load stage config in Setup tab first.")
                return
            if index < 0 or index >= len(app.stage.slots):
                messagebox.showerror("Stage", f"Invalid slot index: {index}")
                return
            if app.stage.move_in_progress:
                messagebox.showwarning("Stage", "A move is already in progress. Wait or press STOP.")
                return
            # Auto-connect if not connected
            if not app.stage.connected:
                port = app.hw.com_ports.get("STAGE", "") or app.stage.config.com_port
                if not port:
                    messagebox.showwarning("Stage", "Set the Stage COM port in Setup tab.")
                    return
                if not app.stage.connect(port):
                    messagebox.showerror("Stage", f"Cannot connect to stage on {port}")
                    return
            # Disable slot buttons during move
            for btn in getattr(app, '_stage_slot_buttons', []):
                btn.config(state="disabled")
            if hasattr(app, '_stage_stop_btn'):
                app._stage_stop_btn.config(state="normal")
            slot_name = app.stage.slots[index].get("name", f"Slot {index + 1}")
            app.stage_move_status.config(text=f"Moving to {slot_name}...", foreground=Colors.INFO)
            if not app.stage.goto_slot(index, on_done=_on_slot_done):
                app.stage_move_status.config(text="Move command failed", foreground=Colors.DANGER)
                for btn in getattr(app, '_stage_slot_buttons', []):
                    btn.config(state="normal")

        def _stop_stage():
            if app.stage is not None:
                app.stage.stop_all()
                app.stage_move_status.config(text="STOPPED", foreground=Colors.DANGER)

        def _refresh_stage_slots_ui():
            """Rebuild slot buttons from current stage config."""
            for w in app.stage_slots_container.winfo_children():
                w.destroy()
            app._stage_slot_buttons = []

            if app.stage is None or not app.stage.config.loaded or not app.stage.slots:
                app.stage_no_slots_label = ttk.Label(
                    app.stage_slots_container,
                    text="No stage config loaded.\nSet path in Setup tab.",
                    foreground=Colors.TEXT_MUTED, font=Fonts.BODY_SMALL,
                )
                app.stage_no_slots_label.pack(anchor="w")
                return

            for i, slot in enumerate(app.stage.slots):
                name = slot.get("name", f"Slot {i + 1}")
                btn = ttk.Button(
                    app.stage_slots_container,
                    text=name,
                    command=lambda idx=i: _goto_slot(idx),
                    width=18,
                )
                btn.pack(anchor="w", pady=2)
                app._stage_slot_buttons.append(btn)

            app._stage_stop_btn = attach_hover(tk.Button(
                app.stage_slots_container,
                text="STOP",
                command=_stop_stage,
                bg=Colors.DANGER, fg=Colors.TEXT_ON_ACCENT,
                activebackground=Colors.DANGER_HOVER,
                activeforeground=Colors.TEXT_ON_ACCENT,
                disabledforeground=Colors.TEXT_DISABLED,
                font=Fonts.BODY_SMALL_BOLD,
                width=16, state="disabled", relief="flat", overrelief="flat",
                cursor="hand2", bd=0, highlightthickness=0, pady=4,
            ))
            app._stage_stop_btn.pack(anchor="w", pady=(6, 0))

        app._refresh_stage_slots_ui = _refresh_stage_slots_ui

        def _column_width(available_width):
            # A quarter of the window, within the range that keeps the controls
            # readable and still leaves the plot the lion's share.
            return max(logical_px(app, 300), min(logical_px(app, 400), int(available_width * 0.24)))

        def _layout_live_tab(width=None, height=None):
            available_width = width or main_frame.winfo_width()
            available_height = height or main_frame.winfo_height()
            if not available_width or not available_height or available_width <= 1:
                return
            gutter = Spacing.PAD_SM

            for row in range(6):
                controls_grid.rowconfigure(row, weight=0)
            for col in range(3):
                controls_grid.columnconfigure(col, weight=0, uniform="")

            strip_height = None
            if available_width >= logical_px(app, 1060) and available_height >= logical_px(app, 380):
                # Plot on the left, one column of cards on the right. From a
                # 1366x768 laptop up: a 300 px column still leaves the plot
                # 760 px, and a plot that wide and tall beats a strip below.
                # The column scrolls on a short window; the plot keeps the height.
                plot_container.grid_configure(row=0, column=0, sticky="nsew", padx=(gutter, gutter), pady=gutter)
                controls_host.grid_configure(row=0, column=1, sticky="nsew", padx=(0, gutter), pady=gutter)
                main_frame.columnconfigure(0, weight=1)
                main_frame.columnconfigure(1, weight=0)
                main_frame.rowconfigure(0, weight=1)
                main_frame.rowconfigure(1, weight=0)
                controls_host.configure(width=_column_width(available_width),
                                        height=max(logical_px(app, 240), available_height - 2 * gutter))
                layouts = (
                    (spectrum_frame, 0, 0),
                    (live_control_frame, 1, 0),
                    (readout_frame, 2, 0),
                    (laser_frame, 3, 0),
                    (headsensor_frame, 4, 0),
                    (stage_frame, 5, 0),
                )
                controls_grid.columnconfigure(0, weight=1)
                two_columns = False
            else:
                # Plot on top, the cards in a strip below it. The plot keeps a
                # working height; the strip takes what is left and scrolls.
                plot_container.grid_configure(row=0, column=0, sticky="nsew", padx=gutter, pady=(gutter, gutter // 2))
                controls_host.grid_configure(row=1, column=0, sticky="nsew", padx=gutter, pady=(0, gutter))
                main_frame.columnconfigure(0, weight=1)
                main_frame.columnconfigure(1, weight=0)
                main_frame.rowconfigure(0, weight=1)
                main_frame.rowconfigure(1, weight=0)
                strip_height = control_strip_height(app, available_height - 2 * gutter, share=0.40,
                                                    preferred=220, cap=380, floor=140, plot_min=300)
                controls_host.configure(width=max(logical_px(app, 320), available_width - 2 * gutter),
                                        height=strip_height)
                two_columns = available_width >= logical_px(app, 700)
                if two_columns:
                    layouts = (
                        (spectrum_frame, 0, 0),
                        (live_control_frame, 0, 1),
                        (readout_frame, 1, 0),
                        (laser_frame, 1, 1),
                        (headsensor_frame, 2, 0),
                        (stage_frame, 2, 1),
                    )
                    controls_grid.columnconfigure(0, weight=1, uniform="live")
                    controls_grid.columnconfigure(1, weight=1, uniform="live")
                else:
                    layouts = (
                        (spectrum_frame, 0, 0),
                        (live_control_frame, 1, 0),
                        (readout_frame, 2, 0),
                        (laser_frame, 3, 0),
                        (headsensor_frame, 4, 0),
                        (stage_frame, 5, 0),
                    )
                    controls_grid.columnconfigure(0, weight=1)

            for widget, row, column in layouts:
                widget.grid_configure(row=row, column=column, sticky="nsew",
                                      padx=(0, gutter) if two_columns and column == 0 else 0,
                                      pady=(0, Spacing.SECTION_GAP))
            if strip_height is not None:
                fit_strip_to_content(controls_host, controls_scroll, strip_height)

        bind_debounced_configure(main_frame, _layout_live_tab)

        # Track current positions (None = Uncertain)
        app.fw1_position = None
        app.fw2_position = None

        def update_position_status(fw_num: int, position: int):
            """Update the position status label for the specified filter wheel."""
            if fw_num == 1:
                app.fw1_position = position
                app.fw1_status_label.config(
                    text=f"FW 1: Position {position}",
                    foreground=Colors.SUCCESS
                )
            elif fw_num == 2:
                app.fw2_position = position
                app.fw2_status_label.config(
                    text=f"FW 2: Position {position}",
                    foreground=Colors.SUCCESS
                )


        def execute_filterwheel_command(*args):
            """Send the selected command to the selected filter wheel on a worker thread."""
            command = app.filterwheel_cmd_var.get()
            if not command:
                return
            app._update_ports_from_ui()
            fw_num = app.filterwheel_var.get()
            app.filterwheel_cmd_var.set("")  # clear the selection right away (re-triggers with "" and returns)

            def _send():
                if command.startswith("Position "):
                    position = int(command.split(" ")[1])
                    return position, app.filterwheel.set_filterwheel(fw_num, position)
                if command == "Test":
                    return 1, app.filterwheel.test_filterwheel(fw_num)
                if command == "Reset":
                    return 1, app.filterwheel.reset_filterwheel(fw_num)
                raise ValueError(f"Unknown filter wheel command '{command}'")

            def _done(result):
                position, ok = result
                history = app.filterwheel.serial_status.get("hst") or [""]
                last = str(history[-1]).lower()
                # A timeout after a move is the device's normal behaviour: assume it moved.
                if ok or "timeout" in last:
                    update_position_status(fw_num, position)
                    app.status.toast(f"Filter wheel {fw_num}: {command}")
                else:
                    app.status.toast(f"Filter wheel {fw_num}: {history[-1] or 'no response'}", kind="warning")

            run_in_worker(app, _send, on_done=_done,
                          on_error=lambda exc: app._post_error("Filter Wheel", exc),
                          busy=(app.filterwheel_cmd_combo,), name="filter-wheel")

        # Bind the command execution to dropdown selection changes
        app.filterwheel_cmd_var.trace_add("write", execute_filterwheel_command)


        def toggle_headsensor():
            """Toggle the head sensor; the serial probe and filter-wheel resets run on a worker."""
            if not app.headsensor_enabled.get():
                app.headsensor_status_label.config(text="Disabled", foreground=Colors.TEXT_MUTED)
                app.headsensor_controls_frame.pack_forget()
                app.filterwheel_var.set(1)
                app.filterwheel_cmd_var.set("")
                app.fw1_position = None
                app.fw2_position = None
                app.fw1_status_label.config(text="FW 1: Uncertain", foreground=Colors.TEXT_MUTED)
                app.fw2_status_label.config(text="FW 2: Uncertain", foreground=Colors.TEXT_MUTED)
                return

            app._update_ports_from_ui()
            app.headsensor_status_label.config(text="Checking connection...", foreground=Colors.INFO)

            def _probe():
                success, device_id = app.filterwheel.query_device_id()
                if not (success and str(device_id).startswith("Pan")):
                    return {"ok": False, "device_id": device_id}
                # Reset both filterwheels to the home position (1). A timeout here is the
                # device's normal behaviour and is tolerated.
                resets = {}
                for wheel in (1, 2):
                    LOGGER.info("Resetting filterwheel %d...", wheel)
                    resets[wheel] = bool(app.filterwheel.reset_filterwheel(wheel))
                    if not resets[wheel]:
                        LOGGER.warning("Filterwheel %d reset may have timed out (common behavior)", wheel)
                return {"ok": True, "device_id": device_id, "resets": resets}

            def _done(result):
                if not result["ok"]:
                    app.headsensor_enabled.set(False)
                    app.headsensor_status_label.config(
                        text=f"✗ No Head Sensor connected (response: {result['device_id']})",
                        foreground=Colors.DANGER,
                    )
                    app.headsensor_controls_frame.pack_forget()
                    messagebox.showwarning(
                        "Head Sensor",
                        f"No Head Sensor connected.\n\nExpected device ID starting with 'Pan', got: {result['device_id']}\n\n"
                        "Please check:\n• COM port configuration in Setup tab\n• Device power and connections",
                    )
                    return
                app.fw1_position = 1
                app.fw2_position = 1
                update_position_status(1, 1)
                update_position_status(2, 1)
                app.headsensor_status_label.config(text=f"✓ {result['device_id']}", foreground=Colors.SUCCESS)
                app.headsensor_controls_frame.pack(anchor="w", pady=(4, 0), fill="x")
                app.filterwheel_var.set(1)
                app.filterwheel_cmd_var.set("")
                app.status.toast(f"Head sensor {result['device_id']} ready; filter wheels at position 1")

            def _error(exc):
                app.headsensor_enabled.set(False)
                app.headsensor_status_label.config(text=f"✗ Error: {exc}", foreground=Colors.DANGER)
                app.headsensor_controls_frame.pack_forget()
                messagebox.showerror("Head Sensor", f"Failed to query Head Sensor:\n\n{exc}")

            run_in_worker(app, _probe, on_done=_done, on_error=_error, busy=(app.headsensor_toggle,), name="head-sensor")

    def apply_it(quiet=False):
        """Apply the spinbox value. ``quiet``: an arrow step, so no dialog when nothing is connected."""
        if not app.spec:
            if not quiet:
                messagebox.showwarning("Spectrometer", "Not connected.")
            return
        # Parse & clamp
        try:
            it = float(app.it_entry.get())
        except Exception as e:
            messagebox.showerror("Apply IT", f"Invalid IT value: {e}")
            return
        it = max(IT_MIN, min(IT_MAX, it))

        # If live is running, defer until between frames
        if getattr(app, 'live_running', None) and app.live_running.is_set():
            app._pending_it = it
            try:
                app.apply_it_btn.state(["disabled"])  # if button exists
            except Exception:
                pass
            if not quiet:
                app.status.toast(f"IT {it:.3f} ms is applied after the current frame")
            return

        # Apply now, but never while another acquisition owns the spectrometer.
        with app.acq.hold("Apply IT") as acquired:
            if not acquired:
                messagebox.showwarning("Apply IT", app.acq.busy_reason() or "Spectrometer is busy.")
                return
            try:
                app._it_updating = True
                res = app.spec.set_it(it)
                if isinstance(res, str) and res not in ("", "OK"):
                    raise RuntimeError(res)
                _live_stats["it"] = it
                _refresh_live_it()
                app.status.toast(f"Integration time set to {it:.3f} ms")
            except Exception as e:
                messagebox.showerror("Apply IT", str(e))
            finally:
                app._it_updating = False
                try:
                    app.apply_it_btn.state(["!disabled"])  # re-enable
                except Exception:
                    pass


    def start_live():
        if app.live_running.is_set():
            return
        if not app.claim_spectrometer("Live View"):
            return
        # The spinbox is the exposure the operator expects to see: apply it
        # now. Other owners (Check Resolution, Check spectrometer, a run)
        # leave the device at whatever they used, which used to stream here
        # at 3 s per frame under a readout that said 2.40 ms.
        app._pending_it = None
        try:
            it = max(IT_MIN, min(IT_MAX, float(app.it_entry.get())))
            res = app.spec.set_it(it)
            if isinstance(res, str) and res not in ("", "OK"):
                raise RuntimeError(res)
            _live_stats["it"] = it
        except (ValueError, tk.TclError):
            pass
        except Exception as exc:
            app.release_spectrometer("Live View")
            messagebox.showerror("Start Live", f"Could not set the integration time: {exc}")
            return
        _live_stats["fps"] = None
        _live_stats["last_frame"] = None
        _set_live_status("Streaming", Colors.SUCCESS)
        _refresh_live_it()
        set_action_enabled(app.live_start_btn, False)
        set_action_enabled(app.live_stop_btn, True)
        app.live_running.set()
        app.live_thread = threading.Thread(target=app._live_loop, daemon=True, name="live-view")
        app.live_thread.start()

    def stop_live():
        was_running = app.live_running.is_set()
        app.live_running.clear()
        if was_running:
            # Only the owner aborts the frame in flight: with a measurement
            # running, Stop Live used to abort the run's capture instead.
            app.request_spectrometer_stop()
        # An IT queued for "after the current frame" has no frame to follow now.
        app._pending_it = None
        _reenable_apply_it()

    def _live_loop():
        """Acquisition thread. Never touches Tk directly: frames go through the pump."""
        try:
            while app.live_running.is_set():
                frame_started = time.perf_counter()
                try:
                    started = app.spec.measure(ncy=1)
                    if isinstance(started, str) and started not in ("", "OK"):
                        raise RuntimeError(f"measure failed: {started}")
                    waited = app.spec.wait_for_measurement()
                    if not app.live_running.is_set():
                        break
                    if isinstance(waited, str) and waited not in ("", "OK"):
                        raise RuntimeError(f"wait_for_measurement failed: {waited}")
                    _note_live_frame_time(time.perf_counter())

                    # Apply any deferred IT safely after the completed frame
                    if app._pending_it is not None:
                        it_to_apply = app._pending_it
                        app._pending_it = None
                        app._it_updating = True
                        try:
                            res = app.spec.set_it(it_to_apply)
                            if isinstance(res, str) and res not in ("", "OK"):
                                raise RuntimeError(res)
                            LOGGER.info("Applied IT=%.3f ms between live frames", it_to_apply)
                            _live_stats["it"] = it_to_apply
                        except Exception as e:
                            app._post_error("Apply IT (deferred)", e)
                        finally:
                            app._it_updating = False
                            app.pump.post(_reenable_apply_it)

                    raw = getattr(app.spec, "rcm", None)
                    if raw is None or (hasattr(raw, '__len__') and len(raw) == 0):
                        time.sleep(0.05)
                        continue
                    if not app._live_redraw.pending:
                        # Skip the copy when the previous frame is still waiting to be drawn.
                        y = np.array(raw, dtype=float)
                        if y.size == 0 or not np.all(np.isfinite(y)):
                            time.sleep(0.05)
                            continue
                        app.npix = len(y)
                        app.data.npix = app.npix
                        app._update_live_plot(np.arange(len(y)), y)

                except Exception as e:
                    app._post_error("Live error", e)
                    break

                # Pace the loop: short integration times would otherwise spin at
                # hundreds of frames/s and starve the UI thread of the GIL.
                remaining = _LIVE_MIN_FRAME_S - (time.perf_counter() - frame_started)
                if remaining > 0:
                    time.sleep(remaining)
        finally:
            app.live_running.clear()
            app.release_spectrometer("Live View")
            _live_stats["fps"] = None
            app.pump.post(_set_live_status, "Stopped", Colors.TEXT_MUTED)
            app.pump.post(_refresh_live_fps)
            app.pump.post(_live_buttons_idle)

    def _live_buttons_idle():
        set_action_enabled(app.live_start_btn, True)
        set_action_enabled(app.live_stop_btn, False)

    def _reenable_apply_it():
        try:
            app.apply_it_btn.state(["!disabled"])
        except Exception:
            pass

    # ``mirrored``: whether the trace on the plot was drawn inverted (last pixel on the left).
    _live_view_state = {"title": None, "xlim": None, "ylim": None, "mirrored": False}
    # Readout state. ``it`` is the last exposure applied (ms); ``fps`` is a
    # smoothed display frame rate maintained by the acquisition thread.
    _live_stats = {"it": None, "fps": None, "last_frame": None, "last_readout": 0.0}
    # Frame processing state (main thread only): the rolling-average window,
    # the captured dark and the last frame at each stage, so a toggle can be
    # shown at once on the frame already on the plot.
    _live_proc = {"frames": deque(), "dark": None, "dark_it": None, "x": None,
                  "last_raw": None, "last_avg": None, "last_processed": None, "last_saturated": False}

    def _note_live_frame_time(now):
        """Acquisition thread: fold one frame period into the smoothed frame rate."""
        last = _live_stats["last_frame"]
        _live_stats["last_frame"] = now
        if last is None:
            return
        rate = 1.0 / max(1e-4, now - last)
        previous = _live_stats["fps"]
        _live_stats["fps"] = rate if previous is None else 0.8 * previous + 0.2 * rate

    def _readout():
        return getattr(app, "live_readout", None)

    def _set_live_status(text, color):
        readout = _readout()
        if readout is not None:
            readout["status"].configure(text=text, foreground=color)

    def _refresh_live_it():
        readout = _readout()
        if readout is not None:
            it = _live_stats["it"]
            readout["it"].configure(text=f"{it:.2f} ms" if it is not None else "—")
        _refresh_dark_caption()

    # -- frame processing --------------------------------------------------------

    def _average_depth() -> int:
        try:
            depth = int(app.live_avg_var.get())
        except (tk.TclError, ValueError):
            depth = 1
        return max(1, min(_MAX_AVERAGE_FRAMES, depth))

    def _average(y_raw):
        """Fold one raw frame into the rolling window and return the window's mean."""
        frames = _live_proc["frames"]
        depth = _average_depth()
        if frames and frames[0].shape != y_raw.shape:
            frames.clear()                      # a different spectrometer: start the window over
        if depth <= 1:
            frames.clear()
            return y_raw
        frames.append(y_raw)
        while len(frames) > depth:
            frames.popleft()
        return np.mean(frames, axis=0) if len(frames) > 1 else y_raw

    def _compose(y_avg):
        """The averaged frame less the dark, when one is captured and switched on."""
        dark = _live_proc["dark"]
        if app.live_subtract_dark.get() and dark is not None and dark.shape == y_avg.shape:
            return y_avg - dark
        return y_avg

    def _refresh_dark_caption():
        caption = getattr(app, "live_dark_caption", None)
        if caption is None:
            return
        dark = _live_proc["dark"]
        if dark is None:
            caption.configure(text="No dark captured", foreground=Colors.TEXT_MUTED)
            return
        dark_it = _live_proc["dark_it"]
        it = _live_stats["it"]
        at = f" at {dark_it:.2f} ms" if dark_it is not None else ""
        if dark_it is not None and it is not None and abs(float(it) - float(dark_it)) > 1e-6:
            caption.configure(text=f"Dark{at}, now {it:.2f} ms: capture it again",
                              foreground=Colors.WARNING)
        else:
            caption.configure(text=f"Dark: {dark.size} px{at}", foreground=Colors.TEXT_SECONDARY)

    def _capture_dark():
        """Keep the frame on the plot (averaged, before any dark) as the dark and switch subtraction on."""
        y_avg = _live_proc["last_avg"]
        if y_avg is None:
            app.status.toast("Start Live View first: the dark is taken from the frame on the plot", kind="warning")
            return
        _live_proc["dark"] = np.array(y_avg, dtype=float, copy=True)
        _live_proc["dark_it"] = _live_stats["it"]
        app.live_dark_check.state(["!disabled"])
        app.live_subtract_dark.set(True)
        _refresh_dark_caption()
        app.status.toast(f"Dark captured ({y_avg.size} px); it is subtracted from the frames that follow")
        _re_present()

    def _re_present():
        """Show a changed setting on the frame already on the plot, streaming or not."""
        y_avg = _live_proc["last_avg"]
        x_values = _live_proc["x"]
        if y_avg is None or x_values is None:
            return
        try:
            _present(x_values, y_avg, _live_proc["last_saturated"])
            app._live_blit.draw()
        except Exception as exc:
            app._post_error("Live View", exc)

    def _save_live_frame():
        """Write the processed and the raw frame to a CSV the operator chooses."""
        processed = _live_proc["last_processed"]
        raw = _live_proc["last_raw"]
        if processed is None or raw is None:
            app.status.toast("No frame to save yet: start Live View first", kind="warning")
            return
        sn = str(getattr(app, "sn", "") or "Unknown").replace(" ", "_")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            app.RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
            initial_dir = str(app.RESULTS_ROOT)
        except Exception:
            initial_dir = None
        path = filedialog.asksaveasfilename(
            title="Save live frame", defaultextension=".csv", initialdir=initial_dir,
            initialfile=f"LiveFrame_{sn}_{stamp}.csv", filetypes=[("CSV", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        it = _live_stats["it"]
        depth = _average_depth()
        dark_used = bool(app.live_subtract_dark.get() and _live_proc["dark"] is not None)
        try:
            with open(path, "w", newline="", encoding="utf-8") as handle:
                handle.write("# SciLab live frame\n")
                handle.write(f"# serial_number: {sn}\n")
                handle.write(f"# saved: {datetime.now().isoformat(timespec='seconds')}\n")
                handle.write(f"# integration_time_ms: {it if it is not None else 'unknown'}\n")
                handle.write(f"# averaged_frames: {min(depth, max(1, len(_live_proc['frames'])))}\n")
                handle.write(f"# dark_subtracted: {'yes' if dark_used else 'no'}\n")
                writer = csv.writer(handle)
                writer.writerow(["pixel", "counts", "raw_counts"])
                for index, (value, raw_value) in enumerate(zip(processed.tolist(), raw.tolist())):
                    writer.writerow([index, f"{value:.3f}", f"{raw_value:.3f}"])
        except Exception as exc:
            app._post_error("Save frame", exc)
            return
        app.status.toast(f"Frame saved: {Path(path).name}")

    def _refresh_live_fps():
        readout = _readout()
        if readout is not None:
            fps = _live_stats["fps"]
            readout["fps"].configure(text=f"{fps:.1f} fps" if fps else "—")

    def _update_live_readout(y_values, saturated, now):
        """Main thread, at most every _READOUT_MIN_S: the numbers beside the plot."""
        readout = _readout()
        if readout is None or now - _live_stats["last_readout"] < _READOUT_MIN_S:
            return
        _live_stats["last_readout"] = now
        if y_values.size:
            peak = float(np.nanmax(y_values))
            readout["peak"].configure(text=f"{peak:,.0f}")
            readout["pixel"].configure(text=str(int(np.nanargmax(y_values))))
            readout["scale"].configure(text=f"{100.0 * peak / _FULL_SCALE:.1f} %")
        readout["sat"].configure(text="SATURATED" if saturated else "OK",
                                 foreground=Colors.DANGER if saturated else Colors.SUCCESS)
        _refresh_live_fps()
        _refresh_live_it()

    def _render_live_frame(payload):
        """Main thread: fold the newest raw frame into the processing chain and show it."""
        x_values, y_raw = payload
        # Saturation is a property of the raw detector frame: averaging and a
        # dark would hide clipped pixels.
        saturated = bool(np.any(y_raw > _LIVE_CLAMP))
        y_avg = _average(y_raw)
        _live_proc["x"] = x_values
        _live_proc["last_raw"] = y_raw
        _live_proc["last_avg"] = y_avg
        _live_proc["last_saturated"] = saturated
        _present(x_values, y_avg, saturated)

    def _present(x_values, y_avg, saturated):
        """Main thread: push a processed frame into the existing artists."""
        y_values = _compose(y_avg)
        _live_proc["last_processed"] = y_values
        raw_over = _live_proc["last_raw"] if _live_proc["last_raw"] is not None else y_values
        # Inverted: the spectrum is drawn last pixel first, on the same 0 -> end axis.
        # The readout follows the plot, so its peak pixel is where the peak is drawn.
        mirrored = app.live_invert_x.get()
        if mirrored:
            y_values = y_values[::-1]
            raw_over = raw_over[::-1]
        _live_view_state["mirrored"] = mirrored
        hint = getattr(app, "live_empty_text", None)
        if hint is not None and hint.get_visible():
            hint.set_visible(False)
            app._live_blit.request_full()
        y_display = np.clip(y_values, None, _LIVE_CLAMP)
        app.live_line.set_data(x_values, y_display)

        if saturated and raw_over.shape == y_display.shape:
            app.live_sat_line.set_data(x_values, np.where(raw_over > _LIVE_CLAMP, y_display, np.nan))
            app.live_sat_line.set_visible(True)
        else:
            app.live_sat_line.set_visible(False)

        if app.live_mark_peak.get() and y_display.size:
            peak_index = int(np.nanargmax(y_display))
            peak_value = float(y_display[peak_index])
            app.live_peak_marker.set_data([peak_index], [peak_value])
            app.live_peak_text.xy = (peak_index, peak_value)
            app.live_peak_text.set_text(f"px {peak_index}")
            app.live_peak_marker.set_visible(True)
            app.live_peak_text.set_visible(True)
        else:
            app.live_peak_marker.set_visible(False)
            app.live_peak_text.set_visible(False)

        sn = getattr(app, 'sn', None)
        title = f"Live Spectrum - Serial Number: {sn}" if app.spec and sn else "Live Spectrum"
        if saturated:
            title += "  [SATURATED]"
        if title != _live_view_state["title"]:
            # set_title() invalidates the layout; only pay for it when the text changes.
            app.live_ax.set_title(title, **plot_title_font(color=Colors.DANGER if saturated else Colors.TEXT_PRIMARY))
            _live_view_state["title"] = title
            app._live_blit.request_full()

        if not app.live_limits_locked:
            xlim = _x_limits(len(x_values))
            if xlim != _live_view_state["xlim"]:
                app.live_ax.set_xlim(*xlim)
                _live_view_state["xlim"] = xlim
                app._live_blit.request_full()
            ylim = _y_limits(y_display, _live_view_state["ylim"])
            if ylim != _live_view_state["ylim"]:
                app.live_ax.set_ylim(*ylim)
                _live_view_state["ylim"] = ylim
                app._live_blit.request_full()

        _update_live_readout(y_values, saturated, time.perf_counter())

    def _x_limits(npoints):
        """Pixel-axis limits for a trace of ``npoints``: 0 -> end, whether or not the trace is inverted."""
        xmax = max(10, npoints - 1)
        return 0, xmax

    def _y_limits(y_display, current=None):
        """Counts-axis limits for the trace, keeping ``current`` while the trace still fits it.

        Hysteresis: the top grows immediately and shrinks only when the trace has
        dropped well below it. Fewer limit changes = fewer full redraws and a
        steadier axis for the operator's eye. On the logarithmic axis the bottom
        is the decade at or below the smallest positive count (the dark level,
        usually); it drops at once but rises only once the trace is clearly into
        the next decade, so noise on a decade boundary cannot force a full redraw
        every frame.
        """
        # Headroom above the peak: more when the peak mark and its label sit there.
        headroom = 1.22 if app.live_mark_peak.get() else 1.1
        top = max(1000.0, float(np.nanmax(y_display)) * headroom if y_display.size else 1.0)
        bottom = raised = 0.0
        log = app.live_log_y.get()
        if log:
            positive = y_display[y_display > 0]
            lowest = max(1.0, float(np.nanmin(positive))) if positive.size else 1.0
            bottom = 10.0 ** np.floor(np.log10(lowest))
            raised = 10.0 ** np.floor(np.log10(max(1.0, lowest / 2.0)))
        elif y_display.size:
            # A dark-subtracted trace dips below zero by its noise: show it
            # rather than clip it at the axis.
            lowest = float(np.nanmin(y_display))
            if lowest < 0.0:
                bottom = lowest * 1.1
        if current is None:
            return bottom, top
        current_bottom, current_top = current
        if 0.6 * current_top <= top <= current_top:
            top = current_top
        if bottom > current_bottom:
            if log:
                bottom = max(current_bottom, raised)
            elif bottom < 0.0 or -current_bottom <= 0.15 * top:
                # A small negative floor stays put while the noise plays around zero.
                bottom = current_bottom
        return bottom, top

    def _idle_y_limits():
        return (1.0 if app.live_log_y.get() else 0.0), _FULL_SCALE

    def _apply_live_axes(reset_zoom=False):
        """Main thread: apply the chart settings, and the full range unless the operator has zoomed in.

        Works from the trace on the plot, so it also applies while Live View is
        stopped. Inverting mirrors the traces already drawn, and a zoomed view
        moves to the mirrored range so the same part of the spectrum stays in
        view; the counts range is recomputed when the scale changes, since a
        linear range from zero has no logarithmic equivalent.
        """
        ax = app.live_ax
        mirror = app.live_invert_x.get() != _live_view_state["mirrored"]
        if mirror:
            for line in (app.live_line, app.live_sat_line):
                line_x, line_y = line.get_data()
                line.set_data(line_x, np.asarray(line_y, dtype=float)[::-1])
            _live_view_state["mirrored"] = app.live_invert_x.get()
        x_data, y_data = app.live_line.get_data()
        y_data = np.asarray(y_data, dtype=float)
        npoints = len(x_data) or int(getattr(app, "npix", 0) or 0) or 2048
        if mirror and app.live_peak_marker.get_visible():
            # The peak mark travels with the trace it sits on.
            marker_x, marker_y = app.live_peak_marker.get_data()
            if len(marker_x):
                flipped = npoints - 1 - float(marker_x[0])
                app.live_peak_marker.set_data([flipped], [marker_y[0]])
                app.live_peak_text.xy = (flipped, float(marker_y[0]))
                app.live_peak_text.set_text(f"px {int(round(flipped))}")
        log = app.live_log_y.get()
        rescaled = ax.get_yscale() != ("log" if log else "linear")
        if rescaled:
            set_counts_scale(ax, log)
        if reset_zoom or not app.live_limits_locked:
            app.live_limits_locked = False
            xlim = _x_limits(npoints)
            ylim = _y_limits(y_data) if y_data.size else _idle_y_limits()
            ax.set_xlim(*xlim)
            ax.set_ylim(*ylim)
            _live_view_state["xlim"], _live_view_state["ylim"] = xlim, ylim
        else:
            if mirror:
                low, high = sorted(ax.get_xlim())
                last = npoints - 1
                ax.set_xlim(last - high, last - low)
            if rescaled:
                ax.set_ylim(*(_y_limits(y_data) if y_data.size else _idle_y_limits()))
        # Drop the zoom history: Home/Back must not bring back a view of the old trace or scale.
        app.live_toolbar.update()
        app._live_blit.request_full()
        app.live_canvas.draw_idle()

    def _reset_live_view():
        """Reset Zoom: the whole trace again, in the direction and scale the chart settings ask for."""
        try:
            _apply_live_axes(reset_zoom=True)
        except Exception as exc:
            app._post_error("Reset Zoom", exc)

    def _open_chart_settings():
        """The toolbar's sliders button: display options for the live plot, applied as they are ticked."""
        window = getattr(app, "live_chart_settings", None)
        if window is not None and window.winfo_exists():
            window.deiconify()
            window.lift()
            window.focus_set()
            return window

        # Not modal: the plot keeps streaming behind it and shows each change at once.
        window = tk.Toplevel(app)
        window.title("Chart settings")
        window.transient(app)
        window.resizable(False, False)
        window.configure(bg=Colors.BG_SURFACE)
        tk.Frame(window, bg=Colors.ACCENT, height=3, bd=0, highlightthickness=0).pack(fill="x", side="top")
        body = ttk.Frame(window, style="Surface.TFrame", padding=Spacing.PAD_LG)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Live View display", style="Strong.TLabel").pack(anchor="w", pady=(0, Spacing.PAD_SM))
        app.live_chart_options = {
            "invert_x": ttk.Checkbutton(body, text="Invert the spectrum (x axis still reads 0 → end)",
                                        variable=app.live_invert_x, command=_apply_live_axes),
            "log_y": ttk.Checkbutton(body, text="Logarithmic y axis (counts)",
                                     variable=app.live_log_y, command=_apply_live_axes),
            "mark_peak": ttk.Checkbutton(body, text="Mark the brightest pixel",
                                         variable=app.live_mark_peak, command=_re_present),
        }
        for check in app.live_chart_options.values():
            check.pack(anchor="w", pady=2)
        ttk.Separator(body, orient="horizontal").pack(fill="x", pady=Spacing.PAD_SM)
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        # matplotlib's own margins tool, which this window replaced on the button.
        ttk.Button(buttons, text="Subplot margins…",
                   command=lambda: NavigationToolbar2Tk.configure_subplots(app.live_toolbar)).pack(side="left")
        ttk.Button(buttons, text="Close", command=window.destroy).pack(side="right", padx=(Spacing.PAD_SM, 0))
        window.bind("<Escape>", lambda _event: window.destroy())
        harmonize(window, Colors.BG_SURFACE)

        # Just above the button that opened it (the toolbar sits under the plot),
        # and on screen even when the bottom of the main window is not.
        anchor = getattr(app.live_toolbar, "_buttons", {}).get("Subplots") or app.live_toolbar
        window.update_idletasks()
        width, height = window.winfo_reqwidth(), window.winfo_reqheight()
        try:
            x = min(anchor.winfo_rootx(), window.winfo_screenwidth() - width)
            # Room for the title bar, which the requested height leaves out, and for the taskbar.
            y = min(anchor.winfo_rooty() - height - logical_px(app, 44),
                    window.winfo_screenheight() - height - logical_px(app, 96))
            window.geometry(f"+{max(0, x)}+{max(0, y)}")
        except tk.TclError:
            pass
        app.live_chart_settings = window
        return window

    def _update_live_plot(self, x, y):
        app._live_redraw.submit((np.asarray(x, dtype=float), np.asarray(y, dtype=float)))


    # Bind live-view helpers before building the UI so button commands capture the correct callables.
    # (toggle_laser lives on SpectroApp so it is available before this tab is built.)
    app.apply_it = apply_it
    app.start_live = start_live
    app.stop_live = stop_live
    app._update_live_plot = MethodType(_update_live_plot, app)
    app._live_loop = _live_loop
    app._live_reset_view = _reset_live_view
    app.live_capture_dark = _capture_dark
    app.live_save_frame = _save_live_frame
    app._live_processing = _live_proc

    # Call the UI builder
    _build_live_tab()
