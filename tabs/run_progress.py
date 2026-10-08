"""Measurement-run progress: the per-source checklist card and the app-bar chip.

Both read a :class:`services.run_progress.RunProgress`. State changes arrive
through the model's listener (posted to the Tk thread by the caller-supplied
``post``); while a run is in flight they also poll a few times a second so
the integration time, peak and elapsed time keep moving between transitions.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from services.run_progress import (DONE, FAILED, IDLE, PENDING, RUNNING, STOPPED, RunProgress,
                                   RunSnapshot, SourceProgress, format_duration, source_label)

from .theme import Colors, Fonts, Spacing, source_color
from .ui_utils import Tooltip

POLL_MS = 200

_GLYPH = {PENDING: "○", RUNNING: "●", DONE: "✓", FAILED: "✕", STOPPED: "–"}
_GLYPH_COLOR = {
    PENDING: Colors.TEXT_MUTED,
    RUNNING: Colors.WARNING,
    DONE: Colors.SUCCESS,
    FAILED: Colors.DANGER,
    STOPPED: Colors.TEXT_MUTED,
}
_SEP = "  ·  "


def _row_text(source: SourceProgress, now: float) -> str:
    if source.state == PENDING:
        return "waiting"
    if source.state == STOPPED:
        return "stopped" if source.started_at is not None else "not run"
    parts = []
    if source.it_ms is not None:
        parts.append(f"{source.it_ms:.2f} ms")
    if source.peak is not None:
        parts.append(f"{source.peak:,.0f} cts")
    if source.state == RUNNING:
        parts.append(f"{source.frames} fr")
    elif source.state == DONE:
        parts.append(format_duration(source.duration_s(now)))
    elif source.state == FAILED:
        parts.append("failed")
    return _SEP.join(parts)


def _header(snapshot: RunSnapshot) -> tuple:
    """Header text and colour for a run state."""
    if snapshot.status == IDLE:
        return "No run yet", Colors.TEXT_MUTED
    if snapshot.status == RUNNING:
        current = snapshot.current
        if current is None:
            return "Starting…", Colors.TEXT_SECONDARY
        return (f"Measuring {source_label(current.tag)}{_SEP}{snapshot.position} of {snapshot.total}",
                Colors.TEXT_PRIMARY)
    if snapshot.status == STOPPED:
        return f"Stopped{_SEP}{snapshot.done} of {snapshot.total} measured", Colors.WARNING
    if snapshot.failed:
        return f"Finished{_SEP}{snapshot.failed} failed, {snapshot.done} measured", Colors.DANGER
    return f"Finished{_SEP}{snapshot.done} of {snapshot.total} measured", Colors.SUCCESS


class RunProgressPanel(ttk.Frame):
    """Checklist of the sources in a run with the live IT, peak and timing per row."""

    def __init__(self, master, model: RunProgress, *, post, background: str = Colors.BG_SURFACE):
        super().__init__(master, style="Surface.TFrame")
        self.model = model
        self._post = post
        self._bg = background
        self._after_id = None
        self._rows: dict = {}
        self._row_tags: tuple = ()

        header = tk.Frame(self, bg=background)
        header.pack(fill="x")
        self.status_label = tk.Label(header, text="No run yet", font=Fonts.BODY_SMALL_BOLD,
                                     bg=background, fg=Colors.TEXT_MUTED, anchor="w")
        self.status_label.pack(side="left", fill="x", expand=True)
        self.elapsed_label = tk.Label(header, text="", font=Fonts.MONO_SMALL_BOLD,
                                      bg=background, fg=Colors.TEXT_SECONDARY)
        self.elapsed_label.pack(side="right")

        self.bar = ttk.Progressbar(self, orient="horizontal", mode="determinate", maximum=1, value=0)
        self.bar.pack(fill="x", pady=(Spacing.PAD_XS, Spacing.PAD_SM))

        self.rows_frame = tk.Frame(self, bg=background)
        self.rows_frame.pack(fill="both", expand=True)
        self.rows_frame.columnconfigure(2, weight=1)

        model.add_listener(lambda: self._post(self.refresh))
        self.refresh()

    def _ensure_rows(self, tags: tuple) -> None:
        if tags == self._row_tags:
            return
        for child in self.rows_frame.winfo_children():
            child.destroy()
        self._rows = {}
        for index, tag in enumerate(tags):
            glyph = tk.Label(self.rows_frame, text=_GLYPH[PENDING], font=Fonts.BODY_SMALL_BOLD,
                             bg=self._bg, fg=Colors.TEXT_MUTED, width=2, anchor="w")
            glyph.grid(row=index, column=0, sticky="w")
            name = tk.Label(self.rows_frame, text=source_label(tag), font=Fonts.BODY_SMALL_BOLD,
                            bg=self._bg, fg=source_color(tag), width=8, anchor="w")
            name.grid(row=index, column=1, sticky="w", padx=(0, Spacing.PAD_SM))
            value = tk.Label(self.rows_frame, text="", font=Fonts.MONO_SMALL,
                             bg=self._bg, fg=Colors.TEXT_SECONDARY, anchor="w")
            value.grid(row=index, column=2, sticky="ew")
            self._rows[tag] = {"glyph": glyph, "name": name, "value": value, "tip": None}
        self._row_tags = tags

    def refresh(self) -> None:
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

        snapshot = self.model.snapshot()
        self._ensure_rows(tuple(source.tag for source in snapshot.sources))
        for source in snapshot.sources:
            row = self._rows[source.tag]
            row["glyph"].configure(text=_GLYPH[source.state], fg=_GLYPH_COLOR[source.state])
            row["value"].configure(
                text=_row_text(source, snapshot.now),
                fg=Colors.DANGER if source.state == FAILED else Colors.TEXT_SECONDARY,
            )
            if source.state == FAILED and source.message:
                if row["tip"] is None:
                    row["tip"] = Tooltip(row["value"], source.message)
                else:
                    row["tip"].text = source.message

        text, color = _header(snapshot)
        self.status_label.configure(text=text, fg=color)
        self.elapsed_label.configure(text=format_duration(snapshot.elapsed_s) if snapshot.status != IDLE else "")
        self.bar.configure(maximum=max(1, snapshot.total), value=snapshot.done + snapshot.failed)

        if snapshot.status == RUNNING:
            self._after_id = self.after(POLL_MS, self.refresh)


class RunChip:
    """App-bar chip: hidden when idle, amber while measuring, then the outcome for a while."""

    HIDE_AFTER_MS = 20000

    def __init__(self, chip, model: RunProgress, *, post, on_click=None, pack_kwargs=None):
        self.chip = chip
        self.model = model
        self._post = post
        self._pack_kwargs = dict(pack_kwargs or {})
        self._after_id = None
        self._hide_id = None
        self._shown = False
        if on_click is not None:
            for widget in (chip, chip.dot, chip.text):
                widget.bind("<Button-1>", lambda _event: on_click())
                widget.configure(cursor="hand2")
        model.add_listener(lambda: self._post(self.refresh))
        self.refresh()

    def _cancel(self, attr: str) -> None:
        handle = getattr(self, attr)
        if handle is not None:
            try:
                self.chip.after_cancel(handle)
            except tk.TclError:
                pass
            setattr(self, attr, None)

    def _show(self) -> None:
        if not self._shown:
            self.chip.pack(**self._pack_kwargs)
            self._shown = True

    def hide(self) -> None:
        self._cancel("_after_id")
        self._cancel("_hide_id")
        if self._shown:
            try:
                self.chip.pack_forget()
            except tk.TclError:
                pass
            self._shown = False

    def refresh(self) -> None:
        try:
            if not self.chip.winfo_exists():
                return
        except tk.TclError:
            return
        self._cancel("_after_id")
        snapshot = self.model.snapshot()
        if snapshot.status == IDLE:
            self.hide()
            return

        if snapshot.status == RUNNING:
            self._cancel("_hide_id")
            current = snapshot.current
            if current is None:
                text = "Measurement starting…"
            else:
                text = f"Measuring {source_label(current.tag)}{_SEP}{snapshot.position}/{snapshot.total}"
                if current.it_ms is not None:
                    text += f"{_SEP}{current.it_ms:.2f} ms"
            color = Colors.WARNING
            self._after_id = self.chip.after(POLL_MS, self.refresh)
        elif snapshot.status == STOPPED:
            text = f"Stopped{_SEP}{snapshot.done}/{snapshot.total} measured{_SEP}{format_duration(snapshot.elapsed_s)}"
            color = Colors.WARNING
        elif snapshot.failed:
            plural = "s" if snapshot.failed > 1 else ""
            text = f"{snapshot.failed} source{plural} failed{_SEP}{snapshot.done}/{snapshot.total} measured"
            color = Colors.DANGER
        else:
            text = f"Measured {snapshot.done}/{snapshot.total}{_SEP}{format_duration(snapshot.elapsed_s)}"
            color = Colors.SUCCESS

        self.chip.dot.configure(fg=color)
        self.chip.text.configure(text=text, fg=Colors.TEXT_PRIMARY)
        self._show()
        if snapshot.status != RUNNING and self._hide_id is None:
            self._hide_id = self.chip.after(self.HIDE_AFTER_MS, self.hide)
