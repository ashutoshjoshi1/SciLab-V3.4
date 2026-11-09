"""The bench's light sources: which controller drives each one, on which channel, at what power.

One table, ``{tag: {"type", "channel", "power_mw"}}``, feeds every switching
path (the Live View and Check Resolution checkboxes, the measurement run, All
off) and every list of sources in the UI. Setup edits it; the settings file
persists it. Power is always in milliwatts here; the OBIS protocol wants watts,
so the controller converts at the last moment.

Settings written by SciLab 3.4.7 and earlier (``laser_configs`` without a
version) stored OBIS setpoints in watts and had no channels: ``load_sources``
converts them once.
"""
from __future__ import annotations

import copy
from typing import Dict, Iterable, List, Mapping, Optional

SETTINGS_VERSION = 2

OBIS = "OBIS"
CUBE = "CUBE"
RELAY = "RELAY"
SOURCE_TYPES = (OBIS, CUBE, RELAY)

# A Coherent OBIS laser box has up to six slots; the relay board has four relays.
OBIS_CHANNELS = tuple(range(1, 7))
RELAY_CHANNELS = tuple(range(1, 5))

LAMP_TAG = "Hg_Ar"

# The SciGlob bench as shipped. Setpoints in mW.
DEFAULT_SOURCES: Dict[str, Dict[str, object]] = {
    "377": {"type": CUBE, "channel": None, "power_mw": 12.0},
    "405": {"type": OBIS, "channel": 5, "power_mw": 50.0},
    "445": {"type": OBIS, "channel": 4, "power_mw": 30.0},
    "488": {"type": OBIS, "channel": 3, "power_mw": 30.0},
    "517": {"type": RELAY, "channel": 2, "power_mw": 30.0},
    "532": {"type": RELAY, "channel": 1, "power_mw": 30.0},
    "640": {"type": OBIS, "channel": 2, "power_mw": 50.0},
    "685": {"type": OBIS, "channel": 6, "power_mw": 30.0},
    LAMP_TAG: {"type": RELAY, "channel": 4, "power_mw": 0.0},
}

# Channel assignments the 3.4.7 code had hard-wired, used to upgrade old settings.
_LEGACY_CHANNELS = {tag: cfg["channel"] for tag, cfg in DEFAULT_SOURCES.items()}

# Optional per-source auto-IT settings: the first frame's integration time and
# the largest change per frame. Absent = the application's defaults.
AUTO_IT_KEYS = ("start_it_ms", "it_step_ms")
START_IT_RANGE_MS = (0.2, 60000.0)
IT_STEP_RANGE_MS = (0.01, 1000.0)


def auto_it_value(config: Mapping[str, object], key: str) -> Optional[float]:
    """A source's auto-IT override for ``key`` (positive float), or None for the default."""
    try:
        value = float(config.get(key) or 0.0)
    except (TypeError, ValueError):
        return None
    return value if value > 0.0 else None


def has_auto_it_override(config: Mapping[str, object]) -> bool:
    return any(auto_it_value(config, key) is not None for key in AUTO_IT_KEYS)


def validate_auto_it(start_text: str, step_text: str) -> tuple:
    """(start_ms, step_ms, problems) from the two fields; blank means default (None)."""
    problems: List[str] = []
    values = []
    for text, (low, high), what in ((start_text, START_IT_RANGE_MS, "start integration time"),
                                    (step_text, IT_STEP_RANGE_MS, "IT step")):
        text = str(text or "").strip().replace("ms", "").strip()
        if not text:
            values.append(None)
            continue
        try:
            value = float(text)
        except ValueError:
            problems.append(f"'{text}' is not a number of milliseconds for the {what}.")
            values.append(None)
            continue
        if not low <= value <= high:
            problems.append(f"The {what} must be between {low:g} and {high:g} ms.")
            values.append(None)
            continue
        values.append(value)
    return values[0], values[1], problems


def default_sources() -> Dict[str, Dict[str, object]]:
    return copy.deepcopy(DEFAULT_SOURCES)


def sort_tags(tags: Iterable[str]) -> List[str]:
    """Ascending wavelength, named sources (the lamp) last."""
    def _key(tag: str):
        try:
            return (0, float(tag), "")
        except ValueError:
            return (1, 0.0, str(tag))
    return sorted((str(tag) for tag in tags), key=_key)


