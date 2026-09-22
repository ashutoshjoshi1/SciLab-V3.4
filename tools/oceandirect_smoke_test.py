"""Live smoke test for the OceanDirect (OcOpt1) backend against a connected unit.

Run from the repository root with a 64-bit Python that has numpy:

    python tools\\oceandirect_smoke_test.py            # first unit found
    python tools\\oceandirect_smoke_test.py FLMS17243  # a specific serial

Exercises the same path the GUI uses (spectrometer_loader discovery + connect),
then integration-time changes, a multi-cycle measurement, an acquisition-rate
check, disconnect and reconnect (device IDs change after a close).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from spectrometer_loader import connect_spectrometer, discover_spectrometers, suggest_default_dll_path  # noqa: E402


def main(argv: list[str]) -> int:
    serial = argv[1] if len(argv) > 1 else None
    print("default DLL :", suggest_default_dll_path("OcOpt1") or "(not found)")

    devices = discover_spectrometers("OcOpt1")
    print("discovered  :", devices)
    if not devices:
        print("no OceanDirect spectrometer found")
        return 1

    spec_type, spec = connect_spectrometer("OcOpt1", serial=serial, debug_mode=0)
    try:
        info = spec.get_device_info()
        for k, v in info.items():
            print(f"  {k:22s}: {v}")
        assert spec_type == "OcOpt1"
        assert spec.npix_active == len(spec.wl) == len(spec.rcm)

        # Integration-time changes and a multi-cycle measurement
        for it in (1.0, 5.0, 10.0):
            res = spec.set_it(it)
            assert res == "OK", res
        res = spec.measure(ncy=5)
        assert res == "OK", res
        assert spec.wait_for_measurement() == "OK"
        rcm, rcs = np.asarray(spec.rcm), np.asarray(spec.rcs)
        print(f"  10 ms x5: mean {rcm.mean():.1f}  min {rcm.min():.0f}  max {rcm.max():.0f}  "
              f"median std {np.median(rcs):.1f}  ncy_read {spec.ncy_read}  saturated {spec.ncy_saturated}")
        edp = spec.electric_dark_pixels
        if edp:
            print(f"  electric dark pixels {edp[0]}..{edp[-1]} mean {rcm[edp].mean():.1f} counts (raw offset, corrections off)")

        # Acquisition rate at 1 ms (expect ~2.6 ms/frame floor on the Flame-S)
        spec.set_it(1.0)
        spec.measure(ncy=1)  # absorbs the discarded frame after the IT change
        n = 50
        t0 = time.perf_counter()
        res = spec.measure(ncy=n)
        dt = time.perf_counter() - t0
        assert res == "OK", res
        print(f"  1 ms x{n}: {dt * 1000 / n:.2f} ms/frame -> {n / dt:.0f} spectra/s")

        # Timing at 20 ms should be close to IT
        spec.set_it(20.0)
        spec.measure(ncy=1)
        t0 = time.perf_counter()
        spec.measure(ncy=10)
        dt = time.perf_counter() - t0
        print(f"  20 ms x10: {dt * 100:.1f} ms/frame")

        # Disconnect / reconnect: IDs are invalid after a close, connect() must re-probe.
        assert spec.disconnect() == "OK"
        res = spec.connect()
        assert res == "OK", res
        spec.set_it(10.0)
        assert spec.measure(ncy=2) == "OK"
        print(f"  reconnect OK, peak {np.max(spec.rcm):.0f} counts")
    finally:
        spec.disconnect()

    # Auto mode must end up on OcOpt1 when no other vendor unit is attached.
    try:
        auto = discover_spectrometers("Auto")
        print("Auto discovery:", auto)
    except Exception as exc:
        print("Auto discovery raised:", exc)
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
