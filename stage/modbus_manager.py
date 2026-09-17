"""
Modbus RTU communication manager for Oriental Motor AZ series.

Provides thread-safe read/write operations over a single serial bus
shared by two motor slaves. Register addresses follow the AZ series
(AZD-KD / AZD-KR) Direct-Data-Operation conventions.

If your motor model uses a different register map, adjust the addresses
in the Registers class below.
"""

import time
import threading
import logging
from pymodbus.client import ModbusSerialClient

logger = logging.getLogger(__name__)

# Exceptions that indicate the serial port itself is gone (USB unplugged, etc.)
_PORT_LOST_EXCEPTIONS = (OSError, )
try:
    import serial
    _PORT_LOST_EXCEPTIONS = (OSError, serial.SerialException)
except ImportError:
    pass

# Retry / timing constants
_MAX_RETRIES = 3
_RETRY_BACKOFF_S = 0.100   # 100 ms between retries
_INTER_CMD_DELAY_S = 0.020  # 20 ms RS-485 turnaround guard


# ── Oriental Motor AZ Series Register Map ─────────────────────────────────

class Registers:
    """Holding-register addresses (hex) for the AZ series."""

    # Remote I/O — command / status
    COMMAND = 0x007D          # Write: driver input command
    STATUS  = 0x007F          # Read : driver output status (lower word)

    # Direct Data Operation (DDO) block
    DDO_OPERATION = 0x1800    # Operation type  (32-bit: 0x1800-0x1801)
    DDO_POSITION  = 0x1802    # Target position (32-bit: 0x1802-0x1803)
    DDO_SPEED     = 0x1804    # Speed           (32-bit: 0x1804-0x1805)
    DDO_ACCEL     = 0x1806    # Acceleration    (32-bit: 0x1806-0x1807)
    DDO_DECEL     = 0x1808    # Deceleration    (32-bit: 0x1808-0x1809)
    DDO_CURRENT   = 0x180A    # Operating current (%)

    # Monitor registers
    FEEDBACK_POS  = 0x00CC    # Current actual position (32-bit: 0x00CC-0x00CD)
    COMMAND_POS   = 0x00C6    # Command position        (32-bit: 0x00C6-0x00C7)
    TORQUE_MONITOR = 0x00D6   # Torque monitor           (32-bit: 0x00D6-0x00D7) % of max holding torque

    # Alarm
    ALARM = 0x0080            # Present alarm code


class CommandBits:
    """Bit masks for the COMMAND register (0x007D)."""

    START   = 0x0008   # Bit  3 — rising-edge triggers operation
    HOME    = 0x0010   # Bit  4 — home return
    STOP    = 0x0020   # Bit  5 — decelerate to stop
    FREE    = 0x0040   # Bit  6 — release holding torque
    ALM_RST = 0x0080   # Bit  7 — alarm reset
    FW_JOG  = 0x1000   # Bit 12 — jog forward  (positive direction)
    RV_JOG  = 0x2000   # Bit 13 — jog reverse  (negative direction)


class StatusBits:
    """Bit masks for the STATUS register (0x007F, lower word)."""

    READY    = 0x0020  # Bit  5 — R-OUT5: driver ready
    HOME_END = 0x0010  # Bit  4 — R-OUT4: home return completed
    MOVING   = 0x2000  # Bit 13 — R-OUT13: motor in motion (MOVE)
    ALARM    = 0x0080  # Bit  7 — R-OUT7: alarm present (ALM-A)


# ── Modbus Manager ────────────────────────────────────────────────────────

