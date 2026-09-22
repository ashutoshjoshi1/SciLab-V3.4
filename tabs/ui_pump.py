"""Main-thread marshalling for Tk without touching Tk from worker threads.

``UiPump`` owns one queue drained by a single ``after`` loop on the Tk main
thread. Worker threads call ``post(fn, *args)``; ``fn`` runs on the main
thread on the next tick. This replaces the previous pattern of calling
``widget.after(...)`` and ``widget.configure(...)`` directly from worker
threads, which relies on undefined behaviour in Tcl and is the usual source
of ``RuntimeError: main thread is not in main loop`` during shutdown.

``ThrottledRedraw`` sits on top of the pump: acquisition threads ``submit``
frames as fast as they arrive, only the newest frame is kept, and it is
rendered at most once per pump tick and drawn only when its tab is visible.

Both classes are Tk-free (they only need an object with ``after``/``after_cancel``)
so they can be unit-tested without ``_tkinter``.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable, Optional

LOGGER = logging.getLogger(__name__)


class UiPump:
    def __init__(self, root, *, interval_ms: int = 15, budget_s: float = 0.008) -> None:
        self._root = root
        self._interval = int(interval_ms)
        self._budget = float(budget_s)
        self._queue: "queue.SimpleQueue[tuple[Callable[..., Any], tuple]]" = queue.SimpleQueue()
        self._after_id: Optional[str] = None
        self._running = False
        self._main_thread = threading.current_thread()

    # -- lifecycle (main thread) -------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._after_id = self._root.after(self._interval, self._drain)

    def stop(self) -> None:
        """Stop draining. Call on the main thread before destroying the root."""
        self._running = False
        if self._after_id is not None:
            try:
                self._root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    @property
    def is_running(self) -> bool:
        return self._running

    # -- posting (any thread) ----------------------------------------------------

    def post(self, fn: Callable[..., Any], *args: Any) -> None:
        """Run ``fn(*args)`` on the main thread soon. Silently dropped after ``stop``."""
        if self._running:
            self._queue.put((fn, args))

    def call_sync(self, fn: Callable[..., Any], *args: Any, timeout: Optional[float] = None) -> Any:
        """Run ``fn`` on the main thread and return its result to the caller.

        Called on the main thread it simply runs ``fn`` (never deadlocks the drainer).
        """
        if threading.current_thread() is self._main_thread:
            return fn(*args)
        if not self._running:
            raise RuntimeError("UI pump is stopped")

        box: dict[str, Any] = {}
        done = threading.Event()

        def _run() -> None:
            try:
                box["result"] = fn(*args)
            except BaseException as exc:  # propagate to the caller's thread
                box["error"] = exc
            finally:
                done.set()

        self.post(_run)
        if not done.wait(timeout):
            raise TimeoutError(f"UI call {getattr(fn, '__name__', fn)!r} timed out after {timeout}s")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    # -- draining (main thread) --------------------------------------------------

    def _drain(self) -> None:
        deadline = time.perf_counter() + self._budget
        while True:
            try:
                fn, args = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception:
                LOGGER.exception("UI pump callable %r failed", fn)
            if time.perf_counter() >= deadline:
                break  # leave the rest for the next tick so input stays responsive
        if self._running:
            self._after_id = self._root.after(self._interval, self._drain)


class ThrottledRedraw:
    """Keep only the newest payload and render it once per pump tick."""

    def __init__(
        self,
        pump: UiPump,
        render: Callable[[Any], None],
        *,
        draw: Optional[Callable[[], None]] = None,
        visible: Callable[[], bool] = lambda: True,
    ) -> None:
        self._pump = pump
        self._render = render
        self._draw = draw
        self._visible = visible
        self._lock = threading.Lock()
        self._payload: Any = None
        self._scheduled = False

    def submit(self, payload: Any) -> None:
        """Any thread. Newer payloads replace older ones that have not rendered yet."""
        with self._lock:
            self._payload = payload
            if self._scheduled:
                return  # the queued flush will pick up this payload
            self._scheduled = True
        self._pump.post(self._flush)

    def cancel(self) -> None:
        with self._lock:
            self._payload = None
            self._scheduled = False

    @property
    def pending(self) -> bool:
        with self._lock:
            return self._scheduled

    def _flush(self) -> None:
        with self._lock:
            payload = self._payload
            self._payload = None
            self._scheduled = False
        if payload is None:
            return
        try:
            self._render(payload)
            if self._draw is not None and self._visible():
                self._draw()
        except Exception:
            LOGGER.exception("Redraw failed")
