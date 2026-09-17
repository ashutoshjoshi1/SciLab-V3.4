"""Run on the Windows bench PC: python tools/diagnose_c13016.py [--dll PATH] [--it-ms 10] [--seconds 6] [--modes]

Exercises MICRO_USB2_CLR.dll and prints a plain-text report to paste into a bug
report. It follows the board's observed rules: one captureStart per capture
(a second start while running throws inside the DLL), stop waited out through
getCaptureStatus, and every experiment isolated so an exception is printed and
the next one still runs. The board's capture mode is restored at the end.

Experiments: identity and settings; getImageData before any start; one
continuous capture polled for --seconds (frame period from the timestamps and
data changes); an exposure change while capturing; buffer shapes while
capturing; captureStop and how the status settles; a second captureStart; and
with --modes each candidate capture mode (stop, wait, set, start, poll).
Requires pythonnet and the board; close u-ApsSpecEvaluation first.
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spectrometers import hama6_spectrometer as h6  # noqa: E402

CANDIDATE_MODES = (0, 1, 2, 4, 8, 16, 32)
BUFFER_SHAPES = ((288, 1), (288, 8), (288 * 32, 32), (289, 1))


def call(api, name, *args):
    """Call and print `name(args) -> code slots=[...]` without stopping on errors."""
    shown_args = tuple(f"Buf({a.length})" if isinstance(a, h6.Buf) else a for a in args)
    try:
        result = api.call(name, *args)
        shown = [f"<array len {len(s)}>" if h6._is_array_like(s) else s for s in result.slots]
        print(f"  {name.replace('MICRO_USB2_', '')}{shown_args} -> {result.result}  slots={shown}")
        return result
    except Exception as exc:
        print(f"  {name.replace('MICRO_USB2_', '')}{shown_args} raised: {str(exc).splitlines()[0]}")
        return None


def status(api, module_id):
    try:
        return api.call(h6.M_CAPTURE_STATUS, module_id).result
    except Exception as exc:
        return f"raised {str(exc).splitlines()[0]}"


def watch_image_data(api, module_id, seconds, label, npix=288, stamps_len=h6.TIMESTAMP_SLOTS):
    """Poll getImageData and print every change of (code, has-data, data-changed, timestamp)."""
    t0 = time.monotonic()
    events = []
    last_key = None
    last_sum = None
    new_frame_times = []
    polls = 0
    while time.monotonic() - t0 < seconds:
        polls += 1
        try:
            result = api.call(h6.M_IMAGE, module_id, h6.Buf(npix), h6.Buf(stamps_len))
            data = [int(v) for v in (result.slots[1] or [])]
            stamps = [int(v) for v in (result.slots[2] or [])][:2]
            code = result.result
            size = len(data)
        except Exception as exc:
            data, stamps, code, size = [], [], f"raised {str(exc).splitlines()[0]}", None
        total = sum(data)
        key = (code, total != 0, tuple(stamps))
        if total and total != last_sum:
            new_frame_times.append(time.monotonic() - t0)
        if key != last_key or (total and total != last_sum):
            events.append((round(time.monotonic() - t0, 3), code, size, total, max(data) if data else None, stamps))
            last_key = key
        last_sum = total
        time.sleep(0.005)
    print(f"  [{label}] {polls} polls in {seconds} s; transitions (t, code, len, sum, max, time[:2]):")
    for event in events[:30]:
        print("    ", event)
    if len(events) > 30:
        print(f"     ... {len(events) - 30} more")
    if len(new_frame_times) > 1:
        gaps = [b - a for a, b in zip(new_frame_times, new_frame_times[1:])]
        print(f"  [{label}] {len(new_frame_times)} distinct frames; period min/mean/max = "
              f"{min(gaps):.3f}/{sum(gaps) / len(gaps):.3f}/{max(gaps):.3f} s")
    else:
        print(f"  [{label}] distinct frames: {len(new_frame_times)}")
    return events, new_frame_times


def stop_and_settle(api, module_id, seconds=3.0):
    """captureStop, then report how getCaptureStatus evolves (it stayed 1 on the bench)."""
    call(api, h6.M_CAPTURE_STOP, module_id)
    t0 = time.monotonic()
    seen = []
    while time.monotonic() - t0 < seconds:
        value = status(api, module_id)
        if not seen or seen[-1][1] != value:
            seen.append((round(time.monotonic() - t0, 3), value))
        time.sleep(0.05)
    print("  captureStatus after stop (t, value):", seen)


def experiment(title):
    """Decorator: print a heading, run, print any exception, continue."""
    def wrap(fn):
        def run(*args, **kwargs):
            print(f"\n== {title} ==")
            try:
                return fn(*args, **kwargs)
            except Exception:
                print("  EXPERIMENT FAILED:")
                traceback.print_exc(file=sys.stdout)
                return None
        return run
    return wrap


@experiment("settings")
def show_settings(api, module_id):
    info = call(api, h6.M_INFO, module_id, h6.OUT, h6.OUT, h6.OUT, h6.OUT, h6.OUT, h6.OUT)
    if info:
        print("  identity:", [h6._text(s) for s in info.slots[1:7]])
    call(api, h6.M_PROPERTY, module_id, h6.OUT, h6.OUT, h6.OUT)
    call(api, h6.M_GET_CAPTURE_MODE, module_id, h6.OUT)
    call(api, h6.M_EXPOSURE_PROPERTY, module_id, h6.OUT, h6.OUT, h6.OUT, h6.OUT, h6.OUT, h6.OUT)
    call(api, h6.M_GET_EXPOSURE, module_id, h6.OUT)
    call(api, h6.M_GET_CYCLE, module_id, h6.OUT, "")
    call(api, "MICRO_USB2_getImageSize", module_id, h6.OUT, h6.OUT, "")
    call(api, "MICRO_USB2_getTriggerOutput", module_id, h6.OUT)
    call(api, "MICRO_USB2_getTriggerPolarity", module_id, h6.OUT)
    call(api, "MICRO_USB2_getSpectroInformationForLabVIEW", module_id, h6.OUT, h6.OUT)
    print("  captureStatus before any start:", status(api, module_id))


@experiment("1: getImageData before any captureStart (stale buffer?)")
def exp_no_start(api, module_id):
    watch_image_data(api, module_id, 1.0, "no start")


@experiment("2: one continuous capture, polled")
def exp_capture(api, module_id, seconds):
    call(api, h6.M_CAPTURE_START, module_id)
    print("  captureStatus right after start:", status(api, module_id))
    watch_image_data(api, module_id, seconds, "capturing")


@experiment("3: change exposure while capturing")
def exp_exposure_change(api, module_id, it_ms, seconds):
    call(api, h6.M_SET_EXPOSURE, module_id, it_ms * 5 * 1000.0)
    call(api, h6.M_GET_EXPOSURE, module_id, h6.OUT)
    watch_image_data(api, module_id, seconds, f"after set {it_ms * 5:g} ms")
    call(api, h6.M_SET_EXPOSURE, module_id, it_ms * 1000.0)
    call(api, h6.M_GET_EXPOSURE, module_id, h6.OUT)


@experiment("4: buffer shapes while capturing")
def exp_buffers(api, module_id):
    for npix, stamps in BUFFER_SHAPES:
        watch_image_data(api, module_id, 1.5, f"imageData[{npix}] time[{stamps}]", npix=npix, stamps_len=stamps)


@experiment("5: captureStop and how the status settles")
def exp_stop(api, module_id):
    stop_and_settle(api, module_id)
    watch_image_data(api, module_id, 1.5, "after stop")


@experiment("6: captureStart again after the stop")
def exp_restart(api, module_id, seconds):
    call(api, h6.M_CAPTURE_START, module_id)
    print("  captureStatus right after restart:", status(api, module_id))
    watch_image_data(api, module_id, seconds, "restarted")
    stop_and_settle(api, module_id, 2.0)


@experiment("7: candidate capture modes (stop, wait, set, start, poll)")
def exp_modes(api, module_id, seconds):
    for mode in CANDIDATE_MODES:
        print(f"  -- mode {mode}")
        call(api, h6.M_SET_CAPTURE_MODE, module_id, mode)
        call(api, h6.M_GET_CAPTURE_MODE, module_id, h6.OUT)
        call(api, h6.M_CAPTURE_START, module_id)
        _events, frames = watch_image_data(api, module_id, seconds, f"mode {mode}")
        stop_and_settle(api, module_id, 1.5)
        print(f"  RESULT: mode {mode}: {len(frames)} distinct frame(s)")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dll", type=Path, default=ROOT / "spectrometers dll files" / h6.DLL_NAME)
    parser.add_argument("--it-ms", type=float, default=10.0)
    parser.add_argument("--seconds", type=float, default=6.0, help="poll duration of the continuous-capture experiments")
    parser.add_argument("--modes", action="store_true", help="also try the candidate capture modes")
    args = parser.parse_args()

    print(f"MICRO_USB2_CLR diagnostic {time.strftime('%Y-%m-%d %H:%M:%S')}  python {sys.version.split()[0]}  dll {args.dll}")
    api = h6.MicroUsb2Api(h6.load_clr_assembly(str(args.dll)))
    print("public methods:", len(api.public_methods))

    module_id = None
    opened = False
    original_mode = None
    try:
        print("\n== library / module ==")
        call(api, h6.M_INIT)
        listing = call(api, h6.M_LIST, h6.Buf(h6.MAX_MODULES), h6.MAX_MODULES)
        number = int(listing.slots[1] or 0) if listing else 0
        ids = [int(listing.slots[0][i]) for i in range(number)] if listing else []
        print("  module ids:", ids)
        if not ids:
            print("No module reported; is the board plugged in and u-ApsSpecEvaluation closed?")
            return
        module_id = ids[0]
        call(api, h6.M_OPEN, module_id)
        opened = True
        mode_call = call(api, h6.M_GET_CAPTURE_MODE, module_id, h6.OUT)
        if mode_call and mode_call.slots[1] is not None:
            original_mode = int(mode_call.slots[1])

        show_settings(api, module_id)
        print(f"\n== exposure {args.it_ms} ms as microseconds ==")
        call(api, h6.M_SET_EXPOSURE, module_id, args.it_ms * 1000.0)
        call(api, h6.M_GET_EXPOSURE, module_id, h6.OUT)

        exp_no_start(api, module_id)
        exp_capture(api, module_id, args.seconds)
        exp_exposure_change(api, module_id, args.it_ms, args.seconds)
        exp_buffers(api, module_id)
        exp_stop(api, module_id)
        exp_restart(api, module_id, args.seconds)
        if args.modes:
            exp_modes(api, module_id, min(4.0, args.seconds))
    finally:
        print("\n== cleanup ==")
        if opened and module_id is not None:
            call(api, h6.M_CAPTURE_STOP, module_id)
            if original_mode is not None:
                call(api, h6.M_SET_CAPTURE_MODE, module_id, original_mode)
                print(f"  restored capture mode {original_mode}")
            call(api, h6.M_CLOSE, module_id)
        call(api, h6.M_UNINIT)


if __name__ == "__main__":
    main()
