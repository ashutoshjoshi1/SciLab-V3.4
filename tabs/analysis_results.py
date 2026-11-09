"""Pieces of a run's Analysis page: the key-results card and the full-size image viewer.

``build_key_results`` turns the metrics JSON the analysis service writes next
to the plots into a card of the numbers an operator reads first (lines
matched, dispersion, one row per measured laser), so the result of a run
can be judged without opening a picture.

``ImageViewer`` shows a plot at the size of the screen inside the
application, with Previous / Next through the run's plots, instead of
handing the PNG to whatever Windows associates with it.
"""
from __future__ import annotations

import json
import logging
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Callable, List, Optional, Sequence, Tuple

from services.run_progress import source_label

from .theme import Colors, Fonts, Spacing, harmonize, logical_px, source_color
from .ui_utils import Tooltip

LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Key results
# ---------------------------------------------------------------------------

def load_metrics(path) -> Optional[dict]:
    """The metrics JSON of a run, or None when it is missing or unreadable."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        LOGGER.debug("Metrics file %s not readable: %s", path, exc)
        return None
    return payload if isinstance(payload, dict) else None


def _fmt(value, digits: int = 3, unit: str = "") -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if number != number:          # NaN
        return "—"
    return f"{number:.{digits}f}{unit}"


def key_results_rows(metrics: dict) -> List[Tuple[str, str, str, str, str, str]]:
    """One row per measured laser: source, peak pixel, FWHM, 20 % width, a2, a3."""
    peaks = metrics.get("laser_peak_pixels") or {}
    slit = {str(sample.get("wavelength_nm")): sample for sample in metrics.get("slit_parameters") or []
            if isinstance(sample, dict)}
    rows = []
    for line in metrics.get("measured_lines") or []:
        if not isinstance(line, dict):
            continue
        label = str(line.get("label") or "")
        tag = label.replace(" nm", "").strip() or _fmt(line.get("wavelength_nm"), 0)
        try:
            wavelength_key = str(float(line.get("wavelength_nm")))
        except (TypeError, ValueError):
            wavelength_key = ""
        sample = slit.get(wavelength_key, {})
        peak = peaks.get(tag)
        if peak is None and sample:
            peak = sample.get("peak_pixel")
        rows.append((
            source_label(tag),
            "—" if peak is None else str(int(peak)),
            _fmt(line.get("fwhm_nm"), 4),
            _fmt(line.get("width_20_percent_nm"), 4),
            _fmt(sample.get("a2"), 4) if sample else "—",
            _fmt(sample.get("a3"), 4) if sample else "—",
        ))
    return rows


def key_results_headline(metrics: dict) -> List[Tuple[str, str]]:
    """(label, value) pairs for the strip above the table."""
    items: List[Tuple[str, str]] = []
    hg = metrics.get("hg_ar") or {}
    count = hg.get("matched_line_count")
    rmse = hg.get("match_rmse_nm")
    if count:
        text = f"{int(count)} lines matched"
        if rmse is not None:
            text += f", RMSE {_fmt(rmse, 3, ' nm')}"
        items.append(("Hg-Ar", text))
    else:
        items.append(("Hg-Ar", "not measured"))
    coefficients = metrics.get("dispersion_coefficients_high_to_low") or []
    if len(coefficients) >= 2:
        items.append(("Dispersion", f"{_fmt(coefficients[-2], 5, ' nm/px')} (linear term)"))
    lines = metrics.get("measured_lines") or []
    labels = [str(line.get("label") or "") for line in lines if isinstance(line, dict)]
    if labels:
        items.append(("Lasers", ", ".join(labels)))
    pixels = metrics.get("pixel_count")
    if pixels:
        items.append(("Detector", f"{int(pixels)} pixels"))
    return items


def build_key_results(parent, metrics: dict, *, root) -> Optional[ttk.LabelFrame]:
    """A card with the headline numbers and a per-laser table. None when there is nothing to show."""
    rows = key_results_rows(metrics)
    headline = key_results_headline(metrics)
    if not rows and not headline:
        return None

    card = ttk.LabelFrame(parent, text="Key results", padding=Spacing.PAD_MD)

    strip = ttk.Frame(card, style="Surface.TFrame")
    strip.pack(fill="x", pady=(0, Spacing.PAD_SM if rows else 0))
    for column, (label, value) in enumerate(headline):
        cell = ttk.Frame(strip, style="Surface.TFrame")
        cell.grid(row=0, column=column, sticky="w", padx=(0, logical_px(root, 28)))
        ttk.Label(cell, text=label, style="FieldLabel.TLabel").pack(anchor="w")
        ttk.Label(cell, text=value, style="Strong.TLabel").pack(anchor="w")

    if rows:
        columns = ("peak", "fwhm", "w20", "a2", "a3")
        tree = ttk.Treeview(card, columns=columns, show="tree headings", height=min(9, len(rows)),
                            selectmode="none")
        tree.heading("#0", text="Source", anchor="w")
        tree.heading("peak", text="Peak pixel", anchor="e")
        tree.heading("fwhm", text="FWHM (nm)", anchor="e")
        tree.heading("w20", text="Width at 20 % (nm)", anchor="e")
        tree.heading("a2", text="a2 (µm)", anchor="e")
        tree.heading("a3", text="a3 (µm)", anchor="e")
        tree.column("#0", width=logical_px(root, 110), minwidth=logical_px(root, 90), stretch=False, anchor="w")
        for name, width in (("peak", 100), ("fwhm", 110), ("w20", 150), ("a2", 100), ("a3", 100)):
            tree.column(name, width=logical_px(root, width), minwidth=logical_px(root, 80),
                        stretch=False, anchor="e")
        for row in rows:
            tag = row[0].replace(" nm", "")
            color_tag = f"src_{tag}"
            tree.tag_configure(color_tag, foreground=source_color(tag))
            tree.insert("", "end", text=row[0], values=row[1:], tags=(color_tag,))
        tree.pack(anchor="w", fill="x")
        Tooltip(tree, "FWHM and the width at 20 % of the peak come from the normalised line-spread "
                      "function of each laser; a2 and a3 are the fitted slit-function parameters.")
    harmonize(card, Colors.BG_SURFACE)
    return card


# ---------------------------------------------------------------------------
# Image viewer
# ---------------------------------------------------------------------------

class ImageViewer(tk.Toplevel):
    """A plot at the size of the screen, with Previous / Next through the run's plots.

    ``items`` are ``(title, path)`` pairs. Left / Right (and the buttons)
    move through them, Escape closes. The PNG is decoded once per path and
    refitted when the window is resized.
    """

    REFIT_MS = 80
    MAX_UPSCALE = 2.0

    def __init__(self, master, items: Sequence[Tuple[str, Path]], index: int = 0,
                 *, open_externally: Optional[Callable[[Path], None]] = None) -> None:
        super().__init__(master)
        self.withdraw()
        self.title("Plot")
        self.configure(bg=Colors.BG_PRIMARY)
        self.minsize(logical_px(master, 480), logical_px(master, 360))
        try:
            self.transient(master)
        except tk.TclError:
            pass
        self._items: List[Tuple[str, Path]] = []
        self._index = 0
        self._open_externally = open_externally
        self._decoded: dict = {}
        self._photo = None
        self._refit_id = None

        bar = ttk.Frame(self, style="Surface.TFrame")
        bar.pack(fill="x", side="top")
        inner = ttk.Frame(bar, style="Surface.TFrame")
        inner.pack(fill="x", padx=Spacing.PAD_LG, pady=Spacing.PAD_SM)
        titles = ttk.Frame(inner, style="Surface.TFrame")
        titles.pack(side="left", fill="x", expand=True)
        self._title = ttk.Label(titles, text="", font=Fonts.H3, background=Colors.BG_SURFACE,
                                foreground=Colors.TEXT_PRIMARY, anchor="w")
        self._title.pack(anchor="w")
        self._caption = ttk.Label(titles, text="", style="Secondary.TLabel", background=Colors.BG_SURFACE,
                                  anchor="w")
        self._caption.pack(anchor="w")
        ttk.Button(inner, text="Close", command=self.destroy).pack(side="right", padx=(Spacing.PAD_SM, 0))
        if open_externally is not None:
            ttk.Button(inner, text="Open in image viewer",
                       command=lambda: self._open_externally(self.current_path)).pack(side="right",
                                                                                      padx=(Spacing.PAD_SM, 0))
        self._next_btn = ttk.Button(inner, text="Next  ▶", command=self.show_next)
        self._next_btn.pack(side="right", padx=(Spacing.PAD_XS, 0))
        self._prev_btn = ttk.Button(inner, text="◀  Previous", command=self.show_previous)
        self._prev_btn.pack(side="right")
        tk.Frame(self, bg=Colors.BORDER_LIGHT, height=1, bd=0, highlightthickness=0).pack(fill="x", side="top")

        self._image_label = tk.Label(self, bg=Colors.BG_PRIMARY, bd=0, highlightthickness=0,
                                     text="", fg=Colors.TEXT_MUTED, font=Fonts.BODY)
        self._image_label.pack(fill="both", expand=True, padx=Spacing.PAD_MD, pady=Spacing.PAD_MD)
        self._image_label.bind("<Configure>", self._schedule_refit, add="+")

        self.bind("<Escape>", lambda _event: self.destroy())
        self.bind("<Left>", lambda _event: self.show_previous())
        self.bind("<Right>", lambda _event: self.show_next())
        self.bind("<Home>", lambda _event: self.show(0))
        self.bind("<End>", lambda _event: self.show(len(self._items) - 1))

        # Most of the screen, centred over the application window.
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        width, height = int(screen_w * 0.86), int(screen_h * 0.84)
        try:
            x = master.winfo_rootx() + (master.winfo_width() - width) // 2
            y = master.winfo_rooty() + (master.winfo_height() - height) // 2
        except tk.TclError:
            x, y = (screen_w - width) // 2, (screen_h - height) // 2
        x = max(0, min(x, screen_w - width))
        y = max(0, min(y, screen_h - height))
        self.geometry(f"{width}x{height}+{x}+{y}")

        self.set_items(items, index)
        self.deiconify()
        self.lift()
        self.focus_set()

    # -- navigation -------------------------------------------------------------

    @property
    def current_path(self) -> Optional[Path]:
        if not self._items:
            return None
        return Path(self._items[self._index][1])

    @property
    def index(self) -> int:
        return self._index

    def set_items(self, items: Sequence[Tuple[str, Path]], index: int = 0) -> None:
        self._items = [(str(title), Path(path)) for title, path in items]
        self._decoded = {}
        self.show(index)

    def show(self, index: int) -> None:
        if not self._items:
            return
        self._index = max(0, min(int(index), len(self._items) - 1))
        title, path = self._items[self._index]
        self.title(title)
        self._title.configure(text=title)
        self._caption.configure(text=f"{self._index + 1} of {len(self._items)}  ·  {path.name}")
        self._prev_btn.state(["!disabled"] if self._index > 0 else ["disabled"])
        self._next_btn.state(["!disabled"] if self._index < len(self._items) - 1 else ["disabled"])
        self._refit()

    def show_next(self) -> None:
        if self._index < len(self._items) - 1:
            self.show(self._index + 1)

    def show_previous(self) -> None:
        if self._index > 0:
            self.show(self._index - 1)

    # -- rendering ---------------------------------------------------------------

    def _decode(self, path: Path):
        cached = self._decoded.get(path)
        if cached is not None:
            return cached
        try:
            from PIL import Image

            with Image.open(path) as raw:
                image = raw.convert("RGB")
        except Exception as exc:
            LOGGER.warning("Could not open %s: %s", path, exc)
            return None
        self._decoded[path] = image
        return image

    def _schedule_refit(self, _event=None) -> None:
        if self._refit_id is not None:
            try:
                self.after_cancel(self._refit_id)
            except tk.TclError:
                pass
        try:
            self._refit_id = self.after(self.REFIT_MS, self._refit)
        except tk.TclError:
            self._refit_id = None

    def _refit(self) -> None:
        self._refit_id = None
        path = self.current_path
        if path is None:
            return
        image = self._decode(path)
        if image is None:
            self._image_label.configure(image="", text=f"Plot file unavailable:\n{path}")
            self._photo = None
            return
        try:
            from PIL import Image, ImageTk
        except Exception:
            return
        self.update_idletasks()
        box_w = max(64, self._image_label.winfo_width() - 4)
        box_h = max(64, self._image_label.winfo_height() - 4)
        scale = min(box_w / image.width, box_h / image.height)
        # A 150 dpi plot may be enlarged up to twice to fill a 4K screen; past
        # that the lines only get soft, so the rest of the window stays empty.
        scale = min(scale, self.MAX_UPSCALE)
        size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
        try:
            fitted = image.resize(size, Image.Resampling.LANCZOS) if size != image.size else image
            self._photo = ImageTk.PhotoImage(fitted, master=self)
            self._image_label.configure(image=self._photo, text="")
        except tk.TclError:
            pass


def open_image_viewer(app, items: Sequence[Tuple[str, Path]], index: int = 0,
                      *, open_externally=None) -> ImageViewer:
    """Show ``items`` in the application's one viewer window, creating it on first use."""
    viewer = getattr(app, "image_viewer", None)
    if viewer is not None:
        try:
            if viewer.winfo_exists():
                viewer.set_items(items, index)
                viewer.deiconify()
                viewer.lift()
                viewer.focus_set()
                return viewer
        except tk.TclError:
            pass
    viewer = ImageViewer(app, items, index, open_externally=open_externally)
    app.image_viewer = viewer
    return viewer
