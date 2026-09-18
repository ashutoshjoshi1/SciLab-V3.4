"""Run on the Windows bench PC: python tools/diagnose_c13016.py [--dll PATH] [--it-ms 10] [--seconds 6] [--skip-modes] [--skip-assembly]

Exercises MICRO_USB2_CLR.dll and prints a plain-text report to paste into a bug
report. It follows the board's observed rules: one captureStart per capture
(a second start while running throws inside the DLL), stop waited out through
getCaptureStatus, and every experiment isolated so an exception is printed and
the next one still runs. The board's capture mode is restored at the end.

Report: the assembly's public API with signatures; its internals (enums,
P/Invoke imports, string literals per method and the user-string heap, to
decode the status codes and the strCheck key); identity and settings; which
strCheck candidates the read-only getExposureCycle accepts; getImageData
before any start; one capture in the board's own mode; when that yields no
frame, each candidate capture mode (stop, wait, set, start, poll) until one
delivers frames; then, in the working mode, an exposure change while
capturing, buffer shapes, captureStop and a restart. A code-0 answer with an
all-zero buffer is reported as an "echo", not a frame. Requires pythonnet and
the board; close u-ApsSpecEvaluation first.
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

CANDIDATE_MODES = (2, 1, 3, 4, 8, 16, 32)     # mode 2 first: the only one that has delivered frames so far
BUFFER_SHAPES = ((288, 1), (288, 8), (288 * 32, 32), (289, 1))
KEY_WORDS = ("HAMAMATSU", "Hamamatsu", "hamamatsu", "MICRO_USB2", "C13016")   # tried as strCheck after the identity strings
MAX_PRINTED_STRINGS = 200
BINDING_ALL_DECLARED = 2 | 4 | 8 | 16 | 32    # DeclaredOnly | Instance | Static | Public | NonPublic
METHOD_PINVOKE_IMPL = 0x2000                  # MethodAttributes.PinvokeImpl


def call(api, name, *args):
    """Call and print `name(args) -> code slots=[...]` without stopping on errors."""
    shown_args = tuple(f"Buf({a.length})" if isinstance(a, h6.Buf) else a for a in args)
    try:
        result = api.call(name, *args)
        shown = [(list(s) if len(s) <= 8 else f"<array len {len(s)}>") if h6._is_array_like(s) else s for s in result.slots]
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
    """Poll getImageData; print every change of (code, has-data, timestamp) and tag frames and echoes."""
    t0 = time.monotonic()
    events = []
    last_key = None
    last_sum = None
    new_frame_times = []
    echoes = 0
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
        is_frame = bool(total) and total != last_sum
        is_echo = code == 0 and not total                  # the DLL serving its empty buffer as if it were new
        if is_frame:
            new_frame_times.append(time.monotonic() - t0)
        if is_echo and key != last_key:
            echoes += 1
        if key != last_key or is_frame:
            tag = "FRAME" if is_frame else ("echo" if is_echo else "")
            events.append((round(time.monotonic() - t0, 3), code, size, total, max(data) if data else None, stamps, tag))
            last_key = key
        last_sum = total
        time.sleep(0.005)
    print(f"  [{label}] {polls} polls in {seconds} s; transitions (t, code, len, sum, max, time[:2], tag):")
    for event in events[:30]:
        print("    ", event)
    if len(events) > 30:
        print(f"     ... {len(events) - 30} more")
    if len(new_frame_times) > 1:
        gaps = [b - a for a, b in zip(new_frame_times, new_frame_times[1:])]
        print(f"  [{label}] {len(new_frame_times)} distinct frames; period min/mean/max = "
              f"{min(gaps):.3f}/{sum(gaps) / len(gaps):.3f}/{max(gaps):.3f} s")
    else:
        print(f"  [{label}] distinct frames: {len(new_frame_times)}; echoes of an empty buffer (code 0, all zeros): {echoes}")
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


# ---------------------------------------------------------------------------
# The assembly itself
# ---------------------------------------------------------------------------


def show_api(api):
    print(f"\n== public API ({len(api.public_methods)} methods) ==")
    for name in api.public_methods:
        for overload in api.overloads[name]:
            print("  " + overload.signature())


def _il_string_tokens(il: bytes):
    """Tokens of ldstr (opcode 0x72) operands found in a method body (heuristic byte scan)."""
    index = 0
    while True:
        index = il.find(b"\x72", index)
        if index < 0 or index + 4 >= len(il):
            return
        token = int.from_bytes(il[index + 1:index + 5], "little")
        if token >> 24 == 0x70:
            yield token
        index += 1


def walk_user_strings(module):
    """Every entry of the assembly's #US heap (ECMA-335: length-prefixed UTF-16 plus one flag byte)."""
    offset, strings = 1, []
    while offset < 0x00FFFFFF and len(strings) < 20000:
        try:
            text = str(module.ResolveString(0x70000000 | offset))
        except Exception:
            break
        strings.append(text)
        size = len(text.encode("utf-16-le", errors="surrogatepass")) + 1
        offset += (1 if size < 0x80 else 2 if size < 0x4000 else 4) + size
    return strings


