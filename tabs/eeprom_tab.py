"""EEPROM Tab - Read and display spectrometer EEPROM data.

The AvaSpec-NEXOS Bench Only is equipped with a 128kb (16kB) serial I2C EEPROM
(24LC128T-E/MNY from Microchip) at device address 0x1010000.

This tab displays all EEPROM information including:
- Structure version, serial number, detector name
- Pixel information (total, optical black, effective pixels)
- Defective/hot pixels
- Wavelength calibration coefficients
"""
import logging

import tkinter as tk
from tkinter import ttk, messagebox

from .ui_utils import ScrollableFrame, bind_debounced_configure

LOGGER = logging.getLogger(__name__)


def build(app):
    """Build the EEPROM tab."""
    from .theme import Colors, Fonts, Spacing

    main_frame = ttk.Frame(app.eeprom_tab)
    main_frame.pack(fill="both", expand=True, padx=4, pady=4)

    scrollable = ScrollableFrame(main_frame, x_scroll=True, y_scroll=True, background=Colors.BG_PRIMARY)
    scrollable.pack(fill="both", expand=True)
    frame = scrollable.content

    # ============ HEADER ============
    header_frame = ttk.Frame(frame)
    header_frame.pack(fill="x", padx=10, pady=(0, Spacing.PAD_MD))
    header_frame.columnconfigure(0, weight=1)
    header_title = ttk.Label(header_frame, text="EEPROM Data Reader",
                             font=Fonts.H1)
    header_title.grid(row=0, column=0, sticky="w")

    read_button = ttk.Button(header_frame, text="Read EEPROM",
                             command=lambda: read_eeprom_data(app),
                             style='Accent.TButton')
    read_button.grid(row=0, column=1, sticky="e", padx=4)
    
    # ============ DEVICE INFO SECTION ============
    device_group = ttk.LabelFrame(frame, text="Device Information",
                                  style='EEPROM.TLabelframe')
    device_group.pack(fill="x", padx=8, pady=8)
    device_group.columnconfigure(1, weight=1)
    
    row = 0
    
    # Structure Version
    ttk.Label(device_group, text="Structure Version:", 
             style='EEPROMLabel.TLabel').grid(row=row, column=0, sticky="w", padx=12, pady=6)
    app.eeprom_struct_version = ttk.Label(device_group, text="--", 
                                         style='EEPROMValue.TLabel', foreground=Colors.ACCENT)
    app.eeprom_struct_version.grid(row=row, column=1, sticky="w", padx=12, pady=6)
    ttk.Label(device_group, text="(Address: 0x00-0x01)", 
             foreground=Colors.TEXT_MUTED, font=Fonts.CAPTION).grid(row=row, column=2, sticky="w", padx=4, pady=6)
    row += 1
    
    # Serial Number
    ttk.Label(device_group, text="Avabench Serial Number:", 
             style='EEPROMLabel.TLabel').grid(row=row, column=0, sticky="w", padx=12, pady=6)
    app.eeprom_serial = ttk.Label(device_group, text="--", 
                                 style='EEPROMValue.TLabel', foreground=Colors.SUCCESS)
    app.eeprom_serial.grid(row=row, column=1, sticky="w", padx=12, pady=6)
    ttk.Label(device_group, text="(Address: 0x02-0x0B)", 
             foreground=Colors.TEXT_MUTED, font=Fonts.CAPTION).grid(row=row, column=2, sticky="w", padx=4, pady=6)
    row += 1
    
    # Detector Name
    ttk.Label(device_group, text="Detector Name:", 
             style='EEPROMLabel.TLabel').grid(row=row, column=0, sticky="w", padx=12, pady=6)
    app.eeprom_detector = ttk.Label(device_group, text="--", 
                                   style='EEPROMValue.TLabel', foreground=Colors.ACCENT)
    app.eeprom_detector.grid(row=row, column=1, sticky="w", padx=12, pady=6)
    ttk.Label(device_group, text="(Address: 0x0C-0x4B)", 
             foreground=Colors.TEXT_MUTED, font=Fonts.CAPTION).grid(row=row, column=2, sticky="w", padx=4, pady=6)
    
    # ============ PIXEL INFORMATION SECTION ============
    pixel_group = ttk.LabelFrame(frame, text="Pixel Configuration",
                                 style='EEPROM.TLabelframe')
    pixel_group.pack(fill="x", padx=8, pady=8)
    pixel_group.columnconfigure(1, weight=1)
    
    row = 0
    
    # Total Pixels
    ttk.Label(pixel_group, text="Total Pixels:", 
             style='EEPROMLabel.TLabel').grid(row=row, column=0, sticky="w", padx=12, pady=6)
    app.eeprom_total_pixels = ttk.Label(pixel_group, text="--", 
                                       style='EEPROMValue.TLabel', foreground=Colors.ACCENT)
    app.eeprom_total_pixels.grid(row=row, column=1, sticky="w", padx=12, pady=6)
    ttk.Label(pixel_group, text="(Address: 0x4C-0x4D)", 
             foreground=Colors.TEXT_MUTED, font=Fonts.CAPTION).grid(row=row, column=2, sticky="w", padx=4, pady=6)
    row += 1
    
    # Optical Black Pixels - Left
    ttk.Label(pixel_group, text="Optical Black Pixels (Left):", 
             style='EEPROMLabel.TLabel').grid(row=row, column=0, sticky="w", padx=12, pady=6)
    app.eeprom_obp_left = ttk.Label(pixel_group, text="--", 
                                   style='EEPROMValue.TLabel', foreground='purple')
    app.eeprom_obp_left.grid(row=row, column=1, sticky="w", padx=12, pady=6)
    ttk.Label(pixel_group, text="(Address: 0x4E-0x4F)", 
             foreground=Colors.TEXT_MUTED, font=Fonts.CAPTION).grid(row=row, column=2, sticky="w", padx=4, pady=6)
    row += 1
    
    # Optical Black Pixels - Right
    ttk.Label(pixel_group, text="Optical Black Pixels (Right):", 
             style='EEPROMLabel.TLabel').grid(row=row, column=0, sticky="w", padx=12, pady=6)
    app.eeprom_obp_right = ttk.Label(pixel_group, text="--", 
                                    style='EEPROMValue.TLabel', foreground='purple')
    app.eeprom_obp_right.grid(row=row, column=1, sticky="w", padx=12, pady=6)
    ttk.Label(pixel_group, text="(Address: 0x50-0x51)", 
             foreground=Colors.TEXT_MUTED, font=Fonts.CAPTION).grid(row=row, column=2, sticky="w", padx=4, pady=6)
    row += 1
    
    # First Effective Pixel
    ttk.Label(pixel_group, text="First Effective Pixel:", 
             style='EEPROMLabel.TLabel').grid(row=row, column=0, sticky="w", padx=12, pady=6)
    app.eeprom_first_pixel = ttk.Label(pixel_group, text="--", 
                                      style='EEPROMValue.TLabel', foreground=Colors.SUCCESS)
    app.eeprom_first_pixel.grid(row=row, column=1, sticky="w", padx=12, pady=6)
    ttk.Label(pixel_group, text="(Address: 0x52-0x53)", 
             foreground=Colors.TEXT_MUTED, font=Fonts.CAPTION).grid(row=row, column=2, sticky="w", padx=4, pady=6)
    row += 1
    
    # Last Effective Pixel
    ttk.Label(pixel_group, text="Last Effective Pixel:", 
             style='EEPROMLabel.TLabel').grid(row=row, column=0, sticky="w", padx=12, pady=6)
    app.eeprom_last_pixel = ttk.Label(pixel_group, text="--", 
                                     style='EEPROMValue.TLabel', foreground=Colors.SUCCESS)
    app.eeprom_last_pixel.grid(row=row, column=1, sticky="w", padx=12, pady=6)
    ttk.Label(pixel_group, text="(Address: 0x54-0x55)", 
             foreground=Colors.TEXT_MUTED, font=Fonts.CAPTION).grid(row=row, column=2, sticky="w", padx=4, pady=6)
    
    # ============ GAIN AND OFFSET SECTION (EDITABLE) ============
    gain_offset_group = ttk.LabelFrame(frame, text="Gain and Offset Values (Editable)", 
                                       style='EEPROM.TLabelframe')
    gain_offset_group.pack(fill="x", padx=8, pady=8)
    gain_offset_group.columnconfigure(1, weight=1)
    
    ttk.Label(gain_offset_group, text="Edit values below and click 'Save' to write to EEPROM:", 
             font=Fonts.CAPTION_ITALIC,
             foreground='gray').grid(row=0, column=0, columnspan=3, sticky="w", padx=12, pady=(4, 8))
    
    row = 1
    
    # Gain values (2 floats) - EDITABLE
    ttk.Label(gain_offset_group, text="Gain[0]:", 
             style='EEPROMLabel.TLabel').grid(row=row, column=0, sticky="w", padx=12, pady=6)
    app.eeprom_gain_0_entry = ttk.Entry(gain_offset_group, width=20, 
                                        font=Fonts.MONO)
    app.eeprom_gain_0_entry.grid(row=row, column=1, sticky="w", padx=12, pady=6)
    app.eeprom_gain_0_entry.insert(0, "--")
    row += 1
    
    ttk.Label(gain_offset_group, text="Gain[1]:", 
             style='EEPROMLabel.TLabel').grid(row=row, column=0, sticky="w", padx=12, pady=6)
    app.eeprom_gain_1_entry = ttk.Entry(gain_offset_group, width=20, 
                                        font=Fonts.MONO)
    app.eeprom_gain_1_entry.grid(row=row, column=1, sticky="w", padx=12, pady=6)
    app.eeprom_gain_1_entry.insert(0, "--")
    row += 1
    
    # Offset values (2 floats) - EDITABLE
    ttk.Label(gain_offset_group, text="Offset[0]:", 
             style='EEPROMLabel.TLabel').grid(row=row, column=0, sticky="w", padx=12, pady=6)
    app.eeprom_offset_0_entry = ttk.Entry(gain_offset_group, width=20, 
                                          font=Fonts.MONO)
    app.eeprom_offset_0_entry.grid(row=row, column=1, sticky="w", padx=12, pady=6)
    app.eeprom_offset_0_entry.insert(0, "--")
    row += 1
    
    ttk.Label(gain_offset_group, text="Offset[1]:", 
             style='EEPROMLabel.TLabel').grid(row=row, column=0, sticky="w", padx=12, pady=6)
    app.eeprom_offset_1_entry = ttk.Entry(gain_offset_group, width=20, 
                                          font=Fonts.MONO)
    app.eeprom_offset_1_entry.grid(row=row, column=1, sticky="w", padx=12, pady=6)
    app.eeprom_offset_1_entry.insert(0, "--")
    row += 1
    
    # Extended Offset - EDITABLE
    ttk.Label(gain_offset_group, text="Extended Offset:", 
             style='EEPROMLabel.TLabel').grid(row=row, column=0, sticky="w", padx=12, pady=6)
    app.eeprom_ext_offset_entry = ttk.Entry(gain_offset_group, width=20, 
                                            font=Fonts.MONO)
    app.eeprom_ext_offset_entry.grid(row=row, column=1, sticky="w", padx=12, pady=6)
    app.eeprom_ext_offset_entry.insert(0, "--")
    row += 1
    
    # Save button for Gain and Offset
    save_gain_button_frame = ttk.Frame(gain_offset_group)
    save_gain_button_frame.grid(row=row, column=0, columnspan=3, sticky="w", padx=12, pady=(8, 8))
    save_gain_button_frame.columnconfigure(0, weight=1)
    
    ttk.Button(save_gain_button_frame, text="Save Gain/Offset to EEPROM", 
              command=lambda: save_gain_offset_to_eeprom(app),
              style='EEPROMButton.TButton').grid(row=0, column=0, sticky="w", padx=(0, 8))
    
    save_gain_warning_label = ttk.Label(save_gain_button_frame, 
                                        text="Warning: This will permanently modify EEPROM data!", 
                                        foreground=Colors.DANGER, font=Fonts.CAPTION)
    save_gain_warning_label.grid(row=1, column=0, sticky="w", pady=(6, 0))
    
    # ============ DEFECTIVE PIXELS SECTION (EDITABLE) ============
    defect_group = ttk.LabelFrame(frame, text="Defective / Hot Pixels (Editable)", 
                                  style='EEPROM.TLabelframe')
    defect_group.pack(fill="both", expand=True, padx=8, pady=8)
    
    defect_intro_label = ttk.Label(defect_group, text="Pixel numbers (Address: 0x56-0x91, 30 entries) - Enter pixel numbers separated by commas:", 
                                   style='EEPROMLabel.TLabel', wraplength=700)
    defect_intro_label.pack(anchor="w", padx=12, pady=(8, 4))
    
    ttk.Label(defect_group, text="Note: Leave empty or enter 65535 for unused entries", 
             font=Fonts.CAPTION_ITALIC,
             foreground='gray').pack(anchor="w", padx=12, pady=(0, 4))
    
    # Editable text widget for defective pixels
    defect_frame = ttk.Frame(defect_group)
    defect_frame.pack(fill="both", expand=True, padx=12, pady=(0, 8))
    
    defect_scrollbar = ttk.Scrollbar(defect_frame, orient="vertical")
    app.eeprom_defect_text = tk.Text(defect_frame, height=4, width=80,
                                     yscrollcommand=defect_scrollbar.set,
                                     font=Fonts.MONO,
                                     wrap='word',
                                     relief='solid',
                                     borderwidth=1,
                                     bg='#FFFACD')  # Light yellow to indicate editable
    defect_scrollbar.config(command=app.eeprom_defect_text.yview)
    app.eeprom_defect_text.pack(side="left", fill="both", expand=True)
    defect_scrollbar.pack(side="right", fill="y")
    app.eeprom_defect_text.insert("1.0", "No data loaded - Click 'Read EEPROM' first")
    
    # Save button for defective pixels
    save_button_frame = ttk.Frame(defect_group)
    save_button_frame.pack(fill="x", padx=12, pady=(0, 8))
    save_button_frame.columnconfigure(0, weight=1)
    
    ttk.Button(save_button_frame, text="Save Defective Pixels to EEPROM", 
              command=lambda: save_defective_pixels_to_eeprom(app),
              style='EEPROMButton.TButton').grid(row=0, column=0, sticky="w", padx=(0, 8))
    
    save_defect_warning_label = ttk.Label(save_button_frame, 
                                          text="Warning: This will permanently modify EEPROM data!", 
                                          foreground=Colors.DANGER, font=Fonts.CAPTION)
    save_defect_warning_label.grid(row=1, column=0, sticky="w", pady=(6, 0))
    
    # ============ WAVELENGTH CALIBRATION SECTION ============
    wl_group = ttk.LabelFrame(frame, text="Wavelength Calibration Polynomial", 
                              style='EEPROM.TLabelframe')
    wl_group.pack(fill="x", padx=8, pady=8)
    wl_group.columnconfigure(1, weight=1)
    
    ttk.Label(wl_group, text="4th order polynomial (Address: 0x92-0xA5):", 
             style='EEPROMLabel.TLabel', foreground=Colors.ACCENT).grid(
        row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(8, 4))
    
    ttk.Label(wl_group, text="λ(pixel) = x₀ + x₁·pixel + x₂·pixel² + x₃·pixel³ + x₄·pixel⁴", 
             font=Fonts.CAPTION_ITALIC,
             foreground='#555').grid(row=1, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 8))
    
    row = 2
    for i in range(5):
        ttk.Label(wl_group, text=f"x{i} (Coefficient {i}):", 
                 style='EEPROMLabel.TLabel').grid(row=row+i, column=0, sticky="w", padx=12, pady=4)
        coef_label = ttk.Label(wl_group, text="--", 
                              style='EEPROMValue.TLabel', foreground=Colors.ACCENT)
        coef_label.grid(row=row+i, column=1, sticky="w", padx=12, pady=4)
        setattr(app, f'eeprom_wl_coef_{i}', coef_label)
    
    ttk.Label(wl_group, text="Note: Avantes uses 3rd order (x₀ to x₃), so x₄ = 0.0", 
             font=Fonts.CAPTION_ITALIC,
             foreground='gray').grid(row=row+5, column=0, columnspan=2, sticky="w", padx=12, pady=(4, 8))
    
    # ============ STATUS BAR ============
    status_frame = ttk.Frame(frame)
    status_frame.pack(fill="x", padx=8, pady=(8, 4))
    status_frame.columnconfigure(1, weight=1)
    
    ttk.Label(status_frame, text="ℹ️ Status:", 
             font=Fonts.BODY_SMALL_BOLD).grid(row=0, column=0, sticky="w", padx=(8, 4))
    app.eeprom_status_label = ttk.Label(status_frame, text="Ready - Click 'Read EEPROM' to load data", 
                                       font=Fonts.BODY_SMALL,
                                       foreground='#555')
    app.eeprom_status_label.grid(row=0, column=1, sticky="ew")

    def _layout_eeprom_tab(width=None, _height=None):
        available_width = width or scrollable.canvas.winfo_width() or main_frame.winfo_width()
        if not available_width:
            return

        if available_width < 620:
            header_title.grid_configure(row=0, column=0, sticky="w", pady=(0, 6))
            read_button.grid_configure(row=1, column=0, sticky="w", padx=0)
        else:
            header_title.grid_configure(row=0, column=0, sticky="w", pady=0)
            read_button.grid_configure(row=0, column=1, sticky="e", padx=4)

        wraplength = max(320, available_width - 140)
        defect_intro_label.configure(wraplength=wraplength)
        save_gain_warning_label.configure(wraplength=wraplength)
        save_defect_warning_label.configure(wraplength=wraplength)
        app.eeprom_status_label.configure(wraplength=max(280, available_width - 180))

    bind_debounced_configure(scrollable.canvas, _layout_eeprom_tab)


