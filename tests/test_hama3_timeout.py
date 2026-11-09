"""Hama3 (C13015-01, DcIcUSB.dll): the dll's data timeout must cover a whole pack of cycles.

The dll times out a capture as a whole, so a 50-cycle signal capture at 100 ms
(5 s) failed with "Timeout error" against the 4 s set at connect. These tests
drive ``measure_pack`` against a fake dll; no hardware or Windows needed.
"""
from __future__ import annotations

from spectrometers.hama3_spectrometer import Hama3_Spectrometer


class FakeDcIc:
    """The DcIc calls measure_pack makes, recording the data timeouts it is given."""

    def __init__(self, timeout_ok=True):
        self.timeout_ok = timeout_ok
        self.timeouts = []
        self.captures = 0

    def DcIc_Abort(self, spec_id):
        return 1

    def DcIc_SetDataTimeout(self, spec_id, timeout):
        self.timeouts.append(timeout.value)
        return 1 if self.timeout_ok else 0

    def DcIc_Capture(self, spec_id, buffer, length):
        self.captures += 1
        return 1

    def DcIc_Wait(self, spec_id):
        return 2  # capture complete

    def DcIc_GetLastError(self):
        return 3  # DcIc_ERROR_PARAMETER


def make_spec(it_ms=100.0, **fake_kwargs):
    spec = Hama3_Spectrometer()
    spec.initialize_spec_logger()
    spec.dll_handler = FakeDcIc(**fake_kwargs)
    spec.spec_id = 1
    spec.it_ms = it_ms
    spec.data_timeout_ms = spec.cycle_timeout_ms  # as connect() leaves it
    spec.docatch = True
    return spec


def test_data_timeout_covers_the_whole_pack():
    spec = make_spec(it_ms=100.0)
    res, data, _ = spec.measure_pack(50)
    assert res == "OK" and data is not None
    # 50 x 100 ms = 5 s of exposure: well past the 4 s set at connect.
    assert spec.dll_handler.timeouts == [4000 + 50 * 110]
    assert spec.data_timeout_ms == 9500


def test_unchanged_timeout_is_not_resent():
    spec = make_spec(it_ms=100.0)
    spec.measure_pack(50)
    spec.measure_pack(50)
    assert spec.dll_handler.timeouts == [9500] and spec.dll_handler.captures == 2
    spec.it_ms = 2.4
    spec.measure_pack(1)                                 # a Live View frame
    assert spec.dll_handler.timeouts == [9500, 4012]


def test_longest_single_frame_has_room():
    spec = make_spec(it_ms=4000.0)
    spec.measure_pack(1)
    assert spec.dll_handler.timeouts == [8010]           # a 4 s exposure no longer meets a 4 s timeout


def test_failed_timeout_stops_the_capture():
    spec = make_spec(it_ms=100.0, timeout_ok=False)
    res, data, _ = spec.measure_pack(50)
    assert data is None and "data timeout" in res and "illegal" in res
    assert spec.dll_handler.captures == 0
