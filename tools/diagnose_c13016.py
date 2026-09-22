"""python tools/diagnose_c13016.py [--dll PATH] [--it-ms 10] [--frames 20] [--skip-assembly] [--trigger] [--free-run]

Exercises MICRO_USB2_CLR.dll against a C13016 / C13985 with Hamamatsu's own
sample sequence (SampleAPL.vb / SampleAPL.cpp from the "Minispectrometer
H-micro edition" download) and prints a plain-text report to paste into a bug
report:

* the assembly's public API and, unless --skip-assembly, its internals (enums,
  P/Invoke imports, string literals, user-string heap);
* identity, module property, exposure limits, capture mode, trigger settings,
  the wavelength polynomial and the key-protected state (exposure cycle, image
  size, wavelength range, A/D mode and offset, boot version);
* capture mode 0 selected, then an exposure sweep showing the cycle the DLL
  derives from each exposure (exposure + 2,000 us on 2026-09-18);
* --frames frames at --it-ms, each as captureStart, getCaptureStatus until 0,
  getImageData, captureStop, with the time to ready and the pixel statistics;
* what getImageData and getCaptureStatus answer once the frame is consumed,
  and whether a frame left waiting for a second survives;
* with --trigger, 3 s in capture mode 2 (no frame is expected without an
  external trigger) and the return to mode 0;
* with --free-run, the undocumented mode 1: on 2026-09-18 it delivered one
  frame and the following setCaptureMode blocked inside the DLL for good (the
  board itself recovers in a new process). The experiment runs last and the
  script exits if the call does not return within 10 s.

Requires pythonnet and the board; close u-ApsSpecEvaluation first. Every
experiment is isolated so an exception is printed and the next one still runs.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spectrometers import hama6_spectrometer as h6  # noqa: E402

KEY = h6.CHECK_KEY
MAX_PRINTED_STRINGS = 200
BINDING_ALL_DECLARED = 2 | 4 | 8 | 16 | 32    # DeclaredOnly | Instance | Static | Public | NonPublic
METHOD_PINVOKE_IMPL = 0x2000                  # MethodAttributes.PinvokeImpl
EXPOSURE_SWEEP_US = (11.0, 1000.0, 50000.0, 1000000.0)

M_GET_IMAGE_SIZE = "MICRO_USB2_getImageSize"
M_GET_AD_MODE = "MICRO_USB2_getADMode"
M_GET_AD_OFFSET = "MICRO_USB2_getAdOffset"
M_GET_AD_OFFSET_PROPERTY = "MICRO_USB2_getAdOffsetPropertyForLabVIEW"
M_GET_BOOT_VERSION = "MICRO_USB2_GetBootVersion"


def call(api, name, *args, quiet=False):
    """Call and print `name(args) -> code (name) slots=[...]` without stopping on errors."""
    shown_args = tuple(f"Buf({a.length})" if isinstance(a, h6.Buf) else a for a in args)
    if not api.has(name):
        print(f"  {name.replace('MICRO_USB2_', '')}: not in this assembly")
        return None
    t0 = time.perf_counter()
    try:
        result = api.call(name, *args)
    except Exception as exc:
        print(f"  {name.replace('MICRO_USB2_', '')}{shown_args} raised: {str(exc).splitlines()[0]}")
        return None
    if not quiet:
        shown = [(list(s) if len(s) <= 8 else f"<array len {len(s)}>") if h6._is_array_like(s) else
                 (h6._text(s) if isinstance(s, str) else s) for s in result.slots]
        print(f"  {name.replace('MICRO_USB2_', '')}{shown_args} -> {h6.code_text(result.result)}  slots={shown}"
              f"  [{(time.perf_counter() - t0) * 1e3:.1f} ms]")
    return result


def status(api, module_id):
    try:
        return api.call(h6.M_CAPTURE_STATUS, module_id).result
    except Exception as exc:
        return f"raised {str(exc).splitlines()[0]}"


def read_image(api, module_id, npix=288):
    """(code, counts, timestamps) from getImageData, or (exception text, [], [])."""
    try:
        result = api.call(h6.M_IMAGE, module_id, h6.Buf(npix), h6.Buf(h6.TIMESTAMP_SLOTS))
        counts = [int(v) for v in (result.slots[1] or [])]
        stamps = [int(v) for v in (result.slots[2] or [])]
        return result.result, counts, stamps
    except Exception as exc:
        return f"raised {str(exc).splitlines()[0]}", [], []


def describe(counts):
    if not counts:
        return "no data"
    return f"n={len(counts)} min={min(counts)} max={max(counts)} mean={sum(counts) / len(counts):.1f}"


def wait_ready(api, module_id, limit_s):
    """Poll getCaptureStatus until 0; returns (seconds or None, {status: polls})."""
    t0 = time.perf_counter()
    seen = {}
    while time.perf_counter() - t0 < limit_s:
        value = status(api, module_id)
        seen[value] = seen.get(value, 0) + 1
        if value == 0:
            return time.perf_counter() - t0, seen
        if value == h6.ERR_NOT_FRAME:
            return None, seen
        time.sleep(0.001)
    return None, seen


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


@experiment("assembly internals (enums, structs, P/Invoke imports, string literals per method, user-string heap)")
def dump_assembly(dll_path):
    import clr  # noqa: F401  (the CLR is already up: load_clr_assembly ran)
    import System
    from System.Reflection import Assembly, BindingFlags

    assembly = Assembly.LoadFrom(str(dll_path))
    print("  assembly:", assembly.FullName)
    types = list(assembly.GetTypes())
    print("  types:", ", ".join(
        f"{t.FullName}{' (enum)' if t.IsEnum else ''}{'' if (t.IsPublic or t.IsNestedPublic) else ' (non-public)'}" for t in types))
    flags = System.Enum.ToObject(clr.GetClrType(BindingFlags), BINDING_ALL_DECLARED)
    for t in types:
        if t.IsEnum:
            names = [str(n) for n in System.Enum.GetNames(t)]
            values = [int(System.Convert.ToInt64(v)) for v in System.Enum.GetValues(t)]
            print(f"  enum {t.FullName}: " + ", ".join(f"{n}={v}" for n, v in zip(names, values)))
        elif t.IsValueType and (t.IsPublic or t.IsNestedPublic):
            fields = [f"{f.Name}:{f.FieldType.Name}" for f in t.GetFields(flags)]
            if fields:
                print(f"  struct {t.Name}: " + ", ".join(fields))
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


@experiment("identity and settings")
def show_settings(api, module_id):
    call(api, h6.M_INFO, module_id, h6.OUT, h6.OUT, h6.OUT, h6.OUT, h6.OUT, h6.OUT)
    call(api, h6.M_SPECTRO_INFO, module_id, h6.OUT, h6.OUT)
    call(api, h6.M_PROPERTY, module_id, h6.OUT, h6.OUT, h6.OUT)
    call(api, h6.M_EXPOSURE_PROPERTY, module_id, h6.OUT, h6.OUT, h6.OUT, h6.OUT, h6.OUT, h6.OUT)
    mode = call(api, h6.M_GET_CAPTURE_MODE, module_id, h6.OUT)
    if mode and mode.result == 0:
        print("  capture mode:", h6.mode_text(mode.slots[1]))
    call(api, h6.M_GET_EXPOSURE, module_id, h6.OUT)
    call(api, h6.M_TRIGGER_OUTPUT, module_id, h6.OUT)
    call(api, h6.M_TRIGGER_POLARITY, module_id, h6.OUT)
    coefficients = call(api, h6.M_COEFFICIENT, module_id, 0, h6.Buf(h6.COEFFICIENT_COUNT))
    if coefficients and coefficients.result == 0:
        block = [float(v) for v in coefficients.slots[2]]
        wavelengths = h6.wavelengths_from_coefficients(block, 288)
        print(f"  wavelength polynomial {block}\n  spans {wavelengths[0]:.1f} .. {wavelengths[-1]:.1f} nm over 288 pixels")
    call(api, h6.M_COEFFICIENT, module_id, 1, h6.Buf(h6.COEFFICIENT_COUNT))
    print("  captureStatus before any start:", status(api, module_id))
    return int(mode.slots[1]) if mode and mode.result == 0 and mode.slots[1] is not None else None


@experiment("key-protected state, read with the assembly's own key")
def show_keyed_state(api, module_id):
    call(api, h6.M_GET_CYCLE, module_id, h6.OUT, KEY)
    call(api, M_GET_IMAGE_SIZE, module_id, h6.OUT, h6.OUT, KEY)
    call(api, h6.M_WAVELENGTH_PROPERTY, module_id, h6.OUT, h6.OUT, KEY)
    call(api, M_GET_AD_MODE, module_id, h6.OUT, KEY)
    call(api, M_GET_AD_OFFSET, module_id, h6.OUT, KEY)
    call(api, M_GET_AD_OFFSET_PROPERTY, module_id, h6.OUT, h6.OUT, KEY)
    boot = call(api, M_GET_BOOT_VERSION, module_id, h6.OUT, KEY)
    if boot and boot.slots[1] is not None:
        print("  boot version:", repr(h6._text(boot.slots[1])))


@experiment("1: capture mode 0 (one frame per captureStart), as the vendor samples set it")
def select_single_shot(api, module_id, current_mode):
    if current_mode == h6.CAPTURE_MODE_SINGLE_SHOT:
        print("  already in", h6.mode_text(current_mode))
        return True
    print("  board is in", h6.mode_text(current_mode))
    result = call(api, h6.M_SET_CAPTURE_MODE, module_id, h6.CAPTURE_MODE_SINGLE_SHOT)
    call(api, h6.M_GET_CAPTURE_MODE, module_id, h6.OUT)
    return bool(result and result.result == 0)


@experiment("2: exposure sweep: the cycle the DLL derives from each exposure (no setExposureCycle involved)")
def sweep_exposure(api, module_id, it_ms):
    for exposure_us in EXPOSURE_SWEEP_US + (it_ms * 1000.0,):
        set_result = call(api, h6.M_SET_EXPOSURE, module_id, exposure_us, quiet=True)
        exposure = call(api, h6.M_GET_EXPOSURE, module_id, h6.OUT, quiet=True)
        cycle = call(api, h6.M_GET_CYCLE, module_id, h6.OUT, KEY, quiet=True)
        print(f"  setExposureTime({exposure_us:>10g} us) -> {h6.code_text(set_result.result) if set_result else 'raised'};"
              f" exposure now {exposure.slots[1] if exposure else '?'} us, cycle {cycle.slots[1] if cycle else '?'} us")


@experiment("3: frames with the sample loop: captureStart, getCaptureStatus until 0, getImageData, captureStop")
def take_frames(api, module_id, frames, it_ms):
    limit_s = max(2.0, 3.0 * it_ms / 1000.0 + 1.0)
    waits, rounds, stamps, means, status_seen = [], [], [], [], {}
    last_counts = None
    identical = 0
    for index in range(frames):
        t0 = time.perf_counter()
        start = call(api, h6.M_CAPTURE_START, module_id, quiet=True)
        if start is None or start.result != 0:
            print(f"  frame {index + 1}: captureStart -> {h6.code_text(start.result) if start else 'raised'}; stopping the test")
            call(api, h6.M_CAPTURE_STOP, module_id)
            break
        ready_after, seen = wait_ready(api, module_id, limit_s)
        for k, v in seen.items():
            status_seen[k] = status_seen.get(k, 0) + v
        if ready_after is None:
            print(f"  frame {index + 1}: no frame within {limit_s:.1f} s; getCaptureStatus answers {seen}")
            code, counts, times = read_image(api, module_id)
            print(f"  getImageData anyway -> {h6.code_text(code)} {describe(counts)} time {times}")
            call(api, h6.M_CAPTURE_STOP, module_id)
            break
        code, counts, times = read_image(api, module_id)
        call(api, h6.M_CAPTURE_STOP, module_id, quiet=True)
        rounds.append(time.perf_counter() - t0)
        if code != 0 or not counts or not any(counts):
            print(f"  frame {index + 1}: ready after {ready_after * 1e3:.1f} ms but getImageData -> {h6.code_text(code)} {describe(counts)}")
            continue
        waits.append(ready_after)
        stamps.append(times[0] if times else None)
        means.append(sum(counts) / len(counts))
        if last_counts is not None and counts == last_counts:
            identical += 1
        last_counts = counts
        if index < 3 or index == frames - 1:
            print(f"  frame {index + 1}: ready after {ready_after * 1e3:.1f} ms, {describe(counts)}, time {times}")
    if waits:
        print(f"  {len(waits)} frames at {it_ms:g} ms: round trip min/mean/max {min(rounds) * 1e3:.1f}/"
              f"{sum(rounds) / len(rounds) * 1e3:.1f}/{max(rounds) * 1e3:.1f} ms; time to ready min/mean/max "
              f"{min(waits) * 1e3:.1f}/{sum(waits) / len(waits) * 1e3:.1f}/{max(waits) * 1e3:.1f} ms")
        print(f"  getCaptureStatus answers while waiting: {status_seen}; timestamps {stamps[:8]}{' ...' if len(stamps) > 8 else ''}")
        print(f"  mean counts {min(means):.0f} .. {max(means):.0f}; identical consecutive frames: {identical}")
    return bool(waits)


@experiment("4: after a frame is consumed: getImageData again, getCaptureStatus, and a frame left waiting for 1 s")
def after_consumption(api, module_id):
    call(api, h6.M_CAPTURE_START, module_id, quiet=True)
    ready_after, seen = wait_ready(api, module_id, 3.0)
    if ready_after is None:
        print("  no frame within 3 s:", seen)
        call(api, h6.M_CAPTURE_STOP, module_id)
        return
    code, counts, _times = read_image(api, module_id)
    print(f"  first read -> {h6.code_text(code)} {describe(counts)}")
    code, counts, _times = read_image(api, module_id)
    print(f"  second read -> {h6.code_text(code)} {describe(counts)} (6 notImage expected: the buffer was consumed)")
    print("  captureStatus after the read:", status(api, module_id), "(1 expected)")
    call(api, h6.M_CAPTURE_STOP, module_id, quiet=True)
    call(api, h6.M_CAPTURE_START, module_id, quiet=True)
    time.sleep(1.0)
    print("  captureStatus 1 s after a start, frame unread:", status(api, module_id), "(0 expected: the frame waits)")
    code, counts, _times = read_image(api, module_id)
    print(f"  read -> {h6.code_text(code)} {describe(counts)}")
    call(api, h6.M_CAPTURE_STOP, module_id, quiet=True)


@experiment("5 (--trigger): capture mode 2 for 3 s without an external trigger, then back to mode 0")
def trigger_mode(api, module_id):
    call(api, h6.M_SET_CAPTURE_MODE, module_id, h6.CAPTURE_MODE_TRIGGER)
    call(api, h6.M_GET_CAPTURE_MODE, module_id, h6.OUT)
    call(api, h6.M_CAPTURE_START, module_id)
    ready_after, seen = wait_ready(api, module_id, 3.0)
    print(f"  ready after {ready_after} s; getCaptureStatus answers {seen}")
    code, counts, times = read_image(api, module_id)
    print(f"  getImageData -> {h6.code_text(code)} {describe(counts)} time {times} (an all-zero echo with code 0 is not a frame)")
    call(api, h6.M_CAPTURE_STOP, module_id)
    call(api, h6.M_SET_CAPTURE_MODE, module_id, h6.CAPTURE_MODE_SINGLE_SHOT)
    call(api, h6.M_GET_CAPTURE_MODE, module_id, h6.OUT)
    call(api, h6.M_CAPTURE_START, module_id, quiet=True)
    ready_after, seen = wait_ready(api, module_id, 3.0)
    print(f"  back in mode 0: frame ready after {ready_after * 1e3 if ready_after else None} ms")
    read_image(api, module_id)
    call(api, h6.M_CAPTURE_STOP, module_id, quiet=True)


@experiment("6 (--free-run): undocumented capture mode 1; the following setCaptureMode hung the DLL on 2026-09-18")
def free_run_mode(api, module_id):
    print("  WARNING: if setCaptureMode does not return, this script exits; the board recovers in a new process")
    call(api, h6.M_SET_CAPTURE_MODE, module_id, h6.CAPTURE_MODE_FREE_RUN)
    call(api, h6.M_GET_CAPTURE_MODE, module_id, h6.OUT)
    call(api, h6.M_CAPTURE_START, module_id)
    t0 = time.perf_counter()
    frames, seen = [], {}
    while time.perf_counter() - t0 < 2.0:
        value = status(api, module_id)
        seen[value] = seen.get(value, 0) + 1
        if value == 0:
            code, counts, times = read_image(api, module_id)
            if code == 0 and counts and any(counts):
                frames.append((round(time.perf_counter() - t0, 4), times[0] if times else None))
        time.sleep(0.0005)
    print(f"  2 s in mode 1: {len(frames)} frame(s) {frames[:5]}; getCaptureStatus answers {seen}")
    call(api, h6.M_CAPTURE_STOP, module_id)
    print("  captureStatus after stop:", status(api, module_id))
    outcome = {}

    def restore():
        outcome["result"] = call(api, h6.M_SET_CAPTURE_MODE, module_id, h6.CAPTURE_MODE_SINGLE_SHOT)

    worker = threading.Thread(target=restore, daemon=True)
    worker.start()
    worker.join(10.0)
    if worker.is_alive():
        print("  setCaptureMode(0) has not returned after 10 s: the DLL is blocked (as on 2026-09-18). Exiting; "
              "the board comes back in mode 0 for the next process.")
        sys.stdout.flush()
        os._exit(2)
    call(api, h6.M_GET_CAPTURE_MODE, module_id, h6.OUT)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dll", type=Path, default=None, help=f"path to {h6.DLL_NAME} (default: the bundled copy or Hamamatsu's install folder)")
    parser.add_argument("--it-ms", type=float, default=10.0)
    parser.add_argument("--frames", type=int, default=20, help="frames to take in experiment 3")
    parser.add_argument("--skip-assembly", action="store_true", help="do not dump the assembly's enums, imports and string literals")
    parser.add_argument("--trigger", action="store_true", help="also try capture mode 2 (external trigger) for 3 s")
    parser.add_argument("--free-run", action="store_true", help="also try the undocumented capture mode 1 (may block the DLL; runs last)")
    args = parser.parse_args()
    try:  # the assembly's string literals include characters a cp1252 console or redirected file cannot encode
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    dll = args.dll
    if dll is None:
        bundled = ROOT / "spectrometers dll files" / h6.DLL_NAME
        dll = bundled if bundled.is_file() else h6.VENDOR_INSTALL_DLL
    print(f"MICRO_USB2_CLR diagnostic {time.strftime('%Y-%m-%d %H:%M:%S')}  python {sys.version.split()[0]}  dll {dll}")
    api = h6.MicroUsb2Api(h6.load_clr_assembly(str(dll)))
    show_api(api)
    if not args.skip_assembly:
        dump_assembly(dll)

    module_id = None
    opened = False
    try:
        print("\n== library / module ==")
        call(api, h6.M_INIT)
        listing = call(api, h6.M_LIST, h6.Buf(h6.MAX_MODULES), 0)
        number = int(listing.slots[1] or 0) if listing else 0
        ids = [int(listing.slots[0][i]) for i in range(number)] if listing else []
        print("  module ids:", ids)
        if not ids:
            print("No module reported; is the board plugged in (Device Manager: 'C13016 USB2(HPK)') and u-ApsSpecEvaluation closed?")
            return
        module_id = ids[0]
        call(api, h6.M_OPEN, module_id)
        opened = True
        call(api, h6.M_CAPTURE_STOP, module_id)      # a capture left running by a previous session
        current_mode = show_settings(api, module_id)
        show_keyed_state(api, module_id)
        if not select_single_shot(api, module_id, current_mode):
            print("\nCONCLUSION: capture mode 0 could not be selected; send this report.")
            return
        sweep_exposure(api, module_id, args.it_ms)
        worked = take_frames(api, module_id, args.frames, args.it_ms)
        if worked:
            after_consumption(api, module_id)
        else:
            print("\nCONCLUSION: no frame in capture mode 0 with the vendor sequence; send this report.")
        if args.trigger:
            trigger_mode(api, module_id)
        if args.free_run:
            free_run_mode(api, module_id)
        if worked:
            print("\nCONCLUSION: the board delivers frames with the vendor sequence in capture mode 0; SciLab's Hama6 backend uses exactly this loop.")
    finally:
        print("\n== cleanup ==")
        if opened and module_id is not None:
            call(api, h6.M_CAPTURE_STOP, module_id)
            call(api, h6.M_CLOSE, module_id)
        call(api, h6.M_UNINIT)


if __name__ == "__main__":
    main()
