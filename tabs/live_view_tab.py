"""Live View tab - real-time spectrum display with laser and filter wheel controls."""
import logging
import threading
import time
from types import MethodType
from typing import Dict

import numpy as np
import tkinter as tk
from tkinter import ttk, messagebox
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

from .ui_utils import ScrollableFrame, bind_debounced_configure

LOGGER = logging.getLogger(__name__)

def build(app):
    from .theme import Colors, Fonts, Spacing, configure_matplotlib_style, make_action_button

    # Import constants from app
    OBIS_LASER_MAP = {
        "405": 5,
        "445": 4,
        "488": 3,
        "640": 2,
        "685": 6,
    }
    IT_MIN = 0.2
    IT_MAX = 3000.0

    def _build_live_tab():
        main_frame = ttk.Frame(app.live_tab)
        main_frame.pack(fill="both", expand=True)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(0, weight=1)

        plot_container = ttk.Frame(main_frame)
        plot_container.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

        controls_host = ttk.Frame(main_frame)
        controls_host.grid(row=0, column=1, sticky="nsew", padx=(0, 4), pady=4)
        controls_host.grid_propagate(False)

        controls_scroll = ScrollableFrame(controls_host, y_scroll=True, background=Colors.BG_PRIMARY)
        controls_scroll.pack(fill="both", expand=True)
        controls_container = controls_scroll.content
        controls_grid = ttk.Frame(controls_container)
        controls_grid.pack(fill="both", expand=True)

        # Matplotlib figure
        app.live_fig = Figure(figsize=(12, 7), dpi=100, constrained_layout=True)
        app.live_ax = app.live_fig.add_subplot(111)
        configure_matplotlib_style(app.live_fig, app.live_ax, title="Live Spectrum")

        # Plot lines
        app.live_line, = app.live_ax.plot([], [], lw=1.8, color="#2563eb", label="Signal", alpha=0.9)
        app.live_sat_line, = app.live_ax.plot([], [], lw=2.2, color=Colors.DANGER, label="Saturated", alpha=0.95)
        app.live_sat_line.set_visible(False)

        app.live_ax.legend(loc="upper right", frameon=True, fancybox=True,
                          framealpha=0.92, fontsize=9, edgecolor=Colors.BORDER_LIGHT)

        app.live_canvas = FigureCanvasTkAgg(app.live_fig, master=plot_container)
        app.live_canvas.draw()
        app.live_canvas.get_tk_widget().pack(fill="both", expand=True)

        app.live_toolbar = NavigationToolbar2Tk(app.live_canvas, plot_container)

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
        ttk.Button(btn_frame, text="Reset Zoom", command=app._live_reset_view).pack(side="left")

        # === Laser Controls Section ===
        laser_frame = ttk.LabelFrame(controls_grid, text="Laser Controls", padding=8)
        laser_frame.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        app.laser_vars = {}

        # All lasers in ascending order
        for tag in ["377", "405", "445", "488", "517", "532", "640", "685"]:
            var = tk.BooleanVar(value=False)
            label_text = f"{tag} nm"
            btn = ttk.Checkbutton(
                laser_frame,
                text=label_text,
                variable=var,
                command=lambda t=tag, v=var: app.toggle_laser(t, v.get())
            )
            btn.pack(anchor="w", pady=1)
            app.laser_vars[tag] = var
        
        # Hg_Ar calibration lamp
        var = tk.BooleanVar(value=False)
        btn = ttk.Checkbutton(
            laser_frame,
            text="Hg_Ar",
            variable=var,
            command=lambda t="Hg_Ar", v=var: app.toggle_laser(t, v.get())
        )
        btn.pack(anchor="w", pady=1)
        app.laser_vars["Hg_Ar"] = var

        ttk.Separator(laser_frame, orient="horizontal").pack(fill="x", pady=(6, 4))
        app.check_spec_btn = ttk.Button(
            laser_frame,
            text="Check Spectrometer",
            command=app.run_check_spectrometer,
        )
        app.check_spec_btn.pack(anchor="w", fill="x", pady=(2, 0))

        # === Live Control Section ===
        live_control_frame = ttk.LabelFrame(controls_grid, text="Live View Control", padding=8)
        live_control_frame.grid(row=0, column=1, sticky="nsew", padx=4, pady=4)
        
        app.live_start_btn = make_action_button(live_control_frame, text="Start Live",
                                                command=app.start_live, width=18)
        app.live_start_btn.pack(pady=(0, 6), fill="x")

        app.live_stop_btn = make_action_button(live_control_frame, text="Stop Live",
                                               command=app.stop_live, danger=True, width=18)
        app.live_stop_btn.pack(fill="x")

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
        app.headsensor_status_label = ttk.Label(headsensor_frame, text="Disabled", foreground="gray", font=Fonts.BODY_SMALL)
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
            foreground="gray",
            font=Fonts.BODY_SMALL
        )
        app.fw1_status_label.pack(anchor="w", pady=(0, 3))
        
        # Filter Wheel 2 position status
        app.fw2_status_label = ttk.Label(
            app.headsensor_controls_frame, 
            text="FW 2: Uncertain", 
            foreground="gray",
            font=Fonts.BODY_SMALL
        )
        app.fw2_status_label.pack(anchor="w")

        # === Stage Slots Section ===
        stage_frame = ttk.LabelFrame(controls_grid, text="Stage Slots", padding=8)
        stage_frame.grid(row=2, column=1, sticky="nsew", padx=4, pady=4)

        app.stage_slots_container = ttk.Frame(stage_frame)
        app.stage_slots_container.pack(fill="both", expand=True)

        app.stage_move_status = ttk.Label(stage_frame, text="", foreground="gray",
                                          font=Fonts.BODY_SMALL)
        app.stage_move_status.pack(anchor="w", pady=(4, 0))

        # No-slots placeholder
        app.stage_no_slots_label = ttk.Label(
            app.stage_slots_container,
            text="No stage config loaded.\nSet path in Setup tab.",
            foreground="gray", font=Fonts.BODY_SMALL,
        )
        app.stage_no_slots_label.pack(anchor="w")

        def _on_slot_done(success, message):
            """Callback from stage move thread — schedule UI update on main thread."""
            def _update():
                if success:
                    app.stage_move_status.config(text=message, foreground="green")
                else:
                    app.stage_move_status.config(text=message, foreground="red")
                # Re-enable all slot buttons
                for btn in getattr(app, '_stage_slot_buttons', []):
                    btn.config(state="normal")
                if hasattr(app, '_stage_stop_btn'):
                    app._stage_stop_btn.config(state="disabled")
            app.after(0, _update)

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
            app.stage_move_status.config(text=f"Moving to {slot_name}...", foreground="blue")
            if not app.stage.goto_slot(index, on_done=_on_slot_done):
                app.stage_move_status.config(text="Move command failed", foreground="red")
                for btn in getattr(app, '_stage_slot_buttons', []):
                    btn.config(state="normal")

        def _stop_stage():
            if app.stage is not None:
                app.stage.stop_all()
                app.stage_move_status.config(text="STOPPED", foreground="red")

        def _refresh_stage_slots_ui():
            """Rebuild slot buttons from current stage config."""
            for w in app.stage_slots_container.winfo_children():
                w.destroy()
            app._stage_slot_buttons = []

            if app.stage is None or not app.stage.config.loaded or not app.stage.slots:
                app.stage_no_slots_label = ttk.Label(
                    app.stage_slots_container,
                    text="No stage config loaded.\nSet path in Setup tab.",
                    foreground="gray", font=Fonts.BODY_SMALL,
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

            app._stage_stop_btn = tk.Button(
                app.stage_slots_container,
                text="STOP",
                command=_stop_stage,
                bg=Colors.DANGER, fg=Colors.TEXT_ON_ACCENT,
                font=Fonts.BODY_SMALL_BOLD,
                width=16, state="disabled", relief="flat", cursor="hand2",
            )
            app._stage_stop_btn.pack(anchor="w", pady=(6, 0))

        app._refresh_stage_slots_ui = _refresh_stage_slots_ui

        def _layout_live_tab(width=None, height=None):
            available_width = width or main_frame.winfo_width()
            available_height = height or main_frame.winfo_height()
            if not available_width or not available_height:
                return

            for row in range(5):
                controls_grid.rowconfigure(row, weight=0)
            for col in range(3):
                controls_grid.columnconfigure(col, weight=0)

            if available_width < 1420 or available_height < 860:
                plot_container.grid_configure(row=0, column=0, sticky="nsew", padx=4, pady=(4, 6))
                controls_host.grid_configure(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))
                main_frame.columnconfigure(0, weight=1)
                main_frame.columnconfigure(1, weight=0)
                main_frame.rowconfigure(0, weight=5)
                main_frame.rowconfigure(1, weight=3)
                controls_host.configure(
                    width=max(320, available_width - 16),
                    height=max(240, min(400, int(available_height * 0.40))),
                )

                if available_width < 780:
                    layouts = (
                        (spectrum_frame, 0, 0),
                        (live_control_frame, 1, 0),
                        (laser_frame, 2, 0),
                        (headsensor_frame, 3, 0),
                        (stage_frame, 4, 0),
                    )
                    controls_grid.columnconfigure(0, weight=1)
                    for row in range(5):
                        controls_grid.rowconfigure(row, weight=1)
                else:
                    layouts = (
                        (spectrum_frame, 0, 0),
                        (live_control_frame, 0, 1),
                        (laser_frame, 1, 0),
                        (headsensor_frame, 1, 1),
                        (stage_frame, 2, 0),
                    )
                    controls_grid.columnconfigure(0, weight=1)
                    controls_grid.columnconfigure(1, weight=1)
                    controls_grid.rowconfigure(0, weight=1)
                    controls_grid.rowconfigure(1, weight=1)
                    controls_grid.rowconfigure(2, weight=1)
            else:
                plot_container.grid_configure(row=0, column=0, sticky="nsew", padx=4, pady=4)
                controls_host.grid_configure(row=0, column=1, sticky="nsew", padx=(0, 4), pady=4)
                main_frame.columnconfigure(0, weight=1)
                main_frame.columnconfigure(1, weight=0)
                main_frame.rowconfigure(0, weight=1)
                main_frame.rowconfigure(1, weight=0)
                controls_host.configure(
                    width=max(340, min(420, int(available_width * 0.27))),
                    height=max(280, available_height - 8),
                )
                layouts = (
                    (spectrum_frame, 0, 0),
                    (live_control_frame, 1, 0),
                    (laser_frame, 2, 0),
                    (headsensor_frame, 3, 0),
                    (stage_frame, 4, 0),
                )
                controls_grid.columnconfigure(0, weight=1)
                for row in range(5):
                    controls_grid.rowconfigure(row, weight=1)

            for widget, row, column in layouts:
                widget.grid_configure(row=row, column=column, sticky="nsew", padx=4, pady=4)

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
                    foreground="green"
                )
            elif fw_num == 2:
                app.fw2_position = position
                app.fw2_status_label.config(
                    text=f"FW 2: Position {position}",
                    foreground="green"
                )
        
        def execute_filterwheel_command(*args):
            """Execute the selected command on the selected filter wheel (auto-triggered on selection)."""
            try:
                # Get selected command
                command = app.filterwheel_cmd_var.get()
                
                if not command:
                    # Empty selection, just return
                    return
                
                app._update_ports_from_ui()
                
                # Get selected filter wheel
                fw_num = app.filterwheel_var.get()
                
                # Parse command and execute
                if command.startswith("Position "):
                    # Extract position number
                    pos = int(command.split(" ")[1])
                    success = app.filterwheel.set_filterwheel(fw_num, pos)
                    if success or "timeout" in app.filterwheel.serial_status["hst"][-1].lower():
                        # Update position status on success (silent operation)
                        update_position_status(fw_num, pos)
                
                elif command == "Test":
                    success = app.filterwheel.test_filterwheel(fw_num)
                    if success or "timeout" in app.filterwheel.serial_status["hst"][-1].lower():
                        # Test command returns to position 1 (silent operation)
                        update_position_status(fw_num, 1)
                
                elif command == "Reset":
                    success = app.filterwheel.reset_filterwheel(fw_num)
                    if success or "timeout" in app.filterwheel.serial_status["hst"][-1].lower():
                        # Reset command returns to position 1 (silent operation)
                        update_position_status(fw_num, 1)
                
                # Clear the selection after execution
                app.filterwheel_cmd_var.set("")
                
            except Exception as e:
                # Silent error handling - just clear the selection
                app.filterwheel_cmd_var.set("")
        
        # Bind the command execution to dropdown selection changes
        app.filterwheel_cmd_var.trace('w', execute_filterwheel_command)
        
        def toggle_headsensor():
            """Toggle Head Sensor on/off with automatic detection."""
            if app.headsensor_enabled.get():
                # User turned it ON - query device
                try:
                    app._update_ports_from_ui()
                    
                    # Update status to "Checking..."
                    app.headsensor_status_label.config(text="Checking connection...", foreground="blue")
                    app.update_idletasks()
                    
                    # Query device ID with "?" command
                    success, device_id = app.filterwheel.query_device_id()
                    
                    if success and device_id.startswith("Pan"):
                        # Valid Pandora device detected
                        app.headsensor_status_label.config(
                            text=f"✓ Connected: {device_id} - Resetting filterwheels...", 
                            foreground="blue"
                        )
                        app.update_idletasks()
                        
                        # === RESET BOTH FILTERWHEELS 1 AND 2 ===
                        # This ensures filterwheels are at home position (Position 1) on enable
                        print("🔄 Resetting Filterwheel 1...")
                        fw1_reset = app.filterwheel.reset_filterwheel(1)
                        if fw1_reset:
                            print("✓ Filterwheel 1 reset successful")
                            app.fw1_position = 1
                            update_position_status(1, 1)
                        else:
                            print("⚠️ Filterwheel 1 reset may have timed out (common behavior)")
                            app.fw1_position = 1  # Assume success even on timeout
                            update_position_status(1, 1)
                        
                        print("🔄 Resetting Filterwheel 2...")
                        fw2_reset = app.filterwheel.reset_filterwheel(2)
                        if fw2_reset:
                            print("✓ Filterwheel 2 reset successful")
                            app.fw2_position = 1
                            update_position_status(2, 1)
                        else:
                            print("⚠️ Filterwheel 2 reset may have timed out (common behavior)")
                            app.fw2_position = 1  # Assume success even on timeout
                            update_position_status(2, 1)
                        
                        # Update final status
                        app.headsensor_status_label.config(
                            text=f"✓ {device_id}", 
                            foreground="green"
                        )
                        
                        # Show filter wheel controls
                        app.headsensor_controls_frame.pack(anchor="w", pady=(4, 0), fill="x")
                        
                        # Reset to default selections
                        app.filterwheel_var.set(1)  # Default to Filter Wheel 1
                        app.filterwheel_cmd_var.set("")  # Clear command selection
                        
                        print("✅ Head Sensor enabled with both filterwheels reset to Position 1")
                    else:
                        # Not a valid device or no response
                        app.headsensor_enabled.set(False)  # Turn off the switch
                        app.headsensor_status_label.config(
                            text=f"✗ No Head Sensor connected (response: {device_id})", 
                            foreground="red"
                        )
                        app.headsensor_controls_frame.pack_forget()
                        messagebox.showwarning(
                            "Head Sensor", 
                            f"No Head Sensor connected.\n\nExpected device ID starting with 'Pan', got: {device_id}\n\nPlease check:\n• COM port configuration in Setup tab\n• Device power and connections"
                        )
                except Exception as e:
                    # Error during query
                    app.headsensor_enabled.set(False)  # Turn off the switch
                    app.headsensor_status_label.config(
                        text=f"✗ Error: {str(e)}", 
                        foreground="red"
                    )
                    app.headsensor_controls_frame.pack_forget()
                    messagebox.showerror("Head Sensor", f"Failed to query Head Sensor:\n\n{str(e)}")
            else:
                # User turned it OFF
                app.headsensor_status_label.config(text="Disabled", foreground="gray")
                app.headsensor_controls_frame.pack_forget()
                # Reset selections and positions
                app.filterwheel_var.set(1)
                app.filterwheel_cmd_var.set("")
                app.fw1_position = None
                app.fw2_position = None
                app.fw1_status_label.config(text="FW 1: Uncertain", foreground="gray")
                app.fw2_status_label.config(text="FW 2: Uncertain", foreground="gray")

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

        # If a measurement is in-flight, wait briefly
        try:
            if getattr(app.spec, 'measuring', False):
                t0 = time.time()
                while getattr(app.spec, 'measuring', False) and time.time() - t0 < 3.0:
                    try:
                        app.spec.wait_for_measurement()
                        break
                    except Exception:
                        time.sleep(0.05)
        except Exception:
            pass

        # Apply now
        try:
            app._it_updating = True
            app.spec.set_it(it)
            messagebox.showinfo("Integration", f"Applied IT = {it:.3f} ms")
        except Exception as e:
            messagebox.showerror("Apply IT", str(e))
        finally:
            app._it_updating = False
            try:
                app.apply_it_btn.state(["!disabled"])  # re-enable
            except Exception:
                pass


    def start_live():
        if not app.spec:
            messagebox.showwarning("Spectrometer", "Not connected.")
            return
        if app.live_running.is_set():
            return
        app.live_running.set()
        app.live_thread = threading.Thread(target=app._live_loop, daemon=True)
        app.live_thread.start()

    def stop_live():
        app.live_running.clear()

    def _live_loop():
        while app.live_running.is_set():
            try:
                # Start one frame
                app.spec.measure(ncy=1)
                # Wait for frame to complete
                app.spec.wait_for_measurement()

                # Apply any deferred IT safely after the completed frame
                if app._pending_it is not None:
                    try:
                        it_to_apply = app._pending_it
                        app._pending_it = None
                        app._it_updating = True
                        app.spec.set_it(it_to_apply)
                        try:
                            app.title(f"Applied IT={it_to_apply:.3f} ms")
                        except Exception:
                            pass
                    except Exception as e:
                        app._post_error("Apply IT (deferred)", e)
                    finally:
                        app._it_updating = False
                        try:
                            app.apply_it_btn.state(["!disabled"])  # if exists
                        except Exception:
                            pass

                # After IT changes (or none), fetch data and draw
                raw = getattr(app.spec, "rcm", None)
                if raw is None or (hasattr(raw, '__len__') and len(raw) == 0):
                    time.sleep(0.05)
                    continue
                y = np.array(raw, dtype=float)
                if y.size == 0 or not np.all(np.isfinite(y)):
                    time.sleep(0.05)
                    continue
                x = np.arange(len(y))
                app.npix = len(y)
                app.data.npix = app.npix
                app._update_live_plot(x, y)

            except Exception as e:
                app._post_error("Live error", e)
                break


    def _update_live_plot(self, x, y):
        app._pending_live_plot = (np.asarray(x, dtype=float), np.asarray(y, dtype=float))
        if getattr(app, "_live_plot_redraw_id", None) is not None:
            return

        CLAMP = 65000  # counts ceiling for display

        def update():
            app._live_plot_redraw_id = None
            pending = getattr(app, "_pending_live_plot", None)
            if pending is None:
                return
            x_values, y_values = pending
            saturated = np.any(y_values > CLAMP)
            y_display = np.clip(y_values, None, CLAMP)
            app.live_line.set_data(x_values, y_display)

            if saturated:
                y_sat = np.where(y_values > CLAMP, CLAMP, np.nan)
                app.live_sat_line.set_data(x_values, y_sat)
                app.live_sat_line.set_visible(True)
                app.live_line.set_color("#1f77b4")
            else:
                app.live_sat_line.set_visible(False)
                app.live_line.set_color("#1f77b4")

            sn = getattr(app, 'sn', None)
            title = f"Live Spectrum - Serial Number: {sn}" if app.spec and sn else "Live Spectrum"
            if saturated:
                title += "  [SATURATED]"
            app.live_ax.set_title(title, color="red" if saturated else "black")

            if not app.live_limits_locked:
                app.live_ax.set_xlim(0, max(10, len(x_values)-1))
                ymax = float(np.nanmax(y_display)) if y_display.size else 1.0
                app.live_ax.set_ylim(0, max(1000, ymax * 1.1))

            if getattr(app, "_is_tab_visible", lambda _w: True)(app.live_tab):
                app.live_fig.canvas.draw_idle()

        app._live_plot_redraw_id = app.after(16, update)


    def toggle_laser(self, tag: str, turn_on: bool):
        try:
            # make sure we use the latest COM port entries
            app._update_ports_from_ui()
            # open the right serial port lazily
            app.lasers.ensure_open_for_tag(tag)

            if tag in OBIS_LASER_MAP:
                ch = OBIS_LASER_MAP[tag]
                if turn_on:
                    watts = float(app._get_power(tag))
                    app.lasers.obis_set_power(ch, watts)
                    app.lasers.obis_on(ch)
                else:
                    app.lasers.obis_off(ch)

            elif tag == "377":
                if turn_on:
                    val = float(app._get_power(tag))
                    mw = val * 1000.0 if val <= 0.3 else val
                    app.lasers.cube_on(power_mw=mw)
                else:
                    app.lasers.cube_off()

            elif tag == "517":
                if turn_on:
                    app.lasers.relay_on(2)
                else:
                    app.lasers.relay_off(2)

            elif tag == "532":
                if turn_on:
                    app.lasers.relay_on(1)
                else:
                    app.lasers.relay_off(1)
            
            elif tag == "640":
                ch = OBIS_LASER_MAP[tag]
                if turn_on:
                    watts = float(app._get_power(tag))
                    app.lasers.obis_set_power(ch, watts)
                    app.lasers.obis_on(ch)
                else:
                    app.lasers.obis_off(ch)
            
            elif tag == "685":
                # New 685 nm laser on OBIS
                ch = OBIS_LASER_MAP[tag]
                if turn_on:
                    watts = float(app._get_power(tag))
                    app.lasers.obis_set_power(ch, watts)
                    app.lasers.obis_on(ch)
                else:
                    app.lasers.obis_off(ch)

        except Exception as e:
            app._post_error(f"Laser {tag}", e)


    # Bind live-view helpers before building the UI so button commands capture the correct callables.
    app.apply_it = apply_it
    app.start_live = start_live
    app.stop_live = stop_live
    app.toggle_laser = MethodType(toggle_laser, app)
    app._update_live_plot = MethodType(_update_live_plot, app)
    app._live_loop = _live_loop

    # Call the UI builder
    _build_live_tab()
