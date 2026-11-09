"""LaserController's OBIS path against a scripted serial port: watts on the wire, OK/ERR handling."""
from __future__ import annotations

import pytest

from hardware.controllers import LaserController, ObisError, parse_obis_reply


class ScriptedPort:
    """Stands in for SerialDevice: records commands, answers from a script."""

    def __init__(self, replies=None):
        self.port = "COM10"
        self.sent = []
        self.replies = dict(replies or {})
        self.opened = 0

    def configure(self, port=None, **_kwargs):
        if port is not None:
            self.port = port

    def open(self):
        self.opened += 1
        return bool(self.port)

    def command(self, text, **_kwargs):
        if not self.open():
            raise RuntimeError(f"Unable to open port '{self.port}'.")
        self.sent.append(text)
        reply = self.replies.get(text, "OK\r\n")
        return reply.strip() if isinstance(reply, str) else reply


@pytest.fixture
def controller():
    ctl = LaserController({"OBIS": "COM10", "CUBE": "", "RELAY": "COM11"})
    ctl.obis = ScriptedPort({
        "SOUR5:POW:LEV:IMM:AMPL?": "0.0500\r\nOK\r\n",
        "SOUR5:POW:LEV?": "0.0498\r\nOK\r\n",
        "SOUR5:POW:LIM:LOW?": "0.0010\r\nOK",
        "SOUR5:POW:LIM:HIGH?": "0.1000\r\nOK",
        "SOUR5:AM:STAT?": "ON\r\nOK",
        "SOUR5:POW:LEV:IMM:AMPL 30.0000": "ERR-220\r\n",      # 30 W: out of range
        "SOUR3:AM:STAT OFF": "ERR-241",                         # empty slot
        "SOUR6:POW:LEV:IMM:AMPL?": "",                          # nobody home
    })
    ctl.relay = ScriptedPort()
    return ctl


def test_parse_obis_reply():
    assert parse_obis_reply("0.0500\r\nOK\r\n", "Q?", expect_value=True) == "0.0500"
    assert parse_obis_reply("OK", "SET", expect_value=False) == ""
    assert parse_obis_reply("", "SET", expect_value=False) == ""          # handshaking off: silence is fine
    with pytest.raises(ObisError, match="ERR-220 .*out of range"):
        parse_obis_reply("ERR-220", "SET", expect_value=False)
    with pytest.raises(ObisError, match="No reply"):
        parse_obis_reply("", "Q?", expect_value=True)


def test_setpoint_goes_out_in_watts_and_is_read_back(controller):
    controller.ensure_open("OBIS")
    controller.obis_set_power(5, 0.05)
    assert controller.obis.sent[-1] == "SOUR5:POW:LEV:IMM:AMPL 0.0500"
    assert controller.obis.sent[0] == "SYST:COMM:HAND ON"           # asked once, so refusals are reported
    assert controller.obis_setpoint_w(5) == 0.05
    assert controller.obis_output_w(5) == pytest.approx(0.0498)
    assert controller.obis_power_limits_w(5) == (0.001, 0.1)
    assert controller.obis_is_on(5) is True
    controller.ensure_open("OBIS")
    assert controller.obis.sent.count("SYST:COMM:HAND ON") == 1


def test_refused_setpoint_raises_with_the_laser_reason(controller):
    with pytest.raises(ObisError, match="refused 'SOUR5:POW:LEV:IMM:AMPL 30.0000': ERR-220"):
        controller.obis_set_power(5, 30.0)
    with pytest.raises(ObisError, match="No reply"):
        controller.obis_setpoint_w(6)


def test_all_off_sweeps_the_configured_channels_past_an_empty_slot(controller):
    controller.configure_sources({
        "405": {"type": "OBIS", "channel": 5},
        "488": {"type": "OBIS", "channel": 3},
        "532": {"type": "RELAY", "channel": 1},
    })
    failed = controller.all_off()
    assert failed == []                                              # ERR-241 on slot 3 is logged, not a failure
    assert "SOUR3:AM:STAT OFF" in controller.obis.sent and "SOUR5:AM:STAT OFF" in controller.obis.sent
    assert controller.relay.sent == ["R1R", "R2R", "R3R", "R4R"]

    controller.obis.open = lambda: False                             # the box went away
    assert controller.all_off() == ["OBIS"]


def test_ensure_open_reports_a_missing_port(controller):
    with pytest.raises(RuntimeError, match="CUBE port ''"):
        controller.ensure_open("CUBE")
    with pytest.raises(RuntimeError, match="Unknown source controller"):
        controller.ensure_open("LAMP")
