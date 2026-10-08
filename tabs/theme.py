"""Centralized UI theme for SciLab.

Provides consistent fonts, colors, spacing and ttk style configuration
across all tabs. Call ``apply_theme(root)`` once after creating the root
Tk window; everything else in the app then inherits the look.

Design notes
------------
* One accent (blue), one neutral ramp (slate). Status colours are reserved
  for status only, so a red or green pixel always means something.
* Surfaces are white cards on a tinted canvas, separated by hairline
  borders instead of bevels -- ttk's ``clam`` bevels are what made the
  older build look like a 2005 dialog.
* Every interactive widget has a visible resting border and a hover state.
  A secondary button that looks like a label is a usability bug, not a
  style choice.
"""

from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import ttk

# ---------------------------------------------------------------------------
# Font family stack
#
# Windows 11 ships Segoe UI Variable in three optical cuts -- Small, Text and
# Display -- each hinted for a size range: Small keeps small print legible
# (tighter spacing, sturdier strokes), Display tightens the letterfit that
# looks loose at heading sizes. Picking the cut per size is what keeps the app
# crisp from a 1366x768 laptop to a 4K panel, rather than stretching one face
# across 8-17 pt. Everything degrades to plain Segoe UI, then to the platform
# sans, so nothing here is Windows-11-only.
# ---------------------------------------------------------------------------
_SANS_CANDIDATES = ("Segoe UI Variable Text", "Segoe UI", "Selawik", "Inter",
                    "Helvetica Neue", "Helvetica", "Arial")
_DISPLAY_CANDIDATES = ("Segoe UI Variable Display", "Segoe UI Variable Text",
                       "Segoe UI Semibold", "Segoe UI", "Helvetica Neue",
                       "Helvetica", "Arial")
_SMALL_CANDIDATES = ("Segoe UI Variable Small", "Segoe UI", "Selawik",
                     "Helvetica Neue", "Helvetica", "Arial")
_MONO_CANDIDATES = ("Cascadia Mono", "Consolas", "Menlo", "DejaVu Sans Mono",
                    "Courier New")

# Resolved against the installed families by ``apply_theme``; the defaults keep
# the module importable (and the tuples below sane) before a root window exists.
FONT_FAMILY = "Segoe UI"
DISPLAY_FAMILY = "Segoe UI"
SMALL_FAMILY = "Segoe UI"
_MONO_FAMILY = "Consolas"

_FALLBACKS = ("Helvetica Neue", "Helvetica", "Arial", "sans-serif")
_MONO_FALLBACKS = ("Cascadia Mono", "Menlo", "DejaVu Sans Mono", "Courier New")

_MPL_FONT_CACHE: dict = {}


def matplotlib_font_family() -> str:
    """Font family for matplotlib text: Segoe UI where installed, else matplotlib's default.

    Resolved once; asking matplotlib for a missing family logs a warning on every
    draw, which is what happened on non-Windows machines before.
    """
    cached = _MPL_FONT_CACHE.get("family")
    if cached is not None:
        return cached
    family = "sans-serif"
    try:
        from matplotlib import font_manager

        available = {entry.name for entry in font_manager.fontManager.ttflist}
        for candidate in ("Segoe UI", FONT_FAMILY, *_FALLBACKS):
            if candidate in available:
                family = candidate
                break
    except Exception:
        pass
    _MPL_FONT_CACHE["family"] = family
    return family


def screen_dpi(root, default: float = 100.0) -> float:
    """Figure DPI matching the display so plots are crisp on scaled (125-150 %) monitors."""
    try:
        dpi = float(root.winfo_fpixels("1i"))
    except Exception:
        return default
    if not (48.0 <= dpi <= 240.0):
        return default
    return dpi


def ui_scale(root) -> float:
    """Device pixels per 96-dpi logical pixel, including the type scale chosen for this screen."""
    return screen_dpi(root, 96.0) / 96.0 * Fonts.SCALE


def logical_px(root, value: float) -> int:
    """Device pixels for a length designed in 96-dpi logical pixels at the 10 pt base size.

    Every layout breakpoint and maximum width in the tabs is written in these
    units, so a form that is 1560 "design pixels" wide is the same physical
    size on a 100 % 1080p panel and a 125 % 4K panel.
    """
    return int(round(value * ui_scale(root)))


def scaled_pt(size: float) -> float:
    """A matplotlib point size that follows the UI type scale."""
    return size * Fonts.SCALE


def plot_title_font(**overrides) -> dict:
    """Keyword arguments for ``Axes.set_title`` that match ``configure_matplotlib_style``.

    ``set_title(text)`` alone resets the title to matplotlib's default face and
    weight, which is how the Live View title used to lose its bold on the
    first frame.
    """
    kwargs = dict(fontsize=scaled_pt(12.5), fontweight="bold",
                  fontfamily=matplotlib_font_family(), pad=_px(12),
                  color=Colors.TEXT_PRIMARY)
    kwargs.update(overrides)
    return kwargs


def _px(value: float) -> int:
    """Pixel padding scaled with the type size (1.0 on a 1080p-class screen)."""
    return int(round(value * Fonts.SCALE))


# ---------------------------------------------------------------------------
# Color palette -- cool slate neutrals with a single blue accent
# ---------------------------------------------------------------------------
class Colors:
    # -- Canvas & surfaces -------------------------------------------------
    BG_PRIMARY = "#eef1f7"       # main window / scrollable areas
    BG_SURFACE = "#ffffff"       # cards / label-frames
    BG_SUBTLE = "#f7f9fc"        # inset rows, plot faces, read-only fields
    BG_HEADER = "#e5eaf3"        # header strips / status bar
    BG_INPUT = "#ffffff"         # entry fields
    BG_HOVER = "#eff4ff"         # hover wash for neutral controls

    # -- Brand / chrome ----------------------------------------------------
    NAVY = "#0f172a"             # app bar, tooltips, splash (matches the logo)
    NAVY_SOFT = "#1e293b"
    NAVY_LINE = "#334155"

    # -- Accents -----------------------------------------------------------
    ACCENT = "#2563eb"           # primary blue
    ACCENT_HOVER = "#1d4ed8"
    ACCENT_ACTIVE = "#1e40af"
    ACCENT_LIGHT = "#dbeafe"     # light blue tint
    ACCENT_SOFT = "#eff6ff"      # faintest blue wash
    ACCENT_DEEP = "#1e3a8a"      # card titles / headings on white

    # -- Status ------------------------------------------------------------
    SUCCESS = "#15a34a"
    SUCCESS_HOVER = "#15803d"
    SUCCESS_BG = "#dcfce7"
    WARNING = "#d97706"
    WARNING_BG = "#fef3c7"
    DANGER = "#dc2626"
    DANGER_HOVER = "#b91c1c"
    DANGER_BG = "#fee2e2"
    INFO = "#2563eb"
    INFO_BG = "#dbeafe"
    EDITABLE_BG = "#fffbeb"      # fields that write back to hardware

    # -- Text --------------------------------------------------------------
    TEXT_PRIMARY = "#0f172a"     # main text (slate-900)
    TEXT_SECONDARY = "#475569"   # secondary (slate-600)
    TEXT_MUTED = "#94a3b8"       # muted hints (slate-400)
    TEXT_DISABLED = "#b4c0cf"
    TEXT_ON_ACCENT = "#ffffff"
    TEXT_ON_NAVY = "#e2e8f0"

    # -- Borders -----------------------------------------------------------
    BORDER = "#c4cedd"           # resting border on inputs / buttons
    BORDER_LIGHT = "#e2e8f0"     # card hairlines, separators
    BORDER_STRONG = "#94a3b8"    # hovered inputs

    # -- Notebook ----------------------------------------------------------
    TAB_BG = "#e1e7f0"
    TAB_HOVER = "#eaf0fb"
    TAB_ACTIVE = "#ffffff"
    TAB_TEXT = "#475569"
    TAB_TEXT_ACTIVE = "#1e3a8a"

    # -- Status indicators -------------------------------------------------
    CONNECTED = "#15a34a"
    DISCONNECTED = "#dc2626"
    IDLE = "#94a3b8"

    # -- Type badges -------------------------------------------------------
    OBIS_COLOR = "#2563eb"
    CUBE_COLOR = "#c2410c"
    RELAY_COLOR = "#15a34a"
    NEUTRAL_COLOR = "#64748b"

    # -- Plot series (colour-blind safe, in draw order) --------------------
    PLOT_FACE = "#fbfcfe"
    PLOT_GRID = "#94a3b8"
    SERIES = ("#2563eb", "#c2410c", "#0f766e", "#7c3aed", "#b45309", "#0369a1")

    # -- Source colours ----------------------------------------------------
    # Each laser is tinted towards its own wavelength, darkened until it passes
    # ~4.5:1 on white -- a 532 nm row reading in actual green is faster to find
    # than one more line of slate text, and the operator already thinks in
    # these colours. Never the only cue: the wavelength is always written out.
    SOURCE = {
        "377": "#6d28d9",   # near-UV  -> violet
        "405": "#5b21b6",   # violet
        "445": "#1d4ed8",   # blue
        "488": "#0e7490",   # cyan (darkened to stay readable)
        "517": "#15803d",   # green
        "532": "#166534",   # green
        "640": "#dc2626",   # red
        "685": "#9f1239",   # deep red
        "Hg_Ar": "#b45309", # lamp -> amber
    }
    SOURCE_DEFAULT = "#475569"


