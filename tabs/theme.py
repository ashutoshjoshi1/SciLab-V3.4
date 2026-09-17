"""Centralized UI theme for SciLab.

Provides consistent fonts, colors, and ttk style configuration
across all tabs. Call ``apply_theme(root)`` once after creating
the root Tk window.
"""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk

# ---------------------------------------------------------------------------
# Font family stack -- Segoe UI is the Windows system font and renders
# crisply on all monitor DPIs.  Fallbacks cover macOS and Linux.
# ---------------------------------------------------------------------------
FONT_FAMILY = "Segoe UI"
_FALLBACKS = ("Helvetica Neue", "Helvetica", "Arial", "sans-serif")

# Resolve first available family at import time so downstream code can
# reference FONT_FAMILY directly.
def _resolve_font() -> str:
    try:
        _tmp = tk.Tk()
        _tmp.withdraw()
        available = set(tk.font.families())
        _tmp.destroy()
        for fam in (FONT_FAMILY, *_FALLBACKS):
            if fam in available:
                return fam
    except Exception:
        pass
    return FONT_FAMILY  # let Tk fall back gracefully

# ---------------------------------------------------------------------------
# Color palette -- professional blue-gray tones
# ---------------------------------------------------------------------------
class Colors:
    # Backgrounds
    BG_PRIMARY = "#f5f6fa"       # main window / scrollable areas
    BG_SURFACE = "#ffffff"       # cards / label-frames
    BG_HEADER = "#e8eaf0"       # header strips
    BG_INPUT = "#ffffff"         # entry fields

    # Accents
    ACCENT = "#2563eb"           # primary blue
    ACCENT_HOVER = "#1d4ed8"
    ACCENT_LIGHT = "#dbeafe"     # light blue tint

    # Status
    SUCCESS = "#16a34a"
    SUCCESS_BG = "#dcfce7"
    WARNING = "#d97706"
    WARNING_BG = "#fef3c7"
    DANGER = "#dc2626"
    DANGER_BG = "#fee2e2"
    INFO = "#2563eb"

    # Text
    TEXT_PRIMARY = "#1e293b"     # main text (slate-800)
    TEXT_SECONDARY = "#475569"   # secondary (slate-600)
    TEXT_MUTED = "#94a3b8"       # muted hints (slate-400)
    TEXT_ON_ACCENT = "#ffffff"

    # Borders
    BORDER = "#cbd5e1"           # slate-300
    BORDER_LIGHT = "#e2e8f0"     # slate-200

    # Notebook
    TAB_BG = "#e2e8f0"
    TAB_ACTIVE = "#ffffff"
    TAB_TEXT = "#475569"
    TAB_TEXT_ACTIVE = "#1e293b"

    # Status indicators
    CONNECTED = "#16a34a"
    DISCONNECTED = "#dc2626"

    # Type badges
    OBIS_COLOR = "#2563eb"
    CUBE_COLOR = "#c2410c"
    RELAY_COLOR = "#16a34a"


# ---------------------------------------------------------------------------
# Font presets
# ---------------------------------------------------------------------------
class Fonts:
    """Font tuples for use with tkinter widgets."""

    FAMILY = FONT_FAMILY

    # Headings
    H1 = (FONT_FAMILY, 15, "bold")
    H2 = (FONT_FAMILY, 13, "bold")
    H3 = (FONT_FAMILY, 11, "bold")

    # Body
    BODY = (FONT_FAMILY, 10)
    BODY_BOLD = (FONT_FAMILY, 10, "bold")
    BODY_SMALL = (FONT_FAMILY, 9)
    BODY_SMALL_BOLD = (FONT_FAMILY, 9, "bold")

    # Monospace (for data values, code, listboxes)
    MONO = ("Consolas", 10)
    MONO_SMALL = ("Consolas", 9)

    # Captions / hints
    CAPTION = (FONT_FAMILY, 8)
    CAPTION_ITALIC = (FONT_FAMILY, 8, "italic")

    # Buttons
    BUTTON = (FONT_FAMILY, 10)
    BUTTON_BOLD = (FONT_FAMILY, 10, "bold")

    # Status indicators
    STATUS_ICON = (FONT_FAMILY, 12)