@experiment("assembly internals (enums, P/Invoke imports, string literals per method, user-string heap)")
def dump_assembly(dll_path):
    import clr  # noqa: F401  (the CLR is already up: load_clr_assembly ran)
    import System
    from System.Reflection import Assembly, BindingFlags

    assembly = Assembly.LoadFrom(str(dll_path))
    print("  assembly:", assembly.FullName)
    types = list(assembly.GetTypes())
    print("  types:", ", ".join(
        f"{t.FullName}{' (enum)' if t.IsEnum else ''}{'' if (t.IsPublic or t.IsNestedPublic) else ' (non-public)'}" for t in types))
    for t in types:
        if t.IsEnum:
            names = [str(n) for n in System.Enum.GetNames(t)]
            values = [int(System.Convert.ToInt64(v)) for v in System.Enum.GetValues(t)]
            print(f"  enum {t.FullName}: " + ", ".join(f"{n}={v}" for n, v in zip(names, values)))
    flags = System.Enum.ToObject(clr.GetClrType(BindingFlags), BINDING_ALL_DECLARED)
    module = assembly.GetModules()[0]
    for t in types:
        methods = list(t.GetMethods(flags)) + list(t.GetConstructors(flags))
        for method in methods:
            if System.Convert.ToInt32(method.Attributes) & METHOD_PINVOKE_IMPL:
                target = "?"
                try:
                    attribute = System.Attribute.GetCustomAttribute(
                        method, clr.GetClrType(System.Runtime.InteropServices.DllImportAttribute))
                    target = f"{attribute.Value}!{attribute.EntryPoint or method.Name}"
                except Exception:
                    pass
                print(f"  P/Invoke {t.Name}.{method.Name} -> {target}")
            try:
                body = method.GetMethodBody()
                il = bytes(body.GetILAsByteArray()) if body is not None else b""
            except Exception:
                il = b""
            literals = []
            for token in _il_string_tokens(il):
                try:
                    literals.append(str(module.ResolveString(token)))
                except Exception:
                    continue
            if literals:
                print(f"  {t.Name}.{method.Name}: {literals}")
    strings = walk_user_strings(module)
    print(f"  user-string heap: {len(strings)} entries")
    for text in strings[:MAX_PRINTED_STRINGS]:
        print("    ", repr(text))
    if len(strings) > MAX_PRINTED_STRINGS:
        print(f"     ... {len(strings) - MAX_PRINTED_STRINGS} more")


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------


@experiment("settings")
def show_settings(api, module_id):
    identity = []
    info = call(api, h6.M_INFO, module_id, h6.OUT, h6.OUT, h6.OUT, h6.OUT, h6.OUT, h6.OUT)
    if info:
        identity = [h6._text(s) for s in info.slots[1:7]]
        print("  identity:", identity)
    call(api, h6.M_PROPERTY, module_id, h6.OUT, h6.OUT, h6.OUT)
    call(api, h6.M_GET_CAPTURE_MODE, module_id, h6.OUT)
    call(api, h6.M_EXPOSURE_PROPERTY, module_id, h6.OUT, h6.OUT, h6.OUT, h6.OUT, h6.OUT, h6.OUT)
    call(api, h6.M_GET_EXPOSURE, module_id, h6.OUT)
    call(api, h6.M_GET_CYCLE, module_id, h6.OUT, "")
    call(api, "MICRO_USB2_getImageSize", module_id, h6.OUT, h6.OUT, "")
    call(api, "MICRO_USB2_getTriggerOutput", module_id, h6.OUT)
    call(api, "MICRO_USB2_getTriggerPolarity", module_id, h6.OUT)
    spectro = call(api, h6.M_SPECTRO_INFO, module_id, h6.OUT, h6.OUT)
    if spectro:
        identity += [h6._text(s) for s in spectro.slots[1:3]]
    # wavelength polynomial: six coefficients at index 0, or one per index
    call(api, h6.M_COEFFICIENT, module_id, 0, h6.Buf(h6.COEFFICIENT_COUNT))
    for index in range(1, h6.COEFFICIENT_COUNT):
        call(api, h6.M_COEFFICIENT, module_id, index, h6.Buf(1))
    print("  captureStatus before any start:", status(api, module_id))
    return identity


@experiment("strCheck candidates on the read-only getters (getExposureCycle, then getImageSize)")
def exp_check_key(api, module_id, identity):
    candidates = []
    for text in [*identity, *KEY_WORDS]:
        if text and text not in candidates:
            candidates.append(text)
    accepted = []
    for key in candidates:
        result = call(api, h6.M_GET_CYCLE, module_id, h6.OUT, key)
        if result is not None and result.result == 0:
            accepted.append(key)
    print("  accepted by getExposureCycle:", accepted or "none (every candidate refused)")
    for key in accepted[:1]:
        call(api, "MICRO_USB2_getImageSize", module_id, h6.OUT, h6.OUT, key)
    return accepted


