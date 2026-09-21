from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk

from .ui_pump import ThrottledRedraw, UiPump  # noqa: F401 - re-exported for the tabs


class ScrollableFrame(ttk.Frame):
    """A ttk frame backed by a canvas with optional x/y scrollbars."""

    def __init__(
        self,
        master,
        *,
        x_scroll: bool = False,
        y_scroll: bool = True,
        background: str | None = None,
    ):
        super().__init__(master)

        self._x_scroll = x_scroll
        self._y_scroll = y_scroll
        self._window_id = None
        self._wheel_bound = False

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        from .theme import Colors

        canvas_kwargs = {"highlightthickness": 0, "borderwidth": 0,
                         "bg": Colors.BG_PRIMARY}
        if background is not None:
            canvas_kwargs["bg"] = background
        self.canvas = tk.Canvas(self, **canvas_kwargs)
        self.canvas.grid(row=0, column=0, sticky="nsew")

        if y_scroll:
            self.v_scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
            self.v_scrollbar.grid(row=0, column=1, sticky="ns")
            self.canvas.configure(yscrollcommand=self.v_scrollbar.set)
        else:
            self.v_scrollbar = None

        if x_scroll:
            self.h_scrollbar = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
            self.h_scrollbar.grid(row=1, column=0, sticky="ew")
            self.canvas.configure(xscrollcommand=self.h_scrollbar.set)
        else:
            self.h_scrollbar = None

        self.content = ttk.Frame(self.canvas)
        self._window_id = self.canvas.create_window((0, 0), window=self.content, anchor="nw")

        self.content.bind("<Configure>", self._on_content_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        for widget in (self.canvas, self.content):
            widget.bind("<Enter>", self._bind_mousewheel, add="+")
            widget.bind("<Leave>", self._unbind_mousewheel, add="+")

    def _on_content_configure(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._sync_content_width()
        self._sync_scrollbars()

    def _on_canvas_configure(self, _event=None):
        self._sync_content_width()
        self._sync_scrollbars()

    def _sync_content_width(self):
        if self._window_id is None:
            return
        canvas_width = max(1, self.canvas.winfo_width())
        if self._x_scroll:
            req_width = self.content.winfo_reqwidth()
            self.canvas.itemconfigure(self._window_id, width=max(canvas_width, req_width))
        else:
            self.canvas.itemconfigure(self._window_id, width=canvas_width)

    def _sync_scrollbars(self):
        """Show each scrollbar only while it has something to scroll.

        A permanently visible bar on a panel that already fits reads as an
        error, and the horizontal one on Setup and EEPROM was exactly that.
        The 4 px slack keeps a bar from flickering on its own presence.
        """
        for bar, needed in (
            (self.v_scrollbar, self.content.winfo_reqheight() > self.canvas.winfo_height() + 4),
            (self.h_scrollbar, self.content.winfo_reqwidth() > self.canvas.winfo_width() + 4),
        ):
            if bar is None:
                continue
            try:
                visible = bool(bar.winfo_manager())
                if needed and not visible:
                    bar.grid()
                elif not needed and visible:
                    bar.grid_remove()
            except tk.TclError:
                pass

    def _bind_mousewheel(self, _event=None):
        if self._wheel_bound:
            return
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        self.canvas.bind_all("<Shift-MouseWheel>", self._on_shift_mousewheel, add="+")
        self.canvas.bind_all("<Button-4>", self._on_linux_mousewheel, add="+")
        self.canvas.bind_all("<Button-5>", self._on_linux_mousewheel, add="+")
        self.canvas.bind_all("<Shift-Button-4>", self._on_linux_shift_mousewheel, add="+")
        self.canvas.bind_all("<Shift-Button-5>", self._on_linux_shift_mousewheel, add="+")
        self._wheel_bound = True

    def _unbind_mousewheel(self, _event=None):
        if not self._wheel_bound:
            return
        self.canvas.unbind_all("<MouseWheel>")
        self.canvas.unbind_all("<Shift-MouseWheel>")
        self.canvas.unbind_all("<Button-4>")
        self.canvas.unbind_all("<Button-5>")
        self.canvas.unbind_all("<Shift-Button-4>")
        self.canvas.unbind_all("<Shift-Button-5>")
        self._wheel_bound = False

    def _on_mousewheel(self, event):
        if not self._y_scroll:
            return
        delta = event.delta
        if delta == 0:
            return
        self.canvas.yview_scroll(int(-delta / 120), "units")

    def _on_shift_mousewheel(self, event):
        if not self._x_scroll:
            return
        delta = event.delta
        if delta == 0:
            return
        self.canvas.xview_scroll(int(-delta / 120), "units")

    def _on_linux_mousewheel(self, event):
        if not self._y_scroll:
            return
        direction = -1 if event.num == 4 else 1
        self.canvas.yview_scroll(direction, "units")

    def _on_linux_shift_mousewheel(self, event):
        if not self._x_scroll:
            return
        direction = -1 if event.num == 4 else 1
        self.canvas.xview_scroll(direction, "units")


def run_in_worker(app, work, *, on_done=None, on_error=None, busy=(), name="worker"):
    """Run blocking hardware I/O off the Tk thread.

    ``work()`` executes in a daemon thread; ``on_done(result)`` or
    ``on_error(exc)`` then run on the Tk thread through the UI pump. Widgets in
    ``busy`` are disabled for the duration so the operator cannot fire the
    same operation twice. Hardware calls share one lock so two workers never
    interleave commands on the same serial port.
    """
    lock = getattr(app, "_hardware_io_lock", None)
    if lock is None:
        lock = app._hardware_io_lock = threading.Lock()
    widgets = [widget for widget in busy if widget is not None]
    for widget in widgets:
        try:
            widget.configure(state="disabled")
        except tk.TclError:
            pass

    def _restore():
        for widget in widgets:
            try:
                if widget.winfo_exists():
                    widget.configure(state="normal")
            except tk.TclError:
                pass

    def _run():
        try:
            with lock:
                result = work()
        except Exception as exc:  # noqa: BLE001 - reported to the operator
            def _fail(error=exc):
                _restore()
                if on_error is not None:
                    on_error(error)
                else:
                    app._post_error(name, error)
            app.pump.post(_fail)
            return

        def _finish(value=result):
            _restore()
            if on_done is not None:
                on_done(value)

        app.pump.post(_finish)

    thread = threading.Thread(target=_run, daemon=True, name=name)
    thread.start()
    return thread


class StatusBar(ttk.Frame):
    """Persistent status strip: message + optional progress bar + transient toasts."""

    TOAST_MS = 4000

    def __init__(self, master, **kwargs):
        super().__init__(master, style="Header.TFrame", **kwargs)
        from .theme import Colors, Fonts

        self._colors = Colors
        self.columnconfigure(1, weight=1)

        # Hairline instead of a bevel: the strip reads as part of the window
        # chrome rather than as a sunken widget.
        tk.Frame(self, bg=Colors.BORDER_LIGHT, height=1, bd=0,
                 highlightthickness=0).grid(row=0, column=0, columnspan=4, sticky="ew")

        self.indicator = ttk.Label(self, text="●", font=Fonts.BODY_SMALL,
                                   background=Colors.BG_HEADER,
                                   foreground=Colors.TEXT_MUTED)
        self.indicator.grid(row=1, column=0, sticky="w", padx=(12, 6), pady=5)
        self.message = ttk.Label(self, text="Ready", anchor="w", font=Fonts.BODY_SMALL_BOLD,
                                 background=Colors.BG_HEADER, foreground=Colors.TEXT_SECONDARY)
        self.message.grid(row=1, column=1, sticky="ew", padx=(0, 6), pady=5)
        self.progress = ttk.Progressbar(self, orient="horizontal", mode="determinate", length=220)
        self.detail = ttk.Label(self, text="", anchor="e", font=Fonts.BODY_SMALL,
                                background=Colors.BG_HEADER, foreground=Colors.TEXT_MUTED)
        self.detail.grid(row=1, column=3, sticky="e", padx=(6, 12), pady=5)
        self._toast_id = None
        self._sticky_text = "Ready"

    # -- messages ----------------------------------------------------------------

    def set(self, text: str, kind: str = "info") -> None:
        """Sticky message (kind: info | success | warning | error)."""
        color = {
            "success": self._colors.SUCCESS,
            "warning": self._colors.WARNING,
            "error": self._colors.DANGER,
        }.get(kind, self._colors.TEXT_SECONDARY)
        dot = {
            "success": self._colors.SUCCESS,
            "warning": self._colors.WARNING,
            "error": self._colors.DANGER,
        }.get(kind, self._colors.TEXT_MUTED)
        self._sticky_text = text
        self.message.configure(text=text, foreground=color)
        self.indicator.configure(foreground=dot)

    def toast(self, text: str, kind: str = "success", ms: int | None = None) -> None:
        """Show ``text`` for a few seconds, then restore the sticky message."""
        sticky = self._sticky_text
        self.set(text, kind)
        self._sticky_text = sticky
        if self._toast_id is not None:
            try:
                self.after_cancel(self._toast_id)
            except tk.TclError:
                pass
        self._toast_id = self.after(ms or self.TOAST_MS, lambda: self.set(sticky))

    # -- progress ----------------------------------------------------------------

    def progress_begin(self, text: str, total: int | None = None) -> None:
        self.set(text)
        if total:
            self.progress.configure(mode="determinate", maximum=total, value=0)
        else:
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
        self.progress.grid(row=1, column=2, sticky="e", padx=6, pady=5)

    def progress_update(self, value: int, text: str | None = None, detail: str | None = None) -> None:
        if str(self.progress.cget("mode")) == "determinate":
            self.progress.configure(value=value)
        if text is not None:
            self.set(text)
        if detail is not None:
            self.detail.configure(text=detail)

    def progress_end(self, text: str | None = None, kind: str = "info") -> None:
        try:
            self.progress.stop()
        except tk.TclError:
            pass
        self.progress.grid_remove()
        self.detail.configure(text="")
        if text is not None:
            self.set(text, kind)


class Tooltip:
    """Small delayed hover tooltip for any widget."""

    DELAY_MS = 550

    def __init__(self, widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self._after_id = None
        self._window = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after_id = self.widget.after(self.DELAY_MS, self._show)

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    def _show(self) -> None:
        if self._window is not None or not self.widget.winfo_exists():
            return
        from .theme import Colors, Fonts

        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        window = tk.Toplevel(self.widget)
        window.wm_overrideredirect(True)
        window.wm_geometry(f"+{x}+{y}")
        try:
            window.attributes("-topmost", True)
        except tk.TclError:
            pass
        # Thin accent rail on the left edge, so a tooltip is recognisable as
        # help text at a glance rather than as a stray black box.
        frame = tk.Frame(window, bg=Colors.ACCENT, bd=0, highlightthickness=0)
        frame.pack()
        label = tk.Label(
            frame, text=self.text, justify="left", wraplength=340,
            bg=Colors.NAVY, fg=Colors.TEXT_ON_NAVY, font=Fonts.BODY_SMALL,
            padx=10, pady=7, bd=0,
        )
        label.pack(padx=(3, 0))
        self._window = window

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._window is not None:
            try:
                self._window.destroy()
            except tk.TclError:
                pass
            self._window = None


class BlitRenderer:
    """Redraw a few fast-changing matplotlib artists over a cached background.

    A full matplotlib draw of a 2048-point trace with ticks, grid and legend
    costs tens of milliseconds; copying a cached background and drawing just
    the line costs a few. Call ``request_full()`` whenever anything other than
    the animated artists changed (axis limits, title, resize); the next
    ``draw()`` then does a full render and re-caches the background.
    """

    def __init__(self, canvas, ax, artists) -> None:
        self.canvas = canvas
        self.ax = ax
        self.artists = list(artists)
        for artist in self.artists:
            artist.set_animated(True)
        self._background = None
        self._needs_full = True
        canvas.mpl_connect("draw_event", self._on_draw)
        canvas.get_tk_widget().bind("<Configure>", lambda _event: self.request_full(), add="+")

    def request_full(self) -> None:
        self._needs_full = True

    def _on_draw(self, _event) -> None:
        # Runs at the end of every full draw (ours, a resize, a toolbar zoom).
        # Animated artists were skipped by the full draw, so the copy is a clean
        # background; draw them now so a static view is never blank.
        self._background = self.canvas.copy_from_bbox(self.ax.bbox)
        self._needs_full = False
        for artist in self.artists:
            if artist.get_visible():
                self.ax.draw_artist(artist)

    def draw(self) -> None:
        """Main thread."""
        if self._needs_full or self._background is None:
            self.canvas.draw()  # synchronous: also re-caches the background via _on_draw
            self.canvas.blit(self.ax.bbox)
            return
        self.canvas.restore_region(self._background)
        for artist in self.artists:
            if artist.get_visible():
                self.ax.draw_artist(artist)
        self.canvas.blit(self.ax.bbox)


def bind_debounced_configure(widget, callback, delay_ms: int = 120):
    """Call callback(width, height) after resize activity settles."""

    pending = {"after_id": None}

    def _schedule(_event=None):
        if pending["after_id"] is not None:
            try:
                widget.after_cancel(pending["after_id"])
            except Exception:
                pass
        pending["after_id"] = widget.after(
            delay_ms,
            lambda: callback(widget.winfo_width(), widget.winfo_height()),
        )

    widget.bind("<Configure>", _schedule, add="+")
    widget.after_idle(_schedule)