# ---------------------------------------------------------------------------
# Font presets
# ---------------------------------------------------------------------------
class Fonts:
    """Font tuples for use with tkinter widgets.

    Sizes are in *points*, so Tk scales them by the display's DPI on its own;
    ``resolve_fonts`` additionally picks the optical cut per size and nudges the
    base size on unusually short screens. Values are rewritten in place at
    startup, which is why every widget must read ``Fonts.X`` when it is built
    rather than copying the tuple at import time.
    """

    FAMILY = FONT_FAMILY
    DISPLAY_FAMILY = DISPLAY_FAMILY
    SMALL_FAMILY = SMALL_FAMILY
    MONO_FAMILY = _MONO_FAMILY
    # Base size / 10 pt: 1.0 on a 1080p-class screen, 1.1-1.2 on 1440p / 4K
    # panels. Paddings, plot typography and layout breakpoints follow it.
    SCALE = 1.0

    # Headings -- display cut, tighter letterfit
    H0 = (DISPLAY_FAMILY, 17, "bold")
    H1 = (DISPLAY_FAMILY, 15, "bold")
    H2 = (DISPLAY_FAMILY, 13, "bold")
    H3 = (FONT_FAMILY, 11, "bold")

    # Body
    BODY = (FONT_FAMILY, 10)
    BODY_BOLD = (FONT_FAMILY, 10, "bold")
    BODY_SMALL = (FONT_FAMILY, 9)
    BODY_SMALL_BOLD = (FONT_FAMILY, 9, "bold")

    # Monospace (for data values, code, listboxes)
    MONO = (_MONO_FAMILY, 10)
    MONO_BOLD = (_MONO_FAMILY, 10, "bold")
    MONO_SMALL = (_MONO_FAMILY, 9)
    MONO_SMALL_BOLD = (_MONO_FAMILY, 9, "bold")

    # Captions / hints -- small cut, built to stay legible at 8 pt
    CAPTION = (SMALL_FAMILY, 8)
    CAPTION_BOLD = (SMALL_FAMILY, 8, "bold")
    CAPTION_ITALIC = (SMALL_FAMILY, 8, "italic")

    # Buttons
    BUTTON = (FONT_FAMILY, 10)
    BUTTON_BOLD = (FONT_FAMILY, 10, "bold")
    BUTTON_SMALL = (FONT_FAMILY, 9, "bold")

    # Status indicators
    STATUS_ICON = (FONT_FAMILY, 12)
    DOT = (FONT_FAMILY, 11)


def _first_available(candidates, installed, default: str) -> str:
    for name in candidates:
        if name in installed:
            return name
    return default


def resolve_fonts(root) -> None:
    """Pick the best installed families and a base size that suits the screen.

    Point sizes already track DPI through Tk's scaling factor, so what is
    left to choose is how much *room* the screen has, in logical pixels:
    these panels are dense, and on a 768 px-tall laptop one point less keeps
    them whole without scrolling, while on a 4K panel (1728 logical rows at
    125 %) 10 pt text is a sliver beside a plot that fills the screen, so the
    base grows to 12 pt and every padding scales with it (``Fonts.SCALE``).
    """
    global FONT_FAMILY, DISPLAY_FAMILY, SMALL_FAMILY, _MONO_FAMILY

    try:
        from tkinter import font as tkfont

        installed = set(tkfont.families(root))
    except Exception:
        installed = set()

    FONT_FAMILY = _first_available(_SANS_CANDIDATES, installed, FONT_FAMILY)
    DISPLAY_FAMILY = _first_available(_DISPLAY_CANDIDATES, installed, FONT_FAMILY)
    SMALL_FAMILY = _first_available(_SMALL_CANDIDATES, installed, FONT_FAMILY)
    _MONO_FAMILY = _first_available(_MONO_CANDIDATES, installed, _MONO_FAMILY)

    # Logical (scaling-independent) screen height: Tk points are already
    # DPI-scaled, so comparing raw pixels would shrink text on a 4K panel.
    base = 10
    try:
        scaling = float(root.tk.call("tk", "scaling")) or 1.0
        logical_height = root.winfo_screenheight() / max(1.0, scaling / (96.0 / 72.0))
        if logical_height < 820:
            base = 9
        elif logical_height >= 1650:
            base = 12  # 4K at 125 %, 5K: see the docstring
        elif logical_height >= 1300:
            base = 11  # 2560x1440 at 100 %, 4K at 150 %
    except Exception:
        pass
    # SCILAB_UI_SCALE=1.0 pins the type scale: an escape hatch for an operator
    # who prefers the compact size, and how a 1080p layout is checked on a 4K
    # development screen.
    try:
        override = float(os.environ.get("SCILAB_UI_SCALE", "") or 0.0)
        if 0.7 <= override <= 1.6:
            base = int(round(10 * override))
    except ValueError:
        pass
    Fonts.SCALE = base / 10.0
    for name, value in _SPACING_BASE.items():
        setattr(Spacing, name, int(round(value * Fonts.SCALE)))

    Fonts.FAMILY = FONT_FAMILY
    Fonts.DISPLAY_FAMILY = DISPLAY_FAMILY
    Fonts.SMALL_FAMILY = SMALL_FAMILY
    Fonts.MONO_FAMILY = _MONO_FAMILY

    Fonts.H0 = (DISPLAY_FAMILY, base + 7, "bold")
    Fonts.H1 = (DISPLAY_FAMILY, base + 5, "bold")
    Fonts.H2 = (DISPLAY_FAMILY, base + 3, "bold")
    Fonts.H3 = (FONT_FAMILY, base + 1, "bold")

    Fonts.BODY = (FONT_FAMILY, base)
    Fonts.BODY_BOLD = (FONT_FAMILY, base, "bold")
    Fonts.BODY_SMALL = (FONT_FAMILY, base - 1)
    Fonts.BODY_SMALL_BOLD = (FONT_FAMILY, base - 1, "bold")

    Fonts.MONO = (_MONO_FAMILY, base)
    Fonts.MONO_BOLD = (_MONO_FAMILY, base, "bold")
    Fonts.MONO_SMALL = (_MONO_FAMILY, base - 1)
    Fonts.MONO_SMALL_BOLD = (_MONO_FAMILY, base - 1, "bold")

    # Never below 8 pt: the Small cut stays legible there, but under it the
    # hints turn into texture on a short 1366x768 panel.
    caption = max(8, base - 2)
    Fonts.CAPTION = (SMALL_FAMILY, caption)
    Fonts.CAPTION_BOLD = (SMALL_FAMILY, caption, "bold")
    Fonts.CAPTION_ITALIC = (SMALL_FAMILY, caption, "italic")

    Fonts.BUTTON = (FONT_FAMILY, base)
    Fonts.BUTTON_BOLD = (FONT_FAMILY, base, "bold")
    Fonts.BUTTON_SMALL = (FONT_FAMILY, base - 1, "bold")

    Fonts.STATUS_ICON = (FONT_FAMILY, base + 2)
    Fonts.DOT = (FONT_FAMILY, base + 1)

    # Tk's named fonts drive the classic widgets and the standard dialogs
    # (messagebox, filedialog); without this they stay on Tk's 9 pt default and
    # every pop-up looks like it belongs to another application.
    try:
        from tkinter import font as tkfont

        named = {
            "TkDefaultFont": Fonts.BODY,
            "TkTextFont": Fonts.BODY,
            "TkMenuFont": Fonts.BODY,
            "TkHeadingFont": Fonts.BODY_BOLD,
            "TkCaptionFont": Fonts.BODY_BOLD,
            "TkSmallCaptionFont": Fonts.BODY_SMALL,
            "TkTooltipFont": Fonts.BODY_SMALL,
            "TkIconFont": Fonts.BODY,
            "TkFixedFont": Fonts.MONO,
        }
        for name, spec in named.items():
            try:
                handle = tkfont.nametofont(name, root=root)
            except Exception:
                continue
            handle.configure(family=spec[0], size=spec[1],
                             weight="bold" if len(spec) > 2 and spec[2] == "bold" else "normal")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Spacing constants (pixels)