# ---------------------------------------------------------------------------
# Spacing constants (pixels)
# ---------------------------------------------------------------------------
class Spacing:
    PAD_XS = 4
    PAD_SM = 8
    PAD_MD = 12
    PAD_LG = 16
    PAD_XL = 20

    SECTION_GAP = 10   # between major sections
    GROUP_PAD = 12      # inside LabelFrames


# ---------------------------------------------------------------------------
# apply_theme -- call once on the root Tk window
# ---------------------------------------------------------------------------
def apply_theme(root: tk.Tk) -> None:
    """Configure all ttk styles for a modern, consistent look."""

    style = ttk.Style(root)

    # Use clam as base -- it's the most customisable cross-platform theme.
    style.theme_use("clam")

    # ---- General defaults ------------------------------------------------
    style.configure(".", font=Fonts.BODY, background=Colors.BG_PRIMARY,
                    foreground=Colors.TEXT_PRIMARY, borderwidth=0)

    # ---- TFrame ----------------------------------------------------------
    style.configure("TFrame", background=Colors.BG_PRIMARY)
    style.configure("Surface.TFrame", background=Colors.BG_SURFACE)
    style.configure("Header.TFrame", background=Colors.BG_HEADER)

    # ---- TLabel ----------------------------------------------------------
    style.configure("TLabel", background=Colors.BG_PRIMARY,
                    foreground=Colors.TEXT_PRIMARY, font=Fonts.BODY)
    style.configure("Secondary.TLabel", foreground=Colors.TEXT_SECONDARY,
                    font=Fonts.BODY_SMALL)
    style.configure("Muted.TLabel", foreground=Colors.TEXT_MUTED,
                    font=Fonts.CAPTION)
    style.configure("Heading.TLabel", font=Fonts.H2,
                    foreground=Colors.TEXT_PRIMARY)
    style.configure("Title.TLabel", font=Fonts.H1,
                    foreground=Colors.TEXT_PRIMARY)
    style.configure("Status.TLabel", font=Fonts.BODY_BOLD)
    style.configure("StatusIcon.TLabel", font=Fonts.STATUS_ICON)

    # ---- TLabelframe (section cards) ------------------------------------
    style.configure("TLabelframe",
                    background=Colors.BG_SURFACE,
                    relief="flat",
                    borderwidth=1,
                    bordercolor=Colors.BORDER_LIGHT,
                    padding=Spacing.GROUP_PAD)
    style.configure("TLabelframe.Label",
                    background=Colors.BG_SURFACE,
                    foreground=Colors.ACCENT,
                    font=Fonts.H3)

    # Setup-specific (kept for backward compatibility with existing code)
    style.configure("Setup.TLabelframe", padding=Spacing.GROUP_PAD)
    style.configure("Setup.TLabelframe.Label", font=Fonts.H3,
                    foreground=Colors.ACCENT)
    style.configure("EEPROM.TLabelframe", padding=Spacing.GROUP_PAD)
    style.configure("EEPROM.TLabelframe.Label", font=Fonts.H3,
                    foreground=Colors.ACCENT)

    # ---- TEntry ----------------------------------------------------------
    style.configure("TEntry", font=Fonts.BODY, padding=(6, 4),
                    fieldbackground=Colors.BG_INPUT,
                    bordercolor=Colors.BORDER,
                    lightcolor=Colors.BORDER_LIGHT)
    style.map("TEntry",
              bordercolor=[("focus", Colors.ACCENT)],
              lightcolor=[("focus", Colors.ACCENT_LIGHT)])

    # ---- TCombobox -------------------------------------------------------
    style.configure("TCombobox", font=Fonts.BODY, padding=(6, 4),
                    fieldbackground=Colors.BG_INPUT,
                    bordercolor=Colors.BORDER)
    style.map("TCombobox",
              bordercolor=[("focus", Colors.ACCENT)],
              fieldbackground=[("readonly", Colors.BG_INPUT)])

    # ---- TButton ---------------------------------------------------------
    style.configure("TButton",
                    font=Fonts.BUTTON,
                    padding=(12, 6),
                    background=Colors.BG_SURFACE,
                    foreground=Colors.TEXT_PRIMARY,
                    bordercolor=Colors.BORDER,
                    relief="flat")
    style.map("TButton",
              background=[("active", Colors.BG_HEADER),
                          ("pressed", Colors.BORDER_LIGHT)],
              bordercolor=[("focus", Colors.ACCENT)])

    # Primary action button (accent color)
    style.configure("Accent.TButton",
                    font=Fonts.BUTTON_BOLD,
                    padding=(14, 7),
                    background=Colors.ACCENT,
                    foreground=Colors.TEXT_ON_ACCENT,
                    bordercolor=Colors.ACCENT)
    style.map("Accent.TButton",
              background=[("active", Colors.ACCENT_HOVER),
                          ("pressed", Colors.ACCENT_HOVER)],
              foreground=[("active", Colors.TEXT_ON_ACCENT)])

    # Backward-compatible aliases
    style.configure("SetupButton.TButton", font=Fonts.BUTTON, padding=(10, 5))
    style.configure("SetupAction.TButton", font=Fonts.BUTTON_BOLD,
                    padding=(12, 6), background=Colors.ACCENT,
                    foreground=Colors.TEXT_ON_ACCENT,
                    bordercolor=Colors.ACCENT)
    style.map("SetupAction.TButton",
              background=[("active", Colors.ACCENT_HOVER),
                          ("pressed", Colors.ACCENT_HOVER)],
              foreground=[("active", Colors.TEXT_ON_ACCENT)])
    style.configure("EEPROMButton.TButton", font=Fonts.BUTTON, padding=(10, 6))

    # ---- TCheckbutton ----------------------------------------------------
    style.configure("TCheckbutton", font=Fonts.BODY,
                    background=Colors.BG_SURFACE,
                    foreground=Colors.TEXT_PRIMARY)
    style.configure("Large.TCheckbutton", font=Fonts.BODY)

    # ---- TRadiobutton ----------------------------------------------------
    style.configure("TRadiobutton", font=Fonts.BODY,
                    background=Colors.BG_SURFACE,
                    foreground=Colors.TEXT_PRIMARY)

    # ---- TNotebook -------------------------------------------------------
    style.configure("TNotebook",
                    background=Colors.BG_PRIMARY,
                    borderwidth=0,
                    tabmargins=(4, 4, 4, 0))
    style.configure("TNotebook.Tab",
                    font=Fonts.BODY_BOLD,
                    padding=(16, 8),
                    background=Colors.TAB_BG,
                    foreground=Colors.TAB_TEXT,
                    borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", Colors.TAB_ACTIVE),
                          ("active", Colors.ACCENT_LIGHT)],
              foreground=[("selected", Colors.TAB_TEXT_ACTIVE)],
              expand=[("selected", (0, 0, 0, 2))])

    # ---- TSeparator ------------------------------------------------------
    style.configure("TSeparator", background=Colors.BORDER_LIGHT)

    # ---- TScrollbar ------------------------------------------------------
    style.configure("TScrollbar",
                    background=Colors.BG_HEADER,
                    troughcolor=Colors.BG_PRIMARY,
                    borderwidth=0,
                    arrowsize=12)

    # ---- Treeview (if used) ---------------------------------------------
    style.configure("Treeview", font=Fonts.BODY, rowheight=26,
                    background=Colors.BG_SURFACE,
                    fieldbackground=Colors.BG_SURFACE,
                    foreground=Colors.TEXT_PRIMARY)
    style.configure("Treeview.Heading", font=Fonts.BODY_BOLD)

    # ---- Label aliases used in tabs (backward compat) -------------------
    style.configure("SetupLabel.TLabel", font=Fonts.BODY)
    style.configure("SetupStatus.TLabel", font=Fonts.BODY_BOLD)
    style.configure("EEPROMLabel.TLabel", font=Fonts.BODY)
    style.configure("EEPROMValue.TLabel", font=Fonts.MONO)

    # ---- Root window background -----------------------------------------
    root.configure(bg=Colors.BG_PRIMARY)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_action_button(parent, text: str, command, *, danger: bool = False,
                       width: int | None = None) -> tk.Button:
    """Create a styled tk.Button for prominent actions (Start/Stop)."""
    if danger:
        bg, active_bg, fg = Colors.DANGER, "#b91c1c", Colors.TEXT_ON_ACCENT
    else:
        bg, active_bg, fg = Colors.SUCCESS, "#15803d", Colors.TEXT_ON_ACCENT

    kwargs = {
        "text": text,
        "command": command,
        "bg": bg,
        "fg": fg,
        "activebackground": active_bg,
        "activeforeground": fg,
        "font": Fonts.BUTTON_BOLD,
        "relief": "flat",
        "cursor": "hand2",
        "bd": 0,
        "highlightthickness": 0,
        "padx": 14,
        "pady": 8,
    }
    if width is not None:
        kwargs["width"] = width
    return tk.Button(parent, **kwargs)


