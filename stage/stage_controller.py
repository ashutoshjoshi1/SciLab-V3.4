"""
High-level stage controller for the All Spec Laser Control application.

Wraps ModbusManager + StageConfig and provides:
  - connect / disconnect
  - load_config (reads slots from Stage Software config.json)
  - goto_slot (safe sequential move: Y→home, X→target, Y→target)
  - stop_all (emergency stop both motors)
  - status polling helpers
"""

import logging
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from .modbus_manager import ModbusManager, StatusBits
from .stage_config import StageConfig

logger = logging.getLogger(__name__)

_MOVE_TIMEOUT_S = 60.0
_POLL_INTERVAL_S = 0.15
_MAX_POSITION_STEPS = 10_000_000


class StageController:
    """Orchestrates 2-axis stage moves using the Stage Software config."""

    def __init__(self):
        self.modbus = ModbusManager()
        self.config = StageConfig()
        self._move_thread: Optional[threading.Thread] = None
        self._abort = threading.Event()

    # ── Configuration ────────────────────────────────────────────────────

    def load_config(self, config_path: str) -> bool:
        """Load stage config.json from *config_path*.  Returns True on success."""
        return self.config.load(config_path)

    @property
    def slots(self) -> List[Dict]:
        return self.config.slots

    # ── Connection ───────────────────────────────────────────────────────

    def connect(self, port: Optional[str] = None) -> bool:
        """Connect to the stage via Modbus RTU.

        If *port* is None, uses the COM port from the loaded config.
        """
        port = port or self.config.com_port
        if not port:
            logger.error("No stage COM port configured")
            return False
        return self.modbus.connect(port, baudrate=115200, parity="E", timeout=1)

    def disconnect(self) -> None:
        self.stop_all()
        self.modbus.disconnect()

    @property
    def connected(self) -> bool:
        return self.modbus.connected

    # ── Status ───────────────────────────────────────────────────────────

    def read_positions(self) -> Tuple[Optional[int], Optional[int]]:
        """Read both motor positions.  Returns (x_pos, y_pos) or (None, None)."""
        m1 = self.config.get_motor(1)
        m2 = self.config.get_motor(2)
        x = self.modbus.read_position(m1["slave_id"])
        y = self.modbus.read_position(m2["slave_id"])
        return x, y

    def is_moving(self) -> bool:
        """True if either motor is currently in motion."""
        m1 = self.config.get_motor(1)
        m2 = self.config.get_motor(2)
        for sid in (m1["slave_id"], m2["slave_id"]):
            st = self.modbus.read_status(sid)
            if st is not None and st & StatusBits.MOVING:
                return True
        return False

    @property
    def move_in_progress(self) -> bool:
        return self._move_thread is not None and self._move_thread.is_alive()

    # ── Motion ───────────────────────────────────────────────────────────

    def stop_all(self) -> None:
        """Emergency stop both motors immediately."""
        self._abort.set()
        m1 = self.config.get_motor(1)
        m2 = self.config.get_motor(2)
        self.modbus.stop(m1["slave_id"])
        self.modbus.stop(m2["slave_id"])

    def goto_slot(
        self,
        slot_index: int,
        on_done: Optional[Callable[[bool, str], None]] = None,
    ) -> bool:
        """Move to slot *slot_index* in a background thread.

        Executes the safe-move sequence:
          1. Motor 2 (Y) → home (0)
          2. Motor 1 (X) → target X
          3. Motor 2 (Y) → target Y

        *on_done(success, message)* is called when complete.
        Returns False if a move is already in progress or slot invalid.
        """
        slots = self.config.slots
        if slot_index < 0 or slot_index >= len(slots):
            logger.error("Invalid slot index %d", slot_index)
            return False
        if self.move_in_progress:
            logger.warning("Slot move already in progress")
            return False
        if not self.connected:
            logger.error("Stage not connected")
            return False

        slot = slots[slot_index]
        try:
            x_target = int(slot["x_position"])
            y_target = int(slot["y_position"])
        except (KeyError, ValueError, TypeError) as exc:
            logger.error("Invalid slot position data for slot %d: %s", slot_index, exc)
            return False
        if abs(x_target) > _MAX_POSITION_STEPS or abs(y_target) > _MAX_POSITION_STEPS:
            logger.error("Position out of safe range: x=%d, y=%d", x_target, y_target)
            return False
        name = slot.get("name", f"Slot {slot_index + 1}")

        self._abort.clear()
        self._move_thread = threading.Thread(
            target=self._slot_move_sequence,
            args=(x_target, y_target, name, on_done),
            daemon=True,
        )
        self._move_thread.start()
        return True

    def _slot_move_sequence(
        self,
        x_target: int,
        y_target: int,
        name: str,
        on_done: Optional[Callable[[bool, str], None]],
    ) -> None:
        """Background thread: safe sequential slot move."""
        m1 = self.config.get_motor(1)
        m2 = self.config.get_motor(2)
        s1 = m1["slave_id"]
        s2 = m2["slave_id"]

        def _move(slave_id: int, motor_cfg: Dict, position: int, label: str) -> bool:
            ok = self.modbus.move_absolute(
                slave_id,
                position,
                motor_cfg.get("speed", 5000),
                motor_cfg.get("acceleration", 5000),
                motor_cfg.get("deceleration", 5000),
            )
            if not ok:
                return False
            return self._wait_move_done(slave_id, label)

        try:
            # Step 1: Y → home
            logger.info("Slot '%s': Step 1/3 — Y-axis → home (0)", name)
            if not _move(s2, m2, 0, "Y→home"):
                self._finish(on_done, False, f"Slot '{name}': Y-axis home failed")
                return

            # Step 2: X → target
            logger.info("Slot '%s': Step 2/3 — X-axis → %d", name, x_target)
            if not _move(s1, m1, x_target, "X→target"):
                self._finish(on_done, False, f"Slot '{name}': X-axis move failed")
                return

            # Step 3: Y → target
            logger.info("Slot '%s': Step 3/3 — Y-axis → %d", name, y_target)
            if not _move(s2, m2, y_target, "Y→target"):
                self._finish(on_done, False, f"Slot '{name}': Y-axis move failed")
                return

            logger.info("Slot '%s': Move complete", name)
            self._finish(on_done, True, f"Moved to '{name}'")

        except Exception as exc:
            logger.exception("Slot move failed")
            self._finish(on_done, False, f"Error: {exc}")

    def _wait_move_done(self, slave_id: int, label: str) -> bool:
        """Poll until MOVING bit clears or timeout / abort."""
        deadline = time.monotonic() + _MOVE_TIMEOUT_S
        time.sleep(0.2)  # allow motor to start
        while time.monotonic() < deadline:
            if self._abort.is_set():
                logger.warning("Move '%s' aborted", label)
                return False
            st = self.modbus.read_status(slave_id)
            if st is None:
                time.sleep(_POLL_INTERVAL_S)
                continue
            if not (st & StatusBits.MOVING):
                return True
            if st & StatusBits.ALARM:
                logger.error("Alarm during move '%s'", label)
                return False
            time.sleep(_POLL_INTERVAL_S)
        logger.error("Move '%s' timed out after %.0fs", label, _MOVE_TIMEOUT_S)
        return False

    @staticmethod
    def _finish(
        callback: Optional[Callable[[bool, str], None]],
        success: bool,
        message: str,
    ) -> None:
        if callback:
            try:
                callback(success, message)
            except Exception:
                logger.exception("Slot-move callback error")