# ---------------------------------------------------------------------------
class Spacing:
    """Rewritten by ``resolve_fonts`` to follow ``Fonts.SCALE``; read at build time."""

    PAD_XS = 4
    PAD_SM = 8
    PAD_MD = 12
    PAD_LG = 16
    PAD_XL = 20

    SECTION_GAP = 10   # between major sections
    GROUP_PAD = 14     # inside LabelFrames
    RADIUS = 6         # nominal corner radius (used by drawn widgets)


_SPACING_BASE = {
    "PAD_XS": 4, "PAD_SM": 8, "PAD_MD": 12, "PAD_LG": 16, "PAD_XL": 20,
    "SECTION_GAP": 10, "GROUP_PAD": 14, "RADIUS": 6,
}


# ---------------------------------------------------------------------------
# Small colour helpers
# ---------------------------------------------------------------------------

def _clamp(value: float) -> int:
    return max(0, min(255, int(round(value))))


def _rgb(color: str):
    """``#rrggbb`` -> (r, g, b), tolerant of short hex."""
    try:
        text = color.lstrip("#")
        if len(text) == 3:
            text = "".join(ch * 2 for ch in text)
        return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return (0, 0, 0)


def shade(color: str, factor: float) -> str:
    """Lighten (factor > 1) or darken (factor < 1) a ``#rrggbb`` colour."""
    try:
        text = color.lstrip("#")
        if len(text) == 3:
            text = "".join(ch * 2 for ch in text)
        r, g, b = (int(text[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return color
    if factor >= 1.0:
        # Blend towards white so light colours keep lightening.
        t = min(1.0, factor - 1.0)
        r, g, b = (c + (255 - c) * t for c in (r, g, b))
    else:
        r, g, b = (c * factor for c in (r, g, b))
    return f"#{_clamp(r):02x}{_clamp(g):02x}{_clamp(b):02x}"


# ---------------------------------------------------------------------------
# apply_theme -- call once on the root Tk window
# ---------------------------------------------------------------------------
def apply_theme(root: tk.Tk) -> None:
    """Configure all ttk styles for a modern, consistent look."""

    # Families and sizes first: every style below reads Fonts.*.
    resolve_fonts(root)

    def pp(*values):
        return tuple(_px(v) for v in values)

    style = ttk.Style(root)

    # clam is the most customisable cross-platform theme; every element below
    # is re-skinned, so none of clam's own bevels survive.
    style.theme_use("clam")

    flat_border = dict(
        bordercolor=Colors.BORDER,
        lightcolor=Colors.BORDER,
        darkcolor=Colors.BORDER,
    )

    # ---- General defaults ------------------------------------------------
    style.configure(".", font=Fonts.BODY, background=Colors.BG_PRIMARY,
                    foreground=Colors.TEXT_PRIMARY, borderwidth=0,
                    focuscolor=Colors.ACCENT_LIGHT,
                    troughcolor=Colors.BG_HEADER,
                    bordercolor=Colors.BORDER_LIGHT,
                    lightcolor=Colors.BORDER_LIGHT,
                    darkcolor=Colors.BORDER_LIGHT)

    # ---- TFrame ----------------------------------------------------------
    style.configure("TFrame", background=Colors.BG_PRIMARY)
    style.configure("Surface.TFrame", background=Colors.BG_SURFACE)
    style.configure("Subtle.TFrame", background=Colors.BG_SUBTLE)
    style.configure("Header.TFrame", background=Colors.BG_HEADER)
    style.configure("Navy.TFrame", background=Colors.NAVY)
    style.configure("NavyLine.TFrame", background=Colors.NAVY_LINE)
    style.configure("Accent.TFrame", background=Colors.ACCENT)
    style.configure("Hairline.TFrame", background=Colors.BORDER_LIGHT)
    # A white card with a hairline border, for places that need a card but
    # not a LabelFrame title.
    style.configure("Card.TFrame", background=Colors.BG_SURFACE,
                    relief="solid", borderwidth=1,
                    bordercolor=Colors.BORDER_LIGHT,
                    lightcolor=Colors.BORDER_LIGHT,
                    darkcolor=Colors.BORDER_LIGHT)

    # ---- TLabel ----------------------------------------------------------
    style.configure("TLabel", background=Colors.BG_PRIMARY,
                    foreground=Colors.TEXT_PRIMARY, font=Fonts.BODY)
    style.configure("Secondary.TLabel", foreground=Colors.TEXT_SECONDARY,
                    font=Fonts.BODY_SMALL)
    style.configure("Muted.TLabel", foreground=Colors.TEXT_MUTED,
                    font=Fonts.CAPTION)
    style.configure("Hint.TLabel", foreground=Colors.TEXT_MUTED,
                    font=Fonts.CAPTION_ITALIC)
    style.configure("Heading.TLabel", font=Fonts.H2,
                    foreground=Colors.TEXT_PRIMARY)
    style.configure("Title.TLabel", font=Fonts.H1,
                    foreground=Colors.TEXT_PRIMARY)
    style.configure("Display.TLabel", font=Fonts.H0,
                    foreground=Colors.TEXT_PRIMARY)
    style.configure("Status.TLabel", font=Fonts.BODY_BOLD)
    # Emphasis styles: the numbers and states an operator scans for.
    style.configure("Strong.TLabel", font=Fonts.BODY_BOLD,
                    foreground=Colors.TEXT_PRIMARY)
    style.configure("Metric.TLabel", font=Fonts.MONO_BOLD,
                    foreground=Colors.ACCENT_DEEP)
    style.configure("MetricSmall.TLabel", font=Fonts.MONO_SMALL_BOLD,
                    foreground=Colors.ACCENT_DEEP)
    style.configure("FieldLabel.TLabel", font=Fonts.BODY_SMALL_BOLD,
                    foreground=Colors.TEXT_SECONDARY)
    style.configure("StatusIcon.TLabel", font=Fonts.STATUS_ICON)
    style.configure("Mono.TLabel", font=Fonts.MONO,
                    foreground=Colors.TEXT_PRIMARY)
    style.configure("Value.TLabel", font=Fonts.MONO_BOLD,
                    foreground=Colors.ACCENT_DEEP)
    # On the navy app bar.
    style.configure("NavyTitle.TLabel", background=Colors.NAVY,
                    foreground="#ffffff", font=Fonts.H2)
    style.configure("NavySub.TLabel", background=Colors.NAVY,
                    foreground="#8ea3c0", font=Fonts.BODY_SMALL)
    style.configure("NavyValue.TLabel", background=Colors.NAVY,
                    foreground=Colors.TEXT_ON_NAVY, font=Fonts.BODY_SMALL_BOLD)
    style.configure("NavyDot.TLabel", background=Colors.NAVY,
                    foreground=Colors.IDLE, font=Fonts.DOT)

    # ---- TLabelframe (section cards) ------------------------------------
    style.configure("TLabelframe",
                    background=Colors.BG_SURFACE,
                    relief="solid",
                    borderwidth=1,
                    bordercolor=Colors.BORDER_LIGHT,
                    lightcolor=Colors.BORDER_LIGHT,
                    darkcolor=Colors.BORDER_LIGHT,
                    padding=Spacing.GROUP_PAD)
    style.configure("TLabelframe.Label",
                    background=Colors.BG_SURFACE,
                    foreground=Colors.ACCENT_DEEP,
                    font=Fonts.H3)

    # Named variants used by the tabs (kept for backward compatibility).
    for prefix in ("Setup", "EEPROM", "Card"):
        style.configure(f"{prefix}.TLabelframe",
                        background=Colors.BG_SURFACE,
                        relief="solid",
                        borderwidth=1,
                        bordercolor=Colors.BORDER_LIGHT,
                        lightcolor=Colors.BORDER_LIGHT,
                        darkcolor=Colors.BORDER_LIGHT,
                        padding=Spacing.GROUP_PAD)
        style.configure(f"{prefix}.TLabelframe.Label",
                        background=Colors.BG_SURFACE,
                        foreground=Colors.ACCENT_DEEP,
                        font=Fonts.H3)

    # ---- TEntry ----------------------------------------------------------
    style.configure("TEntry", font=Fonts.BODY, padding=pp(6, 4),
                    fieldbackground=Colors.BG_INPUT,
                    foreground=Colors.TEXT_PRIMARY,
                    insertcolor=Colors.ACCENT,
                    relief="solid", borderwidth=1, **flat_border)
    style.map("TEntry",
              bordercolor=[("disabled", Colors.BORDER_LIGHT),
                           ("focus", Colors.ACCENT),
                           ("hover", Colors.BORDER_STRONG)],
              lightcolor=[("focus", Colors.ACCENT), ("hover", Colors.BORDER_STRONG)],
              darkcolor=[("focus", Colors.ACCENT), ("hover", Colors.BORDER_STRONG)],
              fieldbackground=[("disabled", Colors.BG_SUBTLE),
                               ("readonly", Colors.BG_SUBTLE)],
              foreground=[("disabled", Colors.TEXT_DISABLED)])
    style.configure("Editable.TEntry", fieldbackground=Colors.EDITABLE_BG)
    style.map("Editable.TEntry",
              fieldbackground=[("disabled", Colors.BG_SUBTLE),
                               ("!disabled", Colors.EDITABLE_BG)])
    style.configure("Mono.TEntry", font=Fonts.MONO)

    # ---- TCombobox -------------------------------------------------------
    style.configure("TCombobox", font=Fonts.BODY, padding=pp(6, 4),
                    fieldbackground=Colors.BG_INPUT,
                    background=Colors.BG_INPUT,
                    foreground=Colors.TEXT_PRIMARY,
                    arrowcolor=Colors.TEXT_SECONDARY,
                    arrowsize=_px(14),
                    relief="solid", borderwidth=1, **flat_border)
    style.map("TCombobox",
              bordercolor=[("disabled", Colors.BORDER_LIGHT),
                           ("focus", Colors.ACCENT),
                           ("hover", Colors.BORDER_STRONG)],
              lightcolor=[("focus", Colors.ACCENT)],
              darkcolor=[("focus", Colors.ACCENT)],
              fieldbackground=[("readonly", Colors.BG_INPUT),
                               ("disabled", Colors.BG_SUBTLE)],
              background=[("active", Colors.BG_HOVER)],
              arrowcolor=[("disabled", Colors.TEXT_DISABLED),
                          ("hover", Colors.ACCENT)],
              foreground=[("disabled", Colors.TEXT_DISABLED)])
    # The dropdown list is a classic Tk listbox, styled through options.
    for option, value in (
        ("*TCombobox*Listbox.background", Colors.BG_SURFACE),
        ("*TCombobox*Listbox.foreground", Colors.TEXT_PRIMARY),
        ("*TCombobox*Listbox.selectBackground", Colors.ACCENT),
        ("*TCombobox*Listbox.selectForeground", Colors.TEXT_ON_ACCENT),
        # Braces are required: an unbraced multi-word family makes Tk read
        # "Segoe UI 10" as family "Segoe" + size "UI", and posting the
        # dropdown then fails with 'expected integer but got "UI"'.
        ("*TCombobox*Listbox.font", "{%s} 10" % FONT_FAMILY),
        ("*TCombobox*Listbox.borderWidth", "0"),
        ("*TCombobox*Listbox.relief", "flat"),
        ("*TCombobox*Listbox.highlightThickness", "1"),
        ("*TCombobox*Listbox.highlightColor", Colors.BORDER),
    ):
        try:
            root.option_add(option, value)
        except tk.TclError:
            pass

    # ---- TSpinbox --------------------------------------------------------
    try:
        style.configure("TSpinbox", font=Fonts.BODY, padding=pp(6, 4),
                        fieldbackground=Colors.BG_INPUT,
                        arrowcolor=Colors.TEXT_SECONDARY, arrowsize=_px(12),
                        relief="solid", borderwidth=1, **flat_border)
        style.map("TSpinbox",
                  bordercolor=[("focus", Colors.ACCENT)],
                  arrowcolor=[("hover", Colors.ACCENT)])
    except tk.TclError:  # pragma: no cover - very old Tk
        pass

    # ---- TButton ---------------------------------------------------------
    # Secondary / default: white with a real border and a blue-tinted hover.
    style.configure("TButton",
                    font=Fonts.BUTTON,
                    padding=pp(13, 7),
                    background=Colors.BG_SURFACE,
                    foreground=Colors.TEXT_PRIMARY,
                    relief="solid",
                    borderwidth=1,
                    anchor="center",
                    **flat_border)
    style.map("TButton",
              background=[("disabled", Colors.BG_SUBTLE),
                          ("pressed", Colors.ACCENT_LIGHT),
                          ("active", Colors.BG_HOVER)],
              foreground=[("disabled", Colors.TEXT_DISABLED),
                          ("pressed", Colors.ACCENT_ACTIVE),
                          ("active", Colors.ACCENT_HOVER)],
              bordercolor=[("disabled", Colors.BORDER_LIGHT),
                           ("pressed", Colors.ACCENT),
                           ("active", Colors.ACCENT),
                           ("focus", Colors.ACCENT)],
              lightcolor=[("disabled", Colors.BORDER_LIGHT),
                          ("active", Colors.ACCENT)],
              darkcolor=[("disabled", Colors.BORDER_LIGHT),
                         ("active", Colors.ACCENT)])

    def _solid_button(name: str, base: str, hover: str, active: str,
                      *, font=Fonts.BUTTON_BOLD, padding=(15, 8)) -> None:
        style.configure(name, font=font, padding=pp(*padding),
                        background=base, foreground=Colors.TEXT_ON_ACCENT,
                        relief="solid", borderwidth=1, anchor="center",
                        bordercolor=base, lightcolor=base, darkcolor=base)
        style.map(name,
                  background=[("disabled", Colors.BG_SUBTLE),
                              ("pressed", active), ("active", hover)],
                  foreground=[("disabled", Colors.TEXT_DISABLED),
                              ("active", Colors.TEXT_ON_ACCENT)],
                  bordercolor=[("disabled", Colors.BORDER_LIGHT),
                               ("pressed", active), ("active", hover)],
                  lightcolor=[("disabled", Colors.BORDER_LIGHT),
                              ("pressed", active), ("active", hover)],
                  darkcolor=[("disabled", Colors.BORDER_LIGHT),
                             ("pressed", active), ("active", hover)])

    # Primary action button (accent), plus success / danger variants.
    _solid_button("Accent.TButton", Colors.ACCENT, Colors.ACCENT_HOVER, Colors.ACCENT_ACTIVE)
    _solid_button("Success.TButton", Colors.SUCCESS, Colors.SUCCESS_HOVER, shade(Colors.SUCCESS_HOVER, 0.85))
    _solid_button("Danger.TButton", Colors.DANGER, Colors.DANGER_HOVER, shade(Colors.DANGER_HOVER, 0.85))
    # Backward-compatible alias used by the Setup tab.
    _solid_button("SetupAction.TButton", Colors.ACCENT, Colors.ACCENT_HOVER,
                  Colors.ACCENT_ACTIVE, padding=(13, 7))

    # Quiet button: no resting border, accent wash on hover. For toolbars.
    style.configure("Ghost.TButton", font=Fonts.BUTTON,
                    padding=pp(11, 6), background=Colors.BG_PRIMARY,
                    foreground=Colors.TEXT_SECONDARY, relief="flat",
                    borderwidth=1, bordercolor=Colors.BG_PRIMARY,
                    lightcolor=Colors.BG_PRIMARY, darkcolor=Colors.BG_PRIMARY)
    style.map("Ghost.TButton",
              background=[("disabled", Colors.BG_PRIMARY),
                          ("pressed", Colors.ACCENT_LIGHT),
                          ("active", Colors.BG_HOVER)],
              foreground=[("disabled", Colors.TEXT_DISABLED),
                          ("active", Colors.ACCENT_HOVER)],
              bordercolor=[("active", Colors.ACCENT_LIGHT)],
              lightcolor=[("active", Colors.ACCENT_LIGHT)],
              darkcolor=[("active", Colors.ACCENT_LIGHT)])

    # Compact neutral button for dense panels (Add / Remove / Clear).
    style.configure("Small.TButton", font=Fonts.BUTTON_SMALL, padding=pp(9, 5),
                    background=Colors.BG_SURFACE, foreground=Colors.TEXT_PRIMARY,
                    relief="solid", borderwidth=1, anchor="center", **flat_border)
    style.map("Small.TButton",
              background=[("disabled", Colors.BG_SUBTLE),
                          ("pressed", Colors.ACCENT_LIGHT),
                          ("active", Colors.BG_HOVER)],
              foreground=[("disabled", Colors.TEXT_DISABLED),
                          ("active", Colors.ACCENT_HOVER)],
              bordercolor=[("disabled", Colors.BORDER_LIGHT), ("active", Colors.ACCENT)],
              lightcolor=[("active", Colors.ACCENT)],
              darkcolor=[("active", Colors.ACCENT)])

    # Backward-compatible aliases.
    style.configure("SetupButton.TButton", font=Fonts.BUTTON, padding=pp(12, 6),
                    background=Colors.BG_SURFACE, foreground=Colors.TEXT_PRIMARY,
                    relief="solid", borderwidth=1, anchor="center", **flat_border)
    style.map("SetupButton.TButton",
              background=[("disabled", Colors.BG_SUBTLE),
                          ("pressed", Colors.ACCENT_LIGHT),
                          ("active", Colors.BG_HOVER)],
              foreground=[("disabled", Colors.TEXT_DISABLED),
                          ("active", Colors.ACCENT_HOVER)],
              bordercolor=[("disabled", Colors.BORDER_LIGHT), ("active", Colors.ACCENT)],
              lightcolor=[("active", Colors.ACCENT)],
              darkcolor=[("active", Colors.ACCENT)])
    style.configure("EEPROMButton.TButton", font=Fonts.BUTTON, padding=pp(12, 7),
                    background=Colors.BG_SURFACE, foreground=Colors.TEXT_PRIMARY,
                    relief="solid", borderwidth=1, anchor="center", **flat_border)
    style.map("EEPROMButton.TButton",
              background=[("disabled", Colors.BG_SUBTLE),
                          ("pressed", Colors.ACCENT_LIGHT),
                          ("active", Colors.BG_HOVER)],
              foreground=[("disabled", Colors.TEXT_DISABLED),
                          ("active", Colors.ACCENT_HOVER)],
              bordercolor=[("disabled", Colors.BORDER_LIGHT), ("active", Colors.ACCENT)],
              lightcolor=[("active", Colors.ACCENT)],
              darkcolor=[("active", Colors.ACCENT)])

    # ---- TCheckbutton ----------------------------------------------------
    style.configure("TCheckbutton", font=Fonts.BODY,
                    background=Colors.BG_SURFACE,
                    foreground=Colors.TEXT_PRIMARY,
                    padding=pp(2, 3),
                    focuscolor=Colors.BG_SURFACE,
                    indicatorbackground=Colors.BG_INPUT,
                    indicatorforeground=Colors.TEXT_ON_ACCENT,
                    indicatormargin=(0, 0, _px(6), 0),
                    indicatorsize=_px(13),
                    upperbordercolor=Colors.BORDER,
                    lowerbordercolor=Colors.BORDER)
    style.map("TCheckbutton",
              indicatorbackground=[("disabled", Colors.BG_SUBTLE),
                                   ("selected", Colors.ACCENT),
                                   ("active", Colors.ACCENT_SOFT)],
              indicatorforeground=[("selected", Colors.TEXT_ON_ACCENT)],
              upperbordercolor=[("disabled", Colors.BORDER_LIGHT),
                                ("selected", Colors.ACCENT),
                                ("active", Colors.ACCENT)],
              lowerbordercolor=[("disabled", Colors.BORDER_LIGHT),
                                ("selected", Colors.ACCENT),
                                ("active", Colors.ACCENT)],
              foreground=[("disabled", Colors.TEXT_DISABLED),
                          ("active", Colors.ACCENT_HOVER)])
    # The laser lists are the primary selection UI on three tabs, so their
    # labels are bold; source_style() then tints each one by wavelength.
    style.configure("Large.TCheckbutton", font=Fonts.BODY_BOLD)
    style.configure("Bold.TCheckbutton", font=Fonts.BODY_BOLD)
    style.configure("Bold.TRadiobutton", font=Fonts.BODY_BOLD)
    style.configure("Subtle.TCheckbutton", background=Colors.BG_SUBTLE)
    style.configure("Canvas.TCheckbutton", background=Colors.BG_PRIMARY,
                    focuscolor=Colors.BG_PRIMARY)

    # ---- TRadiobutton ----------------------------------------------------
    style.configure("TRadiobutton", font=Fonts.BODY,
                    background=Colors.BG_SURFACE,
                    foreground=Colors.TEXT_PRIMARY,
                    padding=pp(2, 3),
                    focuscolor=Colors.BG_SURFACE,
                    indicatorbackground=Colors.BG_INPUT,
                    indicatorforeground=Colors.ACCENT,
                    indicatormargin=(0, 0, _px(6), 0),
                    indicatorsize=_px(13),
                    upperbordercolor=Colors.BORDER,
                    lowerbordercolor=Colors.BORDER)
    style.map("TRadiobutton",
              indicatorbackground=[("disabled", Colors.BG_SUBTLE),
                                   ("selected", Colors.BG_INPUT),
                                   ("active", Colors.ACCENT_SOFT)],
              indicatorforeground=[("selected", Colors.ACCENT)],
              upperbordercolor=[("disabled", Colors.BORDER_LIGHT),
                                ("selected", Colors.ACCENT),
                                ("active", Colors.ACCENT)],
              lowerbordercolor=[("disabled", Colors.BORDER_LIGHT),
                                ("selected", Colors.ACCENT),
                                ("active", Colors.ACCENT)],
              foreground=[("disabled", Colors.TEXT_DISABLED),
                          ("active", Colors.ACCENT_HOVER)])

    # ---- TNotebook -------------------------------------------------------
    style.configure("TNotebook",
                    background=Colors.BG_PRIMARY,
                    borderwidth=0,
                    tabmargins=(2, _px(6), 2, 0))
    style.configure("TNotebook.Tab",
                    font=Fonts.BODY_BOLD,
                    padding=pp(20, 9),
                    background=Colors.TAB_BG,
                    foreground=Colors.TAB_TEXT,
                    borderwidth=1,
                    bordercolor=Colors.TAB_BG,
                    lightcolor=Colors.TAB_BG,
                    darkcolor=Colors.TAB_BG,
                    focuscolor=Colors.TAB_BG)
    style.map("TNotebook.Tab",
              background=[("selected", Colors.TAB_ACTIVE),
                          ("active", Colors.TAB_HOVER)],
              foreground=[("selected", Colors.TAB_TEXT_ACTIVE),
                          ("active", Colors.ACCENT_HOVER)],
              bordercolor=[("selected", Colors.BORDER_LIGHT),
                           ("active", Colors.TAB_HOVER)],
              lightcolor=[("selected", Colors.TAB_ACTIVE),
                          ("active", Colors.TAB_HOVER)],
              darkcolor=[("selected", Colors.TAB_ACTIVE),
                         ("active", Colors.TAB_HOVER)],
              expand=[("selected", (0, _px(3), 0, 0))])
    # Inner notebook (Analysis run tabs): lighter, smaller.
    style.configure("Inner.TNotebook", background=Colors.BG_PRIMARY,
                    borderwidth=0, tabmargins=(0, _px(4), 0, 0))
    style.configure("Inner.TNotebook.Tab", font=Fonts.BODY_SMALL_BOLD,
                    padding=pp(14, 6))

    # ---- TSeparator ------------------------------------------------------
    style.configure("TSeparator", background=Colors.BORDER_LIGHT)

    # ---- TProgressbar ----------------------------------------------------
    for orient in ("Horizontal", "Vertical"):
        style.configure(f"{orient}.TProgressbar",
                        background=Colors.ACCENT,
                        troughcolor=Colors.BORDER_LIGHT,
                        bordercolor=Colors.BORDER_LIGHT,
                        lightcolor=Colors.ACCENT,
                        darkcolor=Colors.ACCENT,
                        borderwidth=0,
                        thickness=_px(8))

    # ---- TScrollbar: thumb only, no arrows -------------------------------
    for orient, side in (("Vertical", "ns"), ("Horizontal", "ew")):
        try:
            style.layout(f"{orient}.TScrollbar", [
                (f"{orient}.Scrollbar.trough", {
                    "sticky": side,
                    "children": [
                        (f"{orient}.Scrollbar.thumb",
                         {"expand": "1", "sticky": "nswe"}),
                    ],
                }),
            ])
        except tk.TclError:  # pragma: no cover - defensive
            pass
    style.configure("TScrollbar",
                    background=Colors.BORDER,
                    troughcolor=Colors.BG_PRIMARY,
                    bordercolor=Colors.BG_PRIMARY,
                    lightcolor=Colors.BORDER,
                    darkcolor=Colors.BORDER,
                    borderwidth=0,
                    relief="flat",
                    arrowsize=_px(12),
                    width=_px(11))
    style.map("TScrollbar",
              background=[("pressed", Colors.ACCENT), ("active", Colors.BORDER_STRONG)],
              lightcolor=[("pressed", Colors.ACCENT), ("active", Colors.BORDER_STRONG)],
              darkcolor=[("pressed", Colors.ACCENT), ("active", Colors.BORDER_STRONG)])

    # ---- TScale ----------------------------------------------------------
    style.configure("TScale", background=Colors.BG_SURFACE,
                    troughcolor=Colors.BORDER_LIGHT,
                    bordercolor=Colors.BORDER_LIGHT,
                    lightcolor=Colors.ACCENT, darkcolor=Colors.ACCENT)

    # ---- Treeview --------------------------------------------------------
    style.configure("Treeview", font=Fonts.BODY, rowheight=_px(28),
                    background=Colors.BG_SURFACE,
                    fieldbackground=Colors.BG_SURFACE,
                    foreground=Colors.TEXT_PRIMARY,
                    borderwidth=1, relief="solid",
                    bordercolor=Colors.BORDER_LIGHT,
                    lightcolor=Colors.BORDER_LIGHT,
                    darkcolor=Colors.BORDER_LIGHT)
    style.map("Treeview",
              background=[("selected", Colors.ACCENT_LIGHT)],
              foreground=[("selected", Colors.ACCENT_DEEP)])
    style.configure("Treeview.Heading", font=Fonts.BODY_SMALL_BOLD,
                    background=Colors.BG_HEADER,
                    foreground=Colors.TEXT_SECONDARY,
                    relief="flat", padding=pp(8, 6))
    style.map("Treeview.Heading",
              background=[("active", Colors.ACCENT_LIGHT)])

    # ---- Label aliases used in tabs (backward compat) -------------------
    style.configure("SetupLabel.TLabel", font=Fonts.BODY,
                    foreground=Colors.TEXT_SECONDARY)
    style.configure("SetupStatus.TLabel", font=Fonts.BODY_BOLD)
    style.configure("EEPROMLabel.TLabel", font=Fonts.BODY,
                    foreground=Colors.TEXT_SECONDARY)
    style.configure("EEPROMValue.TLabel", font=Fonts.MONO_BOLD)
    # Labels living inside white cards need the card background, otherwise the
    # default canvas tint shows through as a grey block.
    for name in ("SetupLabel.TLabel", "SetupStatus.TLabel", "EEPROMLabel.TLabel",
                 "EEPROMValue.TLabel"):
        style.configure(name, background=Colors.BG_SURFACE)

    # ---- Root window background -----------------------------------------
    root.configure(bg=Colors.BG_PRIMARY)
    try:
        root.option_add("*Toplevel.background", Colors.BG_PRIMARY)
        root.option_add("*Menu.background", Colors.BG_SURFACE)
        root.option_add("*Menu.foreground", Colors.TEXT_PRIMARY)
        root.option_add("*Menu.activeBackground", Colors.ACCENT)
        root.option_add("*Menu.activeForeground", Colors.TEXT_ON_ACCENT)
        root.option_add("*Menu.relief", "flat")
        root.option_add("*Menu.borderWidth", "1")
    except tk.TclError:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def attach_hover(button: tk.Button) -> tk.Button:
    """Give a classic ``tk.Button`` a hover state derived from its current colour.

    The colour is read on every ``<Enter>`` rather than captured once, so the
    hover still tracks buttons whose ``bg`` is swapped at runtime (the Run All
    button turns amber while a run is in flight).
    """
    state: dict = {}

    def _enter(_event=None):
        try:
            if str(button.cget("state")) == "disabled":
                return
            current = button.cget("bg")
            state["bg"] = current
            button.configure(bg=shade(current, 0.88))
        except tk.TclError:
            pass

    def _leave(_event=None):
        try:
            if "bg" in state:
                button.configure(bg=state.pop("bg"))
        except tk.TclError:
            pass

    button.bind("<Enter>", _enter, add="+")
    button.bind("<Leave>", _leave, add="+")
    return button


def _tk_button(parent, text, command, *, bg, hover_bg, fg=Colors.TEXT_ON_ACCENT,
               font=Fonts.BUTTON_BOLD, padx=16, pady=9, width=None, **extra):
    kwargs = {
        "text": text,
        "command": command,
        "bg": bg,
        "fg": fg,
        "activebackground": hover_bg,
        "activeforeground": fg,
        "disabledforeground": Colors.TEXT_DISABLED,
        "font": font,
        "relief": "flat",
        "overrelief": "flat",
        "cursor": "hand2",
        "bd": 0,
        "highlightthickness": 0,
        "padx": _px(padx),
        "pady": _px(pady),
    }
    if width is not None:
        kwargs["width"] = width
    kwargs.update(extra)
    return attach_hover(tk.Button(parent, **kwargs))


def make_action_button(parent, text: str, command, *, danger: bool = False,
                       width: int | None = None) -> tk.Button:
    """Create a styled tk.Button for prominent actions (Start/Stop)."""
    if danger:
        bg, active_bg = Colors.DANGER, Colors.DANGER_HOVER
    else:
        bg, active_bg = Colors.SUCCESS, Colors.SUCCESS_HOVER
    return _tk_button(parent, text, command, bg=bg, hover_bg=active_bg,
                      padx=14, pady=9, width=width)


def make_run_button(parent, text: str, command, *, width: int = 15) -> tk.Button:
    """Green 'Run' button used in Measurements."""
    return _tk_button(parent, text, command, bg=Colors.SUCCESS,
                      hover_bg=Colors.SUCCESS_HOVER, padx=12, pady=9,
                      width=width)


# ---------------------------------------------------------------------------
# Background harmonisation
#
# ttk resolves a widget's background from its *style*, not from the widget it
# is packed into, so a plain ``ttk.Label`` dropped inside a white card keeps
# painting the canvas tint and shows up as a grey block behind its text. That
# was visible on every panel of the old build. ``harmonize`` walks a subtree
# once, works out the real background of each container, and pushes it onto
# the children that would otherwise mismatch.
# ---------------------------------------------------------------------------

# Frame styles whose background is known, and the reverse mapping used to pick
# a frame style for a given colour.
_FRAME_BG = {
    "Surface.TFrame": Colors.BG_SURFACE,
    "Card.TFrame": Colors.BG_SURFACE,
    "Subtle.TFrame": Colors.BG_SUBTLE,
    "Header.TFrame": Colors.BG_HEADER,
    "Navy.TFrame": Colors.NAVY,
    "Accent.TFrame": Colors.ACCENT,
    "Hairline.TFrame": Colors.BORDER_LIGHT,
}
_BG_FRAME_STYLE = {
    Colors.BG_SURFACE: "Surface.TFrame",
    Colors.BG_SUBTLE: "Subtle.TFrame",
    Colors.BG_HEADER: "Header.TFrame",
    Colors.NAVY: "Navy.TFrame",
}
# Style-name prefix used to derive an on-background variant of a themed widget.
_BG_PREFIX = {
    Colors.BG_PRIMARY: "Canvas",
    Colors.BG_SUBTLE: "Subtle",
    Colors.BG_HEADER: "Header",
    Colors.NAVY: "Navy",
}
# Label styles that only carry typography, so their background may be retinted.
_RETINTABLE_LABELS = {
    "", "TLabel", "Secondary.TLabel", "Muted.TLabel", "Hint.TLabel",
    "Heading.TLabel", "Title.TLabel", "Display.TLabel", "Status.TLabel",
    "StatusIcon.TLabel", "Mono.TLabel", "Value.TLabel",
    "Strong.TLabel", "Metric.TLabel", "MetricSmall.TLabel", "FieldLabel.TLabel",
    "SetupLabel.TLabel", "SetupStatus.TLabel",
    "EEPROMLabel.TLabel", "EEPROMValue.TLabel",
}
_VARIANT_CACHE: set = set()


def _widget_style(widget) -> str:
    try:
        return str(widget.cget("style") or "")
    except Exception:
        return ""


def _variant_style(widget, base: str, background: str) -> str | None:
    """Return a style like ``Canvas.Large.TCheckbutton`` configured for ``background``."""
    prefix = _BG_PREFIX.get(background)
    if prefix is None:
        return None
    name = f"{prefix}.{base}"
    if name not in _VARIANT_CACHE:
        try:
            style = ttk.Style(widget)
            style.configure(name, background=background, focuscolor=background)
            if background == Colors.NAVY:
                style.configure(name, foreground=Colors.TEXT_ON_NAVY)
        except tk.TclError:
            return None
        _VARIANT_CACHE.add(name)
    return name


def harmonize(widget, background: str = Colors.BG_PRIMARY) -> None:
    """Match label / frame / choice-widget backgrounds to their container.

    Safe to call repeatedly -- it only ever rewrites colours, never geometry --
    so tabs that rebuild content at runtime can simply call it again.
    """
    try:
        children = widget.winfo_children()
    except tk.TclError:
        return

    for child in children:
        try:
            cls = child.winfo_class()
        except tk.TclError:
            continue
        style_name = _widget_style(child)

        if cls == "TLabelframe":
            harmonize(child, Colors.BG_SURFACE)
        elif cls in ("TFrame", "TPanedwindow"):
            known = _FRAME_BG.get(style_name)
            if known is not None:
                harmonize(child, known)
                continue
            if background != Colors.BG_PRIMARY and style_name in ("", "TFrame"):
                target = _BG_FRAME_STYLE.get(background)
                if target:
                    try:
                        child.configure(style=target)
                    except tk.TclError:
                        pass
            harmonize(child, background)
        elif cls in ("Frame", "Canvas", "Labelframe", "Toplevel"):
            try:
                child_bg = str(child.cget("background"))
            except tk.TclError:
                child_bg = background
            harmonize(child, child_bg or background)
        elif cls == "TLabel":
            if style_name in _RETINTABLE_LABELS:
                try:
                    child.configure(background=background)
                except tk.TclError:
                    pass
        elif cls in ("TCheckbutton", "TRadiobutton"):
            base = style_name or f"{cls}"
            if base.startswith(tuple(_BG_PREFIX.values())):
                continue
            variant = _variant_style(child, base, background)
            if variant:
                try:
                    child.configure(style=variant)
                except tk.TclError:
                    pass
        elif cls == "TNotebook":
            harmonize(child, Colors.BG_PRIMARY)
        else:
            harmonize(child, background)


def status_dot(parent, connected: bool = False, **grid_kw) -> ttk.Label:
    """Return a coloured dot label for connection status."""
    color = Colors.CONNECTED if connected else Colors.DISCONNECTED
    lbl = ttk.Label(parent, text="●", foreground=color,
                    font=Fonts.DOT)
    return lbl


def source_color(tag) -> str:
    """Colour for a source tag ("532", "Hg_Ar"), interpolated for unlisted ones."""
    key = str(tag).strip()
    exact = Colors.SOURCE.get(key)
    if exact:
        return exact
    try:
        nm = float(key)
    except (TypeError, ValueError):
        return Colors.SOURCE_DEFAULT
    # A laser the operator added by hand still lands in the right hue band.
    bands = ((390, "#6d28d9"), (430, "#5b21b6"), (470, "#1d4ed8"), (500, "#0e7490"),
             (545, "#15803d"), (580, "#a16207"), (610, "#c2410c"), (660, "#dc2626"),
             (720, "#9f1239"))
    for limit, color in bands:
        if nm < limit:
            return color
    return "#7f1d1d"


def source_style(widget, tag, base: str = "TCheckbutton") -> str:
    """A ttk style that paints ``base`` in the source's colour.

    ttk choice widgets take no per-widget ``foreground``, so a style per colour
    is the only way to tint them. Styles are cached by name, and ``harmonize``
    derives its background variants from whatever name is set here, so the tint
    survives re-tinting.
    """
    color = source_color(tag)
    name = f"S{color.lstrip('#')}.{base}"
    if name not in _VARIANT_CACHE:
        try:
            ttk.Style(widget).configure(name, foreground=color)
        except tk.TclError:
            return base
        _VARIANT_CACHE.add(name)
    return name


def gradient_rail(parent, *, height: int = 3, start: str | None = None,
                  end: str | None = None, background: str | None = None):
    """A thin horizontal gradient strip, redrawn on resize.

    Tk has no gradients, so it is painted as one line per column on a canvas.
    At 3 px tall that is a few hundred cheap line items, only on resize.
    """
    start_rgb = _rgb(start or Colors.ACCENT)
    end_rgb = _rgb(end or "#22d3ee")
    canvas = tk.Canvas(parent, height=height, highlightthickness=0, borderwidth=0,
                       bg=background or Colors.BG_SURFACE)

    def _paint(_event=None):
        try:
            width = max(1, canvas.winfo_width())
            canvas.delete("grad")
            steps = min(width, 256)
            span = width / steps
            for index in range(steps):
                ratio = index / max(1, steps - 1)
                color = "#%02x%02x%02x" % tuple(
                    _clamp(start_rgb[c] + (end_rgb[c] - start_rgb[c]) * ratio)
                    for c in range(3)
                )
                x0 = index * span
                canvas.create_rectangle(x0, 0, x0 + span + 1, height,
                                        outline="", fill=color, tags="grad")
        except tk.TclError:
            pass

    canvas.bind("<Configure>", _paint, add="+")
    return canvas


def style_listbox(widget, *, mono: bool = True) -> None:
    """Apply the theme to a classic ``tk.Listbox`` (ttk has no listbox)."""
    try:
        widget.configure(
            background=Colors.BG_SURFACE,
            foreground=Colors.TEXT_PRIMARY,
            font=Fonts.MONO_SMALL if mono else Fonts.BODY_SMALL,
            selectbackground=Colors.ACCENT_LIGHT,
            selectforeground=Colors.ACCENT_DEEP,
            selectborderwidth=0,
            activestyle="none",
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=Colors.BORDER,
            highlightcolor=Colors.ACCENT,
            disabledforeground=Colors.TEXT_DISABLED,
        )
    except tk.TclError:
        pass


def style_text(widget, *, editable: bool = False, mono: bool = True) -> None:
    """Apply the theme to a classic ``tk.Text``."""
    try:
        widget.configure(
            background=Colors.EDITABLE_BG if editable else Colors.BG_SUBTLE,
            foreground=Colors.TEXT_PRIMARY,
            font=Fonts.MONO_SMALL if mono else Fonts.BODY_SMALL,
            insertbackground=Colors.ACCENT,
            selectbackground=Colors.ACCENT_LIGHT,
            selectforeground=Colors.ACCENT_DEEP,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=Colors.BORDER,
            highlightcolor=Colors.ACCENT,
            padx=10,
            pady=8,
        )
    except tk.TclError:
        pass


def badge(parent, text: str, *, kind: str = "info", background: str | None = None):
    """A small pill label used for source types and states."""
    palette = {
        "info": (Colors.ACCENT_DEEP, Colors.ACCENT_LIGHT),
        "success": (shade(Colors.SUCCESS, 0.75), Colors.SUCCESS_BG),
        "warning": (shade(Colors.WARNING, 0.8), Colors.WARNING_BG),
        "danger": (shade(Colors.DANGER, 0.8), Colors.DANGER_BG),
        "neutral": (Colors.TEXT_SECONDARY, Colors.BG_HEADER),
    }
    fg, bg = palette.get(kind, palette["info"])
    return tk.Label(parent, text=f" {text} ", font=Fonts.CAPTION_BOLD,
                    bg=bg, fg=fg, bd=0, highlightthickness=0,
                    padx=_px(4), pady=_px(1))


def make_chip(parent, text: str = "", *, background: str | None = None):
    """App-bar pill: a status dot and a short label. Returns the frame with ``.dot`` and ``.text``."""
    bg = background or Colors.BG_SUBTLE
    chip = tk.Frame(parent, bg=bg, bd=0, highlightthickness=1,
                    highlightbackground=Colors.BORDER_LIGHT,
                    highlightcolor=Colors.BORDER_LIGHT)
    chip.dot = tk.Label(chip, text="●", font=Fonts.DOT, bg=bg, fg=Colors.IDLE,
                        bd=0, highlightthickness=0)
    chip.dot.pack(side="left", padx=(_px(10), _px(6)), pady=_px(6))
    chip.text = tk.Label(chip, text=text, font=Fonts.BODY_SMALL_BOLD, bg=bg,
                         fg=Colors.TEXT_SECONDARY, bd=0, highlightthickness=0)
    chip.text.pack(side="left", padx=(0, _px(12)), pady=_px(6))
    return chip


def make_flat_button(parent, text: str, command, *, fg: str, bg: str) -> tk.Button:
    """A quiet text button for the inside of chips and strips: no border, a hover wash."""
    return _tk_button(parent, text, command, bg=bg, hover_bg=shade(bg, 0.9), fg=fg,
                      font=Fonts.CAPTION_BOLD, padx=8, pady=3)


def set_action_enabled(button: tk.Button, enabled: bool) -> None:
    """Grey out a ``make_action_button`` or bring it back in its own colour.

    A Start/Stop pair reads wrong when both look live: Stop is greyed while
    nothing runs and Start while something does.
    """
    try:
        if enabled:
            bg = getattr(button, "_enabled_bg", None) or button.cget("bg")
            button.configure(state="normal", bg=bg, fg=Colors.TEXT_ON_ACCENT, cursor="hand2")
            return
        if str(button.cget("state")) == "disabled":
            return
        # Drop any hover shade first, so the resting colour is what is remembered.
        button.event_generate("<Leave>")
        button._enabled_bg = button.cget("bg")
        button.configure(state="disabled", bg=Colors.BG_SUBTLE, cursor="arrow")
    except tk.TclError:
        pass


def empty_state_text(ax, text: str):
    """A muted, centred hint drawn on an axes until the first data arrives."""
    return ax.text(0.5, 0.5, text, transform=ax.transAxes, ha="center", va="center",
                   fontsize=scaled_pt(11.5), color=Colors.TEXT_MUTED,
                   fontfamily=matplotlib_font_family(), zorder=0)


def hairline(parent, *, horizontal: bool = True, color: str | None = None):
    """A 1 px rule -- crisper than ttk.Separator's 2 px bevel."""
    frame = tk.Frame(parent, bg=color or Colors.BORDER_LIGHT,
                     bd=0, highlightthickness=0)
    if horizontal:
        frame.configure(height=1)
    else:
        frame.configure(width=1)
    return frame


def mount_toolbar(canvas, parent, toolbar_class):
    """Add a matplotlib navigation toolbar that is actually visible, and themed.

    ``NavigationToolbar2Tk`` packs itself into its master on construction. Done
    after an expanding canvas has already been packed, pack has no room left to
    give it and the toolbar silently collapses to nothing -- which is why the
    zoom/pan controls could not be reached. Build it unpacked, place it first,
    then let the canvas take the rest.
    """
    try:
        toolbar = toolbar_class(canvas, parent, pack_toolbar=False)
    except TypeError:  # pragma: no cover - matplotlib < 3.3
        toolbar = toolbar_class(canvas, parent)
    try:
        toolbar.update()
    except Exception:
        pass
    style_toolbar(toolbar)
    try:
        toolbar.pack(side="bottom", fill="x")
    except tk.TclError:
        pass
    return toolbar


def style_toolbar(toolbar) -> None:
    """Re-colour a matplotlib Tk toolbar (classic tk widgets) to the theme."""
    try:
        toolbar.configure(background=Colors.BG_SURFACE, bd=0, highlightthickness=0)
    except tk.TclError:
        pass
    for child in getattr(toolbar, "winfo_children", lambda: [])():
        cls = child.winfo_class()
        try:
            if cls in ("Button", "Checkbutton", "Radiobutton"):
                child.configure(background=Colors.BG_SURFACE,
                                activebackground=Colors.BG_HOVER,
                                relief="flat", bd=0, highlightthickness=0)
            elif cls == "Label":
                child.configure(background=Colors.BG_SURFACE,
                                foreground=Colors.TEXT_SECONDARY,
                                font=Fonts.CAPTION)
            elif cls == "Frame":
                child.configure(background=Colors.BG_SURFACE)
        except tk.TclError:
            pass


def bind_figure_margins(canvas, fig, *, left: int = 72, right: int = 16,
                        top: int = 36, bottom: int = 46):
    """Hold the axes margins constant in pixels as the canvas is resized.

    ``subplots_adjust`` takes *fractions*, so a margin tuned for a tall figure
    clips the title once the same figure is short and wide -- which is exactly
    what happens to these plots when the control panels claim their space. The
    margins are given here in reference pixels at 96 dpi and scaled to the
    figure's own dpi, so they hold on 125-150 % displays too.
    """
    widget = canvas.get_tk_widget()

    def _apply(event=None):
        try:
            dpi = float(fig.dpi) or 96.0
            # The *widget* size is authoritative during <Configure>: this
            # handler can run before the canvas has pushed the new size into
            # the figure, and margins computed from a stale figure size are
            # what used to clip the x label away.
            width = float(getattr(event, "width", 0) or widget.winfo_width() or 0)
            height = float(getattr(event, "height", 0) or widget.winfo_height() or 0)
            if width <= 1 or height <= 1:
                width = max(1.0, float(fig.get_figwidth()) * dpi)
                height = max(1.0, float(fig.get_figheight()) * dpi)
            scale = dpi / 96.0 * Fonts.SCALE
            l = min(0.45, left * scale / width)
            r = max(0.55, 1.0 - right * scale / width)
            t = max(0.55, 1.0 - top * scale / height)
            b = min(0.45, bottom * scale / height)
            if r - l < 0.2 or t - b < 0.2:
                return
            fig.subplots_adjust(left=l, right=r, top=t, bottom=b)
        except Exception:
            pass

    _apply()
    try:
        widget.bind("<Configure>", _apply, add="+")
    except tk.TclError:
        pass
    return _apply


def _backend_pixel_ratio(widget) -> float:
    """The device-pixel ratio matplotlib's Tk backend derives for this screen."""
    if sys.platform == "win32":
        return round(float(widget.tk.call("tk", "scaling")) / (96 / 72), 2)
    if sys.platform.startswith("linux"):
        return float(widget.winfo_fpixels("1i")) / 96
    return 1.0


def relax_canvas_size(canvas, width: int = 560, height: int = 320, *, dpi=None):
    """Make a matplotlib canvas fit its container instead of the other way round.

    Two Tk-Agg behaviours fight the layout here:

    * The canvas asks Tk for ``figsize * dpi`` pixels. On a 150 % display a
      14x7 in figure asks for ~2000x1000 px, which starves whatever is packed
      beside it -- the Measurements control panel was squeezed to two pixels
      of height for exactly this reason.
    * On ``<Map>`` the backend re-applies its device-pixel-ratio: it multiplies
      the figure dpi and resizes the *widget* to that physical size. Because
      this app is already DPI-aware, Tk pixels are device pixels, so the extra
      factor just renders an oversized figure that the container then clips --
      the Check Resolution plot lost its x axis to this.

    So: relax the initial request, and after mapping put the figure back on the
    intended dpi and the widget's real allocation.
    """
    widget = canvas.get_tk_widget()
    try:
        widget.configure(width=width, height=height)
    except tk.TclError:
        return widget

    # The backend multiplies the figure dpi by the Windows scale factor the
    # first time the canvas is mapped, resizes, and redraws; the sync below
    # then puts the dpi back and redraws again. Pinning the ratio the backend
    # would compute makes its map handler a no-op, so a tab's first appearance
    # costs one draw instead of three (and the toolbar icons come out at the
    # right size for the screen).
    try:
        canvas._device_pixel_ratio = _backend_pixel_ratio(widget)
    except Exception:
        pass

    target_dpi = float(dpi) if dpi else float(canvas.figure.dpi)
    state = {"busy": False}

    def _sync() -> None:
        if state["busy"]:
            return
        try:
            allocated_w = widget.winfo_width()
            allocated_h = widget.winfo_height()
            if allocated_w <= 1 or allocated_h <= 1:
                return
            fig = canvas.figure
            drawn_w = fig.get_figwidth() * fig.dpi
            drawn_h = fig.get_figheight() * fig.dpi
            if (abs(fig.dpi - target_dpi) < 0.5
                    and abs(drawn_w - allocated_w) < 2
                    and abs(drawn_h - allocated_h) < 2):
                return
            state["busy"] = True
            fig.set_dpi(target_dpi)
            widget.configure(width=allocated_w, height=allocated_h)
            # Hand the backend its own resize path with the real allocation.
            widget.event_generate("<Configure>", width=allocated_w, height=allocated_h)
        except tk.TclError:
            pass
        except Exception:
            pass
        finally:
            state["busy"] = False

    def _schedule(_event=None) -> None:
        try:
            widget.after_idle(_sync)
        except tk.TclError:
            pass

    widget.bind("<Map>", _schedule, add="+")
    _schedule()
    return widget


def configure_matplotlib_style(fig, ax, *, title: str = "",
                                xlabel: str = "Pixel Index",
                                ylabel: str = "Counts") -> None:
    """Apply consistent styling to a matplotlib Figure + Axes."""
    fig.patch.set_facecolor(Colors.BG_SURFACE)
    ax.set_facecolor(Colors.PLOT_FACE)
    family = matplotlib_font_family()

    if title:
        ax.set_title(title, **plot_title_font())
    if xlabel:
        ax.set_xlabel(xlabel, fontsize=scaled_pt(9.5), fontweight="medium",
                      fontfamily=family, labelpad=_px(6),
                      color=Colors.TEXT_SECONDARY)
    if ylabel:
        ax.set_ylabel(ylabel, fontsize=scaled_pt(9.5), fontweight="medium",
                      fontfamily=family, labelpad=_px(6),
                      color=Colors.TEXT_SECONDARY)

    # Major grid only: the minor grid was a third of every full draw at 4K
    # (a hundred extra hairlines per frame) for a texture nobody could see.
    ax.grid(True, which="major", linestyle="-", linewidth=0.5,
            alpha=0.22, color=Colors.PLOT_GRID)
    ax.grid(False, which="minor")
    ax.minorticks_off()
    ax.set_axisbelow(True)

    ax.tick_params(axis="both", which="major", labelsize=scaled_pt(8.5),
                   length=_px(4), width=0.8, colors=Colors.TEXT_SECONDARY,
                   direction="out")
    for label in (*ax.get_xticklabels(), *ax.get_yticklabels()):
        label.set_fontfamily(family)

    # Two spines instead of a box: less ink, same frame of reference.
    for name, spine in ax.spines.items():
        if name in ("top", "right"):
            spine.set_visible(False)
        else:
            spine.set_linewidth(0.9)
            spine.set_color(Colors.BORDER)


def style_legend(ax, **kwargs):
    """Legend with the app's typography and a hairline frame."""
    defaults = dict(
        loc="upper right", frameon=True, framealpha=0.94,
        facecolor=Colors.BG_SURFACE, edgecolor=Colors.BORDER_LIGHT,
        fontsize=scaled_pt(8.5), borderpad=0.6, labelspacing=0.4, handlelength=1.8,
    )
    defaults.update(kwargs)
    legend = ax.legend(**defaults)
    if legend is not None:
        family = matplotlib_font_family()
        legend.get_frame().set_linewidth(0.8)
        for text in legend.get_texts():
            text.set_color(Colors.TEXT_SECONDARY)
            text.set_fontfamily(family)
    return legend
