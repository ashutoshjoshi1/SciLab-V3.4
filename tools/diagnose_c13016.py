"""Run on the Windows bench PC: python tools/diagnose_c13016.py [--dll PATH] [--it-ms 10] [--long-seconds 10] [--skip-modes]

Exercises MICRO_USB2_CLR.dll step by step and prints a plain-text report to paste
into a bug report. Every experiment is isolated: an exception in one is printed
and the next one runs; the board's capture mode is restored at the end.

Experiments: identity and settings (capture mode, exposure limits and cycle,
trigger polarity/output); how getImageData answers without a start, after a
start (short and long waits), after a stop, after start-wait-stop; whether the
exposure unit is microseconds or seconds; which buffer sizes getImageData
accepts; and (unless --skip-modes) which capture modes produce frames.
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

CANDIDATE_MODES = (0, 1, 2, 3, 4, 8, 16, 32, 64)
BUFFER_SHAPES = ((288, 1), (288, 8), (288 * 32, 32), (288 * 32, 1), (289, 1), (576, 2))


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


def watch_image_data(api, module_id, seconds, label, npix=288, stamps_len=h6.TIMESTAMP_SLOTS):
    """Poll getImageData and print every change of (code, has-data, data-changed, timestamp)."""
    t0 = time.monotonic()
    events = []
    last_key = None
    last_sum = None
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
        key = (code, total != 0, total != last_sum, tuple(stamps), size)
        if key != last_key:
            events.append((round(time.monotonic() - t0, 3), code, size, total, max(data) if data else None, stamps))
            last_key = key
        last_sum = total
        time.sleep(0.005)
    print(f"  [{label}] {polls} polls in {seconds} s; transitions (t, code, len, sum, max, time[:2]):")
    for event in events[:40]:
        print("    ", event)
    if len(events) > 40:
        print(f"     ... {len(events) - 40} more")
    return events


def got_data(events):
    return any(e[3] for e in events)


def capture_status(api, module_id, label):
    if api.has(h6.M_CAPTURE_STATUS):
        print(f"  [{label}] captureStatus -> {api.call(h6.M_CAPTURE_STATUS, module_id).result}")


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
    capture_status(api, module_id, "idle")


@experiment("1: getImageData without captureStart")
def exp_no_start(api, module_id, seconds):
    watch_image_data(api, module_id, seconds, "no start")


@experiment("2: captureStart, then poll (long wait to catch a slow exposure cycle)")
def exp_start_poll(api, module_id, seconds):
    call(api, h6.M_CAPTURE_START, module_id)
    capture_status(api, module_id, "after start")
    events = watch_image_data(api, module_id, seconds, "after start")
    capture_status(api, module_id, "before stop")
    print("  RESULT: frames after captureStart:", got_data(events))


@experiment("3: captureStop, then poll")
def exp_stop_poll(api, module_id, seconds):
    call(api, h6.M_CAPTURE_STOP, module_id)
    capture_status(api, module_id, "after stop")
    events = watch_image_data(api, module_id, seconds, "after stop")
    print("  RESULT: frames after captureStop:", got_data(events))


@experiment("4: start, wait one exposure, stop, read")
def exp_start_wait_stop(api, module_id, it_ms):
    call(api, h6.M_CAPTURE_START, module_id)
    time.sleep(it_ms / 1000.0 + 0.05)
    call(api, h6.M_CAPTURE_STOP, module_id)
    events = watch_image_data(api, module_id, 0.5, "start-wait-stop")
    print("  RESULT: frames via start-wait-stop:", got_data(events))


@experiment("5: is the exposure unit seconds? set 0.01 and capture")
def exp_unit(api, module_id, it_ms, seconds):
    call(api, h6.M_SET_EXPOSURE, module_id, it_ms / 1000.0)      # as seconds
    call(api, h6.M_GET_EXPOSURE, module_id, h6.OUT)
    call(api, h6.M_CAPTURE_START, module_id)
    events = watch_image_data(api, module_id, seconds, "exposure as seconds")
    call(api, h6.M_CAPTURE_STOP, module_id)
    print("  RESULT: frames with exposure given in seconds:", got_data(events))
    call(api, h6.M_SET_EXPOSURE, module_id, it_ms * 1000.0)      # back to microseconds
    call(api, h6.M_GET_EXPOSURE, module_id, h6.OUT)


@experiment("6: exposure cycle = exposure (key-protected, expected to be refused)")
def exp_cycle(api, module_id, it_ms, seconds):
    call(api, h6.M_SET_CYCLE, module_id, it_ms * 1000.0, "")
    call(api, h6.M_GET_CYCLE, module_id, h6.OUT, "")
    call(api, h6.M_CAPTURE_START, module_id)
    events = watch_image_data(api, module_id, seconds, "cycle set")
    call(api, h6.M_CAPTURE_STOP, module_id)
    print("  RESULT: frames after setting the cycle:", got_data(events))


@experiment("7: buffer sizes accepted by getImageData")
def exp_buffers(api, module_id):
    for npix, stamps in BUFFER_SHAPES:
        call(api, h6.M_CAPTURE_START, module_id)
        events = watch_image_data(api, module_id, 0.7, f"imageData[{npix}] time[{stamps}]", npix=npix, stamps_len=stamps)
        call(api, h6.M_CAPTURE_STOP, module_id)
        print(f"  RESULT: imageData[{npix}] time[{stamps}] ->", "DATA" if got_data(events) else "no data")


@experiment("8: candidate capture modes")
def exp_modes(api, module_id, seconds):
    for mode in CANDIDATE_MODES:
        print(f"  -- mode {mode}")
        call(api, h6.M_SET_CAPTURE_MODE, module_id, mode)
        call(api, h6.M_GET_CAPTURE_MODE, module_id, h6.OUT)
        call(api, h6.M_CAPTURE_START, module_id)
        events = watch_image_data(api, module_id, seconds, f"mode {mode}")
        capture_status(api, module_id, f"mode {mode} before stop")
        call(api, h6.M_CAPTURE_STOP, module_id)
        print(f"  RESULT: mode {mode}:", "DATA" if got_data(events) else "no data")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dll", type=Path, default=ROOT / "spectrometers dll files" / h6.DLL_NAME)
    parser.add_argument("--it-ms", type=float, default=10.0)
    parser.add_argument("--seconds", type=float, default=2.0, help="poll duration for the short experiments")
    parser.add_argument("--long-seconds", type=float, default=10.0, help="poll duration after captureStart (experiment 2)")
    parser.add_argument("--skip-modes", action="store_true", help="do not try the candidate capture modes")
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

        exp_no_start(api, module_id, min(1.0, args.seconds))
        exp_start_poll(api, module_id, args.long_seconds)
        exp_stop_poll(api, module_id, min(1.0, args.seconds))
        exp_start_wait_stop(api, module_id, args.it_ms)
        exp_unit(api, module_id, args.it_ms, args.seconds)
        exp_cycle(api, module_id, args.it_ms, args.seconds)
        exp_buffers(api, module_id)
        if not args.skip_modes:
            exp_modes(api, module_id, min(1.5, args.seconds))
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
