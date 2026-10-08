"""A collapsible event log above the status bar.

Backend errors and bench diagnostics otherwise go to dialogs and the console;
when a head misbehaves the operator wants the last few hundred log lines in
front of them without leaving the app. ``install_log_handler`` attaches a
handler to the root logger that marshals every record to the Tk thread
through the UI pump; the drawer colours warnings and errors and counts the
ones that arrived while it was closed on its toggle button.
"""
from __future__ import annotations

import logging
import tkinter as tk
from collections import deque
from tkinter import ttk

from .theme import Colors, Fonts, Spacing, logical_px, make_flat_button, style_text
from .ui_utils import Tooltip

MAX_LINES = 2000
_LEVEL_TAG = {
    logging.DEBUG: "debug",
    logging.INFO: "info",
    logging.WARNING: "warning",
    logging.ERROR: "error",
    logging.CRITICAL: "error",
}


class TkLogHandler(logging.Handler):
    """Hands formatted records to ``sink(levelno, line)`` on the Tk thread via ``post``."""

    def __init__(self, post, sink, level: int = logging.INFO) -> None:
        super().__init__(level)
        self._post = post
        self._sink = sink
        self.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
                                            datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._post(self._sink, record.levelno, self.format(record))
        except Exception:  # noqa: BLE001 - logging must never raise into the app
            self.handleError(record)


def install_log_handler(post, sink, level: int = logging.INFO) -> TkLogHandler:
    handler = TkLogHandler(post, sink, level)
    logging.getLogger().addHandler(handler)
    return handler


class LogDrawer(ttk.Frame):
    """Read-only, colour-tagged log view that packs above the status bar on demand."""

    def __init__(self, master, *, toggle_host, height_px: int = 170) -> None:
        super().__init__(master, style="Header.TFrame")
        self._shown = False
        self._unseen = 0
        self._pending = deque()
        self._lines = 0

        # Toggle button lives in the status bar; it also reports unseen warnings.
        self.toggle_btn = make_flat_button(toggle_host, "Log", self.toggle,
                                           fg=Colors.TEXT_SECONDARY, bg=Colors.BG_HEADER)
        self.toggle_btn.pack(side="right")
        Tooltip(self.toggle_btn, "Show or hide the event log (F12). Warnings and errors that arrive "
                                 "while it is closed are counted here.")

        tk.Frame(self, bg=Colors.BORDER_LIGHT, height=1, bd=0, highlightthickness=0).pack(fill="x")
        header = ttk.Frame(self, style="Header.TFrame")
        header.pack(fill="x", padx=Spacing.PAD_MD, pady=(Spacing.PAD_XS, 0))
        ttk.Label(header, text="Event log", font=Fonts.BODY_SMALL_BOLD,
                  background=Colors.BG_HEADER, foreground=Colors.TEXT_SECONDARY).pack(side="left")
        make_flat_button(header, "Close", self.hide, fg=Colors.TEXT_SECONDARY, bg=Colors.BG_HEADER).pack(side="right")
        make_flat_button(header, "Copy", self.copy_to_clipboard, fg=Colors.TEXT_SECONDARY,
                         bg=Colors.BG_HEADER).pack(side="right")
        make_flat_button(header, "Clear", self.clear, fg=Colors.TEXT_SECONDARY, bg=Colors.BG_HEADER).pack(side="right")

        body = ttk.Frame(self, style="Header.TFrame")
        body.pack(fill="both", expand=True, padx=Spacing.PAD_MD, pady=(Spacing.PAD_XS, Spacing.PAD_SM))
        body.pack_propagate(False)
        body.configure(height=logical_px(master, height_px))
        scrollbar = ttk.Scrollbar(body, orient="vertical")
        self.text = tk.Text(body, wrap="none", height=8, yscrollcommand=scrollbar.set)
        style_text(self.text, mono=True)
        self.text.configure(state="disabled")
        self.text.tag_configure("debug", foreground=Colors.TEXT_MUTED)
        self.text.tag_configure("info", foreground=Colors.TEXT_PRIMARY)
        self.text.tag_configure("warning", foreground=Colors.WARNING)
        self.text.tag_configure("error", foreground=Colors.DANGER)
        scrollbar.configure(command=self.text.yview)
        self.text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    # -- state -----------------------------------------------------------------

    @property
    def shown(self) -> bool:
        return self._shown

    def _refresh_toggle(self) -> None:
        if self._unseen and not self._shown:
            self.toggle_btn.configure(text=f"Log  ·  {self._unseen} ⚠", fg=Colors.DANGER)
        else:
            self.toggle_btn.configure(text="Log", fg=Colors.TEXT_SECONDARY)

    def show(self) -> None:
        if self._shown:
            return
        notebook = getattr(self.master, "nb", None)
        try:
            if notebook is not None:
                self.pack(fill="x", side="bottom", before=notebook)
            else:
                self.pack(fill="x", side="bottom")
        except tk.TclError:
            self.pack(fill="x", side="bottom")
        self._shown = True
        self._unseen = 0
        self._refresh_toggle()
        self.text.see("end")

    def hide(self) -> None:
        if not self._shown:
            return
        self.pack_forget()
        self._shown = False
        self._refresh_toggle()

    def toggle(self) -> None:
        if self._shown:
            self.hide()
        else:
            self.show()

    # -- content ---------------------------------------------------------------

    def append(self, levelno: int, line: str) -> None:
        """Tk thread: add one formatted record."""
        tag = _LEVEL_TAG.get(levelno, "error" if levelno > logging.WARNING else "info")
        try:
            at_end = self.text.yview()[1] >= 0.999
            self.text.configure(state="normal")
            self.text.insert("end", line + "\n", tag)
            self._lines += 1
            if self._lines > MAX_LINES:
                self.text.delete("1.0", f"{self._lines - MAX_LINES + 1}.0")
                self._lines = MAX_LINES
            self.text.configure(state="disabled")
            if at_end:
                self.text.see("end")
        except tk.TclError:
            return
        if levelno >= logging.WARNING and not self._shown:
            self._unseen += 1
            self._refresh_toggle()

    def clear(self) -> None:
        try:
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.configure(state="disabled")
        except tk.TclError:
            pass
        self._lines = 0

    def copy_to_clipboard(self) -> None:
        try:
            content = self.text.get("1.0", "end-1c")
            self.clipboard_clear()
            self.clipboard_append(content)
        except tk.TclError:
            pass

    def contents(self) -> str:
        try:
            return self.text.get("1.0", "end-1c")
        except tk.TclError:
            return ""
