"""Analysis tab - tabbed interface for viewing measurement analysis results."""
import logging

from tkinter import ttk

from .ui_utils import bind_debounced_configure

LOGGER = logging.getLogger(__name__)

def _build_empty_state(app, notebook):
    """A centred card explaining what to do, instead of an empty grey panel."""
    from .theme import Colors, Fonts, shade

    frame = ttk.Frame(notebook)
    notebook.add(frame, text="  Welcome  ")

    card = ttk.Frame(frame, style="Card.TFrame", padding=32)
    card.place(relx=0.5, rely=0.5, anchor="center")

    ttk.Label(card, text="◎", font=(Fonts.FAMILY, 34),
              background=Colors.BG_SURFACE,
              foreground=shade(Colors.ACCENT, 1.45)).pack(pady=(0, 6))
    ttk.Label(card, text="No analysis results yet", font=Fonts.H2,
              background=Colors.BG_SURFACE,
              foreground=Colors.TEXT_PRIMARY).pack()
    ttk.Label(card,
              text="\n".join((
                  "Run a measurement, then press Analysis.",
                  "Completed runs are collected here as tabs.",
              )),
              font=Fonts.BODY_SMALL, justify="center",
              background=Colors.BG_SURFACE,
              foreground=Colors.TEXT_MUTED).pack(pady=(8, 16))
    ttk.Button(card, text="Go to Measurements", style="Accent.TButton",
               command=lambda: app.nb.select(app.measure_tab)).pack()

    app.analysis_welcome_tab = frame
    return frame


def build(app):
    """Build the Analysis tab with a tabbed interface for multiple measurement runs."""
    from .theme import Colors, Fonts, Spacing, logical_px

    container = ttk.Frame(app.analysis_tab)
    container.pack(fill="both", expand=True, padx=Spacing.PAD_SM, pady=Spacing.PAD_SM)

    header = ttk.Frame(container, relief="flat")
    header.pack(fill="x", pady=(0, Spacing.PAD_SM))
    header.columnconfigure(0, weight=1)

    title_frame = ttk.Frame(header)
    title_frame.grid(row=0, column=0, sticky="w")
    ttk.Label(title_frame, text="Analysis Results",
              font=Fonts.H1).pack(anchor="w")
    ttk.Label(title_frame, text="One tab per completed run, newest last",
              font=Fonts.BODY_SMALL,
              foreground=Colors.TEXT_MUTED).pack(anchor="w")

    # Action buttons on the right with better styling
    button_frame = ttk.Frame(header)
    button_frame.grid(row=0, column=1, sticky="e", padx=4)

    def clear_all_measurements():
        """Clear all measurement tabs except welcome tab."""
        try:
            # Get all tabs
            tabs = app.analysis_measurements_notebook.tabs()

            # Remove all except welcome (if it exists). forget() only detaches a
            # tab; destroy() is what actually frees its widgets and preview images.
            for tab_id in tabs:
                try:
                    tab_text = app.analysis_measurements_notebook.tab(tab_id, "text")
                    if tab_text.strip() != "Welcome":
                        widget = app.nametowidget(tab_id)
                        app.analysis_measurements_notebook.forget(tab_id)
                        widget.destroy()
                except Exception:
                    pass

            # Reset counter
            app.analysis_measurement_counter = 0

            # Clear measurement tabs dictionary
            app.analysis_measurement_tabs.clear()

            # Re-add welcome tab if no tabs remain
            if not app.analysis_measurements_notebook.tabs():
                _build_empty_state(app, app.analysis_measurements_notebook)

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
    app.analysis_measurements_notebook = ttk.Notebook(container, style="Inner.TNotebook")
    app.analysis_measurements_notebook.pack(fill="both", expand=True)

    # Dictionary to keep track of measurement tabs
    app.analysis_measurement_tabs = {}

    # Welcome tab (shown when no measurements)
    _build_empty_state(app, app.analysis_measurements_notebook)

    # Initialize analysis data storage
    app.analysis_artifacts = []
    app.analysis_summary_lines = []
    app.analysis_measurement_counter = 0

    def _layout_analysis_header(width=None, _height=None):
        available_width = width or header.winfo_width()
        if not available_width:
            return

        if available_width < logical_px(app, 760):
            title_frame.grid_configure(row=0, column=0, sticky="w", pady=(0, 6))
            button_frame.grid_configure(row=1, column=0, sticky="w", padx=0)
        else:
            title_frame.grid_configure(row=0, column=0, sticky="w", pady=0)
            button_frame.grid_configure(row=0, column=1, sticky="e", padx=4)

    bind_debounced_configure(header, _layout_analysis_header)