def _ensure_avantes_eeprom_supported(app, action: str) -> bool:
    if not app.spec:
        messagebox.showwarning("EEPROM Reader", "Please connect to a spectrometer first.")
        if hasattr(app, "eeprom_status_label"):
            app.eeprom_status_label.config(text="❌ Error: No spectrometer connected", foreground="red")
        return False

    spec_type = getattr(app.spec, "spec_type", getattr(app, "spec_backend", ""))
    if spec_type != "Ava1":
        messagebox.showinfo(
            "EEPROM Reader",
            f"{action} is only supported for Avantes spectrometers.\n\n"
            f"Connected backend: {spec_type or 'Unknown'}",
        )
        if hasattr(app, "eeprom_status_label"):
            app.eeprom_status_label.config(
                text=f"⚠ EEPROM editing is only available for Avantes (current: {spec_type or 'Unknown'})",
                foreground="orange",
            )
        return False

    return True


def read_eeprom_data(app):
    """Read EEPROM data from the connected spectrometer via AVS_GetParameter."""
    
    if not _ensure_avantes_eeprom_supported(app, "Reading EEPROM"):
        return
    
    try:
        app.eeprom_status_label.config(text="Reading EEPROM data via AVS_GetParameter...", 
                                      foreground=Colors.ACCENT)
        app.update_idletasks()
        
        # ============ READ ACTUAL EEPROM DATA VIA AVS_GetParameter ============
        # Call the get_device_config() method which uses AVS_GetParameter
        result = app.spec.get_device_config()
        
        if result != "OK":
            raise RuntimeError(f"Failed to read device parameters: {result}")
        
        # Access the DeviceConfigType structure
        # The get_device_config() method populates a DeviceConfigType structure internally
        # We need to call it again to get the structure with data
        
        from ctypes import c_uint32, c_uint16, byref
        from avantes_spectrometer import DeviceConfigType
        
        devconfig = DeviceConfigType()
        size = c_uint32(63484)
        reqsize = c_uint32()
        
        # Read device parameters from EEPROM
        resdll = app.spec.dll_handler.AVS_GetParameter(
            app.spec.spec_id, size, byref(reqsize), byref(devconfig)
        )
        
        if resdll != 0:
            raise RuntimeError(f"AVS_GetParameter failed with code: {resdll}")
        
        # ============ EXTRACT AND DISPLAY EEPROM DATA ============
        
        # Device Information
        serial_number = getattr(app.spec, 'sn', 'Unknown')
        app.eeprom_serial.config(text=serial_number)
        
        detector_name = getattr(app.spec, 'devtype', 'Unknown')
        app.eeprom_detector.config(text=detector_name)
        
        # Structure version
        struct_version = devconfig.m_ConfigVersion
        app.eeprom_struct_version.config(text=str(struct_version))
        
        # Pixel Information from EEPROM
        total_pixels = devconfig.m_Detector_m_NrPixels
        app.eeprom_total_pixels.config(text=str(total_pixels))
        
        # Calculate optical black pixels and effective range
        # These are typically stored in the start/stop pixel settings
        start_pixel = getattr(app.spec.parlist, 'm_StartPixel', 0)
        stop_pixel = getattr(app.spec.parlist, 'm_StopPixel', total_pixels - 1)
        
        # Estimate optical black pixels (common configurations)
        obp_left = start_pixel if start_pixel > 0 else 0
        obp_right = (total_pixels - stop_pixel - 1) if stop_pixel < total_pixels - 1 else 0
        
        app.eeprom_obp_left.config(text=str(obp_left))
        app.eeprom_obp_right.config(text=str(obp_right))
        app.eeprom_first_pixel.config(text=str(start_pixel))
        app.eeprom_last_pixel.config(text=str(stop_pixel))
        
        # ============ GAIN AND OFFSET VALUES FROM EEPROM ============
        gain_0 = devconfig.m_Detector_m_Gain[0]
        gain_1 = devconfig.m_Detector_m_Gain[1]
        offset_0 = devconfig.m_Detector_m_Offset[0]
        offset_1 = devconfig.m_Detector_m_Offset[1]
        ext_offset = devconfig.m_Detector_m_ExtOffset
        
        # Update Entry widgets with values
        app.eeprom_gain_0_entry.delete(0, 'end')
        app.eeprom_gain_0_entry.insert(0, f"{gain_0:.8f}")
        
        app.eeprom_gain_1_entry.delete(0, 'end')
        app.eeprom_gain_1_entry.insert(0, f"{gain_1:.8f}")
        
        app.eeprom_offset_0_entry.delete(0, 'end')
        app.eeprom_offset_0_entry.insert(0, f"{offset_0:.8f}")
        
        app.eeprom_offset_1_entry.delete(0, 'end')
        app.eeprom_offset_1_entry.insert(0, f"{offset_1:.8f}")
        
        app.eeprom_ext_offset_entry.delete(0, 'end')
        app.eeprom_ext_offset_entry.insert(0, f"{ext_offset:.8f}")
        
        # ============ DEFECTIVE/HOT PIXELS (30 entries from EEPROM) ============
        defective_pixels = []
        all_pixels = []
        for i in range(30):
            pixel_num = devconfig.m_Detector_m_DefectivePixels[i]
            all_pixels.append(pixel_num)
            if pixel_num != 65535:  # 65535 means no defective pixel at this position
                defective_pixels.append(pixel_num)
        
        # Display editable defective pixels
        app.eeprom_defect_text.delete("1.0", "end")
        
        if defective_pixels:
            # Show only non-65535 values as comma-separated
            app.eeprom_defect_text.insert("1.0", ', '.join(map(str, defective_pixels)))
        else:
            # Empty - all entries are 65535
            app.eeprom_defect_text.insert("1.0", "")
        
        # Store the devconfig for later saving
        app.eeprom_current_devconfig = devconfig
        app.eeprom_all_defective_pixels = all_pixels
        
        # ============ WAVELENGTH CALIBRATION COEFFICIENTS (5 floats from EEPROM) ============
        # These are the actual polynomial coefficients stored in EEPROM!
        # λ(pixel) = x0 + x1*pixel + x2*pixel^2 + x3*pixel^3 + x4*pixel^4
        
        wl_coefficients = []
        for i in range(5):
            coef_value = devconfig.m_Detector_m_aFit[i]
            wl_coefficients.append(coef_value)
            
            coef_label = getattr(app, f'eeprom_wl_coef_{i}')
            
            # Format with scientific notation for very small/large numbers
            if abs(coef_value) < 0.0001 or abs(coef_value) > 10000:
                coef_label.config(text=f"{coef_value:.6e}")
            else:
                coef_label.config(text=f"{coef_value:.8f}")
        
        # Success message
        app.eeprom_status_label.config(
            text="✅ EEPROM data successfully read via AVS_GetParameter", 
            foreground='green')
        
        # Create detailed summary
        summary = (
            f"✅ EEPROM Data Read Successfully!\n\n"
            f"📟 Device Information:\n"
            f"  • Serial Number: {serial_number}\n"
            f"  • Detector: {detector_name}\n"
            f"  • Structure Version: {struct_version}\n\n"
            f"Pixel Configuration:\n"
            f"  • Total Pixels: {total_pixels}\n"
            f"  • Effective Range: [{start_pixel} - {stop_pixel}]\n"
            f"  • Optical Black (L/R): {obp_left} / {obp_right}\n"
            f"  • Defective Pixels: {len(defective_pixels)}\n\n"
            f"Gain and Offset:\n"
            f"  • Gain[0]: {gain_0:.6f}\n"
            f"  • Gain[1]: {gain_1:.6f}\n"
            f"  • Offset[0]: {offset_0:.6f}\n"
            f"  • Offset[1]: {offset_1:.6f}\n"
            f"  • Extended Offset: {ext_offset:.6f}\n\n"
            f"Wavelength Calibration:\n"
        )
        
        for i, coef in enumerate(wl_coefficients):
            summary += f"  • x{i} = {coef:.6e}\n"
        
        messagebox.showinfo("EEPROM Data", summary)
        
        LOGGER.info("EEPROM data read successfully\n%s", summary)
        
    except Exception as e:
        app.eeprom_status_label.config(text=f"❌ Error reading EEPROM: {str(e)}", 
                                      foreground=Colors.DANGER)
        messagebox.showerror("EEPROM Error", 
                           f"Error reading EEPROM data:\n\n{str(e)}\n\n"
                           f"Please check spectrometer connection.")
        LOGGER.exception("EEPROM operation failed")


