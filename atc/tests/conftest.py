"""Shared pytest fixtures for the offline ATC test suite.

These tests run with NO DCS server and NO SRS: they exercise the pure logic
(callsign recognition, phraseology, brain state machine, airspace geometry,
ATIS, controller routing) directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The bot modules live in atc/ and import each other by bare name.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from airspace import Airspace  # noqa: E402
from brain import AtcBrain, Phraseology  # noqa: E402
from callsigns import CallsignRegistry  # noqa: E402


@pytest.fixture(scope="session")
def airfield():
    return Airspace.load().get("Kutaisi")


@pytest.fixture
def callsigns():
    return CallsignRegistry.from_mission(
        [{"name": "Colt 1"}, {"name": "Ford 2"}, {"name": "Springfield21"}])


@pytest.fixture
def brain(airfield, callsigns):
    return AtcBrain(
        tower=airfield.tower, runway=airfield.active_runway,
        phraseology=Phraseology.load(), callsigns=callsigns,
        ground=airfield.ground, control=airfield.control,
        gates=list(airfield.gates), gate_locator=airfield.nearest_gate)
