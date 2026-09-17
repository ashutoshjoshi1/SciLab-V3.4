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
        except Exception:
            LOGGER.exception("Unable to open serial port %s for %s", self.port, self.name)
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


class LaserController:
    OBIS_MAP = {"405": 5, "445": 4, "488": 3, "640": 2, "685": 6}

    def __init__(self, com_ports: Optional[Dict[str, str]] = None):
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

    def open_all(self) -> None:
        for device in (self.obis, self.cube, self.relay):
            if device.port:
                device.open()

    def ensure_open_for_tag(self, tag: str) -> None:
        if tag in self.OBIS_MAP:
            if not self.obis.open():
                raise RuntimeError(f"Unable to open OBIS port '{self.obis.port}'.")
        elif tag == "377":
            if not self.cube.open():
                raise RuntimeError(f"Unable to open CUBE port '{self.cube.port}'.")
        elif tag in {"517", "532", "Hg_Ar"}:
            if not self.relay.open():
                raise RuntimeError(f"Unable to open RELAY port '{self.relay.port}'.")

    def obis_cmd(self, command: str) -> str:
        if not self.obis.open():
            raise RuntimeError(f"Unable to open OBIS port '{self.obis.port}'.")
        self.obis.reset_buffers()
        self.obis.write_text(command)
        time.sleep(0.2)
        return self.obis.read_all_text()

    def obis_on(self, channel: int) -> None:
        self.obis_cmd(f"SOUR{channel}:AM:STAT ON")

    def obis_off(self, channel: int) -> None:
        self.obis_cmd(f"SOUR{channel}:AM:STAT OFF")

    def obis_set_power(self, channel: int, power_watts: float) -> None:
        self.obis_cmd(f"SOUR{channel}:POW:LEV:IMM:AMPL {float(power_watts):.4f}")

    def cube_cmd(self, command: str) -> str:
        if not self.cube.open():
            raise RuntimeError(f"Unable to open CUBE port '{self.cube.port}'.")
        last_response = ""
        for _ in range(3):
            self.cube.reset_buffers()
            self.cube.write_text(command)
            time.sleep(0.4)
            last_response = self.cube.read_all_text()
            if last_response:
                break
        return last_response

    def cube_on(self, power_mw: float = 12.0) -> None:
        self.cube_cmd("EXT=1")
        time.sleep(1.0)
        self.cube_cmd("CW=1")
        time.sleep(1.0)
        self.cube_cmd(f"P={float(power_mw):.3f}")
        time.sleep(1.0)
        self.cube_cmd("L=1")
        time.sleep(3.0)

    def cube_off(self) -> None:
        self.cube_cmd("L=0")

    def relay_cmd(self, command: str) -> str:
        if not self.relay.open():
            raise RuntimeError(f"Unable to open RELAY port '{self.relay.port}'.")
        self.relay.write_text(command)
        time.sleep(0.05)
        return self.relay.read_all_text()

    def relay_on(self, relay_number: int) -> None:
        self.relay_cmd(f"R{int(relay_number)}S")

    def relay_off(self, relay_number: int) -> None:
        self.relay_cmd(f"R{int(relay_number)}R")

    def all_off(self) -> None:
        for channel in self.OBIS_MAP.values():
            try:
                self.obis_off(channel)
            except Exception:
                pass
        for relay_number in (1, 2, 3, 4):
            try:
                self.relay_off(relay_number)
            except Exception:
                pass
        try:
            self.cube_off()
        except Exception:
            pass


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
            self.device.reset_buffers()
            self.device.write_text(command)
            time.sleep(pause_s)
            response = self.device.read_all_text()
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