def save_defective_pixels_to_eeprom(app):
    """Save edited defective pixel values back to EEPROM."""
    
    if not _ensure_avantes_eeprom_supported(app, "Saving defective pixels to EEPROM"):
        return
    
    # Check if EEPROM data has been read
    if not hasattr(app, 'eeprom_current_devconfig'):
        messagebox.showwarning("Save to EEPROM", 
                             "Please read EEPROM data first before saving.")
        return
    
    try:
        # Get the edited text from the text widget
        defect_text = app.eeprom_defect_text.get("1.0", "end").strip()
        
        # Parse the comma-separated pixel numbers
        if defect_text:
            # Split by commas and clean up
            pixel_strings = [s.strip() for s in defect_text.split(',') if s.strip()]
            defective_pixels = []
            
            for pixel_str in pixel_strings:
                try:
                    pixel_num = int(pixel_str)
                    if 0 <= pixel_num <= 65535:
                        defective_pixels.append(pixel_num)
                    else:
                        raise ValueError(f"Pixel number {pixel_num} out of range (0-65535)")
                except ValueError as e:
                    messagebox.showerror("Invalid Input", 
                                       f"Invalid pixel number: {pixel_str}\n\n{str(e)}\n\n"
                                       f"Please enter valid pixel numbers (0-65535) separated by commas.")
                    return
            
            if len(defective_pixels) > 30:
                messagebox.showerror("Too Many Pixels", 
                                   f"Maximum 30 defective pixels allowed.\n\n"
                                   f"You entered {len(defective_pixels)} pixels.")
                return
        else:
            defective_pixels = []
        
        # Confirm with user before writing to EEPROM
        confirm_msg = (
            f"WARNING: Permanently Modify EEPROM\n\n"
            f"You are about to write {len(defective_pixels)} defective pixel(s) to EEPROM:\n"
            f"{', '.join(map(str, defective_pixels)) if defective_pixels else 'None (all entries will be 65535)'}\n\n"
            f"This will PERMANENTLY modify the spectrometer's EEPROM memory!\n\n"
            f"Are you sure you want to continue?"
        )
        
        if not messagebox.askyesno("Confirm EEPROM Write", confirm_msg, icon='warning'):
            return
        
        # Update the devconfig structure with new defective pixels
        devconfig = app.eeprom_current_devconfig
        
        # Fill all 30 entries
        for i in range(30):
            if i < len(defective_pixels):
                devconfig.m_Detector_m_DefectivePixels[i] = defective_pixels[i]
            else:
                devconfig.m_Detector_m_DefectivePixels[i] = 65535  # Unused entry
        
        # Write to EEPROM using AVS_SetParameter
        app.eeprom_status_label.config(text="💾 Writing to EEPROM...", 
                                      foreground=Colors.ACCENT)
        app.update_idletasks()
        
        from ctypes import byref
        resdll = app.spec.dll_handler.AVS_SetParameter(app.spec.spec_id, byref(devconfig))
        
        if resdll != 0:
            error_msg = app.spec.get_error(resdll)
            raise RuntimeError(f"AVS_SetParameter failed: {error_msg} (code: {resdll})")
        
        # Success!
        app.eeprom_status_label.config(
            text="✅ Defective pixels successfully saved to EEPROM!", 
            foreground='green')
        
        # Update stored config
        app.eeprom_current_devconfig = devconfig
        
        success_msg = (
            f"✅ Success!\n\n"
            f"Defective pixels successfully written to EEPROM:\n\n"
            f"Entries written: {len(defective_pixels)}\n"
            f"Pixel numbers: {', '.join(map(str, defective_pixels)) if defective_pixels else 'None'}\n"
            f"Unused entries: {30 - len(defective_pixels)} (set to 65535)\n\n"
            f"The changes are now permanent in the spectrometer's EEPROM."
        )
        
        messagebox.showinfo("EEPROM Write Success", success_msg)
        
        LOGGER.info("EEPROM write successful: %d defective pixels written", len(defective_pixels))
        
    except Exception as e:
        app.eeprom_status_label.config(text=f"❌ Error saving to EEPROM: {str(e)}", 
                                      foreground=Colors.DANGER)
        messagebox.showerror("EEPROM Write Error", 
                           f"Error writing to EEPROM:\n\n{str(e)}\n\n"
                           f"The EEPROM has NOT been modified.")
        LOGGER.exception("EEPROM operation failed")


