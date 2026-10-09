"""Setup tab - spectrometer, COM port, laser, and head sensor configuration."""
import json
import logging
import os
import traceback

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None  # type: ignore[assignment]

from app import get_resource_path
from spectrometer_loader import (
    SPECTROMETER_TYPE_OPTIONS,
    connect_spectrometer as connect_backend_spectrometer,
    discover_spectrometers,
    suggest_default_dll_path,
)
from services.run_progress import source_label
from .theme import Colors, Fonts, Spacing, badge, harmonize, logical_px, source_color, style_listbox
from .ui_utils import ScrollableFrame, Tooltip, bind_debounced_configure, center_over, run_in_worker

LOGGER = logging.getLogger(__name__)

def build(app):
    # Import constants from app
    DEFAULT_COM_PORTS = app.DEFAULT_COM_PORTS
    DEFAULT_ALL_LASERS = app.DEFAULT_ALL_LASERS
    DEFAULT_LASER_POWERS = app.DEFAULT_LASER_POWERS
    SETTINGS_FILE = app.SETTINGS_FILE

    def _is_suggested_path(path):
        """True when this path is one the app filled in, not one the user typed."""
        candidate = (path or "").strip().lower()
        if not candidate:
            return True
        return any(
            candidate == (suggest_default_dll_path(option) or "").strip().lower()
            for option in SPECTROMETER_TYPE_OPTIONS
        )

    # A word beside the dot: a bare red dot said nothing about what was wrong.
    _PORT_STATES = {
        "untested": ("● not tested", Colors.TEXT_MUTED, "Press Test Connect to open the port."),
        "testing": ("● testing…", Colors.INFO, "Opening the port…"),
        "open": ("● open", Colors.SUCCESS, "The port opened and was released again."),
        "failed": ("● not found", Colors.DANGER,
                   "The port could not be opened: not present, or in use by another program."),
        "unset": ("● no port", Colors.TEXT_MUTED, "No COM port entered."),
    }
    _port_tips = {}

    def _set_port_status(label, state, detail=None):
        text, color, hint = _PORT_STATES[state]
        label.config(text=text, foreground=color)
        tip = _port_tips.get(label)
        if tip is None:
            tip = _port_tips[label] = Tooltip(label, hint)
        tip.text = detail or hint

    app.set_port_status = _set_port_status

    def _build_setup_tab():
        main_frame = ttk.Frame(app.setup_tab)
        main_frame.pack(fill="both", expand=True, padx=Spacing.PAD_LG, pady=Spacing.PAD_MD)

        # The form never grows past a comfortable reading width: on a wide
        # screen it sits centred in two columns instead of stretching every
        # entry across the whole monitor.
        scrollable = ScrollableFrame(main_frame, x_scroll=True, y_scroll=True, background=Colors.BG_PRIMARY,
                                     max_content_width=logical_px(app, 1560))
        scrollable.pack(fill="both", expand=True)
        frame = scrollable.content

        # Title row, with the page actions on the right.
        header = ttk.Frame(frame)
        header.pack(fill="x", pady=(0, Spacing.PAD_MD))
        header.columnconfigure(0, weight=1)
        title_frame = ttk.Frame(header)
        title_frame.grid(row=0, column=0, sticky="w")
        ttk.Label(title_frame, text="System Configuration",
                  font=Fonts.H1).pack(anchor="w")
        ttk.Label(title_frame, text="Instrument, serial ports, light sources and bench options.",
                  style="Secondary.TLabel").pack(anchor="w")
        header_actions = ttk.Frame(header)
        header_actions.grid(row=0, column=1, sticky="e")
        ttk.Button(header_actions, text="Load Settings", command=load_settings_into_ui,
                   style='SetupButton.TButton').pack(side="right")
        ttk.Button(header_actions, text="Save Settings", command=save_settings,
                   style='SetupAction.TButton').pack(side="right", padx=(0, Spacing.PAD_SM))

        # Two columns on a wide window, one below the other otherwise
        # (_layout_setup); every card keeps its column either way.
        columns = ttk.Frame(frame)
        columns.pack(fill="both", expand=True)
        left_col = ttk.Frame(columns)
        right_col = ttk.Frame(columns)
        card_pack = dict(fill="x", pady=(0, Spacing.SECTION_GAP))

        # ============ SPECTROMETER SECTION ============
        spec_group = ttk.LabelFrame(left_col, text="Spectrometer Configuration",
                                    style='Setup.TLabelframe')
        spec_group.pack(**card_pack)

        # Configure grid columns
        spec_group.columnconfigure(1, weight=1)

        ttk.Label(spec_group, text="Spectrometer Type:",
                 style='SetupLabel.TLabel').grid(row=0, column=0, sticky="w", padx=8, pady=8)
        app.spec_type_var = tk.StringVar(value=getattr(app.hw, "spectrometer_type", "Auto"))
        app.spec_type_combo = ttk.Combobox(
            spec_group,
            textvariable=app.spec_type_var,
            values=SPECTROMETER_TYPE_OPTIONS,
            state="readonly",
            width=18,
            font=Fonts.BODY,
        )
        app.spec_type_combo.grid(row=0, column=1, sticky="w", padx=8, pady=8)

        def on_spec_type_change(*_args):
            selected = app.spec_type_var.get().strip()
            current = app.dll_entry.get()
            if selected == "Auto":
                # Auto resolves the DLL per backend as it probes, so a path
                # left over from a previous selection only gets in the way --
                # it used to make Auto probe that one backend and nothing else.
                # A path the operator typed is theirs, so it stays.
                if _is_suggested_path(current):
                    app.dll_entry.delete(0, "end")
                return
            if current.strip():
                return
            suggested = suggest_default_dll_path(selected)
            if suggested:
                app.dll_entry.delete(0, "end")
                app.dll_entry.insert(0, suggested)

        app.spec_type_combo.bind("<<ComboboxSelected>>",
                                 lambda event: (on_spec_type_change(event), app._update_dll_row_visibility()))

        dll_label = ttk.Label(spec_group, text="Driver DLL / Runtime Path:", style='SetupLabel.TLabel')
        dll_label.grid(row=1, column=0, sticky="w", padx=8, pady=8)
        app.dll_entry = ttk.Entry(spec_group, width=40, font=Fonts.BODY)
        app.dll_entry.grid(row=1, column=1, sticky="ew", padx=8, pady=8)
        dll_browse_btn = ttk.Button(spec_group, text="Browse…", command=browse_dll, style='SetupButton.TButton')
        dll_browse_btn.grid(row=1, column=2, padx=8, pady=8)
        dll_hint = ttk.Label(spec_group, text="Driver: located automatically for the selected type.",
                             foreground=Colors.TEXT_MUTED, font=Fonts.CAPTION_ITALIC)
        dll_hint.grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 6))

        def _update_dll_row_visibility(*_args):
            """Only OcOpt1 needs a path typed in; the other backends ship their DLL,
            and Auto / Simulated never need one, unless the operator typed an override."""
            selected = app.spec_type_var.get().strip()
            typed = app.dll_entry.get().strip()
            custom = bool(typed) and not _is_suggested_path(typed)
            show = selected not in ("Auto", "Simulated") or custom
            for widget in (dll_label, app.dll_entry, dll_browse_btn):
                if show:
                    widget.grid()
                else:
                    widget.grid_remove()
            if show:
                dll_hint.grid_remove()
            else:
                dll_hint.grid()

        app._update_dll_row_visibility = _update_dll_row_visibility
        _update_dll_row_visibility()

        # Status indicator frame
        status_frame = ttk.Frame(spec_group)
        status_frame.grid(row=2, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 8))

        ttk.Label(status_frame, text="Status:",
                 style='SetupLabel.TLabel').pack(side="left", padx=(0, 8))
        app.spec_status = ttk.Label(status_frame, text="● Disconnected",
                                   foreground=Colors.DANGER, style='SetupStatus.TLabel')
        app.spec_status.pack(side="left")

        # Action buttons frame
        button_frame = ttk.Frame(spec_group)
        button_frame.grid(row=3, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 8))

        app.connect_btn = ttk.Button(button_frame, text="Connect", command=connect_spectrometer,
                                     style='SetupAction.TButton', width=15)
        app.connect_btn.pack(side="left", padx=(0, 8))
        Tooltip(app.connect_btn, "Enumerate and connect the selected spectrometer type. Runs in the background; "
                                 "the status bar shows progress. Auto tries each hardware backend in turn.")
        ttk.Button(button_frame, text="Disconnect", command=disconnect_spectrometer,
                  style='SetupButton.TButton', width=15).pack(side="left")

        # ============ TWO-COLUMN LAYOUT FOR COM PORTS AND HEAD SENSOR ============
        ports_headsensor_row = ttk.Frame(left_col)
        ports_headsensor_row.pack(**card_pack)
        ports_headsensor_row.columnconfigure(0, weight=1)
        ports_headsensor_row.columnconfigure(1, weight=1)

        # Left column: COM PORTS
        ports_group = ttk.LabelFrame(ports_headsensor_row, text="COM Port Configuration",
                                    style='Setup.TLabelframe')
        ports_group.grid(row=0, column=0, sticky="nsew", padx=(0, 4))

        # Configure grid
        ports_group.columnconfigure(1, weight=1)

        ttk.Label(ports_group, text="OBIS Laser:",
                 style='SetupLabel.TLabel').grid(row=0, column=0, sticky="e", padx=8, pady=6)
        ttk.Label(ports_group, text="CUBE Laser:",
                 style='SetupLabel.TLabel').grid(row=1, column=0, sticky="e", padx=8, pady=6)
        ttk.Label(ports_group, text="RELAY:",
                 style='SetupLabel.TLabel').grid(row=2, column=0, sticky="e", padx=8, pady=6)

        app.obis_entry = ttk.Entry(ports_group, width=15, font=Fonts.BODY)
        app.cube_entry = ttk.Entry(ports_group, width=15, font=Fonts.BODY)
        app.relay_entry = ttk.Entry(ports_group, width=15, font=Fonts.BODY)
        app.obis_entry.grid(row=0, column=1, padx=8, pady=6, sticky="w")
        app.cube_entry.grid(row=1, column=1, padx=8, pady=6, sticky="w")
        app.relay_entry.grid(row=2, column=1, padx=8, pady=6, sticky="w")

        # Status words (see _set_port_status)
        app.obis_status = ttk.Label(ports_group, font=Fonts.BODY_SMALL_BOLD)
        app.cube_status = ttk.Label(ports_group, font=Fonts.BODY_SMALL_BOLD)
        app.relay_status = ttk.Label(ports_group, font=Fonts.BODY_SMALL_BOLD)
        app.obis_status.grid(row=0, column=2, padx=8, sticky="w")
        app.cube_status.grid(row=1, column=2, padx=8, sticky="w")
        app.relay_status.grid(row=2, column=2, padx=8, sticky="w")
        for label in (app.obis_status, app.cube_status, app.relay_status):
            _set_port_status(label, "untested")

        # Action buttons
        ports_button_frame = ttk.Frame(ports_group)
        ports_button_frame.grid(row=3, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 8))

        refresh_btn = ttk.Button(ports_button_frame, text="Refresh Ports", command=refresh_ports,
                                 style='SetupButton.TButton', width=18)
        refresh_btn.pack(side="left", padx=(0, 8))
        Tooltip(refresh_btn, "List the serial ports Windows currently sees and fill the first four in (F5).")
        app.test_com_btn = ttk.Button(ports_button_frame, text="Test Connect", command=test_com_connect,
                                      style='SetupButton.TButton', width=18)
        app.test_com_btn.pack(side="left")
        Tooltip(app.test_com_btn, "Open and close each laser port to check it exists and is not in use.")

        # Right column: HEAD SENSOR
        headsensor_group = ttk.LabelFrame(ports_headsensor_row, text="Head Sensor Connection",
                                         style='Setup.TLabelframe')
        headsensor_group.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

        # Configure grid
        headsensor_group.columnconfigure(1, weight=1)

        ttk.Label(headsensor_group, text="COM Port:",
                 style='SetupLabel.TLabel').grid(row=0, column=0, sticky="e", padx=8, pady=8)
        app.headsensor_entry = ttk.Entry(headsensor_group, width=15, font=Fonts.BODY)
        app.headsensor_entry.grid(row=0, column=1, padx=8, pady=8, sticky="w")

        app.headsensor_status = ttk.Label(headsensor_group, font=Fonts.BODY_SMALL_BOLD)
        app.headsensor_status.grid(row=0, column=2, padx=8, sticky="w")
        _set_port_status(app.headsensor_status, "untested")

        # Test button
        headsensor_button_frame = ttk.Frame(headsensor_group)
        headsensor_button_frame.grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 8))

        app.test_headsensor_btn = ttk.Button(headsensor_button_frame, text="Test Connect",
                                             command=test_headsensor_connect,
                                             style='SetupButton.TButton', width=18)
        app.test_headsensor_btn.pack(side="left")

        def _layout_ports_and_headsensor(column_width):
            gap = Spacing.PAD_SM
            if column_width < logical_px(app, 700):
                ports_group.grid_configure(row=0, column=0, padx=0, pady=(0, gap))
                headsensor_group.grid_configure(row=1, column=0, padx=0, pady=0)
                ports_headsensor_row.columnconfigure(0, weight=1)
                ports_headsensor_row.columnconfigure(1, weight=0)
            else:
                ports_group.grid_configure(row=0, column=0, padx=(0, gap // 2), pady=0)
                headsensor_group.grid_configure(row=0, column=1, padx=(gap - gap // 2, 0), pady=0)
                ports_headsensor_row.columnconfigure(0, weight=1)
                ports_headsensor_row.columnconfigure(1, weight=1)

        # ============ STAGE CONTROLLER SECTION ============
        stage_group = ttk.LabelFrame(left_col, text="Stage Motor Controller",
                                     style='Setup.TLabelframe')
        stage_group.pack(**card_pack)
        stage_group.columnconfigure(1, weight=1)

        ttk.Label(stage_group, text="Config File:",
                 style='SetupLabel.TLabel').grid(row=0, column=0, sticky="e", padx=8, pady=6)
        app.stage_config_entry = ttk.Entry(stage_group, width=34, font=Fonts.BODY)
        app.stage_config_entry.grid(row=0, column=1, sticky="ew", padx=8, pady=6)

        def browse_stage_config():
            path = filedialog.askopenfilename(
                title="Select Stage Software config.json",
                filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            )
            if path:
                app.stage_config_entry.delete(0, "end")
                app.stage_config_entry.insert(0, path)

        ttk.Button(stage_group, text="Browse", command=browse_stage_config,
                  style='SetupButton.TButton').grid(row=0, column=2, padx=8, pady=6)

        ttk.Label(stage_group, text="Stage COM:",
                 style='SetupLabel.TLabel').grid(row=1, column=0, sticky="e", padx=8, pady=6)
        app.stage_com_entry = ttk.Entry(stage_group, width=15, font=Fonts.BODY)
        app.stage_com_entry.grid(row=1, column=1, sticky="w", padx=8, pady=6)

        app.stage_status = ttk.Label(stage_group, font=Fonts.BODY_SMALL_BOLD)
        app.stage_status.grid(row=1, column=2, padx=8, sticky="w")
        _set_port_status(app.stage_status, "untested")

        stage_btn_frame = ttk.Frame(stage_group)
        stage_btn_frame.grid(row=2, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 8))

        def load_stage_config():
            cfg_path = app.stage_config_entry.get().strip()
            if not cfg_path:
                messagebox.showwarning("Stage", "Please enter the path to the Stage Software config.json.")
                return
            if app.stage is None:
                messagebox.showerror("Stage", "Stage controller module is not available.\n\nInstall pymodbus: pip install pymodbus")
                return
            if app.stage.load_config(cfg_path):
                app.stage_config_path = cfg_path
                # Auto-fill COM port from config if the entry is empty
                if not app.stage_com_entry.get().strip() and app.stage.config.com_port:
                    app.stage_com_entry.delete(0, "end")
                    app.stage_com_entry.insert(0, app.stage.config.com_port)
                slot_names = [s.get("name", "?") for s in app.stage.slots]
                LOGGER.info("Stage config loaded: %d slots (%s)", len(app.stage.slots), ", ".join(slot_names))
                # Refresh slot buttons in Live View if builder exists
                if hasattr(app, '_refresh_stage_slots_ui'):
                    app._refresh_stage_slots_ui()
                messagebox.showinfo("Stage", f"Loaded {len(app.stage.slots)} slots:\n" + "\n".join(f"  - {n}" for n in slot_names))
            else:
                messagebox.showerror("Stage", f"Failed to load stage config:\n{cfg_path}")


        def test_stage_connect():
            if app.stage is None:
                messagebox.showerror("Stage", "Stage controller module is not available.")
                return
            port = app.stage_com_entry.get().strip()
            if not port:
                messagebox.showwarning("Stage", "Enter a Stage COM port first.")
                return
            _set_port_status(app.stage_status, "testing")

            def _probe():
                if not app.stage.connect(port):
                    return None
                try:
                    return app.stage.read_positions()
                finally:
                    app.stage.disconnect()

            def _done(positions):
                ok = positions is not None
                _set_port_status(app.stage_status, "open" if ok else "failed")
                if not ok:
                    messagebox.showerror("Stage", f"Could not connect to stage on {port}")
                    return
                app.hw.com_ports["STAGE"] = port
                x, y = positions
                pos_str = f"X={x}, Y={y}" if x is not None else "positions unavailable"
                app.status.toast(f"Stage connected on {port}: {pos_str}")

            run_in_worker(app, _probe, on_done=_done, name="stage-test")

        ttk.Button(stage_btn_frame, text="Load Config", command=load_stage_config,
                  style='SetupButton.TButton', width=16).pack(side="left", padx=(0, 8))
        ttk.Button(stage_btn_frame, text="Test Connect", command=test_stage_connect,
                  style='SetupButton.TButton', width=16).pack(side="left")

        # ============ LASER MANAGEMENT SECTION ============
        laser_mgmt_group = ttk.LabelFrame(right_col, text="Laser Management",
                                        style='Setup.TLabelframe')
        laser_mgmt_group.pack(**card_pack)

        # Info label
        ttk.Label(laser_mgmt_group, text="Manage available lasers in the system:",
                 style='SetupLabel.TLabel').pack(anchor="w", padx=8, pady=(0, 6))

        # Laser list display with better styling
        laser_list_frame = ttk.Frame(laser_mgmt_group)
        laser_list_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        # Column headers
        header_frame = ttk.Frame(laser_list_frame)
        header_frame.pack(fill="x", pady=(0, 2))
        ttk.Label(header_frame, text="Wavelength",
                 font=Fonts.BODY_SMALL_BOLD,
                 foreground=Colors.TEXT_SECONDARY).pack(side="left", padx=(2, 90))
        ttk.Label(header_frame, text="Type",
                 font=Fonts.BODY_SMALL_BOLD,
                 foreground=Colors.TEXT_SECONDARY).pack(side="left", padx=(0, 80))
        ttk.Label(header_frame, text="Power",
                 font=Fonts.BODY_SMALL_BOLD,
                 foreground=Colors.TEXT_SECONDARY).pack(side="left")

        # Scrollbar and listbox with increased height
        list_container = ttk.Frame(laser_list_frame)
        list_container.pack(fill="both", expand=True)

        laser_scrollbar = ttk.Scrollbar(list_container, orient="vertical")
        app.laser_listbox = tk.Listbox(list_container, height=6,
                                       yscrollcommand=laser_scrollbar.set,
                                       selectmode=tk.SINGLE)
        style_listbox(app.laser_listbox)
        laser_scrollbar.config(command=app.laser_listbox.yview)
        app.laser_listbox.pack(side="left", fill="both", expand=True)
        laser_scrollbar.pack(side="right", fill="y")

        # Initialize laser configurations from settings or defaults
        if not hasattr(app, 'laser_configs'):
            # Structure: {"wavelength": {"type": "OBIS/CUBE/RELAY", "power": float}}
            app.laser_configs = {}
            for laser in DEFAULT_ALL_LASERS:
                # Assign default types based on wavelength
                if laser == "377":
                    laser_type = "CUBE"
                elif laser in ["517", "Hg_Ar"]:
                    laser_type = "RELAY"
                else:
                    laser_type = "OBIS"
                app.laser_configs[laser] = {
                    "type": laser_type,
                    "power": DEFAULT_LASER_POWERS.get(laser, 0.01)
                }

        # Populate listbox with formatted laser info
        def format_laser_display(wavelength):
            """Format laser for display in listbox with proper alignment."""
            config = app.laser_configs.get(wavelength, {})
            laser_type = config.get("type", "OBIS")
            power = config.get("power", 0.01)
            # Fixed-width formatting for alignment
            wl_str = f"{source_label(wavelength):>9}"
            type_str = f"{laser_type:<6}"
            power_str = f"{power:>8.2f} mW"
            return f"{wl_str}  │  {type_str}  │  {power_str}"

        for laser in sorted(app.laser_configs.keys(),
                           key=lambda x: float(x) if x.replace('.','').isdigit() else 999):
            app.laser_listbox.insert(tk.END, format_laser_display(laser))
            # Tint the row towards its own wavelength; the text still says which.
            app.laser_listbox.itemconfigure(tk.END, foreground=source_color(laser))

        # Buttons for laser management
        laser_button_frame = ttk.Frame(laser_mgmt_group)
        laser_button_frame.pack(fill="x", padx=8, pady=(0, 8))

        def add_laser():
            """Add a new laser with custom dialog for wavelength, type, and power."""
            # Create custom dialog
            dialog = tk.Toplevel(app)
            dialog.title("Add New Laser")
            dialog.transient(app)
            dialog.resizable(False, False)
            dialog.grab_set()

            # Set icon if available
            try:
                dialog.iconbitmap(get_resource_path("sciglob_symbol.ico"))
            except Exception:
                pass

            # Configure dialog style
            dialog_frame = ttk.Frame(dialog, padding=Spacing.PAD_XL)
            dialog_frame.pack(fill="both", expand=True)

            # Title
            title_label = ttk.Label(dialog_frame, text="Add New Laser Configuration",
                                   font=Fonts.H3)
            title_label.pack(pady=(0, 20))

            # Wavelength field
            wl_frame = ttk.Frame(dialog_frame)
            wl_frame.pack(fill="x", pady=8)
            ttk.Label(wl_frame, text="Wavelength (nm):",
                     font=Fonts.BODY).pack(side="left", padx=(0, 12))
            wl_entry = ttk.Entry(wl_frame, width=15, font=Fonts.BODY)
            wl_entry.pack(side="left")
            wl_entry.insert(0, "532")  # Default value
            ttk.Label(wl_frame, text="e.g., 405, 532, 638",
                     foreground=Colors.TEXT_MUTED, font=Fonts.CAPTION).pack(side="left", padx=(8, 0))

            # Type selection
            type_frame = ttk.Frame(dialog_frame)
            type_frame.pack(fill="x", pady=8)
            ttk.Label(type_frame, text="Laser Type:",
                     font=Fonts.BODY).pack(side="left", padx=(0, 32))
            type_var = tk.StringVar(value="OBIS")
            type_combo = ttk.Combobox(type_frame, textvariable=type_var,
                                     values=["OBIS", "CUBE", "RELAY"],
                                     state="readonly", width=12,
                                     font=Fonts.BODY)
            type_combo.pack(side="left")

            # Power field
            power_frame = ttk.Frame(dialog_frame)
            power_frame.pack(fill="x", pady=8)
            ttk.Label(power_frame, text="Power (mW):",
                     font=Fonts.BODY).pack(side="left", padx=(0, 35))
            power_entry = ttk.Entry(power_frame, width=15, font=Fonts.BODY)
            power_entry.pack(side="left")
            power_entry.insert(0, "30.0")  # Default value
            ttk.Label(power_frame, text="milliwatts",
                     foreground=Colors.TEXT_MUTED, font=Fonts.CAPTION).pack(side="left", padx=(8, 0))

            # Info text
            info_text = (
                "Configure laser wavelength, type, and power.\n"
                "OBIS: Multi-channel laser controller\n"
                "CUBE: Single laser unit\n"
                "RELAY: Relay-controlled lasers"
            )
            info_label = ttk.Label(dialog_frame, text=info_text,
                                  foreground=Colors.TEXT_SECONDARY, font=Fonts.CAPTION,
                                  justify="left")
            info_label.pack(pady=(12, 0))

            # Separator before buttons
            ttk.Separator(dialog_frame, orient="horizontal").pack(fill="x", pady=(16, 12))

            # Buttons with clear labels
            button_frame = ttk.Frame(dialog_frame)
            button_frame.pack(pady=(0, 8))

            result = {"confirmed": False, "wavelength": None, "type": None, "power": None}

            def on_confirm():
                wavelength = wl_entry.get().strip()
                laser_type = type_var.get()
                power_str = power_entry.get().strip()

                try:
                    power_value = float(power_str)
                except ValueError:
                    messagebox.showerror("Invalid Power",
                                        "Please enter a valid number for power.",
                                        parent=dialog)
                    return

                # Validate wavelength
                try:
                    float(wavelength)  # Check if it's a valid number
                except ValueError:
                    messagebox.showerror("Invalid Wavelength",
                                        "Please enter a valid number for wavelength (e.g., 405, 532)",
                                        parent=dialog)
                    return

                if wavelength in app.laser_configs:
                    messagebox.showwarning("Duplicate",
                                          f"Laser {wavelength} nm already exists.",
                                          parent=dialog)
                    return

                # Add laser configuration
                app.laser_configs[wavelength] = {
                    "type": laser_type,
                    "power": power_value
                }

                # Store values before destroying dialog
                result["confirmed"] = True
                result["wavelength"] = wavelength
                result["type"] = laser_type
                result["power"] = power_str

                dialog.destroy()

            def on_cancel():
                dialog.destroy()

            # Make buttons more prominent with better styling
            save_btn = ttk.Button(button_frame, text="Save & Add", command=on_confirm,
                                 style='SetupAction.TButton', width=16)
            save_btn.pack(side="left", padx=(0, 8))

            cancel_btn = ttk.Button(button_frame, text="Cancel", command=on_cancel,
                                   style='SetupButton.TButton', width=12)
            cancel_btn.pack(side="left")

            # Toplevels are outside the tab tree the app harmonises on show.
            dialog.configure(bg=Colors.BG_SURFACE)
            dialog_frame.configure(style="Surface.TFrame")
            harmonize(dialog, Colors.BG_SURFACE)

            # Sized to its content and centred over the main window.
            center_over(dialog, app, min_width=logical_px(app, 440))

            # Focus on wavelength entry
            wl_entry.focus_set()
            wl_entry.select_range(0, tk.END)

            # Wait for dialog to close
            app.wait_window(dialog)

            if result["confirmed"]:
                # Get values from result dictionary (saved before dialog was destroyed)
                wavelength = result["wavelength"]
                laser_type = result["type"]
                power = result["power"]

                LOGGER.info("Adding laser: %s nm (%s, %s mW)", wavelength, laser_type, power)

                # Refresh the laser management display
                refresh_laser_display()

                # Update available lasers list
                app.available_lasers = list(app.laser_configs.keys())
                # Ensure Hg_Ar is always in the list
                if "Hg_Ar" not in app.available_lasers:
                    app.available_lasers.append("Hg_Ar")
                    # Also add to laser_configs if missing
                    if "Hg_Ar" not in app.laser_configs:
                        app.laser_configs["Hg_Ar"] = {
                            "type": "RELAY",
                            "power": 1.0
                        }
                LOGGER.debug("Available lasers: %s", app.available_lasers)

                # Auto-save settings after adding laser
                save_settings_silently()

                # Rebuild laser checkboxes in other tabs
                LOGGER.debug("Calling rebuild_laser_ui()")
                try:
                    app.rebuild_laser_ui()
                    LOGGER.debug("rebuild_laser_ui() completed")
                except Exception as e:
                    LOGGER.error("Error in rebuild_laser_ui(): %s", e)


                messagebox.showinfo("Laser Added",
                                   f"Laser configuration saved successfully!\n\n"
                                   f"Wavelength: {wavelength} nm\n"
                                   f"Type: {laser_type}\n"
                                   f"Power: {power} mW\n\n"
                                   f"The laser is now available in all tabs.",
                                   icon='info')

        def remove_laser():
            """Remove selected laser wavelength."""
            selection = app.laser_listbox.curselection()
            if not selection:
                messagebox.showwarning("No Selection", "Please select a laser to remove.")
                return

            idx = selection[0]
            laser_text = app.laser_listbox.get(idx)
            # Parse wavelength from display format: "405 nm  │  OBIS  │  5.0 mW"
            wavelength = laser_text.split("nm")[0].strip()

            # Prevent Hg_Ar from being removed
            if wavelength == "Hg_Ar":
                messagebox.showwarning("Cannot Remove",
                                      "Hg_Ar calibration lamp cannot be removed.\n\n"
                                      "It is a permanent part of the system.")
                return

            if len(app.laser_configs) <= 1:
                messagebox.showwarning("Cannot Remove", "You must have at least one laser configured.")
                return

            config = app.laser_configs.get(wavelength, {})
            laser_type = config.get("type", "OBIS")
            power = config.get("power", 0.01)

            confirm = messagebox.askyesno("Confirm Removal",
                                         f"Remove laser configuration?\n\n"
                                         f"Wavelength: {wavelength} nm\n"
                                         f"Type: {laser_type}\n"
                                         f"Power: {power} mW")
            if confirm:
                LOGGER.info("Removing laser: %s nm (%s)", wavelength, laser_type)

                del app.laser_configs[wavelength]
                # Remove power entry if exists
                if wavelength in app.power_entries:
                    del app.power_entries[wavelength]

                # Refresh display
                refresh_laser_display()

                # Update available lasers list
                app.available_lasers = list(app.laser_configs.keys())
                # Ensure Hg_Ar is always in the list (never remove it)
                if "Hg_Ar" not in app.available_lasers:
                    app.available_lasers.append("Hg_Ar")
                    # Also add to laser_configs if missing
                    if "Hg_Ar" not in app.laser_configs:
                        app.laser_configs["Hg_Ar"] = {
                            "type": "RELAY",
                            "power": 1.0
                        }
                LOGGER.debug("Available lasers: %s", app.available_lasers)

                # Auto-save settings after removing laser
                save_settings_silently()

                # Rebuild laser checkboxes in other tabs
                LOGGER.debug("Calling rebuild_laser_ui()")
                try:
                    app.rebuild_laser_ui()
                    LOGGER.debug("rebuild_laser_ui() completed")
                except Exception as e:
                    LOGGER.error("Error in rebuild_laser_ui(): %s", e)


                messagebox.showinfo("Laser Removed",
                                   f"Laser configuration removed and saved!\n\n"
                                   f"Removed: {wavelength} nm ({laser_type})\n\n"
                                   f"The laser has been removed from all tabs.",
                                   icon='info')

        def refresh_laser_display():
            """Refresh the laser listbox and power entries."""
            # Clear and repopulate listbox
            app.laser_listbox.delete(0, tk.END)
            for wavelength in sorted(app.laser_configs.keys(),
                                   key=lambda x: float(x) if x.replace('.','').isdigit() else 999):
                app.laser_listbox.insert(tk.END, format_laser_display(wavelength))
                app.laser_listbox.itemconfigure(tk.END, foreground=source_color(wavelength))

            # Rebuild power configuration section
            rebuild_power_entries()

        ttk.Button(laser_button_frame, text="+ Add Laser", command=add_laser,
                  style='SetupButton.TButton', width=16).pack(side="left", padx=(0, 6))
        ttk.Button(laser_button_frame, text="- Remove Laser", command=remove_laser,
                  style='SetupButton.TButton', width=16).pack(side="left")

        ttk.Label(laser_mgmt_group, text="Tip: Laser changes are saved automatically when you add or remove lasers.",
                 foreground=Colors.TEXT_MUTED, font=Fonts.CAPTION_ITALIC).pack(anchor="w", padx=8, pady=(6, 4))

        # ============ LASER POWER CONFIGURATION ============
        power_group = ttk.LabelFrame(right_col, text="Laser Power Configuration",
                                    style='Setup.TLabelframe')
        power_group.pack(**card_pack)

        app.power_entries = {}
        app.power_group_frame = power_group  # Store reference for rebuilding

        def rebuild_power_entries(force=False, width=None):
            """Rebuild power entry widgets based on current laser configurations."""
            available_width = width or power_group.winfo_width() or 0
            column_groups = 1 if available_width < logical_px(app, 640) or len(app.laser_configs) <= 4 else 2
            if (
                not force
                and getattr(app, "_setup_power_column_groups", None) == column_groups
                and getattr(app, "_setup_power_laser_count", None) == len(app.laser_configs)
            ):
                return

            app._setup_power_column_groups = column_groups
            app._setup_power_laser_count = len(app.laser_configs)

            # Preserve anything the operator has typed but not yet saved so a
            # resize-triggered rebuild never discards their edits.
            typed_values = {}
            for wavelength, entry in getattr(app, "power_entries", {}).items():
                try:
                    typed_values[wavelength] = entry.get()
                except tk.TclError:
                    pass

            # Clear existing widgets
            for widget in power_group.winfo_children():
                widget.destroy()

            app.power_entries = {}

            # Sort lasers numerically
            sorted_lasers = sorted(app.laser_configs.keys(),
                                  key=lambda x: float(x) if x.replace('.','').isdigit() else 999)
            lasers_per_group = max(1, (len(sorted_lasers) + column_groups - 1) // column_groups)
            total_columns = column_groups * 4

            # Groups sit at the left edge and a trailing spacer column takes the
            # slack; weights on the label columns used to float the groups
            # towards the middle of the card.
            for col in range(9):
                power_group.columnconfigure(col, weight=0)
            power_group.columnconfigure(total_columns, weight=1)

            header_row = 0
            for group_index in range(column_groups):
                col_offset = group_index * 4
                ttk.Label(power_group, text="Wavelength",
                         font=Fonts.BODY_SMALL_BOLD,
                         foreground=Colors.TEXT_SECONDARY).grid(row=header_row, column=col_offset, sticky="e", padx=(8, 4), pady=(2, 8))
                ttk.Label(power_group, text="Type",
                         font=Fonts.BODY_SMALL_BOLD,
                         foreground=Colors.TEXT_SECONDARY).grid(row=header_row, column=col_offset + 1, sticky="w", padx=(4, 0), pady=(2, 8))
                ttk.Label(power_group, text="Power",
                         font=Fonts.BODY_SMALL_BOLD,
                         foreground=Colors.TEXT_SECONDARY).grid(row=header_row, column=col_offset + 2, sticky="w", padx=(0, 0), pady=(2, 8))

            max_row_used = header_row
            for idx, wavelength in enumerate(sorted_lasers):
                config = app.laser_configs[wavelength]
                laser_type = config.get("type", "OBIS")
                power = config.get("power", 0.01)

                row = (idx % lasers_per_group) + 1
                col_offset = (idx // lasers_per_group) * 4
                max_row_used = max(max_row_used, row)

                # Wavelength label
                label_text = source_label(wavelength)
                label = ttk.Label(power_group, text=label_text,
                                 style='SetupLabel.TLabel',
                                 font=Fonts.BODY_BOLD,
                                 foreground=source_color(wavelength))
                label.grid(row=row, column=col_offset, sticky="e", padx=(8, 4), pady=4)

                # Type pill: filled, so the controller a source hangs off is
                # readable at a glance instead of being bracketed grey text.
                kinds = {"OBIS": "info", "CUBE": "warning", "RELAY": "success"}
                type_badge = badge(power_group, laser_type,
                                   kind=kinds.get(laser_type, "neutral"))
                type_badge.grid(row=row, column=col_offset+1, sticky="w", padx=(0, 8), pady=4)

                # Power entry
                e = ttk.Entry(power_group, width=10, font=Fonts.BODY)
                e.insert(0, typed_values.get(wavelength, str(power)))
                e.grid(row=row, column=col_offset+2, sticky="w", padx=(0, 4), pady=4)
                app.power_entries[wavelength] = e

                # Unit label
                ttk.Label(power_group, text="mW", foreground=Colors.TEXT_MUTED,
                         font=Fonts.BODY_SMALL).grid(
                    row=row, column=col_offset+3, sticky="w", padx=0, pady=4)

            # Add separator line
            separator_row = max_row_used + 1
            ttk.Separator(power_group, orient="horizontal").grid(
                row=separator_row, column=0, columnspan=total_columns + 1, sticky="ew", padx=8, pady=(12, 8))

            # Add info label at bottom after separator
            info_row = separator_row + 1
            info_frame = ttk.Frame(power_group)
            info_frame.grid(row=info_row, column=0, columnspan=total_columns + 1, sticky="ew", padx=8, pady=(0, 8))

            ttk.Label(info_frame,
                     text="Power values are the laser-control setpoints.",
                     foreground=Colors.TEXT_MUTED,
                     font=Fonts.CAPTION_ITALIC).pack(side="left")

            # These rows are rebuilt long after the tab was tinted, so re-run
            # the pass; otherwise they come back as grey blocks on the card.
            harmonize(power_group, Colors.BG_SURFACE)

        # Initial build of power entries; _layout_setup re-flows them for the column width.
        rebuild_power_entries(force=True)

        # ============ ADVANCED (BENCH-VALIDATED) OPTIONS ============
        # Collapsed by default: three bench-only options took a card's worth of
        # the page and pushed the form into a scroll on a 1080p screen. It opens
        # on its own when any option is enabled.
        advanced_card = ttk.LabelFrame(left_col, text="Advanced", style='Setup.TLabelframe')
        advanced_card.pack(**card_pack)
        advanced_toggle = tk.Label(advanced_card, bg=Colors.BG_SURFACE, fg=Colors.ACCENT_DEEP,
                                   font=Fonts.BODY_BOLD, cursor="hand2", anchor="w")
        advanced_toggle.pack(fill="x", padx=12, pady=(2, 4))
        advanced_group = ttk.Frame(advanced_card)
        app.advanced_open = tk.BooleanVar(value=False)

        def _show_advanced(open_):
            app.advanced_open.set(bool(open_))
            advanced_toggle.configure(text=("▾" if open_ else "▸") +
                                      "   Speed options (validate on the bench first)")
            if open_:
                advanced_group.pack(fill="x", pady=(0, 4))
            else:
                advanced_group.pack_forget()

        advanced_toggle.bind("<Button-1>", lambda _event: _show_advanced(not app.advanced_open.get()))
        app.advanced_group = advanced_group
        app._show_advanced = _show_advanced
        _show_advanced(any(bool(value) for value in app.measurement_flags.values()))
        app.measurement_flag_vars = {}
        flag_help = {
            "smart_source_off": ("Turn off only the lit source between lasers",
                                 "Skips the full all-off sweep before every source (~1-2 s of serial waits each). "
                                 "Verify the previous laser is really off in the dark frames before enabling."),
            "cube_prearm": ("Pre-arm the 377 nm CUBE during the previous dark",
                            "Sends EXT/CW/P to the CUBE while the previous source's dark is acquired, so only "
                            "the shutter + 3 s settle remain. Check the dark frames for stray 377 nm light."),
            "framed_serial_reads": ("Framed serial replies (no fixed waits)",
                                    "Wait for the reply terminator instead of sleeping 0.2-0.4 s per command. "
                                    "Confirm every OBIS/CUBE/relay command still gets its reply."),
        }
        for row_index, (key, (label, help_text)) in enumerate(flag_help.items()):
            var = tk.BooleanVar(value=bool(app.measurement_flags.get(key, False)))
            app.measurement_flag_vars[key] = var

            def _on_flag(k=key, v=var):
                app.measurement_flags[k] = bool(v.get())
                app.apply_measurement_flags()

            check = ttk.Checkbutton(advanced_group, text=label, variable=var, command=_on_flag)
            check.grid(row=row_index, column=0, sticky="w", padx=12, pady=3)
            Tooltip(check, help_text)
        ttk.Label(advanced_group, text="All off by default (= v3.3 timing). Each option is saved with Settings.",
                  foreground=Colors.TEXT_MUTED, font=Fonts.CAPTION_ITALIC).grid(
            row=len(flag_help), column=0, sticky="w", padx=12, pady=(4, 6))

        # ============ RESPONSIVE COLUMNS ============
        def _layout_setup(width=None, _height=None):
            available = width or scrollable.canvas.winfo_width()
            if not available or available <= 1:
                return
            content_width = min(available, logical_px(app, 1560))
            gap = Spacing.PAD_LG
            # Side by side only when both columns fit at their natural widths
            # (with a little slack); otherwise the wider one would push the
            # page into a horizontal scroll.
            needed = int((left_col.winfo_reqwidth() + right_col.winfo_reqwidth() + gap) * 1.03)
            if content_width >= max(needed, logical_px(app, 1100)):
                left_col.grid(row=0, column=0, sticky="nsew", padx=(0, gap // 2))
                right_col.grid(row=0, column=1, sticky="nsew", padx=(gap - gap // 2, 0))
                columns.columnconfigure(0, weight=1)
                columns.columnconfigure(1, weight=1)
                column_width = (content_width - gap) // 2
            else:
                left_col.grid(row=0, column=0, sticky="ew", padx=0)
                right_col.grid(row=1, column=0, sticky="ew", padx=0)
                columns.columnconfigure(0, weight=1)
                columns.columnconfigure(1, weight=0)
                column_width = content_width
            _layout_ports_and_headsensor(column_width)
            rebuild_power_entries(width=column_width - 2 * Spacing.GROUP_PAD)

        bind_debounced_configure(scrollable.canvas, _layout_setup)


    def refresh_ports():
        if serial is None:
            messagebox.showwarning("Ports", "pyserial is not installed.")
            return
        ports = list(serial.tools.list_ports.comports())
        names = [p.device for p in ports]
        if not names:
            app.status.toast("No serial ports detected", kind="warning")
            return
        entries = (app.obis_entry, app.cube_entry, app.relay_entry, app.headsensor_entry)
        for entry, name in zip(entries, names):
            entry.delete(0, "end")
            entry.insert(0, name)
        app.status.toast(f"Detected {len(names)} serial port(s); first four filled in, adjust as needed")


    def test_com_connect():
        """Open/close the three laser ports on a worker; the dots update when it finishes."""
        app._update_ports_from_ui()
        for dot in (app.obis_status, app.cube_status, app.relay_status):
            _set_port_status(dot, "testing")

        def _probe():
            results = {}
            for key, device in (("OBIS", app.lasers.obis), ("CUBE", app.lasers.cube), ("RELAY", app.lasers.relay)):
                if not device.port:
                    results[key] = (False, "unset", None)
                    continue
                ok = bool(device.open())
                if ok:
                    device.close()  # free the port again
                    detail = f"{device.port} opened and was released again."
                else:
                    detail = f"{device.port} could not be opened: not present, or in use by another program."
                results[key] = (ok, "open" if ok else "failed", detail)
            return results

        def _done(results):
            for key, dot in (("OBIS", app.obis_status), ("CUBE", app.cube_status), ("RELAY", app.relay_status)):
                ok, state, detail = results.get(key, (False, "failed", None))
                _set_port_status(dot, state, detail)
            failed = [key for key, (ok, _state, _detail) in results.items() if not ok]
            if failed:
                app.status.toast(f"Serial test: could not open {', '.join(failed)}", kind="warning")
            else:
                app.status.toast("Serial test: all laser ports opened")

        run_in_worker(app, _probe, on_done=_done, busy=(app.test_com_btn,), name="serial-test")


    def test_headsensor_connect():
        app._update_ports_from_ui()
        _set_port_status(app.headsensor_status, "testing")

        def _probe():
            ok = bool(app.filterwheel.device.port) and app.filterwheel.open()
            if ok:
                app.filterwheel.close()
            return ok

        def _done(ok):
            _set_port_status(app.headsensor_status, "open" if ok else "failed")
            app.status.toast("Head sensor port opened" if ok else "Head sensor port could not be opened",
                             kind="success" if ok else "warning")

        run_in_worker(app, _probe, on_done=_done, busy=(app.test_headsensor_btn,), name="head-sensor-test")

    def browse_dll():
        path = filedialog.askopenfilename(
            title="Select driver DLL / runtime", filetypes=[("DLL", "*.dll"), ("All files", "*.*")])
        if path:
            app.dll_entry.delete(0, "end")
            app.dll_entry.insert(0, path)

    def show_spectrometer_selection_dialog(devices):
        """
        Show a dialog for the user to select which spectrometer to connect to.

        Args:
            devices: list of discovery dicts with serial / label / type

        Returns:
            Selected device dict or None if cancelled
        """
        # Create custom dialog
        dialog = tk.Toplevel(app)
        dialog.title("Select Spectrometer")
        dialog.transient(app)
        dialog.grab_set()

        # Set icon if available
        try:
            dialog.iconbitmap(get_resource_path("sciglob_symbol.ico"))
        except Exception:
            pass

        # Configure dialog style
        dialog_frame = ttk.Frame(dialog, padding=Spacing.PAD_XL)
        dialog_frame.pack(fill="both", expand=True)

        # Title
        title_label = ttk.Label(dialog_frame, text="Multiple Spectrometers Detected",
                               font=Fonts.H3)
        title_label.pack(pady=(0, 10))

        # Info text
        info_text = f"Found {len(devices)} spectrometers.\nPlease select the one you want to connect to:"
        info_label = ttk.Label(dialog_frame, text=info_text,
                              font=Fonts.BODY)
        info_label.pack(pady=(0, 15))

        # Listbox frame with scrollbar
        list_frame = ttk.Frame(dialog_frame)
        list_frame.pack(fill="both", expand=True, pady=(0, 15))

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical")
        listbox = tk.Listbox(list_frame, height=8,
                            yscrollcommand=scrollbar.set,
                            selectmode=tk.SINGLE)
        style_listbox(listbox)
        scrollbar.config(command=listbox.yview)
        listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Populate listbox with detected devices
        for i, device in enumerate(devices):
            display_text = f"[{i+1}]  {device.get('label', device.get('serial', 'Unknown'))}"
            listbox.insert(tk.END, display_text)

        # Select first item by default
        if devices:
            listbox.selection_set(0)
            listbox.activate(0)

        # Result container
        result = {"selected": None}

        def on_select():
            selection = listbox.curselection()
            if not selection:
                messagebox.showwarning("No Selection",
                                      "Please select a spectrometer from the list.",
                                      parent=dialog)
                return

            idx = selection[0]
            result["selected"] = devices[idx]
            dialog.destroy()

        def on_cancel():
            result["selected"] = None
            dialog.destroy()

        def on_double_click(event):
            on_select()

        # Bind double-click to select
        listbox.bind("<Double-Button-1>", on_double_click)

        # Separator before buttons
        ttk.Separator(dialog_frame, orient="horizontal").pack(fill="x", pady=(0, 12))

        # Buttons
        button_frame = ttk.Frame(dialog_frame)
        button_frame.pack(pady=(0, 8))

        select_btn = ttk.Button(button_frame, text="Connect to Selected",
                               command=on_select,
                               style='SetupAction.TButton', width=22)
        select_btn.pack(side="left", padx=(0, 8))

        cancel_btn = ttk.Button(button_frame, text="Cancel",
                               command=on_cancel,
                               style='SetupButton.TButton', width=12)
        cancel_btn.pack(side="left")

        # Toplevels are outside the tab tree the app harmonises on show.
        dialog.configure(bg=Colors.BG_SURFACE)
        dialog_frame.configure(style="Surface.TFrame")
        harmonize(dialog, Colors.BG_SURFACE)

        center_over(dialog, app, min_width=logical_px(app, 500), min_height=logical_px(app, 340))

        # Focus on listbox
        listbox.focus_set()

        # Wait for dialog to close
        app.wait_window(dialog)

        return result["selected"]


    def connect_spectrometer():
        """Discover + connect on a worker thread; DLL loading and USB enumeration take seconds."""
        if getattr(app, "_connect_in_progress", False):
            return
        requested_type = app.spec_type_var.get().strip() or "Auto"
        dll = app.dll_entry.get().strip()
        if not dll:
            suggested = suggest_default_dll_path(requested_type)
            if suggested:
                dll = suggested
                app.dll_entry.delete(0, "end")
                app.dll_entry.insert(0, dll)

        app._connect_in_progress = True
        app.spec_status.config(text="● Connecting…", foreground=Colors.INFO)
        app.status.progress_begin(f"Searching for {requested_type} spectrometers…")

        def _fail(exc):
            app._connect_in_progress = False
            app.spec = None
            app.spec_backend = None
            app.spec_status.config(text="● Disconnected", foreground=Colors.DANGER)
            app.refresh_instrument_chip()
            app.status.progress_end("Spectrometer connection failed", kind="error")
            app._post_error("Spectrometer Connect", exc)

        def _cancelled():
            app._connect_in_progress = False
            app.spec_status.config(text="● Disconnected", foreground=Colors.DANGER)
            app.refresh_instrument_chip()
            app.status.progress_end("Connection cancelled")

        def _finish(payload):
            resolved_type, spec, selected_device = payload
            app._connect_in_progress = False
            app.spec = spec
            # Allow saturated data through so the UI can clamp & display it
            spec.abort_on_saturation = False
            app.spec_backend = resolved_type
            app.install_laser_controller_for_backend(resolved_type)
            if resolved_type != "Simulated":
                app.hw.last_connected_type = resolved_type  # Auto probes this backend first next time
            if requested_type != "Auto":
                app.hw.spectrometer_type = resolved_type
                app.spec_type_var.set(resolved_type)
            app.sn = getattr(spec, "sn", selected_device.get("serial", "Unknown"))
            app.data.serial_number = app.sn
            app.npix = getattr(spec, "npix_active", app.npix)
            app.data.npix = app.npix
            app.spec_status.config(text=f"● Connected ({resolved_type}, S/N: {app.sn})", foreground=Colors.SUCCESS)
            app.refresh_instrument_chip()
            app.status.progress_end(f"Connected: {resolved_type} S/N {app.sn}, {app.npix} pixels", kind="success")

        def _connect_selected(devices, selected_device):
            resolved_type = selected_device.get("type", requested_type)
            app.status.progress_begin(f"Connecting to {resolved_type}…")

            def _work():
                backend_type, spec = connect_backend_spectrometer(
                    resolved_type,
                    dll_path=dll,
                    serial=selected_device.get("serial"),
                    debug_mode=1,
                    devices=devices,  # reuse the enumeration instead of probing the hardware twice
                )
                return backend_type, spec, selected_device

            run_in_worker(app, _work, on_done=_finish, on_error=_fail, busy=(app.connect_btn,), name="spectrometer-connect")

        def _discovered(devices):
            if not devices:
                _fail(RuntimeError("No compatible spectrometers detected."))
                return
            selected_device = devices[0]
            if len(devices) > 1:
                selected_device = show_spectrometer_selection_dialog(devices)
                if selected_device is None:
                    _cancelled()
                    return
            _connect_selected(devices, selected_device)

        preferred = getattr(app.hw, "last_connected_type", "") or ""

        def _probe_progress(text):
            app.pump.post(app.status.set, text)

        run_in_worker(
            app,
            lambda: discover_spectrometers(requested_type, dll, preferred_type=preferred, progress=_probe_progress),
            on_done=_discovered,
            on_error=_fail,
            busy=(app.connect_btn,),
            name="spectrometer-discover",
        )

    def disconnect_spectrometer():
        try:
            app.stop_live()
            app.stop_measure()
            if app.spec:
                try:
                    app.spec.disconnect()
                except Exception:
                    pass
            app.spec = None
            app.spec_backend = None
            app.install_laser_controller_for_backend(None)
            app.spec_status.config(text="● Disconnected", foreground=Colors.DANGER)
            app.refresh_instrument_chip()
        except Exception as e:
            app._post_error("Spectrometer Disconnect", e)

    def _update_ports_from_ui():
        app.hw.com_ports["OBIS"] = app.obis_entry.get().strip() or DEFAULT_COM_PORTS["OBIS"]
        app.hw.com_ports["CUBE"] = app.cube_entry.get().strip() or DEFAULT_COM_PORTS["CUBE"]
        app.hw.com_ports["RELAY"] = app.relay_entry.get().strip() or DEFAULT_COM_PORTS["RELAY"]
        app.hw.com_ports["HEADSENSOR"] = app.headsensor_entry.get().strip() or DEFAULT_COM_PORTS["HEADSENSOR"]
        app.hw.com_ports["STAGE"] = app.stage_com_entry.get().strip()
        app.lasers.configure_ports(app.hw.com_ports)
        app.filterwheel.configure_port(app.hw.com_ports["HEADSENSOR"])

    def _get_power(tag: str) -> float:
        try:
            e = app.power_entries.get(tag)
            if e is None:
                return DEFAULT_LASER_POWERS.get(tag, 0.01)
            return float(e.get().strip())
        except (ValueError, AttributeError):
            return DEFAULT_LASER_POWERS.get(tag, 0.01)

    def save_settings_silently():
        """Save all settings without showing a message box."""
        app._update_ports_from_ui()
        app.hw.dll_path = app.dll_entry.get().strip()
        app.hw.spectrometer_type = app.spec_type_var.get().strip() or "Auto"
        app.stage_config_path = app.stage_config_entry.get().strip()

        # Update laser configurations with current power values
        for wavelength, e in app.power_entries.items():
            try:
                power_value = float(e.get().strip())
                if wavelength in app.laser_configs:
                    app.laser_configs[wavelength]["power"] = power_value
                # Also update legacy laser_power dict for compatibility
                app.hw.laser_power[wavelength] = power_value
            except Exception:
                pass

        payload = {
            "spectrometer_type": app.hw.spectrometer_type,
            "last_connected_type": getattr(app.hw, "last_connected_type", ""),
            "dll_path": app.hw.dll_path,
            "com_ports": app.hw.com_ports,
            "laser_power": app.hw.laser_power,
            "laser_configs": app.laser_configs,
            "available_lasers": app.available_lasers,
            "stage_config_path": app.stage_config_path,
            # Bench-validated speed options (all default to the v3.3 behaviour).
            "measurement_flags": dict(app.measurement_flags),
        }
        # Atomic write: a crash mid-write must never leave a truncated file
        # that silently resets every COM port and power value to defaults.
        tmp_path = f"{SETTINGS_FILE}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp_path, SETTINGS_FILE)
            LOGGER.info("Settings saved to %s", SETTINGS_FILE)
        except Exception as e:
            LOGGER.error("Error saving settings: %s", e)
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def save_settings():
        """Save all settings including laser configurations."""
        save_settings_silently()
        try:
            messagebox.showinfo("Settings Saved",
                              f"Settings saved successfully!\n\n"
                              f"File: {SETTINGS_FILE}\n"
                              f"Lasers configured: {len(app.laser_configs)}\n\n"
                              f"Note: Restart required for COM port and DLL changes.",
                              icon='info')
        except Exception as e:
            messagebox.showerror("Settings", str(e))

    def load_settings_into_ui():
        """Load settings from file into UI, including laser configurations."""
        def apply_defaults():
            app.spec_type_var.set(getattr(app.hw, "spectrometer_type", "Auto"))
            app.dll_entry.delete(0, "end")
            app.dll_entry.insert(0, app.hw.dll_path)
            app._update_dll_row_visibility()
            app.obis_entry.delete(0, "end"); app.obis_entry.insert(0, DEFAULT_COM_PORTS["OBIS"])
            app.cube_entry.delete(0, "end"); app.cube_entry.insert(0, DEFAULT_COM_PORTS["CUBE"])
            app.relay_entry.delete(0, "end"); app.relay_entry.insert(0, DEFAULT_COM_PORTS["RELAY"])
            app.headsensor_entry.delete(0, "end"); app.headsensor_entry.insert(0, DEFAULT_COM_PORTS["HEADSENSOR"])
            for wavelength, e in app.power_entries.items():
                config = app.laser_configs.get(wavelength, {})
                e.delete(0, "end"); e.insert(0, str(config.get("power", 0.01)))

        if not os.path.isfile(SETTINGS_FILE):
            apply_defaults()
            return
        try:
            file_size = os.path.getsize(SETTINGS_FILE)
            if file_size > 5_000_000:
                LOGGER.error("Settings file too large (%d bytes), using defaults", file_size)
                apply_defaults()
                return
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                obj = json.load(f)
            if not isinstance(obj, dict):
                LOGGER.error("Settings file root is not a JSON object, using defaults")
                apply_defaults()
                return

            # Always come up on Auto. Reopening pinned to whichever backend was
            # saved is how an operator ends up staring at "MiniSpec" on a bench
            # that has an Avantes plugged in. last_connected_type is still
            # honoured -- it just orders the probe instead of restricting it.
            saved_type = obj.get("spectrometer_type", getattr(app.hw, "spectrometer_type", "Auto"))
            if saved_type != "Auto":
                LOGGER.info("Starting on Auto (saved type %s is used as the probe hint)", saved_type)
            app.spec_type_var.set("Auto")
            app.hw.spectrometer_type = "Auto"
            app.hw.last_connected_type = str(
                obj.get("last_connected_type", "") or (saved_type if saved_type != "Auto" else "")
            )
            # Load DLL path. Since we start on Auto, a path the app suggested
            # for some other backend is dropped for the same reason as in
            # on_spec_type_change; a hand-typed one is kept and shown.
            app.hw.dll_path = obj.get("dll_path", "")
            if _is_suggested_path(app.hw.dll_path):
                app.hw.dll_path = ""
            app.dll_entry.delete(0, "end")
            app.dll_entry.insert(0, app.hw.dll_path)
            app._update_dll_row_visibility()

            # Load COM ports
            cp = obj.get("com_ports", DEFAULT_COM_PORTS)
            app.obis_entry.delete(0, "end"); app.obis_entry.insert(0, cp.get("OBIS", DEFAULT_COM_PORTS["OBIS"]))
            app.cube_entry.delete(0, "end"); app.cube_entry.insert(0, cp.get("CUBE", DEFAULT_COM_PORTS["CUBE"]))
            app.relay_entry.delete(0, "end"); app.relay_entry.insert(0, cp.get("RELAY", DEFAULT_COM_PORTS["RELAY"]))
            app.headsensor_entry.delete(0, "end"); app.headsensor_entry.insert(0, cp.get("HEADSENSOR", DEFAULT_COM_PORTS["HEADSENSOR"]))

            # Load laser configurations (new format) or available_lasers (legacy format)
            if "laser_configs" in obj:
                # New format with full configurations
                app.laser_configs = obj["laser_configs"]
                LOGGER.info("Loaded laser configurations: %s", list(app.laser_configs.keys()))
            elif "available_lasers" in obj:
                # Legacy format - convert to new format
                app.laser_configs = {}
                lp = obj.get("laser_power", DEFAULT_LASER_POWERS)
                for wavelength in obj["available_lasers"]:
                    # Assign default types based on wavelength
                    if wavelength == "377":
                        laser_type = "CUBE"
                    elif wavelength in ["517", "Hg_Ar"]:
                        laser_type = "RELAY"
                    else:
                        laser_type = "OBIS"
                    app.laser_configs[wavelength] = {
                        "type": laser_type,
                        "power": lp.get(wavelength, DEFAULT_LASER_POWERS.get(wavelength, 0.01))
                    }
                LOGGER.info("Converted legacy laser list: %s", list(app.laser_configs.keys()))

            # Update power entries with loaded values
            for wavelength, e in app.power_entries.items():
                if wavelength in app.laser_configs:
                    power = app.laser_configs[wavelength].get("power", 0.01)
                    e.delete(0, "end")
                    e.insert(0, str(power))

            # Bench-validated speed options; unknown keys are ignored.
            flags = obj.get("measurement_flags", {})
            if isinstance(flags, dict):
                for key in app.DEFAULT_MEASUREMENT_FLAGS:
                    if key in flags:
                        app.measurement_flags[key] = bool(flags[key])
            app.apply_measurement_flags()
            # The checkboxes were built with the defaults before the file was
            # read: show the saved state, and open the card when an option is
            # on so it cannot be missed.
            for key, var in getattr(app, "measurement_flag_vars", {}).items():
                var.set(bool(app.measurement_flags.get(key, False)))
            if hasattr(app, "_show_advanced"):
                app._show_advanced(any(app.measurement_flags.values()))

            # Load stage configuration path and COM port
            stage_cfg = obj.get("stage_config_path", "")
            app.stage_config_entry.delete(0, "end")
            app.stage_config_entry.insert(0, stage_cfg)
            stage_com = cp.get("STAGE", "")
            app.stage_com_entry.delete(0, "end")
            app.stage_com_entry.insert(0, stage_com)
            # Auto-load stage config if path is saved
            if stage_cfg and app.stage is not None:
                if app.stage.load_config(stage_cfg):
                    app.stage_config_path = stage_cfg
                    LOGGER.info("Stage config auto-loaded: %d slots", len(app.stage.slots))
                    if hasattr(app, '_refresh_stage_slots_ui'):
                        app._refresh_stage_slots_ui()

            # Don't show messagebox at startup - just log to console
            LOGGER.info("Settings loaded: %d lasers configured", len(app.laser_configs))
        except Exception as e:
            LOGGER.warning("Could not load settings file '%s': %s", SETTINGS_FILE, e)
            apply_defaults()

    # ------------------ General helpers ------------------

    def _post_error(title: str, ex: Exception):
        tb = "".join(traceback.format_exception(type(ex), ex, ex.__traceback__))
        LOGGER.error("[%s] %s\n%s", title, ex, tb)
        app.pump.post(messagebox.showerror, title, str(ex))  # safe from any thread

    def on_close():
        try:
            app.stop_live()
            app.stop_measure()
            # Give a running acquisition thread a moment to notice the stop
            # flag before the DLL is torn down underneath it.
            worker = getattr(app, "measure_thread", None)
            if worker is not None and worker.is_alive():
                worker.join(timeout=2.0)
            try:
                save_settings_silently()
            except Exception:
                LOGGER.exception("Could not save settings on exit")
            # Safety: never leave a laser emitting after the app closes.
            try:
                app.lasers.all_off()
            except Exception:
                LOGGER.exception("Could not switch sources off on exit")
            if app.spec:
                try:
                    app.spec.disconnect()
                except Exception:
                    pass
            for dev in [app.lasers.obis, app.lasers.cube, app.lasers.relay]:
                try:
                    dev.close()
                except Exception:
                    pass
            try:
                app.filterwheel.close()
            except Exception:
                pass
            if app.stage is not None and app.stage.connected:
                try:
                    app.stage.disconnect()
                except Exception:
                    pass
        finally:
            app.destroy()

    # Bind functions to app object
    app.refresh_ports = refresh_ports
    app.test_com_connect = test_com_connect
    app.test_headsensor_connect = test_headsensor_connect
    app.browse_dll = browse_dll
    app.connect_spectrometer = connect_spectrometer
    app.disconnect_spectrometer = disconnect_spectrometer
    app._update_ports_from_ui = _update_ports_from_ui
    app._get_power = _get_power
    app.save_settings = save_settings
    app.save_settings_silently = save_settings_silently
    app.load_settings_into_ui = load_settings_into_ui
    app._post_error = _post_error
    app.on_close = on_close

    # Call the UI builder
    _build_setup_tab()
