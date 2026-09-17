"""Analysis tab - tabbed interface for viewing measurement analysis results."""
import logging

import tkinter as tk
from tkinter import ttk

from .ui_utils import bind_debounced_configure

LOGGER = logging.getLogger(__name__)

def build(app):
    """Build the Analysis tab with a tabbed interface for multiple measurement runs."""
    from .theme import Colors, Fonts, Spacing

    container = ttk.Frame(app.analysis_tab)
    container.pack(fill="both", expand=True, padx=6, pady=6)

    header = ttk.Frame(container, relief="flat")
    header.pack(fill="x", pady=(0, 6))
    header.columnconfigure(0, weight=1)

    title_frame = ttk.Frame(header)
    title_frame.grid(row=0, column=0, sticky="w")
    ttk.Label(title_frame, text="Analysis Results",
             font=Fonts.H1).pack(side="left")
    
    # Action buttons on the right with better styling
    button_frame = ttk.Frame(header)
    button_frame.grid(row=0, column=1, sticky="e", padx=4)
    
    def clear_all_measurements():
        """Clear all measurement tabs except welcome tab."""
        try:
            # Get all tabs
            tabs = app.analysis_measurements_notebook.tabs()
            
            # Remove all except welcome (if it exists)
            for tab_id in tabs:
                try:
                    # Get tab text to check if it's not the welcome tab
                    tab_text = app.analysis_measurements_notebook.tab(tab_id, "text")
                    if tab_text != "Welcome":
                        app.analysis_measurements_notebook.forget(tab_id)
                except Exception:
                    pass
            
            # Reset counter
            app.analysis_measurement_counter = 0
            
            # Clear measurement tabs dictionary
            app.analysis_measurement_tabs.clear()
            
            # Re-add welcome tab if no tabs remain
            if not app.analysis_measurements_notebook.tabs():
                welcome_frame = ttk.Frame(app.analysis_measurements_notebook)
                app.analysis_measurements_notebook.add(welcome_frame, text="  Welcome  ")

                welcome_container = ttk.Frame(welcome_frame)
                welcome_container.place(relx=0.5, rely=0.5, anchor="center")

                ttk.Label(welcome_container, text="No Analysis Results Yet",
                         font=Fonts.H1).pack(pady=(0, 10))
                ttk.Label(welcome_container,
                         text="Run a measurement from the Measurements tab\nand click 'Analysis' to view results here.",
                         font=Fonts.BODY, justify="center",
                         foreground=Colors.TEXT_MUTED).pack()
                
                app.analysis_welcome_tab = welcome_frame
            
            # Disable action buttons
            if hasattr(app, 'export_plots_btn'):
                app.export_plots_btn.state(["disabled"])
            if hasattr(app, 'open_folder_btn'):
                app.open_folder_btn.state(["disabled"])
            
            LOGGER.info("All measurement tabs cleared")
        except Exception as e:
            LOGGER.warning("Error clearing measurements: %s", e)
    
    ttk.Button(
        button_frame,
        text="Clear All",
        command=clear_all_measurements,
        width=12
    ).pack(side="left", padx=2)

    app.export_plots_btn = ttk.Button(
        button_frame,
        text="Export Plots",
        command=app.export_analysis_plots,
        state="disabled",
        width=14
    )
    app.export_plots_btn.pack(side="left", padx=2)

    app.open_folder_btn = ttk.Button(
        button_frame,
        text="Open Folder",
        command=app.open_results_folder,
        state="disabled",
        width=14
    )
    app.open_folder_btn.pack(side="left", padx=2)
    
    # Notebook for multiple measurement runs
    app.analysis_measurements_notebook = ttk.Notebook(container)
    app.analysis_measurements_notebook.pack(fill="both", expand=True)
    
    # Dictionary to keep track of measurement tabs
    app.analysis_measurement_tabs = {}
    
    # Welcome tab (shown when no measurements)
    welcome_frame = ttk.Frame(app.analysis_measurements_notebook)
    app.analysis_measurements_notebook.add(welcome_frame, text="  Welcome  ")

    welcome_container = ttk.Frame(welcome_frame)
    welcome_container.place(relx=0.5, rely=0.5, anchor="center")

    ttk.Label(
        welcome_container,
        text="No Analysis Results Yet",
        font=Fonts.H1
    ).pack(pady=(0, 10))

    welcome_label = ttk.Label(
        welcome_container,
        text="Run a measurement from the Measurements tab\nand click 'Analysis' to view results here.",
        font=Fonts.BODY,
        justify="center",
        foreground=Colors.TEXT_MUTED
    )
    welcome_label.pack()
    
    # Store reference to welcome tab
    app.analysis_welcome_tab = welcome_frame
    
    # Initialize analysis data storage
    app.analysis_canvases = []
    app.analysis_artifacts = []
    app.analysis_summary_lines = []
    app.analysis_measurement_counter = 0

    def _layout_analysis_header(width=None, _height=None):
        available_width = width or header.winfo_width()
        if not available_width:
            return

        if available_width < 760:
            title_frame.grid_configure(row=0, column=0, sticky="w", pady=(0, 6))
            button_frame.grid_configure(row=1, column=0, sticky="w", padx=0)
        else:
            title_frame.grid_configure(row=0, column=0, sticky="w", pady=0)
            button_frame.grid_configure(row=0, column=1, sticky="e", padx=4)

    bind_debounced_configure(header, _layout_analysis_header)
