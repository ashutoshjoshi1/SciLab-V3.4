"""Setup tab - spectrometer, COM port, laser, and head sensor configuration."""
import json
import logging
import os
import sys
import time
import traceback
from typing import Dict, List, Optional

import numpy as np
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
from .ui_utils import ScrollableFrame, bind_debounced_configure

LOGGER = logging.getLogger(__name__)

def build(app):
    from .theme import Colors, Fonts, Spacing

    # Import constants from app
    DEFAULT_COM_PORTS = app.DEFAULT_COM_PORTS
    DEFAULT_ALL_LASERS = app.DEFAULT_ALL_LASERS
    DEFAULT_LASER_POWERS = app.DEFAULT_LASER_POWERS
    SETTINGS_FILE = app.SETTINGS_FILE
    OBIS_LASER_MAP = {
        "405": 5,
        "445": 4,
        "488": 3,
        "640": 2,
        "685": 6,
    }

    def _build_setup_tab():
        # Main container with better spacing
        main_frame = ttk.Frame(app.setup_tab)
        main_frame.pack(fill="both", expand=True, padx=16, pady=16)

        scrollable = ScrollableFrame(main_frame, x_scroll=True, y_scroll=True, background=Colors.BG_PRIMARY)
        scrollable.pack(fill="both", expand=True)
        frame = scrollable.content

        # Add a title header
        title_frame = ttk.Frame(frame)
        title_frame.pack(fill="x", padx=10, pady=(0, Spacing.PAD_MD))
        ttk.Label(title_frame, text="System Configuration",
                 font=Fonts.H1).pack(anchor="w")

        # ============ SPECTROMETER SECTION ============
        spec_group = ttk.LabelFrame(frame, text="Spectrometer Configuration",
                                    style='Setup.TLabelframe')
        spec_group.pack(fill="x", padx=8, pady=8)
        
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
            if app.dll_entry.get().strip():
                return
            suggested = suggest_default_dll_path(app.spec_type_var.get())
            if suggested:
                app.dll_entry.delete(0, "end")
                app.dll_entry.insert(0, suggested)

        app.spec_type_combo.bind("<<ComboboxSelected>>", on_spec_type_change)

        ttk.Label(spec_group, text="Driver DLL / Runtime Path:",
                 style='SetupLabel.TLabel').grid(row=1, column=0, sticky="w", padx=8, pady=8)
        app.dll_entry = ttk.Entry(spec_group, width=70, font=Fonts.BODY)
        app.dll_entry.grid(row=1, column=1, sticky="ew", padx=8, pady=8)
        ttk.Button(spec_group, text="Browse...", command=browse_dll,
                  style='SetupButton.TButton').grid(row=1, column=2, padx=8, pady=8)

        # Status indicator frame
        status_frame = ttk.Frame(spec_group)
        status_frame.grid(row=2, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 8))
        
        ttk.Label(status_frame, text="Status:", 
                 style='SetupLabel.TLabel').pack(side="left", padx=(0, 8))
        app.spec_status = ttk.Label(status_frame, text="● Disconnected", 
                                   foreground="red", style='SetupStatus.TLabel')
        app.spec_status.pack(side="left")

        # Action buttons frame
        button_frame = ttk.Frame(spec_group)
        button_frame.grid(row=3, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 8))
        
        ttk.Button(button_frame, text="Connect", command=connect_spectrometer,
                  style='SetupAction.TButton', width=15).pack(side="left", padx=(0, 8))
        ttk.Button(button_frame, text="Disconnect", command=disconnect_spectrometer,
                  style='SetupButton.TButton', width=15).pack(side="left")

        # ============ TWO-COLUMN LAYOUT FOR COM PORTS AND HEAD SENSOR ============
        ports_headsensor_row = ttk.Frame(frame)
        ports_headsensor_row.pack(fill="x", padx=8, pady=8)
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

        # Status indicators
        app.obis_status = ttk.Label(ports_group, text="●", foreground="red", 
                                    font=Fonts.STATUS_ICON)
        app.cube_status = ttk.Label(ports_group, text="●", foreground="red", 
                                    font=Fonts.STATUS_ICON)
        app.relay_status = ttk.Label(ports_group, text="●", foreground="red", 
                                     font=Fonts.STATUS_ICON)
        app.obis_status.grid(row=0, column=2, padx=8)
        app.cube_status.grid(row=1, column=2, padx=8)
        app.relay_status.grid(row=2, column=2, padx=8)

        # Action buttons
        ports_button_frame = ttk.Frame(ports_group)
        ports_button_frame.grid(row=3, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 8))
        
        ttk.Button(ports_button_frame, text="Refresh Ports", command=refresh_ports,
                  style='SetupAction.TButton', width=18).pack(side="left", padx=(0, 8))
        ttk.Button(ports_button_frame, text="Test Connect", command=test_com_connect,
                  style='SetupButton.TButton', width=18).pack(side="left")

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
        
        app.headsensor_status = ttk.Label(headsensor_group, text="●", foreground="red",
                                         font=Fonts.STATUS_ICON)
        app.headsensor_status.grid(row=0, column=2, padx=8)

        # Test button
        headsensor_button_frame = ttk.Frame(headsensor_group)
        headsensor_button_frame.grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 8))
        
        ttk.Button(headsensor_button_frame, text="Test Connect", 
                  command=test_headsensor_connect,
                  style='SetupButton.TButton', width=18).pack(side="left")

        def _layout_ports_and_headsensor(width=None, _height=None):
            available_width = width or scrollable.canvas.winfo_width() or main_frame.winfo_width()
            if available_width and available_width < 920:
                ports_group.grid_configure(row=0, column=0, padx=0, pady=(0, 4))
                headsensor_group.grid_configure(row=1, column=0, padx=0, pady=(4, 0))
                ports_headsensor_row.columnconfigure(0, weight=1)
                ports_headsensor_row.columnconfigure(1, weight=0)
            else:
                ports_group.grid_configure(row=0, column=0, padx=(0, 4), pady=0)
                headsensor_group.grid_configure(row=0, column=1, padx=(4, 0), pady=0)
                ports_headsensor_row.columnconfigure(0, weight=1)
                ports_headsensor_row.columnconfigure(1, weight=1)

        bind_debounced_configure(scrollable.canvas, _layout_ports_and_headsensor)

        # ============ STAGE CONTROLLER SECTION ============
        stage_group = ttk.LabelFrame(frame, text="Stage Motor Controller",
                                     style='Setup.TLabelframe')
        stage_group.pack(fill="x", padx=8, pady=8)
        stage_group.columnconfigure(1, weight=1)

        ttk.Label(stage_group, text="Config File:",
                 style='SetupLabel.TLabel').grid(row=0, column=0, sticky="e", padx=8, pady=6)
        app.stage_config_entry = ttk.Entry(stage_group, width=50, font=Fonts.BODY)
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

        app.stage_status = ttk.Label(stage_group, text="●", foreground="red",
                                     font=Fonts.STATUS_ICON)
        app.stage_status.grid(row=1, column=2, padx=8)

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
            ok = app.stage.connect(port)
            app.stage_status.config(foreground=("green" if ok else "red"))
            if ok:
                app.hw.com_ports["STAGE"] = port
                # Read positions to verify communication
                x, y = app.stage.read_positions()
                app.stage.disconnect()
                pos_str = f"X={x}, Y={y}" if x is not None else "positions unavailable"
                messagebox.showinfo("Stage", f"Stage connected successfully!\n{pos_str}")
            else:
                messagebox.showerror("Stage", f"Could not connect to stage on {port}")

        ttk.Button(stage_btn_frame, text="Load Config", command=load_stage_config,
                  style='SetupAction.TButton', width=16).pack(side="left", padx=(0, 8))
        ttk.Button(stage_btn_frame, text="Test Connect", command=test_stage_connect,
                  style='SetupButton.TButton', width=16).pack(side="left")

        # ============ LASER MANAGEMENT SECTION ============
        laser_mgmt_group = ttk.LabelFrame(frame, text="Laser Management",
                                        style='Setup.TLabelframe')
        laser_mgmt_group.pack(fill="x", padx=8, pady=8)
        
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
                                       font=Fonts.MONO,
                                       selectmode=tk.SINGLE,
                                       activestyle='none',
                                       highlightthickness=1,
                                       highlightcolor=Colors.ACCENT,
                                       relief='solid',
                                       borderwidth=1)
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
            wl_str = f"{wavelength:>6} nm"
            type_str = f"{laser_type:<6}"
            power_str = f"{power:>8.2f} mW"
            return f"{wl_str}  │  {type_str}  │  {power_str}"
        
        for laser in sorted(app.laser_configs.keys(), 
                           key=lambda x: float(x) if x.replace('.','').isdigit() else 999):
            app.laser_listbox.insert(tk.END, format_laser_display(laser))
        
        # Buttons for laser management
        laser_button_frame = ttk.Frame(laser_mgmt_group)
        laser_button_frame.pack(fill="x", padx=8, pady=(0, 8))
        
        def add_laser():
            """Add a new laser with custom dialog for wavelength, type, and power."""
            # Create custom dialog
            dialog = tk.Toplevel(app)
            dialog.title("Add New Laser")
            dialog.geometry("450x340")
            dialog.transient(app)
            dialog.grab_set()
            
            # Set icon if available
            try:
                dialog.iconbitmap(get_resource_path("sciglob_symbol.ico"))
            except Exception:
                pass
            
            # Center dialog on screen
            dialog.update_idletasks()
            x = (dialog.winfo_screenwidth() // 2) - (450 // 2)
            y = (dialog.winfo_screenheight() // 2) - (340 // 2)
            dialog.geometry(f"+{x}+{y}")
            
            # Configure dialog style
            dialog_frame = ttk.Frame(dialog, padding=20)
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
                     foreground="gray", font=Fonts.CAPTION).pack(side="left", padx=(8, 0))
            
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
                     foreground="gray", font=Fonts.CAPTION).pack(side="left", padx=(8, 0))
            
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
            
            # Rebuild power configuration section
            rebuild_power_entries()
        
        ttk.Button(laser_button_frame, text="+ Add Laser", command=add_laser,
                  style='SetupAction.TButton', width=16).pack(side="left", padx=(0, 6))
        ttk.Button(laser_button_frame, text="- Remove Laser", command=remove_laser,
                  style='SetupButton.TButton', width=16).pack(side="left")
        
        ttk.Label(laser_mgmt_group, text="Tip: Laser changes are saved automatically when you add or remove lasers.",
                 foreground=Colors.TEXT_MUTED, font=Fonts.CAPTION_ITALIC).pack(anchor="w", padx=8, pady=(6, 4))

        # ============ LASER POWER CONFIGURATION ============
        power_group = ttk.LabelFrame(frame, text="Laser Power Configuration",
                                    style='Setup.TLabelframe')
        power_group.pack(fill="x", padx=8, pady=8)

        app.power_entries = {}
        app.power_group_frame = power_group  # Store reference for rebuilding
        
        def rebuild_power_entries(force=False):
            """Rebuild power entry widgets based on current laser configurations."""
            available_width = scrollable.canvas.winfo_width() or main_frame.winfo_width() or app.winfo_width()
            column_groups = 1 if (available_width and available_width < 980) or len(app.laser_configs) <= 4 else 2
            if (
                not force
                and getattr(app, "_setup_power_column_groups", None) == column_groups
                and getattr(app, "_setup_power_laser_count", None) == len(app.laser_configs)
            ):
                return

            app._setup_power_column_groups = column_groups
            app._setup_power_laser_count = len(app.laser_configs)

            # Clear existing widgets
            for widget in power_group.winfo_children():
                widget.destroy()
            
            app.power_entries = {}
            
            # Sort lasers numerically
            sorted_lasers = sorted(app.laser_configs.keys(), 
                                  key=lambda x: float(x) if x.replace('.','').isdigit() else 999)
            lasers_per_group = max(1, (len(sorted_lasers) + column_groups - 1) // column_groups)
            total_columns = column_groups * 4

            for col in range(8):
                power_group.columnconfigure(col, weight=0)
            for col in range(total_columns):
                weight = 1 if col % 4 == 0 else 0
                power_group.columnconfigure(col, weight=weight)

            header_row = 0
            for group_index in range(column_groups):
                col_offset = group_index * 4
                ttk.Label(power_group, text="Wavelength", 
                         font=Fonts.BODY_SMALL_BOLD,
                         foreground=Colors.TEXT_SECONDARY).grid(row=header_row, column=col_offset, sticky="w", padx=(12, 0), pady=(2, 8))
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
                label_text = f"{wavelength} nm"
                label = ttk.Label(power_group, text=label_text, 
                                 style='SetupLabel.TLabel')
                label.grid(row=row, column=col_offset, sticky="e", padx=(8, 4), pady=4)
                
                # Type badge
                type_colors = {"OBIS": Colors.OBIS_COLOR, "CUBE": Colors.CUBE_COLOR, "RELAY": Colors.RELAY_COLOR}
                type_color = type_colors.get(laser_type, "#666666")
                type_badge = ttk.Label(power_group, text=f"[{laser_type}]", 
                                      foreground=type_color,
                                      font=Fonts.CAPTION)
                type_badge.grid(row=row, column=col_offset+1, sticky="w", padx=(0, 8), pady=4)
                
                # Power entry
                e = ttk.Entry(power_group, width=10, font=Fonts.BODY)
                e.insert(0, str(power))
                e.grid(row=row, column=col_offset+2, sticky="w", padx=(0, 4), pady=4)
                app.power_entries[wavelength] = e
                
                # Unit label
                ttk.Label(power_group, text="mW", foreground="gray",
                         font=Fonts.BODY_SMALL).grid(
                    row=row, column=col_offset+3, sticky="w", padx=0, pady=4)
            
            # Add separator line
            separator_row = max_row_used + 1
            ttk.Separator(power_group, orient="horizontal").grid(
                row=separator_row, column=0, columnspan=total_columns, sticky="ew", padx=8, pady=(12, 8))
            
            # Add info label at bottom after separator
            info_row = separator_row + 1
            info_frame = ttk.Frame(power_group)
            info_frame.grid(row=info_row, column=0, columnspan=total_columns, sticky="ew", padx=8, pady=(0, 8))
            
            ttk.Label(info_frame,
                     text="Power values are used as setpoints for laser control",
                     foreground=Colors.TEXT_MUTED,
                     font=Fonts.CAPTION_ITALIC).pack(side="left")
        
        # Initial build of power entries
        rebuild_power_entries(force=True)
        bind_debounced_configure(scrollable.canvas, lambda *_size: rebuild_power_entries())

        # ============ SAVE/LOAD SECTION ============
        ttk.Separator(frame, orient="horizontal").pack(fill="x", padx=8, pady=12)
        
        save_group = ttk.Frame(frame)
        save_group.pack(fill="x", padx=8, pady=(0, 8))
        
        ttk.Button(save_group, text="Save Settings", command=save_settings,
                  style='SetupAction.TButton', width=20).pack(side="left", padx=(0, 8))
        ttk.Button(save_group, text="Load Settings", command=load_settings_into_ui,
                  style='SetupButton.TButton', width=20).pack(side="left")

    def refresh_ports():
        if serial is None:
            messagebox.showwarning("Ports", "pyserial is not installed.")
            return
        ports = list(serial.tools.list_ports.comports())
        names = [p.device for p in ports]
        if names:
            app.obis_entry.delete(0, "end"); app.obis_entry.insert(0, names[0])
            if len(names) > 1:
                app.cube_entry.delete(0, "end"); app.cube_entry.insert(0, names[1])
            if len(names) > 2:
                app.relay_entry.delete(0, "end"); app.relay_entry.insert(0, names[2])
            if len(names) > 3:
                app.headsensor_entry.delete(0, "end"); app.headsensor_entry.insert(0, names[3])
            messagebox.showinfo("Ports", "Populated with first detected ports.\nAdjust as needed.")
        else:
            messagebox.showwarning("Ports", "No serial ports detected.")

    def test_com_connect():
        app._update_ports_from_ui()
        ok_obis = app.lasers.obis.open()
        app.obis_status.config(foreground=("green" if ok_obis else "red"))
        ok_cube = app.lasers.cube.open()
        app.cube_status.config(foreground=("green" if ok_cube else "red"))
        ok_relay = app.lasers.relay.open()
        app.relay_status.config(foreground=("green" if ok_relay else "red"))
        # Close after test to free ports (or keep open if you prefer)
        time.sleep(0.2)
        if ok_obis: app.lasers.obis.close()
        if ok_cube: app.lasers.cube.close()
        if ok_relay: app.lasers.relay.close()

    def test_headsensor_connect():
        app._update_ports_from_ui()
        ok_headsensor = app.filterwheel.open()
        app.headsensor_status.config(foreground=("green" if ok_headsensor else "red"))
        # Close after test to free ports
        time.sleep(0.2)
        if ok_headsensor: app.filterwheel.close()

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
        dialog.geometry("500x350")
        dialog.transient(app)
        dialog.grab_set()
        
        # Set icon if available
        try:
            dialog.iconbitmap(get_resource_path("sciglob_symbol.ico"))
        except Exception:
            pass
        
        # Center dialog on screen
        dialog.update_idletasks()
        x = (dialog.winfo_screenwidth() // 2) - (500 // 2)
        y = (dialog.winfo_screenheight() // 2) - (350 // 2)
        dialog.geometry(f"+{x}+{y}")
        
        # Configure dialog style
        dialog_frame = ttk.Frame(dialog, padding=20)
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
                            font=Fonts.MONO,
                            selectmode=tk.SINGLE,
                            activestyle='dotbox',
                            highlightthickness=1,
                            highlightcolor=Colors.ACCENT,
                            relief='solid',
                            borderwidth=1)
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
        
        # Focus on listbox
        listbox.focus_set()
        
        # Wait for dialog to close
        app.wait_window(dialog)
        
        return result["selected"]

    def connect_spectrometer():
        try:
            requested_type = app.spec_type_var.get().strip() or "Auto"
            dll = app.dll_entry.get().strip()
            if not dll:
                suggested = suggest_default_dll_path(requested_type)
                if suggested:
                    dll = suggested
                    app.dll_entry.delete(0, "end")
                    app.dll_entry.insert(0, dll)

            devices = discover_spectrometers(requested_type, dll)
            if not devices:
                raise RuntimeError("No compatible spectrometers detected.")

            selected_device = devices[0]
            if len(devices) > 1:
                selected_device = show_spectrometer_selection_dialog(devices)
                if selected_device is None:
                    return

            resolved_type = selected_device.get("type", requested_type)
            resolved_type, spec = connect_backend_spectrometer(
                resolved_type,
                dll_path=dll,
                serial=selected_device.get("serial"),
                debug_mode=1,
            )

            app.spec = spec
            # Allow saturated data through so the UI can clamp & display it
            spec.abort_on_saturation = False
            app.spec_backend = resolved_type
            app.hw.spectrometer_type = resolved_type
            app.spec_type_var.set(resolved_type)
            app.sn = getattr(spec, "sn", selected_device.get("serial", "Unknown"))
            app.data.serial_number = app.sn
            app.npix = getattr(spec, "npix_active", app.npix)
            app.data.npix = app.npix
            app.spec_status.config(text=f"● Connected ({resolved_type}, S/N: {app.sn})", foreground="green")
        except Exception as e:
            app.spec = None
            app.spec_backend = None
            app.spec_status.config(text="● Disconnected", foreground="red")
            app._post_error("Spectrometer Connect", e)

    def disconnect_spectrometer():
        try:
            app.stop_live()
            if app.spec:
                try:
                    app.spec.disconnect()
                except Exception:
                    pass
            app.spec = None
            app.spec_backend = None
            app.spec_status.config(text="● Disconnected", foreground="red")
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

        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "spectrometer_type": app.hw.spectrometer_type,
                    "dll_path": app.hw.dll_path,
                    "com_ports": app.hw.com_ports,
                    "laser_power": app.hw.laser_power,
                    "laser_configs": app.laser_configs,
                    "available_lasers": app.available_lasers,
                    "stage_config_path": app.stage_config_path,
                }, f, indent=2)
            LOGGER.info("Settings saved to %s", SETTINGS_FILE)
        except Exception as e:
            LOGGER.error("Error saving settings: %s", e)
    
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
            
            saved_type = obj.get("spectrometer_type", getattr(app.hw, "spectrometer_type", "Auto"))
            app.spec_type_var.set(saved_type)
            app.hw.spectrometer_type = saved_type
            # Load DLL path
            app.hw.dll_path = obj.get("dll_path", "")
            app.dll_entry.delete(0, "end")
            app.dll_entry.insert(0, app.hw.dll_path)
            
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
        app.after(0, lambda: messagebox.showerror(title, str(ex)))

    def on_close():
        try:
            app.stop_live()
            app.stop_measure()
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
    app.load_settings_into_ui = load_settings_into_ui
    app._post_error = _post_error
    app.on_close = on_close

    # Call the UI builder
    _build_setup_tab()