@experiment("1: getImageData before any captureStart (stale buffer?)")
def exp_no_start(api, module_id):
    watch_image_data(api, module_id, 1.0, "no start")


@experiment("2: one capture in the board's own capture mode, polled")
def exp_capture(api, module_id, seconds):
    call(api, h6.M_CAPTURE_START, module_id)
    print("  captureStatus right after start:", status(api, module_id))
    _events, frames = watch_image_data(api, module_id, seconds, "capturing")
    return frames


@experiment("3: candidate capture modes (stop, wait, set, start, poll) until one delivers frames")
def exp_modes(api, module_id, seconds, skip_mode):
    for mode in CANDIDATE_MODES:
        if mode == skip_mode:
            continue
        print(f"  -- mode {mode}")
        stop_and_settle(api, module_id, 1.0)
        call(api, h6.M_SET_CAPTURE_MODE, module_id, mode)
        call(api, h6.M_GET_CAPTURE_MODE, module_id, h6.OUT)
        call(api, h6.M_CAPTURE_START, module_id)
        _events, frames = watch_image_data(api, module_id, seconds, f"mode {mode}")
        print(f"  RESULT: mode {mode}: {len(frames)} distinct frame(s)")
        if frames:
            return mode        # the capture is left running in this mode for the following experiments
    return None


@experiment("4: change exposure while capturing")
def exp_exposure_change(api, module_id, it_ms, seconds):
    call(api, h6.M_SET_EXPOSURE, module_id, it_ms * 5 * 1000.0)
    call(api, h6.M_GET_EXPOSURE, module_id, h6.OUT)
    watch_image_data(api, module_id, seconds, f"after set {it_ms * 5:g} ms")
    call(api, h6.M_SET_EXPOSURE, module_id, it_ms * 1000.0)
    call(api, h6.M_GET_EXPOSURE, module_id, h6.OUT)


@experiment("5: buffer shapes while capturing")
def exp_buffers(api, module_id):
    for npix, stamps in BUFFER_SHAPES:
        watch_image_data(api, module_id, 1.5, f"imageData[{npix}] time[{stamps}]", npix=npix, stamps_len=stamps)


@experiment("6: captureStop and how the status settles")
def exp_stop(api, module_id):
    stop_and_settle(api, module_id)
    watch_image_data(api, module_id, 1.5, "after stop")


@experiment("7: captureStart again after the stop")
def exp_restart(api, module_id, seconds):
    call(api, h6.M_CAPTURE_START, module_id)
    print("  captureStatus right after restart:", status(api, module_id))
    watch_image_data(api, module_id, seconds, "restarted")
    stop_and_settle(api, module_id, 2.0)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dll", type=Path, default=ROOT / "spectrometers dll files" / h6.DLL_NAME)
    parser.add_argument("--it-ms", type=float, default=10.0)
    parser.add_argument("--seconds", type=float, default=6.0, help="poll duration of each capture experiment")
    parser.add_argument("--skip-modes", action="store_true", help="do not sweep the candidate capture modes when the board's own mode yields nothing")
    parser.add_argument("--skip-assembly", action="store_true", help="do not dump the assembly's enums, imports and string literals")
    parser.add_argument("--modes", action="store_true", help=argparse.SUPPRESS)   # former flag; the sweep is on by default now
    args = parser.parse_args()

    print(f"MICRO_USB2_CLR diagnostic {time.strftime('%Y-%m-%d %H:%M:%S')}  python {sys.version.split()[0]}  dll {args.dll}")
    api = h6.MicroUsb2Api(h6.load_clr_assembly(str(args.dll)))
    show_api(api)
    if not args.skip_assembly:
        dump_assembly(args.dll)

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

        identity = show_settings(api, module_id) or []
        exp_check_key(api, module_id, identity)
        print(f"\n== exposure {args.it_ms} ms as microseconds ==")
        call(api, h6.M_SET_EXPOSURE, module_id, args.it_ms * 1000.0)
        call(api, h6.M_GET_EXPOSURE, module_id, h6.OUT)

        exp_no_start(api, module_id)
        frames = exp_capture(api, module_id, args.seconds) or []
        working_mode = original_mode if frames else None
        if not frames and not args.skip_modes:
            working_mode = exp_modes(api, module_id, args.seconds, original_mode)
        if working_mode is None:
            print(f"\nCONCLUSION: no frame in the board's own mode {original_mode}"
                  f"{'' if args.skip_modes else ' nor in any candidate mode'}; experiments 4-7 skipped. Send this report.")
            return
        print(f"\nWORKING MODE: {working_mode} (the board's own mode is {original_mode}); experiments 4-7 run in it")
        exp_exposure_change(api, module_id, args.it_ms, args.seconds)
        exp_buffers(api, module_id)
        exp_stop(api, module_id)
        exp_restart(api, module_id, args.seconds)
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
