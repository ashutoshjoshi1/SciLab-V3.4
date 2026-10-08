"""RunProgress: the model behind the Measurements progress card and the app-bar chip."""
from __future__ import annotations

from services.run_progress import (DONE, FAILED, FINISHED, IDLE, PENDING, RUNNING, STOPPED,
                                   RunProgress, format_duration, source_label)


def _model():
    clock = {"t": 100.0}
    model = RunProgress(clock=lambda: clock["t"])
    return model, clock


def test_idle_snapshot_is_empty():
    model, _clock = _model()
    snapshot = model.snapshot()
    assert snapshot.status == IDLE
    assert snapshot.sources == () and snapshot.total == 0 and snapshot.elapsed_s == 0.0
    assert snapshot.current is None and snapshot.position == 0


def test_lifecycle_tracks_states_numbers_and_timing():
    model, clock = _model()
    seen = []
    model.add_listener(lambda: seen.append(model.snapshot().status))

    model.start(["640", "405"])
    model.set_sequence(["405", "640"])            # the orchestrator runs 640 nm last
    snapshot = model.snapshot()
    assert snapshot.status == RUNNING
    assert [source.tag for source in snapshot.sources] == ["405", "640"]
    assert all(source.state == PENDING for source in snapshot.sources)

    model.source_started("405")
    clock["t"] += 5.0
    model.frame("405", 2.7, 1200.0)
    model.frame("405", 3.0, 1500.0)
    snapshot = model.snapshot()
    assert snapshot.current is not None and snapshot.current.tag == "405"
    assert snapshot.current.frames == 2
    assert snapshot.current.it_ms == 3.0 and snapshot.current.peak == 1500.0
    assert snapshot.elapsed_s == 5.0 and snapshot.position == 1
    assert snapshot.current.duration_s(snapshot.now) == 5.0

    clock["t"] += 2.0
    model.source_completed("405")
    model.source_started("640")
    model.source_failed("640", "boom")
    model.finish()
    snapshot = model.snapshot()
    assert snapshot.status == FINISHED
    assert snapshot.done == 1 and snapshot.failed == 1 and snapshot.total == 2
    assert snapshot.sources[0].state == DONE and snapshot.sources[0].duration_s(snapshot.now) == 7.0
    assert snapshot.sources[1].state == FAILED and snapshot.sources[1].message == "boom"
    assert snapshot.elapsed_s == 7.0

    # Frames do not notify (they are polled); every transition does.
    assert seen == [RUNNING, RUNNING, RUNNING, RUNNING, RUNNING, RUNNING, FINISHED]

    # Elapsed time stops at finish.
    clock["t"] += 50.0
    assert model.snapshot().elapsed_s == 7.0


def test_stop_marks_the_running_and_pending_sources():
    model, clock = _model()
    model.start(["377", "405", "445"])
    model.source_started("377")
    model.source_completed("377")
    model.source_started("405")
    clock["t"] += 1.0
    model.finish(stopped_early=True)
    snapshot = model.snapshot()
    assert snapshot.status == STOPPED
    assert [source.state for source in snapshot.sources] == [DONE, STOPPED, STOPPED]
    assert snapshot.sources[1].started_at is not None and snapshot.sources[2].started_at is None
    assert snapshot.done == 1 and snapshot.position == 1


def test_finish_with_untouched_sources_counts_as_stopped():
    """An exception before the loop leaves rows pending: that is a cut-short run, not a finished one."""
    model, _clock = _model()
    model.start(["377", "405"])
    model.finish()
    assert model.snapshot().status == STOPPED


def test_unknown_tag_gets_a_row_and_listener_errors_are_contained():
    model, _clock = _model()
    model.add_listener(lambda: 1 / 0)
    model.start(["405"])
    model.source_started("517")
    snapshot = model.snapshot()
    assert [source.tag for source in snapshot.sources] == ["405", "517"]
    assert snapshot.current.tag == "517"


def test_restart_resets_the_rows():
    model, _clock = _model()
    model.start(["405"])
    model.source_started("405")
    model.source_failed("405", "boom")
    model.finish()
    model.start(["405", "532"])
    snapshot = model.snapshot()
    assert snapshot.status == RUNNING
    assert [source.state for source in snapshot.sources] == [PENDING, PENDING]
    assert snapshot.sources[0].message == ""


def test_labels_and_durations():
    assert source_label("532") == "532 nm"
    assert source_label("Hg_Ar") == "Hg-Ar"
    assert format_duration(0) == "0:00"
    assert format_duration(65.9) == "1:05"
    assert format_duration(3661) == "1:01:01"
    assert format_duration(-3) == "0:00"
