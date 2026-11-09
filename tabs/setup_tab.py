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
from services import light_sources
from services.run_progress import source_label
from .theme import Colors, Fonts, Spacing, harmonize, logical_px, source_color, style_listbox
from .ui_utils import ScrollableFrame, Tooltip, bind_debounced_configure, center_over, run_in_worker

LOGGER = logging.getLogger(__name__)

def build(app):
    # Import constants from app
    DEFAULT_COM_PORTS = app.DEFAULT_COM_PORTS
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

    # ---- serial-port dropdowns ------------------------------------------------
    _PORT_SEP = "  ·  "
    app._serial_ports = []          # [(device, description)] as last enumerated
    app._port_comboboxes = []

    def _port_values():
        return [f"{device}{_PORT_SEP}{description}" if description else device
                for device, description in app._serial_ports]

    def _on_port_pick(event):
        """Keep only the device name once an entry with a description is picked."""
        combo = event.widget
        text = combo.get()
        device = text.split(_PORT_SEP, 1)[0].strip()
        if device != text:
            combo.set(device)
        combo.selection_clear()

    def _port_combobox(parent):
        combo = ttk.Combobox(parent, width=14, font=Fonts.BODY, values=_port_values())
        combo.bind("<<ComboboxSelected>>", _on_port_pick)
        Tooltip(combo, "Type a port (COM4) or open the list of the serial ports Windows sees. "
                       "Refresh Ports (F5) updates the list.")
        app._port_comboboxes.append(combo)
        return combo

    def _refresh_port_lists():
        values = _port_values()
        for combo in app._port_comboboxes:
            try:
                combo.configure(values=values)
            except tk.TclError:
                pass

    def _enumerate_ports():
        """Worker: the ports and their descriptions, sorted by number."""
        if serial is None:
            return []
        ports = []
        for port in serial.tools.list_ports.comports():
            description = str(getattr(port, "description", "") or "")
            device = str(port.device)
            if description.endswith(f"({device})"):
                description = description[: -len(device) - 2].strip()
            ports.append((device, description))

        def _key(item):
            digits = "".join(ch for ch in item[0] if ch.isdigit())
            return (0, int(digits)) if digits else (1, item[0])
        return sorted(ports, key=_key)

    def _ports_found(ports):
        app._serial_ports = list(ports)
        _refresh_port_lists()

    def refresh_port_lists_async():
        """Fill the dropdowns without stalling the window (enumeration can take a moment on Windows)."""
        if serial is None:
            return
        run_in_worker(app, _enumerate_ports, on_done=_ports_found,
                      on_error=lambda exc: LOGGER.debug("Serial port listing failed: %s", exc),
                      name="serial-ports")

    app.refresh_port_lists_async = refresh_port_lists_async

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

        # Editable dropdowns: type a port, or pick one from the ports Windows
        # sees (with their descriptions, so the laser box can be told from the
        # relay board). The list is filled on a worker at startup and by
        # Refresh Ports; what is typed or picked is the plain "COMn".
        app.obis_entry = _port_combobox(ports_group)
        app.cube_entry = _port_combobox(ports_group)
        app.relay_entry = _port_combobox(ports_group)
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
        Tooltip(refresh_btn, "List the serial ports Windows currently sees in every port dropdown (F5). "
                             "Fields that are set are left alone.")
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
        app.headsensor_entry = _port_combobox(headsensor_group)
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
        app.stage_com_entry = _port_combobox(stage_group)
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
                app.status.toast(f"Stage: {len(app.stage.slots)} slots loaded ({', '.join(slot_names)}); "
                                 "the buttons are in Live View")
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

        # ============ LIGHT SOURCES ============
        # One table for everything about the bench's sources: the controller
        # and channel each hangs off, its setpoint, and what the laser itself
        # reports. A setpoint reaches an OBIS laser at once (lit or not) and the
        # CUBE when it is next armed; every change is saved and the source lists
        # on the other tabs follow. This replaced a list box, a separate card of
        # power entries and a dialog that only showed the right values until the
        # settings file was next read.
        sources_group = ttk.LabelFrame(right_col, text="Light sources", style='Setup.TLabelframe')
        sources_group.pack(**card_pack)

        app._source_reports = {}     # tag -> what the laser last reported (Read from lasers / Apply)
        tree_columns = ("controller", "setpoint", "reports", "state")
        tree_frame = ttk.Frame(sources_group)
        tree_frame.pack(fill="x", padx=8, pady=(2, 6))
        tree = ttk.Treeview(tree_frame, columns=tree_columns, show="tree headings", selectmode="browse",
                            height=len(app.laser_configs))
        tree.heading("#0", text="Source", anchor="w")
        tree.heading("controller", text="Controller", anchor="w")
        tree.heading("setpoint", text="Setpoint", anchor="e")
        tree.heading("reports", text="Laser reports", anchor="w")
        tree.heading("state", text="State", anchor="w")
        tree.column("#0", width=logical_px(app, 96), minwidth=logical_px(app, 80), stretch=False, anchor="w")
        tree.column("controller", width=logical_px(app, 100), minwidth=logical_px(app, 84), stretch=False, anchor="w")
        tree.column("setpoint", width=logical_px(app, 90), minwidth=logical_px(app, 76), stretch=False, anchor="e")
        tree.column("reports", width=logical_px(app, 250), minwidth=logical_px(app, 120), stretch=True, anchor="w")
        tree.column("state", width=logical_px(app, 56), minwidth=logical_px(app, 48), stretch=False, anchor="w")
        tree_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=tree_scroll.set)
        tree.pack(side="left", fill="x", expand=True)
        tree_scroll.pack(side="right", fill="y")
        app.sources_tree = tree

        def _report_text(tag):
            report = app._source_reports.get(tag)
            config = app.laser_configs.get(tag, {})
            kind = str(config.get("type", "")).upper()
            if kind != light_sources.OBIS:
                return "switch only" if kind == light_sources.RELAY else "set when armed"
            if not report:
                return "not read yet"
            if "error" in report:
                return f"no answer: {report['error']}"
            # Apply only learns the setpoint; Read from lasers fills in the rest.
            parts = []
            if report.get("max_mw") is not None:
                parts.append(f"{report.get('min_mw', 0.0):.1f}–{report['max_mw']:.1f} mW")
            if report.get("setpoint_mw") is not None:
                parts.append(f"set {report['setpoint_mw']:.1f}")
            if report.get("on") and report.get("output_mw") is not None:
                parts.append(f"out {report['output_mw']:.1f} mW")
            return "  ·  ".join(parts) or "not read yet"

        def _setpoint_text(tag):
            config = app.laser_configs.get(tag, {})
            if str(config.get("type", "")).upper() == light_sources.RELAY:
                return "—"
            return f"{float(config.get('power_mw', 0.0) or 0.0):.1f} mW"

        def refresh_sources_table():
            """Rebuild the rows from the source table; keeps the selection."""
            selected = tree.selection()
            keep = selected[0] if selected else None
            for item in tree.get_children():
                tree.delete(item)
            tags = light_sources.sort_tags(app.laser_configs)
            for tag in tags:
                config = app.laser_configs[tag]
                color_tag = f"src_{tag}"
                tree.tag_configure(color_tag, foreground=source_color(tag))
                tree.insert("", "end", iid=tag, text=source_label(tag), tags=(color_tag,),
                            values=(light_sources.describe_controller(config), _setpoint_text(tag),
                                    _report_text(tag), "on" if tag in app.lit_sources else "off"))
            tree.configure(height=max(3, min(10, len(tags))))
            if keep in tags:
                tree.selection_set(keep)
            _sync_setpoint_editor()

        # ---- setpoint editor ------------------------------------------------
        editor = ttk.Frame(sources_group)
        editor.pack(fill="x", padx=8, pady=(0, 4))
        app.source_setpoint_label = ttk.Label(editor, text="Setpoint:", style='SetupLabel.TLabel')
        app.source_setpoint_label.pack(side="left", padx=(0, 6))
        app.source_setpoint_entry = ttk.Entry(editor, width=9, font=Fonts.BODY, justify="right")
        app.source_setpoint_entry.pack(side="left")
        ttk.Label(editor, text="mW", foreground=Colors.TEXT_MUTED, font=Fonts.BODY_SMALL).pack(side="left", padx=(4, 8))
        app.source_apply_btn = ttk.Button(editor, text="Apply", style='SetupAction.TButton', width=9)
        app.source_apply_btn.pack(side="left")
        Tooltip(app.source_apply_btn, "Send this setpoint to the laser now and save it. An OBIS laser takes "
                                      "it whether it is lit or not and reports the value it accepted.")
        app.source_read_btn = ttk.Button(editor, text="Read from lasers", style='SetupButton.TButton', width=16)
        app.source_read_btn.pack(side="right")
        Tooltip(app.source_read_btn, "Ask every OBIS laser for its power range, setpoint and output. "
                                     "Needs the OBIS port; Coherent Connection must be closed.")

        def _selected_tag():
            selected = tree.selection()
            return selected[0] if selected and selected[0] in app.laser_configs else None

        def _sync_setpoint_editor(*_event):
            tag = _selected_tag()
            entry = app.source_setpoint_entry
            entry.configure(state="normal")
            entry.delete(0, "end")
            if tag is None:
                app.source_setpoint_label.configure(text="Setpoint:")
                entry.configure(state="disabled")
                app.source_apply_btn.state(["disabled"])
                return
            kind = str(app.laser_configs[tag].get("type", "")).upper()
            app.source_setpoint_label.configure(text=f"Setpoint for {source_label(tag)}:")
            if kind == light_sources.RELAY:
                entry.insert(0, "—")
                entry.configure(state="disabled")
                app.source_apply_btn.state(["disabled"])
            else:
                entry.insert(0, f"{app.source_power_mw(tag):.1f}")
                app.source_apply_btn.state(["!disabled"])

        tree.bind("<<TreeviewSelect>>", _sync_setpoint_editor)
        tree.bind("<Double-1>", lambda _event: (app.source_setpoint_entry.focus_set(),
                                               app.source_setpoint_entry.select_range(0, "end")))

        def apply_setpoint(*_event):
            tag = _selected_tag()
            if tag is None:
                return
            text = app.source_setpoint_entry.get().strip().replace("mW", "").strip()
            try:
                power_mw = float(text)
            except ValueError:
                messagebox.showerror("Setpoint", f"'{text}' is not a number. Enter the setpoint in mW.")
                return
            problems = light_sources.validate_source(tag, app.laser_configs[tag]["type"],
                                                     app.laser_configs[tag].get("channel"), power_mw,
                                                     app.laser_configs, replacing=tag)
            if problems:
                messagebox.showerror("Setpoint", "\n".join(problems))
                return
            report = app._source_reports.get(tag) or {}
            if report.get("max_mw") and power_mw > float(report["max_mw"]) + 1e-6:
                if not messagebox.askyesno("Setpoint", f"{source_label(tag)} reported a maximum of "
                                           f"{float(report['max_mw']):.1f} mW. Send {power_mw:.1f} mW anyway?"):
                    return
            app._update_ports_from_ui()

            def _done(accepted_mw):
                stored = power_mw if accepted_mw is None else float(accepted_mw)
                app.set_source_power_mw(tag, stored)
                if accepted_mw is not None and abs(float(accepted_mw) - power_mw) > 0.05:
                    app.status.toast(f"{source_label(tag)}: the laser accepted {stored:.1f} mW "
                                     f"(asked {power_mw:.1f} mW)", kind="warning")
                else:
                    app.status.toast(f"{source_label(tag)} setpoint {stored:.1f} mW")
                if accepted_mw is not None:
                    app._source_reports.setdefault(tag, {}).update({"setpoint_mw": float(accepted_mw)})
                save_settings_silently()
                refresh_sources_table()

            def _failed(exc):
                refresh_sources_table()
                _post_error(f"Setpoint {source_label(tag)}", exc)

            run_in_worker(app, lambda: app.apply_source_power(tag, power_mw), on_done=_done,
                          on_error=_failed, busy=(app.source_apply_btn,), name=f"setpoint-{tag}")

        app.source_apply_btn.configure(command=apply_setpoint)
        app.source_setpoint_entry.bind("<Return>", apply_setpoint)

        def read_from_lasers():
            tags = [tag for tag in light_sources.sort_tags(app.laser_configs)
                    if str(app.laser_configs[tag].get("type", "")).upper() == light_sources.OBIS]
            if not tags:
                app.status.toast("No OBIS source is configured", kind="warning")
                return
            app._update_ports_from_ui()

            def _work():
                reports = {}
                for tag in tags:
                    try:
                        reports[tag] = app.read_source_report(tag)
                    except Exception as exc:
                        reports[tag] = {"error": str(exc)}
                return reports

            def _done(reports):
                app._source_reports.update(reports)
                failed = [tag for tag, report in reports.items() if "error" in report]
                refresh_sources_table()
                if failed:
                    first = reports[failed[0]]["error"]
                    app.status.toast(f"No answer from {', '.join(source_label(t) for t in failed)}: {first}",
                                     kind="warning")
                else:
                    app.status.toast(f"Read {len(reports)} OBIS source(s)", kind="success")

            run_in_worker(app, _work, on_done=_done, on_error=lambda exc: _post_error("Read from lasers", exc),
                          busy=(app.source_read_btn,), name="read-sources")

        app.source_read_btn.configure(command=read_from_lasers)

        # ---- add / remove ---------------------------------------------------
        actions = ttk.Frame(sources_group)
        actions.pack(fill="x", padx=8, pady=(4, 2))

        def add_source():
            """A small form; problems are shown on it, not in a chain of message boxes."""
            dialog = tk.Toplevel(app)
            dialog.title("Add light source")
            dialog.transient(app)
            dialog.resizable(False, False)
            dialog.grab_set()
            try:
                dialog.iconbitmap(get_resource_path("sciglob_symbol.ico"))
            except Exception:
                pass
            body = ttk.Frame(dialog, padding=Spacing.PAD_XL, style="Surface.TFrame")
            body.pack(fill="both", expand=True)
            ttk.Label(body, text="Add light source", font=Fonts.H3).grid(row=0, column=0, columnspan=3,
                                                                           sticky="w", pady=(0, 12))
            body.columnconfigure(1, weight=1)

            ttk.Label(body, text="Wavelength (nm)").grid(row=1, column=0, sticky="w", pady=4, padx=(0, 12))
            wl_entry = ttk.Entry(body, width=12, font=Fonts.BODY)
            wl_entry.grid(row=1, column=1, sticky="w", pady=4)
            ttk.Label(body, text="e.g. 561", foreground=Colors.TEXT_MUTED,
                      font=Fonts.CAPTION).grid(row=1, column=2, sticky="w", padx=(8, 0))

            ttk.Label(body, text="Controller").grid(row=2, column=0, sticky="w", pady=4, padx=(0, 12))
            kind_var = tk.StringVar(value=light_sources.OBIS)
            kind_combo = ttk.Combobox(body, textvariable=kind_var, values=list(light_sources.SOURCE_TYPES),
                                      state="readonly", width=10, font=Fonts.BODY)
            kind_combo.grid(row=2, column=1, sticky="w", pady=4)
            kind_hint = ttk.Label(body, text="", foreground=Colors.TEXT_MUTED, font=Fonts.CAPTION)
            kind_hint.grid(row=2, column=2, sticky="w", padx=(8, 0))

            ttk.Label(body, text="Channel").grid(row=3, column=0, sticky="w", pady=4, padx=(0, 12))
            channel_var = tk.StringVar(value="1")
            channel_spin = ttk.Spinbox(body, textvariable=channel_var, from_=1, to=6, width=5, font=Fonts.BODY)
            channel_spin.grid(row=3, column=1, sticky="w", pady=4)
            channel_hint = ttk.Label(body, text="", foreground=Colors.TEXT_MUTED, font=Fonts.CAPTION)
            channel_hint.grid(row=3, column=2, sticky="w", padx=(8, 0))

            ttk.Label(body, text="Setpoint (mW)").grid(row=4, column=0, sticky="w", pady=4, padx=(0, 12))
            power_entry = ttk.Entry(body, width=12, font=Fonts.BODY)
            power_entry.insert(0, "10.0")
            power_entry.grid(row=4, column=1, sticky="w", pady=4)
            power_hint = ttk.Label(body, text="milliwatts", foreground=Colors.TEXT_MUTED, font=Fonts.CAPTION)
            power_hint.grid(row=4, column=2, sticky="w", padx=(8, 0))

            problem = ttk.Label(body, text="", foreground=Colors.DANGER, font=Fonts.BODY_SMALL,
                                wraplength=logical_px(app, 360), justify="left")
            problem.grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))

            def _on_kind(*_args):
                kind = kind_var.get()
                used = {int(cfg["channel"]) for cfg in app.laser_configs.values()
                        if str(cfg.get("type", "")).upper() == kind and cfg.get("channel")}
                free = []
                if kind == light_sources.OBIS:
                    kind_hint.configure(text="Coherent laser box, by slot")
                    channel_spin.configure(from_=1, to=6, state="normal")
                    power_entry.configure(state="normal")
                    free = [c for c in light_sources.OBIS_CHANNELS if c not in used]
                    channel_hint.configure(text=f"slot 1–6; free: {', '.join(map(str, free)) or 'none'}")
                elif kind == light_sources.RELAY:
                    kind_hint.configure(text="switched by the relay board")
                    channel_spin.configure(from_=1, to=4, state="normal")
                    power_entry.configure(state="normal")
                    free = [c for c in light_sources.RELAY_CHANNELS if c not in used]
                    channel_hint.configure(text=f"relay 1–4; free: {', '.join(map(str, free)) or 'none'}")
                else:
                    kind_hint.configure(text="single CUBE laser on its own port")
                    channel_spin.configure(state="disabled")
                    power_entry.configure(state="normal")
                    channel_hint.configure(text="not needed")
                if kind != light_sources.CUBE and free:
                    channel_var.set(str(free[0]))

            kind_var.trace_add("write", _on_kind)
            _on_kind()

            result = {"tag": None}

            def on_confirm(*_event):
                tag = wl_entry.get().strip()
                kind = kind_var.get()
                try:
                    channel = int(channel_var.get()) if kind != light_sources.CUBE else None
                except ValueError:
                    channel = None
                problems = light_sources.validate_source(tag, kind, channel, power_entry.get().strip(),
                                                         app.laser_configs)
                if problems:
                    problem.configure(text="\n".join(problems))
                    return
                app.laser_configs[tag] = {"type": kind, "channel": channel,
                                          "power_mw": float(power_entry.get().strip())}
                result["tag"] = tag
                dialog.destroy()

            buttons = ttk.Frame(body, style="Surface.TFrame")
            buttons.grid(row=6, column=0, columnspan=3, sticky="e", pady=(16, 0))
            ttk.Button(buttons, text="Cancel", command=dialog.destroy,
                       style='SetupButton.TButton', width=10).pack(side="right")
            ttk.Button(buttons, text="Add", command=on_confirm,
                       style='SetupAction.TButton', width=10).pack(side="right", padx=(0, 8))
            dialog.bind("<Return>", on_confirm)
            dialog.bind("<Escape>", lambda _event: dialog.destroy())
            dialog.configure(bg=Colors.BG_SURFACE)
            harmonize(dialog, Colors.BG_SURFACE)
            center_over(dialog, app, min_width=logical_px(app, 440))
            wl_entry.focus_set()
            app.add_source_dialog = dialog
            app.wait_window(dialog)
            app.add_source_dialog = None

            tag = result["tag"]
            if tag is not None:
                LOGGER.info("Source added: %s (%s)", tag, light_sources.describe_controller(app.laser_configs[tag]))
                app.rebuild_laser_ui()
                save_settings_silently()
                refresh_sources_table()
                tree.selection_set(tag)
                app.status.toast(f"{source_label(tag)} added to every tab", kind="success")

        def remove_source():
            tag = _selected_tag()
            if tag is None:
                messagebox.showwarning("Remove source", "Select a source in the table first.")
                return
            if tag == light_sources.LAMP_TAG:
                messagebox.showwarning("Remove source", "The Hg-Ar calibration lamp is part of every bench "
                                       "and cannot be removed.")
                return
            if len(app.laser_configs) <= 2:
                messagebox.showwarning("Remove source", "Keep at least one laser besides the lamp.")
                return
            if tag in app.lit_sources:
                messagebox.showwarning("Remove source", f"{source_label(tag)} is on. Switch it off first.")
                return
            config = app.laser_configs[tag]
            if not messagebox.askyesno("Remove source",
                                       f"Remove {source_label(tag)} ({light_sources.describe_controller(config)}) "
                                       f"from the bench?\n\nIt disappears from Live View, Check Resolution and "
                                       f"Measurements. Add it again later if needed."):
                return
            LOGGER.info("Source removed: %s", tag)
            del app.laser_configs[tag]
            app._source_reports.pop(tag, None)
            app.rebuild_laser_ui()
            save_settings_silently()
            refresh_sources_table()
            app.status.toast(f"{source_label(tag)} removed", kind="success")

        app.add_source = add_source
        app.remove_source = remove_source
        app.refresh_sources_table = refresh_sources_table
        ttk.Button(actions, text="+ Add source…", command=add_source,
                   style='SetupButton.TButton', width=15).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="− Remove", command=remove_source,
                   style='SetupButton.TButton', width=11).pack(side="left")
        ttk.Label(sources_group,
                  text="OBIS setpoints reach the laser at once; the CUBE takes its power when next armed; "
                       "relay sources only switch. Changes are saved automatically.",
                  foreground=Colors.TEXT_MUTED, font=Fonts.CAPTION_ITALIC,
                  wraplength=logical_px(app, 640), justify="left").pack(anchor="w", padx=8, pady=(4, 6))

        # The table follows every switch (on/off) and every edit of the source table.
        app._source_listeners.append(refresh_sources_table)
        refresh_sources_table()

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

        bind_debounced_configure(scrollable.canvas, _layout_setup)


    def refresh_ports():
        """List the ports in every dropdown. Fields are not overwritten: which
        port is the laser box and which the relay board is the operator's call,
        and the old first-four fill put arbitrary ports into them."""
        if serial is None:
            messagebox.showwarning("Ports", "pyserial is not installed.")
            return

        def _done(ports):
            _ports_found(ports)
            if not ports:
                app.status.toast("No serial ports detected", kind="warning")
                return
            names = ", ".join(f"{device} ({description})" if description else device
                              for device, description in ports)
            app.status.toast(f"{len(ports)} serial port{'s' if len(ports) != 1 else ''}: {names}")

        run_in_worker(app, _enumerate_ports, on_done=_done, name="serial-ports")


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
        busy = app.acq.busy_reason()
        if busy:
            messagebox.showwarning("Connect", busy)
            return
        if app.spec is not None:
            # A second Connect used to drop the live backend without releasing its DLL handle.
            disconnect_spectrometer()
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
            app.eeprom_current_devconfig = None   # an EEPROM read belongs to one device only
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
        """Stop whatever is driving the device, let its thread finish, then release the backend."""
        try:
            app.stop_live()
            app.stop_measure()
            running = getattr(app, "check_res_running", None)
            if running is not None:
                running.clear()
            app.request_spectrometer_stop()
            # The acquisition threads call measure()/set_it() on this handle:
            # give them a moment to leave before the DLL is torn down under them.
            for attr in ("live_thread", "measure_thread", "check_res_thread"):
                worker = getattr(app, attr, None)
                if worker is not None and worker.is_alive():
                    worker.join(timeout=2.0)
            if app.spec:
                try:
                    app.spec.disconnect()
                except Exception:
                    pass
            app.spec = None
            app.spec_backend = None
            app.eeprom_current_devconfig = None
            app.acq.release(None)
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
        """The setpoint for a source in mW, from the source table (not a widget: safe from any thread)."""
        return app.source_power_mw(tag)

    def save_settings_silently():
        """Save all settings without showing a message box."""
        app._update_ports_from_ui()
        app.hw.dll_path = app.dll_entry.get().strip()
        app.hw.spectrometer_type = app.spec_type_var.get().strip() or "Auto"
        app.stage_config_path = app.stage_config_entry.get().strip()

        # The source table is the record; laser_power (mW) is kept for older readers.
        app.hw.laser_power = {tag: float(cfg.get("power_mw", 0.0) or 0.0) for tag, cfg in app.laser_configs.items()}

        payload = {
            "spectrometer_type": app.hw.spectrometer_type,
            "last_connected_type": getattr(app.hw, "last_connected_type", ""),
            "dll_path": app.hw.dll_path,
            "com_ports": app.hw.com_ports,
            "laser_power": app.hw.laser_power,
            "laser_configs": app.laser_configs,
            "laser_configs_version": light_sources.SETTINGS_VERSION,
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
        """Save all settings including laser configurations.

        Ports and the DLL path are read from the form on every use, so no
        restart is needed (the old dialog said one was). A toast replaces it.
        """
        try:
            save_settings_silently()
        except Exception as e:
            messagebox.showerror("Settings", str(e))
            return
        app.status.toast(f"Settings saved ({len(app.laser_configs)} sources) to {os.path.basename(SETTINGS_FILE)}")

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
            app.rebuild_laser_ui()

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

            # The source table. Files from 3.4.7 and earlier carry no version:
            # their OBIS setpoints were in watts and their channels implied.
            if isinstance(obj.get("laser_configs"), dict):
                raw_sources = obj["laser_configs"]
            elif "available_lasers" in obj:
                # Older still: a list of tags plus a power dict.
                defaults = light_sources.default_sources()
                powers = obj.get("laser_power", {}) if isinstance(obj.get("laser_power"), dict) else {}
                raw_sources = {}
                for tag in obj["available_lasers"]:
                    base = defaults.get(str(tag), {"type": "OBIS"})
                    raw_sources[str(tag)] = {"type": base["type"],
                                             "power": powers.get(str(tag), base.get("power_mw", 0.0))}
            else:
                raw_sources = None
            if raw_sources is not None:
                app.laser_configs = light_sources.load_sources(raw_sources, obj.get("laser_configs_version"))
                LOGGER.info("Loaded %d light sources: %s", len(app.laser_configs),
                            ", ".join(light_sources.sort_tags(app.laser_configs)))
            # Every source list (Setup's table, the checkboxes, the run checklist) follows the file.
            app.rebuild_laser_ui()

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
    # The port dropdowns fill in a moment after the window is up.
    try:
        app.after(1500, refresh_port_lists_async)
    except tk.TclError:
        pass
