"""Progress of a measurement run, one row per light source.

The orchestrator's callbacks feed the model from the worker thread; the
Measurements tab and the app bar read snapshots on the Tk thread. State
transitions (a source starting, finishing or failing, the run ending) notify
listeners on the caller's thread; auto-IT frames only update numbers, which
the panels poll while a run is in flight.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, List, Optional, Sequence, Tuple

LOGGER = logging.getLogger(__name__)

# Per-source states.
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
STOPPED = "stopped"     # the run ended before this source finished

# Run states (RUNNING and STOPPED double as run states).
IDLE = "idle"
FINISHED = "finished"   # every source ran (some may have failed)


def source_label(tag: str) -> str:
    """Operator-facing name for a source tag: "532" -> "532 nm", the lamp -> "Hg-Ar"."""
    tag = str(tag)
    if tag == "Hg_Ar":
        return "Hg-Ar"
    return f"{tag} nm"


def format_duration(seconds: float) -> str:
    """65 -> "1:05", 3661 -> "1:01:01"."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


@dataclass
class SourceProgress:
    tag: str
    state: str = PENDING
    it_ms: Optional[float] = None
    peak: Optional[float] = None
    frames: int = 0             # auto-IT frames captured so far
    message: str = ""           # why it failed
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def duration_s(self, now: float) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at if self.finished_at is not None else now
        return max(0.0, end - self.started_at)


@dataclass(frozen=True)
class RunSnapshot:
    status: str
    sources: Tuple[SourceProgress, ...]
    elapsed_s: float
    now: float
    current: Optional[SourceProgress]
    done: int
    failed: int
    total: int

    @property
    def position(self) -> int:
        """1-based index of the source in flight (or how many have finished)."""
        for index, source in enumerate(self.sources):
            if source.state == RUNNING:
                return index + 1
        return self.done + self.failed


class RunProgress:
    """Thread-safe record of the current (or most recent) measurement run."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._listeners: List[Callable[[], None]] = []
        self.status = IDLE
        self.sources: List[SourceProgress] = []
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None

    # -- listeners -------------------------------------------------------------

    def add_listener(self, callback: Callable[[], None]) -> None:
        """``callback()`` runs after every state transition, on the thread that caused it."""
        self._listeners.append(callback)

    def _notify(self) -> None:
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:  # noqa: BLE001 - a broken panel must not stop the run
                LOGGER.exception("Run-progress listener failed")

    # -- transitions (any thread) ----------------------------------------------

    def start(self, tags: Sequence[str]) -> None:
        with self._lock:
            self.status = RUNNING
            self.sources = [SourceProgress(str(tag)) for tag in tags]
            self.started_at = self._clock()
            self.finished_at = None
        self._notify()

    def set_sequence(self, sequence: Sequence[str]) -> None:
        """Adopt the order the orchestrator will actually use (640 nm runs last)."""
        with self._lock:
            existing = {source.tag: source for source in self.sources}
            self.sources = [existing.get(str(tag)) or SourceProgress(str(tag)) for tag in sequence]
        self._notify()

    def _row(self, tag: str) -> SourceProgress:
        tag = str(tag)
        for source in self.sources:
            if source.tag == tag:
                return source
        source = SourceProgress(tag)
        self.sources.append(source)
        return source

    def source_started(self, tag: str) -> None:
        with self._lock:
            row = self._row(tag)
            row.state = RUNNING
            row.started_at = self._clock()
            row.finished_at = None
            row.frames = 0
            row.message = ""
        self._notify()

    def frame(self, tag: str, it_ms: float, peak: float) -> None:
        """An auto-IT frame: numbers only, no notification (the panels poll)."""
        with self._lock:
            row = self._row(tag)
            row.it_ms = float(it_ms)
            row.peak = float(peak)
            row.frames += 1

    def source_completed(self, tag: str) -> None:
        with self._lock:
            row = self._row(tag)
            row.state = DONE
            row.finished_at = self._clock()
        self._notify()

    def source_failed(self, tag: str, message: str) -> None:
        with self._lock:
            row = self._row(tag)
            row.state = FAILED
            row.message = str(message)
            row.finished_at = self._clock()
        self._notify()

    def finish(self, stopped_early: bool = False) -> None:
        """The run ended: anything still pending or running did not complete."""
        with self._lock:
            now = self._clock()
            cut_short = bool(stopped_early)
            for row in self.sources:
                if row.state in (PENDING, RUNNING):
                    row.state = STOPPED
                    row.finished_at = now if row.started_at is not None else None
                    cut_short = True
            self.finished_at = now
            self.status = STOPPED if cut_short else FINISHED
        self._notify()

    # -- reading (any thread) --------------------------------------------------

    def snapshot(self) -> RunSnapshot:
        with self._lock:
            now = self._clock()
            sources = tuple(replace(source) for source in self.sources)
            if self.started_at is None:
                elapsed = 0.0
            else:
                end = self.finished_at if self.finished_at is not None else now
                elapsed = max(0.0, end - self.started_at)
            current = next((source for source in sources if source.state == RUNNING), None)
            return RunSnapshot(
                status=self.status,
                sources=sources,
                elapsed_s=elapsed,
                now=now,
                current=current,
                done=sum(1 for source in sources if source.state == DONE),
                failed=sum(1 for source in sources if source.state == FAILED),
                total=len(sources),
            )
