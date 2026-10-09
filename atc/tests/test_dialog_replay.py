"""Replay of the real training sortie (see the 2026-10-09 comm log).

Each test reproduces a line that the bot mishandled in the live run — the ones
that answered "say again" or went silent because of STT/state-machine gaps — and
asserts the *intended* behaviour. This is the regression guard for the fixes
made after that sortie:

- runway-in-use / QNH readback (previously "say again")
- "Clear taxi" STT slip on the taxi readback (previously "say again")
- garbled callsign ("Spring fail 1-1") — extracted, or attributed via speaker
- "channel 7, push" handoff ack (previously "say again")
- takeoff readback after already clearing (previously "say again")

The flight is Gudauta, runway 33, callsign "Springfield 1-1", SRS name "Caveman"
— exactly as in the log.
"""

import pytest

from airspace import Airspace
from brain import AtcBrain, Controller, Phase, Phraseology
from callsigns import CallsignRegistry
from scenario import Scenario


@pytest.fixture
def gudauta():
    return Airspace.load().get("Gudauta")


@pytest.fixture
def springfield(gudauta):
    """A brain + scenario for the logged flight (Gudauta 33, Springfield 1-1)."""
    # The mission offers Springfield11 (among others); the registrar strips the
    # trailing digit, so "Springfield 1-1" is recognised as a real flight.
    registry = CallsignRegistry.from_mission(
        [{"name": "Springfield11"}, {"name": "Colt1"},
         {"name": "Enfield11"}, {"name": "Viper11"}])
    brain = AtcBrain(
        tower=gudauta.tower, runway="33", phraseology=Phraseology.load(),
        callsigns=registry, ground=gudauta.ground, control=gudauta.control,
        gates=list(gudauta.gates), gate_locator=gudauta.nearest_gate,
        airfield=gudauta)
    gudauta.set_active_runway("33")  # wind in the log favoured 33
    brain.set_runway("33")
    return Scenario(gudauta, brain, callsign="Springfield 1-1",
                    speaker="Caveman")


def test_replay_ground_checkin_qnh_readback_and_clearance(springfield):
    """Ground: check-in -> runway/QNH -> readback -> clearance -> readback.

    In the log the runway/QNH readback got "say again", which stalled the very
    first exchange; it must now be confirmed.
    """
    sc = springfield.park("Ramp South")

    # 18:08:16 "Ground, Springfield 1-1." -> acknowledged.
    assert "Ground" in sc.say("Ground, Springfield 1-1", Controller.GROUND)

    # 18:08:40 "Springfield 1, one ship hornet on ramp north." -> runway + QNH.
    r = sc.say("Springfield 1, one ship hornet on ramp north", Controller.GROUND)
    assert "runway 33 in use" in r.lower()

    # 18:09:12 "33 in use, QNH, 2992, Springfield 11." -> readback correct.
    r = sc.say("33 in use, QNH, 2992, Springfield 11", Controller.GROUND)
    assert "readback correct" in r.lower()

    # 18:09:50 clearance, 18:10:05 readback (the "exit northwest" case).
    r = sc.say("Springfield 1-1, ready to copy clearance", Controller.GROUND)
    assert "after departure" in r.lower()
    r = sc.say("After departure, exit northwest 1,500 feet or below, "
               "Springfield 1-1", Controller.GROUND)
    assert "readback correct" in r.lower()


def test_replay_clear_taxi_slip(springfield):
    """18:10:43 'Clear taxi, and hold short runway 33' must read back correctly.

    Whisper dropped the "-ed" ("Clear taxi"), which the old pattern missed.
    """
    sc = springfield.park("Ramp South")
    sc.say("Ground, Springfield 1-1, one ship hornet on ramp north",
           Controller.GROUND)

    r = sc.say("Springfield 1-1 requesting taxi", Controller.GROUND)
    assert "cleared taxi" in r.lower()
    assert sc.phase() == Phase.TAXI

    r = sc.say("Clear taxi, and hold short runway 33, Springfield 1-1",
               Controller.GROUND)
    assert "readback correct" in r.lower()


def test_replay_garbled_callsign_is_understood(springfield):
    """18:23:22 'Springfield 1-1 on final' — STT often mangles the name.

    'Spring fail 1-1 on final' must still be recognised (via the fracture rules
    or the speaker heuristic) and clear the pilot to land.
    """
    sc = springfield.on_final("33", nm=3.0)
    r = sc.say("Spring fail 1-1 on final", Controller.TOWER)
    assert "cleared to land" in r.lower()
    assert sc.phase() == Phase.LANDING


def test_replay_garbled_callsign_attributed_to_known_pilot(springfield):
    """19:xx 'Spring failed, runway in sight' — no parseable flight number.

    The transmission has no number, so extraction fails; because the speaker is
    a known pilot whose callsign the text resembles, it is attributed to them.
    """
    gudauta = springfield.airfield
    brain = springfield.brain
    # Live picture: the only pilot online is Springfield 1-1.
    brain.set_pilot_count(1)
    brain.set_active_pilots(["Springfield 1-1"])

    sc = springfield.on_final("33", nm=4.0)
    r = sc.say("Spring fail, on final", Controller.TOWER)
    assert "cleared to land" in r.lower()
    assert sc.phase() == Phase.LANDING


def test_replay_push_handoff_is_silent(springfield):
    """18:12:47 / 18:15:48 'channel 7/8, push' must be silent, not 'say again'."""
    sc = springfield.holding("Holding E")
    reply = sc.say("Springfield 1-1, channel 7, push", Controller.GROUND)
    assert reply == ""  # acknowledged by switching; we do not transmit


def test_replay_takeoff_readback_does_not_loop(springfield):
    """18:13:35 'Right turn out, cleared for takeoff, 33' after clearing.

    The pilot was already cleared (phase Departure); the old brain answered
    "say again" and the flow looped.
    """
    sc = springfield.at_threshold("33")
    sc.say("Tower, Springfield 1-1, at runway 33, ready for departure",
           Controller.TOWER)
    sc.say("Line up and wait 33, Springfield 1-1", Controller.TOWER)
    assert sc.phase() == Phase.DEPARTURE

    r = sc.say("Right turn out, cleared for takeoff, 33, Springfield 1-1",
               Controller.TOWER)
    assert "say again" not in (r or "").lower()
    assert sc.phase() == Phase.DEPARTURE