def save_gain_offset_to_eeprom(app):
    """Save edited Gain and Offset values back to EEPROM."""
    
    if not _ensure_avantes_eeprom_supported(app, "Saving gain/offset to EEPROM"):
        return
    
    # Check if EEPROM data has been read
    if not hasattr(app, 'eeprom_current_devconfig'):
        messagebox.showwarning("Save to EEPROM", 
                             "Please read EEPROM data first before saving.")
        return
    
    try:
        # Get values from Entry widgets
        try:
            gain_0 = float(app.eeprom_gain_0_entry.get().strip())
            gain_1 = float(app.eeprom_gain_1_entry.get().strip())
            offset_0 = float(app.eeprom_offset_0_entry.get().strip())
            offset_1 = float(app.eeprom_offset_1_entry.get().strip())
            ext_offset = float(app.eeprom_ext_offset_entry.get().strip())
        except ValueError as e:
            messagebox.showerror("Invalid Input", 
                               f"Invalid numeric value entered!\n\n{str(e)}\n\n"
                               f"Please enter valid floating-point numbers.")
            return
        
        # Confirm with user before writing to EEPROM
        confirm_msg = (
            f"WARNING: Permanently Modify EEPROM\n\n"
            f"You are about to write the following Gain/Offset values to EEPROM:\n\n"
            f"Gain[0]: {gain_0:.8f}\n"
            f"Gain[1]: {gain_1:.8f}\n"
            f"Offset[0]: {offset_0:.8f}\n"
            f"Offset[1]: {offset_1:.8f}\n"
            f"Extended Offset: {ext_offset:.8f}\n\n"
            f"This will PERMANENTLY modify the spectrometer's EEPROM memory!\n\n"
            f"These values affect detector calibration and data quality.\n\n"
            f"Are you sure you want to continue?"
        )
        
        if not messagebox.askyesno("Confirm EEPROM Write", confirm_msg, icon='warning'):
            return
        
        # Update the devconfig structure with new Gain and Offset values
        devconfig = app.eeprom_current_devconfig
        
        devconfig.m_Detector_m_Gain[0] = gain_0
        devconfig.m_Detector_m_Gain[1] = gain_1
        devconfig.m_Detector_m_Offset[0] = offset_0
        devconfig.m_Detector_m_Offset[1] = offset_1
        devconfig.m_Detector_m_ExtOffset = ext_offset
        
        # Write to EEPROM using AVS_SetParameter
        app.eeprom_status_label.config(text="💾 Writing Gain/Offset to EEPROM...", 
                                      foreground=Colors.ACCENT)
        app.update_idletasks()
        
        from ctypes import byref
        resdll = app.spec.dll_handler.AVS_SetParameter(app.spec.spec_id, byref(devconfig))
        
        if resdll != 0:
            error_msg = app.spec.get_error(resdll)
            raise RuntimeError(f"AVS_SetParameter failed: {error_msg} (code: {resdll})")
        
        # Success!
        app.eeprom_status_label.config(
            text="✅ Gain/Offset values successfully saved to EEPROM!", 
            foreground='green')
        
        # Update stored config
        app.eeprom_current_devconfig = devconfig
        
        success_msg = (
            f"✅ Success!\n\n"
            f"Gain and Offset values successfully written to EEPROM:\n\n"
            f"Gain[0]: {gain_0:.8f}\n"
            f"Gain[1]: {gain_1:.8f}\n"
            f"Offset[0]: {offset_0:.8f}\n"
            f"Offset[1]: {offset_1:.8f}\n"
            f"Extended Offset: {ext_offset:.8f}\n\n"
            f"The changes are now permanent in the spectrometer's EEPROM.\n"
            f"These values will be used for detector calibration."
        )
        
        messagebox.showinfo("EEPROM Write Success", success_msg)
        
        LOGGER.info("Gain/Offset EEPROM write successful: G[%.8f, %.8f] O[%.8f, %.8f] ExtO=%.8f", gain_0, gain_1, offset_0, offset_1, ext_offset)
        
    except Exception as e:
        app.eeprom_status_label.config(text=f"❌ Error saving Gain/Offset: {str(e)}", 
                                      foreground=Colors.DANGER)
        messagebox.showerror("EEPROM Write Error", 
                           f"Error writing Gain/Offset to EEPROM:\n\n{str(e)}\n\n"
                           f"The EEPROM has NOT been modified.")
        LOGGER.exception("EEPROM operation failed")