def power_watts(power_mw: float) -> float:
    return float(power_mw) / 1000.0


def describe_controller(config: Mapping[str, object]) -> str:
    """'OBIS ch 5', 'CUBE', 'Relay 2': the controller and channel a source hangs off."""
    kind = str(config.get("type", "")).upper()
    channel = config.get("channel")
    if kind == OBIS:
        return f"OBIS ch {channel}" if channel else "OBIS"
    if kind == RELAY:
        return f"Relay {channel}" if channel else "Relay"
    if kind == CUBE:
        return "CUBE"
    return kind or "?"


def _coerce_channel(value, kind: str, tag: str):
    if kind == CUBE:
        return None
    try:
        channel = int(value) if value is not None else None
    except (TypeError, ValueError):
        channel = None
    if channel is None:
        channel = _LEGACY_CHANNELS.get(tag)
    return channel


def load_sources(raw: Optional[Mapping[str, Mapping]], version: Optional[int]) -> Dict[str, Dict[str, object]]:
    """A clean source table from a settings file's ``laser_configs`` (any version).

    Unknown types are dropped with the entry; a missing channel falls back to
    the bench's historical assignment for that tag. The lamp is always present.
    """
    sources: Dict[str, Dict[str, object]] = {}
    legacy = not version or int(version) < 2
    for tag, entry in (raw or {}).items():
        if not isinstance(entry, Mapping):
            continue
        tag = str(tag)
        kind = str(entry.get("type", OBIS)).upper()
        if kind not in SOURCE_TYPES:
            continue
        if legacy:
            value = float(entry.get("power", entry.get("power_mw", 0.0)) or 0.0)
            if kind == OBIS and value < 1.0:
                value *= 1000.0          # 0.05 W -> 50 mW
            elif kind == CUBE and value <= 0.3:
                value *= 1000.0          # the old "W if small" guess, applied once
            power_mw = value
        else:
            power_mw = float(entry.get("power_mw", entry.get("power", 0.0)) or 0.0)
        sources[tag] = {
            "type": kind,
            "channel": _coerce_channel(entry.get("channel"), kind, tag),
            "power_mw": max(0.0, power_mw),
        }
        for key in AUTO_IT_KEYS:
            value = auto_it_value(entry, key)
            if value is not None:
                sources[tag][key] = value
    if LAMP_TAG not in sources:
        sources[LAMP_TAG] = copy.deepcopy(DEFAULT_SOURCES[LAMP_TAG])
    return sources


def validate_source(tag: str, kind: str, channel, power_mw, existing: Mapping[str, Mapping],
                    *, replacing: Optional[str] = None) -> List[str]:
    """Why a source as entered in the Add dialog cannot be accepted (empty = fine)."""
    problems: List[str] = []
    tag = str(tag).strip()
    if not tag:
        problems.append("Enter the wavelength in nm (for example 561).")
    elif tag != LAMP_TAG:
        try:
            wavelength = float(tag)
        except ValueError:
            problems.append("The wavelength must be a number of nanometres.")
        else:
            if not 100.0 <= wavelength <= 5000.0:
                problems.append("The wavelength must be between 100 and 5000 nm.")
    if tag in existing and tag != replacing:
        problems.append(f"{tag} is already configured.")

    kind = str(kind).upper()
    if kind not in SOURCE_TYPES:
        problems.append("Choose a controller type.")
    elif kind == OBIS:
        if channel not in OBIS_CHANNELS:
            problems.append("An OBIS source needs a laser-box channel from 1 to 6.")
    elif kind == RELAY:
        if channel not in RELAY_CHANNELS:
            problems.append("A relay source needs a relay number from 1 to 4.")
    if kind in (OBIS, RELAY) and not problems:
        for other_tag, other in existing.items():
            if other_tag in (tag, replacing):
                continue
            if str(other.get("type", "")).upper() == kind and other.get("channel") == channel:
                problems.append(f"{describe_controller(other)} is already used by {other_tag}.")
                break

    try:
        value = float(power_mw)
    except (TypeError, ValueError):
        problems.append("Enter the setpoint in mW.")
    else:
        if value < 0.0:
            problems.append("The setpoint cannot be negative.")
        elif value > 10000.0:
            problems.append("A setpoint above 10 W is not accepted; the unit is milliwatts.")
    return problems
