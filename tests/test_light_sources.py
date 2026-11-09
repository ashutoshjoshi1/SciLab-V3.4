"""The source table (services.light_sources): defaults, settings migration, validation."""
from __future__ import annotations

from services import light_sources as ls


def test_defaults_cover_the_bench_in_milliwatts():
    sources = ls.default_sources()
    assert ls.sort_tags(sources) == ["377", "405", "445", "488", "517", "532", "640", "685", "Hg_Ar"]
    assert sources["405"] == {"type": "OBIS", "channel": 5, "power_mw": 50.0}
    assert sources["377"] == {"type": "CUBE", "channel": None, "power_mw": 12.0}
    assert sources["Hg_Ar"]["type"] == "RELAY" and sources["Hg_Ar"]["channel"] == 4
    sources["405"]["power_mw"] = 1.0
    assert ls.default_sources()["405"]["power_mw"] == 50.0          # a copy each time


def test_legacy_settings_are_converted_once():
    # SciLab 3.4.7 wrote OBIS setpoints in watts and no channels.
    legacy = {
        "405": {"type": "OBIS", "power": 0.05},
        "377": {"type": "CUBE", "power": 0.012},
        "532": {"type": "RELAY", "power": 30.0},
        "561": {"type": "OBIS", "power": 0.02},
        "bad": {"type": "LAMP?", "power": 1.0},
    }
    sources = ls.load_sources(legacy, None)
    assert sources["405"] == {"type": "OBIS", "channel": 5, "power_mw": 50.0}
    assert sources["377"] == {"type": "CUBE", "channel": None, "power_mw": 12.0}
    assert sources["532"] == {"type": "RELAY", "channel": 1, "power_mw": 30.0}
    assert sources["561"] == {"type": "OBIS", "channel": None, "power_mw": 20.0}   # no historical slot
    assert "bad" not in sources and sources["Hg_Ar"]["channel"] == 4              # the lamp is always there

    # Version 2 files are taken as they are, in mW.
    current = {"405": {"type": "OBIS", "channel": 1, "power_mw": 0.5}}
    assert ls.load_sources(current, 2)["405"] == {"type": "OBIS", "channel": 1, "power_mw": 0.5}


def test_validate_source_reports_every_problem():
    existing = ls.default_sources()
    assert ls.validate_source("561", "OBIS", 1, "10", existing) == []
    assert ls.validate_source("405", "OBIS", 1, 10.0, existing) == ["405 is already configured."]
    assert "1 to 6" in ls.validate_source("561", "OBIS", 9, 10.0, existing)[0]
    assert "already used by 445" in ls.validate_source("561", "OBIS", 4, 10.0, existing)[0]
    assert ls.validate_source("561", "RELAY", 3, 10.0, existing) == []
    assert "relay" in ls.validate_source("561", "RELAY", 7, 10.0, existing)[0].lower()
    assert "mW" in ls.validate_source("561", "CUBE", None, "lots", existing)[0]
    assert "milliwatts" in ls.validate_source("561", "CUBE", None, 20000, existing)[0]
    problems = ls.validate_source("", "OBIS", 1, -1, existing)
    assert len(problems) == 2
    # Editing a source's own setpoint is not a duplicate of itself.
    assert ls.validate_source("405", "OBIS", 5, 60.0, existing, replacing="405") == []


def test_auto_it_overrides_travel_with_the_source_and_validate():
    raw = {"405": {"type": "OBIS", "channel": 5, "power_mw": 50.0, "start_it_ms": 200, "it_step_ms": "5"},
           "445": {"type": "OBIS", "channel": 4, "power_mw": 30.0, "start_it_ms": "lots", "it_step_ms": 0}}
    sources = ls.load_sources(raw, 2)
    assert sources["405"]["start_it_ms"] == 200.0 and sources["405"]["it_step_ms"] == 5.0
    assert "start_it_ms" not in sources["445"] and "it_step_ms" not in sources["445"]
    assert ls.has_auto_it_override(sources["405"]) and not ls.has_auto_it_override(sources["445"])

    assert ls.validate_auto_it("200", "5") == (200.0, 5.0, [])
    assert ls.validate_auto_it("", " 3 ms ") == (None, 3.0, [])
    start, step, problems = ls.validate_auto_it("0", "abc")
    assert start is None and step is None and len(problems) == 2
    assert "between 0.2 and 60000" in problems[0] and "not a number" in problems[1]


def test_describe_controller_and_watts():
    assert ls.describe_controller({"type": "OBIS", "channel": 5}) == "OBIS ch 5"
    assert ls.describe_controller({"type": "RELAY", "channel": 2}) == "Relay 2"
    assert ls.describe_controller({"type": "CUBE"}) == "CUBE"
    assert ls.power_watts(50.0) == 0.05
