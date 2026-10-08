"""Live View tab - real-time spectrum display with laser and filter wheel controls."""
import logging
import threading
import time
from types import MethodType

import numpy as np
import tkinter as tk
from tkinter import ttk, messagebox
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

from .theme import (Colors, Fonts, Spacing, attach_hover, bind_figure_margins,
                    configure_matplotlib_style, logical_px, make_action_button,
                    mount_toolbar, plot_title_font, relax_canvas_size, screen_dpi,
                    source_style, style_legend)
from .ui_utils import BlitRenderer, ScrollableFrame, ThrottledRedraw, Tooltip, bind_debounced_configure, run_in_worker

LOGGER = logging.getLogger(__name__)

_LIVE_CLAMP = 65000        # counts ceiling for display
_LIVE_MIN_FRAME_S = 0.04   # cap the acquisition loop at ~25 frames/s
_FULL_SCALE = 65535.0      # 16-bit detector full scale, for the readout's percentage
_READOUT_MIN_S = 0.1       # the numeric readout refreshes at most 10x per second


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

        style_legend(app.live_ax, loc="upper right")

        app.live_canvas = FigureCanvasTkAgg(app.live_fig, master=plot_container)
        relax_canvas_size(app.live_canvas, 620, 340, dpi=screen_dpi(app))
        bind_figure_margins(app.live_canvas, app.live_fig)
        # Toolbar first, canvas second: see mount_toolbar.
        app.live_toolbar = mount_toolbar(app.live_canvas, plot_container, NavigationToolbar2Tk)
        app.live_canvas.get_tk_widget().pack(side="top", fill="both", expand=True)

        # Blit only the two trace lines over a cached background; a full draw is
        # requested when limits/title change, on resize, or after a toolbar zoom.
        app._live_blit = BlitRenderer(app.live_canvas, app.live_ax, [app.live_line, app.live_sat_line])
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
        app.it_entry = ttk.Entry(spectrum_frame, width=15)
        app.it_entry.insert(0, "2.4")
        app.it_entry.pack(anchor="w", pady=(2, 6))

        btn_frame = ttk.Frame(spectrum_frame)
        btn_frame.pack(anchor="w", fill="x")
        app.apply_it_btn = ttk.Button(btn_frame, text="Apply IT", command=app.apply_it)
        app.apply_it_btn.pack(side="left", padx=(0, 4))
        Tooltip(app.it_entry, "Detector integration (exposure) time in milliseconds. "
                              "While Live View runs the new value is applied between frames.")
        ttk.Button(btn_frame, text="Reset Zoom", command=app._live_reset_view).pack(side="left")

        # === Laser Controls Section ===
        laser_frame = ttk.LabelFrame(controls_grid, text="Laser Controls", padding=8)
        laser_frame.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)

        # All sources in ascending order; variables are shared app-wide. Two
        # columns: the list is half as tall and the column stays scroll-free.
        laser_grid = ttk.Frame(laser_frame)
        laser_grid.pack(fill="x")
        laser_grid.columnconfigure(0, weight=1)
        laser_grid.columnconfigure(1, weight=1)
        for index, tag in enumerate(app.laser_tags()):
            var = app.laser_var(tag)
            label_text = "Hg_Ar" if tag == "Hg_Ar" else f"{tag} nm"
            btn = ttk.Checkbutton(
                laser_grid,
                text=label_text,
                variable=var,
                style=source_style(laser_grid, tag, "Bold.TCheckbutton"),
                command=lambda t=tag, v=var: app.toggle_laser(t, v.get())
            )
            btn.grid(row=index // 2, column=index % 2, sticky="w", padx=(0, Spacing.PAD_SM), pady=2)

        ttk.Separator(laser_frame, orient="horizontal").pack(fill="x", pady=(Spacing.PAD_SM, Spacing.PAD_XS))
        app.check_spec_btn = ttk.Button(
            laser_frame,
            text="Check Spectrometer",
            command=app.run_check_spectrometer,
        )
        app.check_spec_btn.pack(anchor="w", fill="x", pady=(2, 0))
        Tooltip(app.check_spec_btn, "Auto-exposes on the brightest line, fits a super-Gaussian and reports "
                                    "centre, width and shape. Saves a CSV, a plot and a .ddf next to the results.")

        # === Live Control Section ===
        live_control_frame = ttk.LabelFrame(controls_grid, text="Live View Control", padding=8)
        live_control_frame.grid(row=0, column=1, sticky="nsew", padx=4, pady=4)

        app.live_start_btn = make_action_button(live_control_frame, text="Start Live",
                                                command=app.start_live, width=18)
        app.live_start_btn.pack(pady=(0, 6), fill="x")

        app.live_stop_btn = make_action_button(live_control_frame, text="Stop Live",
                                               command=app.stop_live, danger=True, width=18)
        app.live_stop_btn.pack(fill="x")

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

            if available_width >= logical_px(app, 1240) and available_height >= logical_px(app, 560):
                # Plot on the left, one column of cards on the right.
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
                # Plot on top, the cards in a strip below it.
                plot_container.grid_configure(row=0, column=0, sticky="nsew", padx=gutter, pady=(gutter, gutter // 2))
                controls_host.grid_configure(row=1, column=0, sticky="nsew", padx=gutter, pady=(0, gutter))
                main_frame.columnconfigure(0, weight=1)
                main_frame.columnconfigure(1, weight=0)
                main_frame.rowconfigure(0, weight=1)
                main_frame.rowconfigure(1, weight=0)
                controls_host.configure(
                    width=max(logical_px(app, 320), available_width - 2 * gutter),
                    height=max(logical_px(app, 220), min(logical_px(app, 380), int(available_height * 0.40))),
                )
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

    def apply_it():
        if not app.spec:
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
            # non-blocking toast via title/status
            try:
                app.title(f"Queued IT={it:.3f} ms (will apply after current frame)")
            except Exception:
                pass
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
        if _live_stats["it"] is None:
            # Nothing applied yet this session: the entry is the best statement
            # of the exposure in force (apply_it keeps it current from here on).
            try:
                _live_stats["it"] = max(IT_MIN, min(IT_MAX, float(app.it_entry.get())))
            except (ValueError, tk.TclError):
                pass
        _live_stats["fps"] = None
        _live_stats["last_frame"] = None
        _set_live_status("Streaming", Colors.SUCCESS)
        _refresh_live_it()
        app.live_running.set()
        app.live_thread = threading.Thread(target=app._live_loop, daemon=True, name="live-view")
        app.live_thread.start()

    def stop_live():
        app.live_running.clear()
        app.request_spectrometer_stop()

    def _live_loop():
        """Acquisition thread. Never touches Tk directly: frames go through the pump."""
        try:
            while app.live_running.is_set():
                frame_started = time.perf_counter()
                try:
                    app.spec.measure(ncy=1)
                    app.spec.wait_for_measurement()
                    if not app.live_running.is_set():
                        break
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

    def _reenable_apply_it():
        try:
            app.apply_it_btn.state(["!disabled"])
        except Exception:
            pass

    _live_view_state = {"title": None, "xmax": None, "ymax": None}
    # Readout state. ``it`` is the last exposure applied (ms); ``fps`` is a
    # smoothed display frame rate maintained by the acquisition thread.
    _live_stats = {"it": None, "fps": None, "last_frame": None, "last_readout": 0.0}

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
        """Main thread: push the newest frame into the existing artists."""
        x_values, y_values = payload
        saturated = bool(np.any(y_values > _LIVE_CLAMP))
        y_display = np.clip(y_values, None, _LIVE_CLAMP)
        app.live_line.set_data(x_values, y_display)

        if saturated:
            app.live_sat_line.set_data(x_values, np.where(y_values > _LIVE_CLAMP, _LIVE_CLAMP, np.nan))
            app.live_sat_line.set_visible(True)
        else:
            app.live_sat_line.set_visible(False)

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
            xmax = max(10, len(x_values) - 1)
            if xmax != _live_view_state["xmax"]:
                app.live_ax.set_xlim(0, xmax)
                _live_view_state["xmax"] = xmax
                app._live_blit.request_full()
            peak = float(np.nanmax(y_display)) if y_display.size else 1.0
            ymax = max(1000.0, peak * 1.1)
            current = _live_view_state["ymax"]
            # Hysteresis: grow immediately, shrink only when the trace has dropped well
            # below the current ceiling. Fewer limit changes = fewer full redraws and
            # a steadier axis for the operator's eye.
            if current is None or ymax > current or ymax < 0.6 * current:
                app.live_ax.set_ylim(0, ymax)
                _live_view_state["ymax"] = ymax
                app._live_blit.request_full()

        _update_live_readout(y_values, saturated, time.perf_counter())

    def _update_live_plot(self, x, y):
        app._live_redraw.submit((np.asarray(x, dtype=float), np.asarray(y, dtype=float)))


    # Bind live-view helpers before building the UI so button commands capture the correct callables.
    # (toggle_laser lives on SpectroApp so it is available before this tab is built.)
    app.apply_it = apply_it
    app.start_live = start_live
    app.stop_live = stop_live
    app._update_live_plot = MethodType(_update_live_plot, app)
    app._live_loop = _live_loop

    # Call the UI builder
    _build_live_tab()
