"""Serial-based hardware controllers: laser sources, filter wheels.

Extracted from app.py to keep the main GUI module focused on the Tk root.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Optional, Tuple

try:
    import serial
except Exception:  # pragma: no cover - runtime dependency on target machine
    serial = None


LOGGER = logging.getLogger(__name__)


def _clean_text(value: object) -> str:
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="ignore")
    else:
        text = str(value)
    text = text.strip()
    if text.startswith("b'") and text.endswith("'"):
        text = text[2:-1]
    if text.startswith('b"') and text.endswith('"'):
        text = text[2:-1]
    return text.strip()


class SerialDevice:
    def __init__(
        self,
        name: str,
        port: str = "",
        baudrate: int = 9600,
        timeout: float = 1.0,
        line_ending: str = "\r\n",
        **serial_kwargs,
    ):
        self.name = name
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.line_ending = line_ending
        self.serial_kwargs = serial_kwargs
        self._ser = None

    def configure(self, port: Optional[str] = None, baudrate: Optional[int] = None, **serial_kwargs) -> None:
        if port is not None:
            self.port = port
        if baudrate is not None:
            self.baudrate = baudrate
        if serial_kwargs:
            self.serial_kwargs.update(serial_kwargs)

    def open(self) -> bool:
        if not self.port or serial is None:
            return False
        if self._ser is not None and getattr(self._ser, "is_open", False):
            if getattr(self._ser, "port", None) == self.port:
                return True
            self.close()

        try:
            self._ser = serial.Serial(
                self.port,
                baudrate=self.baudrate,
                timeout=self.timeout,
                write_timeout=self.timeout,
                **self.serial_kwargs,
            )
            return True
        except Exception as exc:
            # A missing/locked port is an operator-facing condition, not a crash:
            # one line, no traceback.
            LOGGER.warning("Unable to open serial port %s for %s: %s", self.port, self.name, exc)
            self._ser = None
            return False

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    def reset_buffers(self) -> None:
        if self._ser is None:
            return
        for method_name in ("reset_input_buffer", "reset_output_buffer"):
            try:
                getattr(self._ser, method_name)()
            except Exception:
                pass

    def write_text(self, text: str) -> None:
        if self._ser is None:
            raise RuntimeError(f"{self.name} serial port is not open.")
        payload = (text + self.line_ending).encode("utf-8")
        self._ser.write(payload)
        self._ser.flush()

    def read_all_text(self) -> str:
        if self._ser is None:
            return ""
        try:
            data = self._ser.read_all()
        except Exception:
            return ""
        return data.decode("utf-8", errors="ignore").strip()

    def read_line_text(self) -> str:
        """Read one reply framed by ``line_ending`` (bounded by the port timeout)."""
        if self._ser is None:
            return ""
        try:
            data = self._ser.read_until(self.line_ending.encode("utf-8"))
        except Exception:
            return ""
        return data.decode("utf-8", errors="ignore").strip()

    def command(
        self,
        text: str,
        *,
        pause_s: float,
        retries: int = 1,
        reset: bool = True,
        framed: bool = False,
    ) -> str:
        """Send ``text`` and return the device reply.

        ``framed=False`` reproduces the historical behaviour: sleep ``pause_s``
        then read whatever is buffered. ``framed=True`` (bench option) waits for
        the reply terminator instead, so a fast device answers in milliseconds
        and a slow one is not cut off mid-reply.
        """
        if not self.open():
            raise RuntimeError(f"Unable to open {self.name} port '{self.port}'.")
        response = ""
        for _ in range(max(1, int(retries))):
            if reset:
                self.reset_buffers()
            self.write_text(text)
            if framed:
                response = self.read_line_text()
            else:
                time.sleep(pause_s)
                response = self.read_all_text()
            if response:
                break
        return response


class ObisError(RuntimeError):
    """The OBIS answered a command with ERR-nnn, or did not answer a query at all."""


# From Coherent's OBIS remote-control reference: the codes a laser box returns
# in place of OK. Anything else is reported by number.
OBIS_ERRORS = {
    0: "no error",
    100: "unrecognized command or query",
    102: "syntax error",
    109: "parameter missing",
    200: "execution error",
    203: "command protected",
    220: "invalid parameter (out of range?)",
    221: "settings conflict (is the laser in CW power mode?)",
    241: "device unavailable (no laser in that slot?)",
    310: "system error",
    321: "out of memory",
    350: "queue overflow",
    400: "query unavailable",
    500: "CCB fault",
    510: "I2C bus fault",
    520: "controller time-out",
    900: "CCB message timed out",
}


def parse_obis_reply(reply: str, command: str, *, expect_value: bool) -> str:
    """The value line of an OBIS reply, or '' for a plain acknowledgement.

    With handshaking on (the box's default, and SciLab asks for it) every
    command is answered with ``OK`` or ``ERR-nnn``; a query puts its value on
    the line before. An ERR raises; so does a query that got nothing back,
    which is how an unplugged or busy box shows up.
    """
    lines = [line.strip() for line in reply.replace("\r", "\n").split("\n") if line.strip()]
    for line in lines:
        if line.upper().startswith("ERR"):
            try:
                code = int(line.split("-", 1)[1])
            except (IndexError, ValueError):
                code = -1
            meaning = OBIS_ERRORS.get(code, line)
            raise ObisError(f"OBIS refused '{command}': {line} ({meaning})")
    values = [line for line in lines if line.upper() != "OK"]
    if expect_value and not values:
        raise ObisError(f"No reply from the OBIS to '{command}'. Is the laser box on and "
                        f"Coherent Connection closed?")
    return values[0] if values else ""


class LaserController:
    # The SciGlob bench's slot numbers; the application passes the configured
    # table instead (configure_sources), this is only the fallback.
    OBIS_MAP = {"405": 5, "445": 4, "488": 3, "640": 2, "685": 6}
    RELAY_MAP = {"517": 2, "532": 1, "Hg_Ar": 4}

    def __init__(self, com_ports: Optional[Dict[str, str]] = None, *, framed_reads: bool = False):
        # Bench option: framed replies instead of fixed sleeps (see SerialDevice.command).
        self.framed_reads = bool(framed_reads)
        self._obis_channels: Tuple[int, ...] = tuple(sorted(set(self.OBIS_MAP.values())))
        self._relay_numbers: Tuple[int, ...] = (1, 2, 3, 4)
        self._obis_handshake_done = False
        cube_kwargs = {}
        if serial is not None:
            cube_kwargs = {
                "parity": serial.PARITY_NONE,
                "stopbits": serial.STOPBITS_ONE,
                "bytesize": serial.EIGHTBITS,
            }

        self.obis = SerialDevice("OBIS", baudrate=9600, timeout=1.0, line_ending="\r\n")
        self.cube = SerialDevice("CUBE", baudrate=19200, timeout=1.0, line_ending="\r", **cube_kwargs)
        self.relay = SerialDevice("RELAY", baudrate=9600, timeout=1.0, line_ending="\r")
        self.configure_ports(com_ports or {})

    def configure_ports(self, com_ports: Dict[str, str]) -> None:
        self.obis.configure(port=com_ports.get("OBIS", ""))
        self.cube.configure(port=com_ports.get("CUBE", ""))
        self.relay.configure(port=com_ports.get("RELAY", ""))
        self._obis_handshake_done = False

    def configure_sources(self, sources: Dict[str, Dict[str, object]]) -> None:
        """Remember which OBIS channels and relays the bench uses, for all_off()."""
        obis = {int(cfg["channel"]) for cfg in sources.values()
                if str(cfg.get("type", "")).upper() == "OBIS" and cfg.get("channel")}
        relays = {int(cfg["channel"]) for cfg in sources.values()
                  if str(cfg.get("type", "")).upper() == "RELAY" and cfg.get("channel")}
        self._obis_channels = tuple(sorted(obis)) or tuple(sorted(set(self.OBIS_MAP.values())))
        self._relay_numbers = tuple(sorted(relays | {1, 2, 3, 4}))

    def open_all(self) -> None:
        for device in (self.obis, self.cube, self.relay):
            if device.port:
                device.open()

    def ensure_open(self, kind: str) -> None:
        """Open the port behind a controller type ('OBIS', 'CUBE', 'RELAY'); raise when it cannot be."""
        kind = str(kind).upper()
        device = {"OBIS": self.obis, "CUBE": self.cube, "RELAY": self.relay}.get(kind)
        if device is None:
            raise RuntimeError(f"Unknown source controller '{kind}'.")
        if not device.open():
            raise RuntimeError(f"Unable to open {kind} port '{device.port}'.")
        if kind == "OBIS" and not self._obis_handshake_done:
            # Ask for OK / ERR after every command so a refused setpoint is
            # reported instead of silently kept at the old value.
            self._obis_handshake_done = True
            try:
                self.obis_cmd("SYST:COMM:HAND ON")
            except Exception as exc:
                LOGGER.debug("OBIS handshake request failed: %s", exc)

    def ensure_open_for_tag(self, tag: str) -> None:
        """Legacy routing by the bench's historical tags; the application routes by its source table."""
        if tag in self.OBIS_MAP:
            self.ensure_open("OBIS")
        elif tag == "377":
            self.ensure_open("CUBE")
        elif tag in self.RELAY_MAP:
            self.ensure_open("RELAY")

    def obis_cmd(self, command: str) -> str:
        return self.obis.command(command, pause_s=0.2, framed=self.framed_reads)

    def _obis(self, command: str, *, expect_value: bool = False) -> str:
        return parse_obis_reply(self.obis_cmd(command), command, expect_value=expect_value)

    def obis_on(self, channel: int) -> None:
        self._obis(f"SOUR{int(channel)}:AM:STAT ON")

    def obis_off(self, channel: int) -> None:
        self._obis(f"SOUR{int(channel)}:AM:STAT OFF")

    def obis_set_power(self, channel: int, power_watts: float) -> None:
        """CW power setpoint in watts; takes effect at once, lit or not."""
        self._obis(f"SOUR{int(channel)}:POW:LEV:IMM:AMPL {float(power_watts):.4f}")

    def obis_setpoint_w(self, channel: int) -> float:
        return float(self._obis(f"SOUR{int(channel)}:POW:LEV:IMM:AMPL?", expect_value=True))

    def obis_output_w(self, channel: int) -> float:
        """The power the laser reports it is emitting now (0 while off)."""
        return float(self._obis(f"SOUR{int(channel)}:POW:LEV?", expect_value=True))

    def obis_power_limits_w(self, channel: int) -> Tuple[float, float]:
        low = float(self._obis(f"SOUR{int(channel)}:POW:LIM:LOW?", expect_value=True))
        high = float(self._obis(f"SOUR{int(channel)}:POW:LIM:HIGH?", expect_value=True))
        return low, high

    def obis_is_on(self, channel: int) -> bool:
        return "ON" in self._obis(f"SOUR{int(channel)}:AM:STAT?", expect_value=True).upper()

    def obis_wavelength_nm(self, channel: int) -> Optional[float]:
        try:
            return float(self._obis(f"SYST{int(channel)}:INF:WAV?", expect_value=True))
        except (ObisError, ValueError):
            return None

    def cube_cmd(self, command: str) -> str:
        return self.cube.command(command, pause_s=0.4, retries=3, framed=self.framed_reads)

    def cube_arm(self, power_mw: float = 12.0) -> None:
        """Configure the CUBE (external control, CW, power). Emits no light."""
        self.cube_cmd("EXT=1")
        time.sleep(1.0)
        self.cube_cmd("CW=1")
        time.sleep(1.0)
        self.cube_cmd(f"P={float(power_mw):.3f}")
        time.sleep(1.0)

    def cube_emit(self) -> None:
        """Open the CUBE shutter and wait for the output to settle."""
        self.cube_cmd("L=1")
        time.sleep(3.0)

    def cube_on(self, power_mw: float = 12.0) -> None:
        self.cube_arm(power_mw)
        self.cube_emit()

    def cube_off(self) -> None:
        self.cube_cmd("L=0")

    def relay_cmd(self, command: str) -> str:
        # The relay board never had its buffers reset before a command; keep that.
        return self.relay.command(command, pause_s=0.05, reset=False, framed=self.framed_reads)

    def relay_on(self, relay_number: int) -> None:
        self.relay_cmd(f"R{int(relay_number)}S")

    def relay_off(self, relay_number: int) -> None:
        self.relay_cmd(f"R{int(relay_number)}R")

    def all_off(self, obis_channels=None, relay_numbers=None) -> list:
        """Switch every source off. Returns the names of devices that could not be reached.

        This is the safety path, so failures are logged (once per device) instead
        of being swallowed silently; ports that are not configured are skipped.
        An OBIS slot that answers ERR (an empty slot, say) is logged and the
        sweep goes on: only a box that does not answer counts as unreachable.
        """
        failed = []
        if self.obis.port:
            for channel in (obis_channels or self._obis_channels):
                try:
                    self.obis_off(channel)
                except ObisError as exc:
                    LOGGER.warning("all_off: OBIS channel %s: %s", channel, exc)
                except Exception as exc:
                    LOGGER.warning("all_off: could not switch OBIS channel %s off: %s", channel, exc)
                    failed.append("OBIS")
                    break
        if self.relay.port:
            for relay_number in (relay_numbers or self._relay_numbers):
                try:
                    self.relay_off(relay_number)
                except Exception as exc:
                    LOGGER.warning("all_off: could not release relay %s: %s", relay_number, exc)
                    failed.append("RELAY")
                    break
        if self.cube.port:
            try:
                self.cube_off()
            except Exception as exc:
                LOGGER.warning("all_off: could not switch the CUBE off: %s", exc)
                failed.append("CUBE")
        return failed


class FilterWheelController:
    def __init__(self, port: str = ""):
        self.device = SerialDevice("HEADSENSOR", port=port, baudrate=4800, timeout=1.0, line_ending="\r")
        self.serial_status = {"hst": ["Head sensor idle."]}

    def configure_port(self, port: str) -> None:
        self.device.configure(port=port)

    def _record_status(self, status: str) -> None:
        messages = self.serial_status.setdefault("hst", [])
        messages.append(status)
        if len(messages) > 50:
            del messages[:-50]

    def open(self) -> bool:
        ok = self.device.open()
        self._record_status("opened" if ok else "open failed")
        return ok

    def close(self) -> None:
        self.device.close()
        self._record_status("closed")

    def send_raw_command(self, command: str, pause_s: float = 0.2) -> Tuple[bool, str]:
        if not self.device.open():
            status = f"Unable to open head sensor port '{self.device.port}'."
            self._record_status(status)
            return False, status

        try:
            response = self.device.command(command, pause_s=pause_s)
            status = response or "timeout"
            self._record_status(status)
            return True, status
        except Exception as exc:
            status = str(exc)
            self._record_status(status)
            return False, status

    def query_device_id(self) -> Tuple[bool, str]:
        success, response = self.send_raw_command("?", pause_s=0.3)
        cleaned = _clean_text(response)
        if cleaned.startswith("Pan"):
            return True, cleaned
        return success and bool(cleaned), cleaned or response

    def set_filterwheel(self, fw_num: int, position: int) -> bool:
        success, response = self.send_raw_command(f"F{int(fw_num)}{int(position)}", pause_s=0.3)
        return success and "error" not in response.lower()

    def reset_filterwheel(self, fw_num: int) -> bool:
        success, response = self.send_raw_command(f"F{int(fw_num)}r", pause_s=0.5)
        return success and "error" not in response.lower()

    def test_filterwheel(self, fw_num: int) -> bool:
        success, response = self.send_raw_command(f"F{int(fw_num)}m", pause_s=0.3)
        return success and "error" not in response.lower()