class ModbusManager:
    """Thread-safe Modbus RTU manager for a single serial bus."""

    def __init__(self):
        self.client = None
        self._lock = threading.Lock()
        self._connected = False
        # Per-slave tracking: last command timestamp and consecutive failure count
        self._last_cmd_time = {}   # slave_id → monotonic timestamp
        self._fail_counts = {}     # slave_id → int

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def connected(self):
        return self._connected

    # ── Connection ────────────────────────────────────────────────────────

    def connect(self, port, baudrate=115200, parity="E", timeout=1):
        """Open a Modbus RTU connection. Returns True on success."""
        with self._lock:
            if self._connected:
                self._close_unlocked()

            self.client = ModbusSerialClient(
                port=port,
                baudrate=baudrate,
                parity=parity,
                stopbits=1,
                bytesize=8,
                timeout=timeout,
            )
            ok = self.client.connect()
            self._connected = ok
            self._fail_counts.clear()
            self._last_cmd_time.clear()
            if ok:
                logger.info("Connected to %s @ %d baud (parity=%s)", port, baudrate, parity)
            else:
                logger.error("Failed to connect to %s", port)
            return ok

    def disconnect(self):
        """Close the serial connection."""
        with self._lock:
            self._close_unlocked()

    def _close_unlocked(self):
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
        self._connected = False
        logger.info("Disconnected")

    # ── Inter-command delay ───────────────────────────────────────────────

    def _turnaround_delay(self, slave_id):
        """Wait if needed so consecutive commands to the same slave respect
        the AZ series RS-485 turnaround time (~20 ms). Must be called while
        self._lock is held."""
        last = self._last_cmd_time.get(slave_id, 0.0)
        elapsed = time.monotonic() - last
        if elapsed < _INTER_CMD_DELAY_S:
            time.sleep(_INTER_CMD_DELAY_S - elapsed)

    def _stamp(self, slave_id):
        """Record the timestamp of the most recent bus operation for a slave."""
        self._last_cmd_time[slave_id] = time.monotonic()

    # ── Failure tracking ─────────────────────────────────────────────────

    def _record_success(self, slave_id):
        """Reset the consecutive-failure counter for *slave_id*."""
        if self._fail_counts.get(slave_id, 0) != 0:
            self._fail_counts[slave_id] = 0

    def _record_failure(self, slave_id):
        """Increment and return the new consecutive-failure count."""
        count = self._fail_counts.get(slave_id, 0) + 1
        self._fail_counts[slave_id] = count
        return count

    def get_fail_count(self, slave_id):
        """Public read of consecutive failure count (used by poll loop)."""
        return self._fail_counts.get(slave_id, 0)

    # ── 32-bit helpers ────────────────────────────────────────────────────

    @staticmethod
    def _to_signed32(reg_upper, reg_lower):
        """Two unsigned 16-bit registers → signed 32-bit int.

        Oriental Motor convention: upper word at address N, lower at N+1.
        """
        val = (reg_upper << 16) | reg_lower
        if val >= 0x80000000:
            val -= 0x100000000
        return val

    @staticmethod
    def _from_signed32(value):
        """Signed 32-bit int → (upper_word, lower_word) unsigned 16-bit pair."""
        if value < 0:
            value += 0x100000000
        return (value >> 16) & 0xFFFF, value & 0xFFFF

    # ── Read operations ───────────────────────────────────────────────────

    def read_position(self, slave_id):
        """Read current feedback position (steps). Returns int or None."""
        with self._lock:
            if not self._connected:
                return None
            for attempt in range(_MAX_RETRIES):
                try:
                    self._turnaround_delay(slave_id)
                    rr = self.client.read_holding_registers(
                        Registers.FEEDBACK_POS, count=2, device_id=slave_id
                    )
                    self._stamp(slave_id)
                    if rr.isError():
                        fails = self._record_failure(slave_id)
                        if fails >= 3:
                            logger.warning("Position read error (slave %d, attempt %d/%d): %s",
                                           slave_id, attempt + 1, _MAX_RETRIES, rr)
                        else:
                            logger.debug("Position read error (slave %d, attempt %d/%d): %s",
                                         slave_id, attempt + 1, _MAX_RETRIES, rr)
                        if attempt < _MAX_RETRIES - 1:
                            time.sleep(_RETRY_BACKOFF_S)
                            continue
                        return None
                    self._record_success(slave_id)
                    return self._to_signed32(rr.registers[0], rr.registers[1])
                except _PORT_LOST_EXCEPTIONS as exc:
                    logger.error("Serial port lost during position read (slave %d): %s", slave_id, exc)
                    self._connected = False
                    self._record_failure(slave_id)
                    return None
                except Exception as exc:
                    fails = self._record_failure(slave_id)
                    if fails >= 3:
                        logger.warning("Position read exception (slave %d, attempt %d/%d): %s",
                                       slave_id, attempt + 1, _MAX_RETRIES, exc)
                    else:
                        logger.debug("Position read exception (slave %d, attempt %d/%d): %s",
                                     slave_id, attempt + 1, _MAX_RETRIES, exc)
                    if attempt < _MAX_RETRIES - 1:
                        time.sleep(_RETRY_BACKOFF_S)
                        continue
                    return None
            return None

    def read_status(self, slave_id):
        """Read the status register. Returns raw 16-bit value or None."""
        with self._lock:
            if not self._connected:
                return None
            for attempt in range(_MAX_RETRIES):
                try:
                    self._turnaround_delay(slave_id)
                    rr = self.client.read_holding_registers(
                        Registers.STATUS, count=1, device_id=slave_id
                    )
                    self._stamp(slave_id)
                    if rr.isError():
                        fails = self._record_failure(slave_id)
                        if fails >= 3:
                            logger.warning("Status read error (slave %d, attempt %d/%d): %s",
                                           slave_id, attempt + 1, _MAX_RETRIES, rr)
                        else:
                            logger.debug("Status read error (slave %d, attempt %d/%d): %s",
                                         slave_id, attempt + 1, _MAX_RETRIES, rr)
                        if attempt < _MAX_RETRIES - 1:
                            time.sleep(_RETRY_BACKOFF_S)
                            continue
                        return None
                    self._record_success(slave_id)
                    return rr.registers[0]
                except _PORT_LOST_EXCEPTIONS as exc:
                    logger.error("Serial port lost during status read (slave %d): %s", slave_id, exc)
                    self._connected = False
                    self._record_failure(slave_id)
                    return None
                except Exception as exc:
                    fails = self._record_failure(slave_id)
                    if fails >= 3:
                        logger.warning("Status read exception (slave %d, attempt %d/%d): %s",
                                       slave_id, attempt + 1, _MAX_RETRIES, exc)
                    else:
                        logger.debug("Status read exception (slave %d, attempt %d/%d): %s",
                                     slave_id, attempt + 1, _MAX_RETRIES, exc)
                    if attempt < _MAX_RETRIES - 1:
                        time.sleep(_RETRY_BACKOFF_S)
                        continue
                    return None
            return None

    def read_torque(self, slave_id):
        """Read current torque (% of max holding torque). Returns float or None.

        The register value is in units of 0.1%, so 1000 = 100.0%.
        """
        with self._lock:
            if not self._connected:
                return None
            for attempt in range(_MAX_RETRIES):
                try:
                    self._turnaround_delay(slave_id)
                    rr = self.client.read_holding_registers(
                        Registers.TORQUE_MONITOR, count=2, device_id=slave_id
                    )
                    self._stamp(slave_id)
                    if rr.isError():
                        if attempt < _MAX_RETRIES - 1:
                            time.sleep(_RETRY_BACKOFF_S)
                            continue
                        return None
                    return self._to_signed32(rr.registers[0], rr.registers[1]) / 10.0
                except _PORT_LOST_EXCEPTIONS as exc:
                    logger.error("Serial port lost during torque read (slave %d): %s", slave_id, exc)
                    self._connected = False
                    return None
                except Exception:
                    if attempt < _MAX_RETRIES - 1:
                        time.sleep(_RETRY_BACKOFF_S)
                        continue
                    return None
            return None

    def write_current(self, slave_id, percent):
        """Set DDO operating current. *percent* is 0.0–100.0%. Returns True on success."""
        with self._lock:
            if not self._connected:
                return False
            for attempt in range(_MAX_RETRIES):
                try:
                    self._turnaround_delay(slave_id)
                    raw = max(0, min(1000, int(round(percent * 10))))
                    hi, lo = self._from_signed32(raw)
                    wr = self.client.write_registers(
                        Registers.DDO_CURRENT, [hi, lo], device_id=slave_id
                    )
                    self._stamp(slave_id)
                    if wr.isError():
                        logger.error("write_current error (slave %d, attempt %d/%d): %s",
                                     slave_id, attempt + 1, _MAX_RETRIES, wr)
                        if attempt < _MAX_RETRIES - 1:
                            time.sleep(_RETRY_BACKOFF_S)
                            continue
                        return False
                    return True
                except _PORT_LOST_EXCEPTIONS as exc:
                    logger.error("Serial port lost during write_current (slave %d): %s", slave_id, exc)
                    self._connected = False
                    return False
                except Exception as exc:
                    logger.error("write_current failed (slave %d, attempt %d/%d): %s",
                                 slave_id, attempt + 1, _MAX_RETRIES, exc)
                    if attempt < _MAX_RETRIES - 1:
                        time.sleep(_RETRY_BACKOFF_S)
                        continue
                    return False
            return False

    # ── Motion commands ───────────────────────────────────────────────────

    def move_absolute(self, slave_id, position, speed, accel, decel):
        """Absolute positioning via Direct Data Operation. Returns True on success."""
        with self._lock:
            if not self._connected:
                return False
            try:
                pos_hi, pos_lo = self._from_signed32(position)
                spd_hi, spd_lo = self._from_signed32(speed)
                acc_hi, acc_lo = self._from_signed32(accel)
                dec_hi, dec_lo = self._from_signed32(decel)

                # DDO block: operation-type(2) + position(2) + speed(2) + accel(2) + decel(2)
                # Each 32-bit value: upper word at register N, lower word at N+1
                values = [
                    0, 1,               # Operation type = 1 (absolute positioning)
                    pos_hi, pos_lo,     # Target position
                    spd_hi, spd_lo,     # Speed
                    acc_hi, acc_lo,     # Acceleration
                    dec_hi, dec_lo,     # Deceleration
                ]
                self._turnaround_delay(slave_id)
                wr = self.client.write_registers(
                    Registers.DDO_OPERATION, values, device_id=slave_id
                )
                self._stamp(slave_id)
                if wr.isError():
                    logger.error("DDO write error (slave %d): %s", slave_id, wr)
                    return False

                # Pulse START
                return self._pulse_command(slave_id, CommandBits.START)
            except _PORT_LOST_EXCEPTIONS as exc:
                logger.error("Serial port lost during move_absolute (slave %d): %s", slave_id, exc)
                self._connected = False
                return False
            except Exception as exc:
                logger.error("move_absolute failed (slave %d): %s", slave_id, exc)
                return False

    def jog_forward(self, slave_id, speed=None):
        """Begin jogging in the positive direction. Hold until stop_jog()."""
        with self._lock:
            if not self._connected:
                return False
            try:
                if speed is not None:
                    self._turnaround_delay(slave_id)
                    hi, lo = self._from_signed32(speed)
                    self.client.write_registers(
                        Registers.DDO_SPEED, [hi, lo], device_id=slave_id
                    )
                    self._stamp(slave_id)
                self._turnaround_delay(slave_id)
                self.client.write_register(
                    Registers.COMMAND, CommandBits.FW_JOG, device_id=slave_id
                )
                self._stamp(slave_id)
                return True
            except _PORT_LOST_EXCEPTIONS as exc:
                logger.error("Serial port lost during jog_forward (slave %d): %s", slave_id, exc)
                self._connected = False
                return False
            except Exception as exc:
                logger.error("jog_forward failed (slave %d): %s", slave_id, exc)
                return False

    def jog_reverse(self, slave_id, speed=None):
        """Begin jogging in the negative direction. Hold until stop_jog()."""
        with self._lock:
            if not self._connected:
                return False
            try:
                if speed is not None:
                    self._turnaround_delay(slave_id)
                    hi, lo = self._from_signed32(speed)
                    self.client.write_registers(
                        Registers.DDO_SPEED, [hi, lo], device_id=slave_id
                    )
                    self._stamp(slave_id)
                self._turnaround_delay(slave_id)
                self.client.write_register(
                    Registers.COMMAND, CommandBits.RV_JOG, device_id=slave_id
                )
                self._stamp(slave_id)
                return True
            except _PORT_LOST_EXCEPTIONS as exc:
                logger.error("Serial port lost during jog_reverse (slave %d): %s", slave_id, exc)
                self._connected = False
                return False
            except Exception as exc:
                logger.error("jog_reverse failed (slave %d): %s", slave_id, exc)
                return False

    def stop_jog(self, slave_id):
        """Release the jog command (motor decelerates to stop)."""
        with self._lock:
            if not self._connected:
                return False
            try:
                self._turnaround_delay(slave_id)
                self.client.write_register(
                    Registers.COMMAND, 0x0000, device_id=slave_id
                )
                self._stamp(slave_id)
                return True
            except _PORT_LOST_EXCEPTIONS as exc:
                logger.error("Serial port lost during stop_jog (slave %d): %s", slave_id, exc)
                self._connected = False
                return False
            except Exception as exc:
                logger.error("stop_jog failed (slave %d): %s", slave_id, exc)
                return False

    def stop(self, slave_id):
        """Immediate deceleration stop."""
        with self._lock:
            if not self._connected:
                return False
            return self._pulse_command(slave_id, CommandBits.STOP)

    def home(self, slave_id):
        """Execute home-return operation."""
        with self._lock:
            if not self._connected:
                return False
            return self._pulse_command(slave_id, CommandBits.HOME)

    def free(self, slave_id):
        """Release motor holding torque."""
        with self._lock:
            if not self._connected:
                return False
            return self._pulse_command(slave_id, CommandBits.FREE)

    def alarm_reset(self, slave_id):
        """Reset the current alarm."""
        with self._lock:
            if not self._connected:
                return False
            return self._pulse_command(slave_id, CommandBits.ALM_RST)

    # ── Internal helpers ──────────────────────────────────────────────────

    def _pulse_command(self, slave_id, bits):
        """Write command bits then clear them (edge-triggered commands).

        Must be called while self._lock is held.
        """
        try:
            self._turnaround_delay(slave_id)
            wr = self.client.write_register(Registers.COMMAND, bits, device_id=slave_id)
            self._stamp(slave_id)
            if wr.isError():
                logger.error("Command pulse set failed (slave %d, bits=0x%04X): %s",
                             slave_id, bits, wr)
                return False
            time.sleep(0.05)  # 50 ms pulse width
            self._turnaround_delay(slave_id)
            self.client.write_register(Registers.COMMAND, 0x0000, device_id=slave_id)
            self._stamp(slave_id)
            return True
        except _PORT_LOST_EXCEPTIONS as exc:
            logger.error("Serial port lost during command pulse (slave %d, bits=0x%04X): %s",
                         slave_id, bits, exc)
            self._connected = False
            return False
        except Exception as exc:
            logger.error("Command pulse failed (slave %d, bits=0x%04X): %s",
                         slave_id, bits, exc)
            return False