def make_run_button(parent, text: str, command, *, width: int = 15) -> tk.Button:
    """Green 'Run' button used in Measurements."""
    return tk.Button(
        parent,
        text=text,
        command=command,
        bg=Colors.SUCCESS,
        fg=Colors.TEXT_ON_ACCENT,
        activebackground="#15803d",
        activeforeground=Colors.TEXT_ON_ACCENT,
        font=Fonts.BUTTON_BOLD,
        relief="flat",
        cursor="hand2",
        bd=0,
        highlightthickness=0,
        width=width,
    )


def status_dot(parent, connected: bool = False, **grid_kw) -> ttk.Label:
    """Return a coloured dot label for connection status."""
    color = Colors.CONNECTED if connected else Colors.DISCONNECTED
    lbl = ttk.Label(parent, text="\u25cf", foreground=color,
                    font=Fonts.STATUS_ICON)
    return lbl


def configure_matplotlib_style(fig, ax, *, title: str = "",
                                xlabel: str = "Pixel Index",
                                ylabel: str = "Counts") -> None:
    """Apply consistent styling to a matplotlib Figure + Axes."""
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#fafbfc")

    if title:
        ax.set_title(title, fontsize=13, fontweight="bold",
                     fontfamily=FONT_FAMILY, pad=10,
                     color=Colors.TEXT_PRIMARY)
    if xlabel:
        ax.set_xlabel(xlabel, fontsize=10, fontweight="semibold",
                      fontfamily=FONT_FAMILY, color=Colors.TEXT_SECONDARY)
    if ylabel:
        ax.set_ylabel(ylabel, fontsize=10, fontweight="semibold",
                      fontfamily=FONT_FAMILY, color=Colors.TEXT_SECONDARY)

    ax.grid(True, which="major", linestyle="-", linewidth=0.4,
            alpha=0.25, color="#94a3b8")
    ax.grid(True, which="minor", linestyle=":", linewidth=0.25,
            alpha=0.15, color="#94a3b8")
    ax.minorticks_on()

    ax.tick_params(axis="both", which="major", labelsize=9, length=5,
                   width=1, colors=Colors.TEXT_SECONDARY)
    ax.tick_params(axis="both", which="minor", length=3, width=0.7)

    for spine in ax.spines.values():
        spine.set_linewidth(0.8)
        spine.set_color(Colors.BORDER)
