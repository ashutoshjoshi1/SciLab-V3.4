"""Tk-free tests for UiPump, ThrottledRedraw and AcquisitionGuard."""
from __future__ import annotations

import threading

import pytest

from services.acquisition_guard import AcquisitionGuard
from tabs.ui_pump import ThrottledRedraw, UiPump


class FakeRoot:
    """Minimal stand-in for a Tk root: records after() callbacks and runs them on demand."""

    def __init__(self) -> None:
        self.pending = {}
        self._next = 0

    def after(self, _ms, fn):
        self._next += 1
        token = f"after#{self._next}"
        self.pending[token] = fn
        return token

    def after_cancel(self, token):
        self.pending.pop(token, None)

    def run_pending(self):
        callbacks = list(self.pending.items())
        self.pending.clear()
        for _token, fn in callbacks:
            fn()


@pytest.fixture
def pump():
    root = FakeRoot()
    pump = UiPump(root, interval_ms=1, budget_s=10.0)
    pump.start()
    yield pump, root
    pump.stop()


def test_posted_callables_run_on_drain_in_order(pump):
    pump, root = pump
    seen = []
    for i in range(5):
        pump.post(seen.append, i)
    assert seen == []
    root.run_pending()
    assert seen == [0, 1, 2, 3, 4]
    assert len(root.pending) == 1  # re-armed


def test_post_from_worker_thread(pump):
    pump, root = pump
    seen = []
    worker = threading.Thread(target=lambda: pump.post(seen.append, "from-worker"))
    worker.start()
    worker.join()
    root.run_pending()
    assert seen == ["from-worker"]


def test_call_sync_returns_result_and_propagates_errors(pump):
    pump, root = pump
    results = {}

    def worker():
        results["value"] = pump.call_sync(lambda a, b: a + b, 2, 3, timeout=2.0)
        try:
            pump.call_sync(lambda: 1 / 0, timeout=2.0)
        except ZeroDivisionError:
            results["raised"] = True

    thread = threading.Thread(target=worker)
    thread.start()
    # Drain on the "main" thread until the worker is done.
    for _ in range(200):
        root.run_pending()
        if thread.join(timeout=0.01) is None and not thread.is_alive():
            break
    assert results == {"value": 5, "raised": True}


def test_call_sync_on_main_thread_runs_inline(pump):
    pump, _root = pump
    assert pump.call_sync(lambda: "inline") == "inline"


def test_stop_drops_later_posts(pump):
    pump, root = pump
    pump.stop()
    seen = []
    pump.post(seen.append, 1)
    root.run_pending()
    assert seen == []


def test_throttled_redraw_keeps_only_newest_payload(pump):
    pump, root = pump
    rendered, drawn = [], []
    redraw = ThrottledRedraw(pump, rendered.append, draw=lambda: drawn.append(True), visible=lambda: True)
    for i in range(10):
        redraw.submit(i)
    assert redraw.pending
    root.run_pending()
    assert rendered == [9] and drawn == [True]
    assert not redraw.pending


def test_throttled_redraw_skips_draw_when_hidden(pump):
    pump, root = pump
    rendered, drawn = [], []
    redraw = ThrottledRedraw(pump, rendered.append, draw=lambda: drawn.append(True), visible=lambda: False)
    redraw.submit("frame")
    root.run_pending()
    assert rendered == ["frame"] and drawn == []


def test_throttled_redraw_reschedules_after_flush(pump):
    pump, root = pump
    rendered = []
    redraw = ThrottledRedraw(pump, rendered.append)
    redraw.submit(1)
    root.run_pending()
    redraw.submit(2)
    root.run_pending()
    assert rendered == [1, 2]


def test_acquisition_guard_single_owner():
    guard = AcquisitionGuard()
    assert guard.busy_reason() is None
    assert guard.try_acquire("Live View")
    assert not guard.try_acquire("Measurement")
    assert guard.busy_reason() == "Live View is running. Stop it first."
    guard.release("Measurement")  # wrong owner: no effect
    assert guard.owner == "Live View"
    guard.release("Live View")
    assert guard.owner is None


def test_acquisition_guard_hold_context():
    guard = AcquisitionGuard()
    with guard.hold("EEPROM") as ok:
        assert ok and guard.owner == "EEPROM"
        with guard.hold("Apply IT") as nested:
            assert not nested
    assert guard.owner is None
