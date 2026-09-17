import tkinter as tk
from tkinter import ttk


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

        canvas_kwargs = {"highlightthickness": 0, "borderwidth": 0}
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

    def _on_canvas_configure(self, _event=None):
        self._sync_content_width()

    def _sync_content_width(self):
        if self._window_id is None:
            return
        canvas_width = max(1, self.canvas.winfo_width())
        if self._x_scroll:
            req_width = self.content.winfo_reqwidth()
            self.canvas.itemconfigure(self._window_id, width=max(canvas_width, req_width))
        else:
            self.canvas.itemconfigure(self._window_id, width=canvas_width)

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
