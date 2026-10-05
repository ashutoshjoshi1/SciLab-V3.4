"""Live smoke test for the JETI (Jeti1) backend against a connected PE60_2.

Run from the repository root with a 64-bit Python that has numpy (and pyserial
for the COM-port listing):

    python tools\\jeti_smoke_test.py            # first JETI device found
    python tools\\jeti_smoke_test.py 2360093    # a specific serial

Exercises the same path the GUI uses (spectrometer_loader discovery + connect),
then integration-time changes, a multi-cycle measurement, per-frame versus
on-board averaging, the acquisition rate, a cancelled long exposure,
disconnect and reconnect, and Auto discovery. Close JETI VersaSpec / LiVal
first: only one program can hold the COM port.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from spectrometer_loader import connect_spectrometer, discover_spectrometers, suggest_default_dll_path  # noqa: E402


def _list_com_ports() -> None:
    try:
        from serial.tools import list_ports
    except ImportError:
        print("  (pyserial not installed: no COM-port listing)")
        return
    for port in list_ports.comports():
        vid = f"{port.vid:04X}" if port.vid is not None else "----"
        pid = f"{port.pid:04X}" if port.pid is not None else "----"
        mark = "  <- JETI" if port.vid == 0x0C6C else ""
        print(f"  {port.device:6s} {vid}:{pid}  {port.description}{mark}")


def _summary(label: str, spec) -> None:
    rcm, rcs = np.asarray(spec.rcm), np.asarray(spec.rcs)
    print(f"  {label}: mean {rcm.mean():.1f}  min {rcm.min():.0f}  max {rcm.max():.0f}  "
          f"median std {np.median(rcs):.2f}  ncy_read {spec.ncy_read}  saturated {spec.ncy_saturated}")


def _rate(spec, it_ms: float, n: int) -> float:
    assert spec.set_it(it_ms) == "OK"
    t0 = time.perf_counter()
    res = spec.measure(ncy=n)
    dt = time.perf_counter() - t0
    assert res == "OK", res
    return dt * 1000.0 / n


def main(argv: list[str]) -> int:
    serial = argv[1] if len(argv) > 1 else None
    print("COM ports:")
    _list_com_ports()
    print("default DLL :", suggest_default_dll_path("Jeti1") or "(not found)")

    t0 = time.perf_counter()
    devices = discover_spectrometers("Jeti1")
    print(f"discovered  : {devices}  ({time.perf_counter() - t0:.1f} s)")
    if not devices:
        print("no JETI spectrometer found")
        return 1

    spec_type, spec = connect_spectrometer("Jeti1", serial=serial, debug_mode=0)
    try:
        assert spec_type == "Jeti1"
        for key, value in spec.get_device_info().items():
            print(f"  {key:18s}: {value}")
        assert spec.npix_active == len(spec.rcm)

        # Integration-time steps: the counts must follow the exposure at once
        # (JETI sends the time with every start, so no frame is stale).
        print("integration-time steps (5 cycles each):")
        for it in (spec.min_it_ms, 10.0, 100.0, 10.0):
            assert spec.set_it(it) == "OK"
            res = spec.measure(ncy=5)
            assert res == "OK", res
            assert spec.wait_for_measurement() == "OK"
            _summary(f"{spec.it_ms:8.3f} ms", spec)

        # Per-frame (default) versus on-board averaging of the same 20 cycles.
        assert spec.set_it(10.0) == "OK"
        t0 = time.perf_counter()
        assert spec.measure(ncy=20) == "OK"
        per_frame_s = time.perf_counter() - t0
        per_frame_mean = np.asarray(spec.rcm).copy()
        spec.device_averaging = True
        t0 = time.perf_counter()
        assert spec.measure(ncy=20) == "OK"
        on_board_s = time.perf_counter() - t0
        spec.device_averaging = False
        on_board_mean = np.asarray(spec.rcm)
        print(f"  20 x 10 ms: per-frame {per_frame_s * 1000:.0f} ms, on-board averaging {on_board_s * 1000:.0f} ms; "
              f"mean difference {np.mean(on_board_mean - per_frame_mean):+.2f} counts, "
              f"on-board values integer: {bool(np.all(on_board_mean == np.round(on_board_mean)))}")

        # Acquisition rate at the shortest exposure and at 20 ms.
        ms_min = _rate(spec, spec.min_it_ms, 50)
        print(f"  {spec.min_it_ms:.3f} ms x50: {ms_min:.2f} ms/frame -> {1000.0 / ms_min:.0f} spectra/s")
        ms_20 = _rate(spec, 20.0, 10)
        print(f"  20 ms x10: {ms_20:.1f} ms/frame (overhead {ms_20 - 20.0:.1f} ms)")

        # A long exposure must be cancellable, and the device usable afterwards.
        assert spec.set_it(5000.0) == "OK"
        timer = threading.Timer(0.3, spec.request_stop)
        timer.start()
        t0 = time.perf_counter()
        res = spec.measure(ncy=1)
        stopped_after = time.perf_counter() - t0
        timer.join()
        print(f"  5 s exposure stopped after {stopped_after:.2f} s: {res!r} / wait: {spec.wait_for_measurement()!r}")
        assert stopped_after < 2.0, "stop did not cancel the exposure"
        # The PE60_2 ignores the break and finishes the exposure; the next start waits for that.
        assert spec.set_it(10.0) == "OK"
        t0 = time.perf_counter()
        res = spec.measure(ncy=2)
        assert res == "OK", f"device not usable after a cancelled exposure: {res}"
        print(f"  measurement after the cancel: OK after {time.perf_counter() - t0:.2f} s "
              "(waits for the cancelled exposure to end)")

        # Disconnect / reconnect.
        assert spec.disconnect() == "OK"
        res = spec.connect()
        assert res == "OK", res
        assert spec.measure(ncy=2) == "OK"
        print(f"  reconnect OK, peak {np.max(spec.rcm):.0f} counts")
    finally:
        spec.disconnect()

    try:
        print("Auto discovery:", discover_spectrometers("Auto"))
    except Exception as exc:
        print("Auto discovery raised:", exc)
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
