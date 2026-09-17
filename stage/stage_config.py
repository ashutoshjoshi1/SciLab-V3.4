"""
Read-only accessor for the Stage Controller config.json.

This module reads the slot definitions and motor parameters from the
external Stage Software config file.  The path to that file is stored
in our own application settings (Setup tab) so it can point to whatever
machine the Stage Software lives on.
"""

import json
import logging
from copy import deepcopy
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_MOTOR = {
    "slave_id": 1,
    "speed": 5000,
    "acceleration": 5000,
    "deceleration": 5000,
    "jog_speed": 1000,
    "last_position": 0,
}


class StageConfig:
    """Reads stage configuration (slots, motor params, COM port) from a JSON file."""

    def __init__(self, config_path: Optional[str] = None):
        self._config: Dict = {}
        self._path: Optional[Path] = None
        if config_path:
            self.load(config_path)

    # ── Loading ──────────────────────────────────────────────────────────

    _MAX_CONFIG_BYTES = 1_048_576  # 1 MB

    def load(self, config_path: str) -> bool:
        """Load config from *config_path*.  Returns True on success."""
        path = Path(config_path)
        if not path.is_file():
            logger.warning("Stage config not found: %s", path)
            self._config = {}
            self._path = None
            return False
        try:
            file_size = path.stat().st_size
            if file_size > self._MAX_CONFIG_BYTES:
                logger.error("Stage config too large (%d bytes), refusing to load", file_size)
                self._config = {}
                self._path = None
                return False
            with open(path, "r", encoding="utf-8") as fh:
                self._config = json.load(fh)
            if not isinstance(self._config, dict):
                logger.error("Stage config root is not a JSON object")
                self._config = {}
                self._path = None
                return False
            self._path = path
            self._validate_slots()
            logger.info("Stage config loaded from %s (%d slots)",
                        path, len(self.slots))
            return True
        except (json.JSONDecodeError, IOError) as exc:
            logger.error("Failed to load stage config %s: %s", path, exc)
            self._config = {}
            self._path = None
            return False

    @property
    def loaded(self) -> bool:
        return self._path is not None and bool(self._config)

    @property
    def path(self) -> Optional[str]:
        return str(self._path) if self._path else None

    # ── Slots ────────────────────────────────────────────────────────────

    @property
    def slots(self) -> List[Dict]:
        """Return list of slot dicts: [{name, x_position, y_position}, ...]"""
        return self._config.get("slots", [])

    # ── Motor parameters ─────────────────────────────────────────────────

    @property
    def com_port(self) -> str:
        return self._config.get("com_port", "")

    def get_motor(self, motor_num: int) -> Dict:
        """Return motor config dict for motor 1 or 2."""
        key = f"motor{motor_num}"
        default = deepcopy(DEFAULT_MOTOR)
        default["slave_id"] = motor_num
        return self._config.get(key, default)

    # ── Validation ───────────────────────────────────────────────────────

    def _validate_slots(self) -> None:
        raw = self._config.get("slots", [])
        if not isinstance(raw, list):
            self._config["slots"] = []
            return
        clean = []
        for i, slot in enumerate(raw):
            if not isinstance(slot, dict):
                continue
            if "x_position" not in slot or "y_position" not in slot:
                continue
            try:
                int(slot["x_position"])
                int(slot["y_position"])
            except (TypeError, ValueError):
                continue
            if "name" not in slot:
                slot["name"] = f"Slot {i + 1}"
            clean.append(slot)
        self._config["slots"] = clean
