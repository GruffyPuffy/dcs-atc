"""Phraseology config: templates load, render, and cover every brain key."""

import json
from pathlib import Path

import pytest

from brain import Phraseology

PHRASEOLOGY = Path(__file__).resolve().parent.parent / "phraseology.json"

# Every template key the brain can render. Keep in sync with brain.py.
REQUIRED_TEMPLATES = {
    "taxi", "hold_short", "takeoff", "line_up",
    "inbound_outside", "inbound_inside", "inbound_via_gate",
    "report_runway_in_sight", "cleared_land", "cleared_overhead",
    "departure_exit", "contact_tower", "contact_control", "control_join",
    "go_around", "ctr_warning", "roger", "say_again",
    "help_idle", "help_clearance", "help_taxi", "help_holding",
    "help_departure", "help_airborne", "help_inbound", "help_landing",
}


def test_phraseology_is_valid_json():
    data = json.loads(PHRASEOLOGY.read_text(encoding="utf-8"))
    assert "templates" in data
    assert "variables" in data


def test_all_required_templates_present():
    data = json.loads(PHRASEOLOGY.read_text(encoding="utf-8"))
    missing = REQUIRED_TEMPLATES - set(data["templates"])
    assert not missing, f"missing templates: {missing}"


def test_render_fills_placeholders():
    ph = Phraseology.load()
    text = ph.render("taxi", callsign="Colt 1", tower="Kutaisi Tower",
                     ground="Kutaisi Ground", runway="25", wind="calm")
    assert "Colt 1" in text
    assert "{" not in text  # no unfilled placeholders


def test_render_unknown_key_raises():
    ph = Phraseology.load()
    with pytest.raises(KeyError):
        ph.render("does_not_exist", callsign="Colt 1")


def test_every_template_renders_with_common_fields():
    """Every template must render given the fields the brain always supplies."""
    ph = Phraseology.load()
    common = dict(callsign="Colt 1", tower="Kutaisi Tower",
                  ground="Kutaisi Ground", control="Kutaisi Control",
                  runway="25", wind="calm", squawk="4201", taxi_route="alpha",
                  downwind="left", channel="8", tower_channel="7",
                  ground_channel="6", heading="150", turn="right",
                  altitude="1500 ft", gate="East", position="4 miles north")
    for key in REQUIRED_TEMPLATES:
        text = ph.render(key, **common)
        assert "{" not in text, f"{key} left a placeholder unfilled"
