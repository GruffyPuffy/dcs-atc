"""Brain: intent recognition, controller routing, per-pilot state machine."""

import dataclasses

from brain import (AtcBrain, Controller, INCURSION_POLLS, Phase, Phraseology,
                   _fold_bullseye_figures, _fold_spoken_figures,
                   _spoken_figure, parse_bullseye_call, parse_marking_call)
from ctr import AircraftTrack


def _track(airfield, lat, lon, alt_ft=1000.0, heading=0.0):
    """Build an AircraftTrack for a raw position (no tracker state)."""
    return AircraftTrack(
        callsign="x", inside=airfield.ctr.contains(lat, lon, alt_ft),
        lat=lat, lon=lon, alt_ft=alt_ft,
        distance_nm=airfield.ctr.distance_nm(lat, lon),
        relative=airfield.ctr.relative_position(lat, lon), heading=heading)


def _brain_for(airfield, callsigns):
    return AtcBrain(tower=airfield.tower, runway=airfield.active_runway,
                    phraseology=Phraseology.load(), callsigns=callsigns,
                    ground=airfield.ground, control=airfield.control,
                    gates=list(airfield.gates),
                    gate_locator=airfield.nearest_gate, airfield=airfield)


# ---------- Radio channels (per-airfield) ----------

def test_channels_come_from_airfield(airfield, callsigns):
    # the channel numbers are per-airfield config, not hardcoded
    af = dataclasses.replace(airfield,
                             channels={"ground": "1", "tower": "2",
                                       "control": "3"})
    b = _brain_for(af, callsigns)
    b.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    reply = b.handle("Colt 1, holding short runway 25",
                     controller=Controller.GROUND)
    assert "channel 2" in reply


def test_channels_fall_back_to_phraseology(airfield, callsigns):
    # an airfield without channels uses the phraseology.json defaults
    af = dataclasses.replace(airfield, channels={})
    b = _brain_for(af, callsigns)
    b.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    reply = b.handle("Colt 1, holding short runway 25",
                     controller=Controller.GROUND)
    assert "channel 7" in reply


# ---------- Ground ----------

def test_ground_clearance_assigns_exit_gate(brain):
    reply = brain.handle("Ground, Colt 1, ready to copy clearance",
                         controller=Controller.GROUND)
    assert "Colt 1" in reply
    assert "Ground" in reply
    assert brain.pilots["Colt 1"].phase == Phase.CLEARANCE
    assert brain.pilots["Colt 1"].exit_gate


def test_ground_clearance_exit_matches_runway(brain, airfield):
    # active runway 25 -> Exit West (the gate matching the departure heading)
    reply = brain.handle("Ground, Colt 1, ready to copy clearance",
                         controller=Controller.GROUND)
    assert "Exit West" in reply
    assert "1500 ft or below" in reply


def test_ground_clearance_exit_follows_runway_change(brain, airfield):
    brain.set_runway("07")
    reply = brain.handle("Ground, Colt 1, ready to copy clearance",
                         controller=Controller.GROUND)
    assert "Exit East" in reply


def test_ground_taxi(brain):
    reply = brain.handle("Ground, Colt 1, requesting taxi",
                         controller=Controller.GROUND)
    assert "cleared taxi" in reply
    assert brain.pilots["Colt 1"].phase == Phase.TAXI


def test_ground_taxi_route_by_position(brain, airfield):
    # Ramp South -> Sierra Echo for 25; Ramp North -> November Delta
    south = _track(airfield, *airfield.parking_areas["Ramp South"])
    reply = brain.handle("Ground, Colt 1, requesting taxi", track=south,
                         controller=Controller.GROUND)
    assert "Sierra Echo" in reply
    brain.pilots.clear()
    north = _track(airfield, *airfield.parking_areas["Ramp North"])
    reply = brain.handle("Ground, Colt 1, requesting taxi", track=north,
                         controller=Controller.GROUND)
    assert "November Delta" in reply


def test_ground_taxi_route_follows_active_runway(brain, airfield):
    # same ramp, different active runway -> different route
    south = _track(airfield, *airfield.parking_areas["Ramp South"])
    brain.set_runway("07")
    reply = brain.handle("Ground, Colt 1, requesting taxi", track=south,
                         controller=Controller.GROUND)
    assert "Whiskey" in reply


def test_ground_hold_short_hands_off_to_tower(brain):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    reply = brain.handle("Colt 1, holding short runway 25",
                         controller=Controller.GROUND)
    assert "contact" in reply.lower()
    assert "Tower" in reply
    assert brain.pilots["Colt 1"].phase == Phase.HOLDING


# ---------- Tower ----------

def test_tower_takeoff(brain):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    reply = brain.handle("Tower, Colt 1, ready for departure",
                         controller=Controller.TOWER)
    # Master Arms: Tower answers "line up and wait"; takeoff follows the readback.
    assert "line up and wait" in reply
    assert brain.pilots["Colt 1"].phase == Phase.LINEUP
    reply = brain.handle("Line up and wait 25, Colt 1",
                         controller=Controller.TOWER)
    assert "cleared for takeoff" in reply
    assert brain.pilots["Colt 1"].phase == Phase.TAKEOFF


def test_tower_runway_in_sight_overhead(brain):
    reply = brain.handle("Tower, Colt 1, runway in sight",
                         controller=Controller.TOWER)
    assert "overhead break" in reply


def test_tower_overhead_break_clears_break(brain):
    reply = brain.handle("Kutaisi Traffic, Colt 1, overhead break runway 25",
                         controller=Controller.TOWER)
    assert "overhead break" in reply


def test_formation_learned_and_used_in_clearance(brain):
    # a two-ship checks in, then the break clearance addresses the flight
    brain.handle("Ground, Colt 1, two-ship Hornets on Ramp South",
                 controller=Controller.GROUND)
    assert brain.pilots["Colt 1"].formation == 2
    reply = brain.handle("Tower, Colt 1, runway in sight",
                         controller=Controller.TOWER)
    assert "2-ship Colt 1" in reply


def test_single_ship_has_no_formation_prefix(brain):
    reply = brain.handle("Tower, Colt 1, runway in sight",
                         controller=Controller.TOWER)
    assert "ship" not in reply


def test_formation_digit_form(brain):
    brain.handle("Control, Colt 1, 4-ship Hornets inbound",
                 controller=Controller.CONTROL)
    assert brain.pilots["Colt 1"].formation == 4
    reply = brain.handle("Tower, Colt 1, on final", controller=Controller.TOWER)
    assert "4-ship Colt 1" in reply


def test_tower_in_the_break_is_silent(brain):
    reply = brain.handle("Tower, Colt 1, in the break",
                         controller=Controller.TOWER)
    assert reply == ""
    assert brain.pilots["Colt 1"].phase == Phase.LANDING
    assert not brain.pilots["Colt 1"].cleared_landing


def test_tower_break_clearance_readback_is_silent(brain):
    brain.handle("Tower, Colt 1, runway in sight", controller=Controller.TOWER)
    reply = brain.handle(
        "cleared for right overhead break, runway 07, Colt 1",
        controller=Controller.TOWER)
    assert reply == ""


def test_tower_break_clearance_readback_tolerates_stt_brake(brain):
    brain.handle("Tower, Colt 1, runway in sight", controller=Controller.TOWER)
    reply = brain.handle(
        "cleared for right overhead brake, runway 07, Colt 1",
        controller=Controller.TOWER)
    assert reply == ""


def test_tower_turning_final_is_silent(brain):
    brain.handle("Tower, Colt 1, runway in sight", controller=Controller.TOWER)
    assert brain.handle("Colt 1, turning final",
                        controller=Controller.TOWER) == ""
    assert brain.pilots["Colt 1"].phase == Phase.LANDING
    assert not brain.pilots["Colt 1"].cleared_landing


def test_tower_pattern_calls_are_optional_before_final(brain):
    # Skip in-the-break and turning-final; on final still clears.
    brain.handle("Tower, Colt 1, runway in sight", controller=Controller.TOWER)
    reply = brain.handle("Tower, Colt 1, on final", controller=Controller.TOWER)
    assert "cleared to land" in reply


def test_tower_pattern_then_final(brain):
    brain.handle("Tower, Colt 1, runway in sight", controller=Controller.TOWER)
    brain.handle("cleared for left overhead break runway 25, Colt 1",
                 controller=Controller.TOWER)
    assert brain.handle("Colt 1, in the break",
                        controller=Controller.TOWER) == ""
    assert brain.handle("Colt 1, turning final",
                        controller=Controller.TOWER) == ""
    reply = brain.handle("Colt 1, on final", controller=Controller.TOWER)
    assert "cleared to land" in reply


def test_tower_report_on_final_readback_is_silent(brain):
    brain.handle("Tower, Colt 1, in the break", controller=Controller.TOWER)
    assert brain.handle("report on final, Colt 1",
                        controller=Controller.TOWER) == ""
    reply = brain.handle("Colt 1, on final", controller=Controller.TOWER)
    assert "cleared to land" in reply


# ---------- Master Arms Mission Procedures flow ----------

def test_ground_checkin_acknowledged(brain):
    # "Ground, Adder11" -> "Adder11, Ground"
    reply = brain.handle("Ground, Colt 1", controller=Controller.GROUND)
    assert reply == "Colt 1, Ground."


def test_ground_gives_runway_and_qnh_without_atis(brain):
    # MA SOP: without "with information X" Ground also reads out the weather.
    reply = brain.handle("Ground, Colt 1, two-ship Hornets on Ramp South",
                         controller=Controller.GROUND)
    assert "weather is" in reply
    assert "runway 25 in use" in reply
    assert "QNH" in reply


def test_ground_acknowledges_with_information(brain):
    # MA SOP: with the information letter the call is just runway/QNH (the
    # pilot already has the weather from ATIS).
    reply = brain.handle(
        "Ground, Colt 1, two-ship Hornets on Ramp South with information Charlie",
        controller=Controller.GROUND)
    assert "weather is" not in reply
    assert "runway 25 in use" in reply
    assert "QNH" in reply


def test_ground_runway_qnh_readback(brain):
    # The kneeboard line "25 in use, QNH 2992, Adder11" must be answered with
    # "readback correct, advise when ready for clearance" — not "say again".
    # This is the ONE place MA says "readback correct": nothing follows it.
    brain.handle("Ground, Colt 1, two-ship Hornets on Ramp South",
                 controller=Controller.GROUND)
    reply = brain.handle("25 in use, QNH 2992, Colt 1",
                         controller=Controller.GROUND)
    assert "readback correct" in reply
    assert "ready for clearance" in reply


# ---------- Readback acknowledgement rule (real/MA practice) ----------
# A correct readback is acknowledged ONLY when the controller has nothing else
# to say. When a next instruction follows, the controller just gives it (no
# "readback correct"). These tests lock that rule in.

def test_correct_readback_is_silent_when_a_next_step_follows(brain):
    # Departure clearance readback -> next step is the taxi request: silent.
    brain.handle("Ground, Colt 1, ready to copy clearance",
                 controller=Controller.GROUND)
    reply = brain.handle("After departure turn right Exit West, 1500 ft or below, Colt 1",
                         controller=Controller.GROUND)
    assert reply == ""


def test_correct_readback_is_acknowledged_when_nothing_follows(brain):
    # Ground runway/QNH readback -> nothing follows: explicit acknowledgement.
    brain.handle("Ground, Colt 1, two-ship Hornets on Ramp South",
                 controller=Controller.GROUND)
    reply = brain.handle("25 in use, QNH 2992, Colt 1",
                         controller=Controller.GROUND)
    assert "readback correct" in reply


def test_lineup_readback_gives_takeoff_without_readback_correct(brain):
    # The line-up readback's "next instruction" IS the takeoff clearance, so it
    # must not be prefixed with "readback correct" (MA kneeboard).
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    brain.handle("Tower, Colt 1, ready for departure", controller=Controller.TOWER)
    reply = brain.handle("Line up and wait 25, Colt 1", controller=Controller.TOWER)
    assert "cleared for takeoff" in reply
    assert "readback correct" not in reply.lower()


def test_incomplete_readback_still_coaches(brain):
    # The rule only changes the *correct* case; an incomplete readback is still
    # flagged (never silently accepted).
    brain.handle("Ground, Colt 1, ready to copy clearance",
                 controller=Controller.GROUND)
    reply = brain.handle("After departure turn right Exit West, Colt 1",
                         controller=Controller.GROUND)
    assert "readback incomplete" in reply.lower()


def test_ground_clearance_readback_without_exit_word(brain):
    # STT may drop the "Exit" prefix; the gate name alone must still read back.
    # A correct readback with a next step (taxi) is SILENT (no "readback
    # correct") — the controller just waits for the taxi request.
    brain.handle("Ground, Colt 1, ready to copy clearance",
                 controller=Controller.GROUND)
    reply = brain.handle("After departure turn right West 1500 ft or below, Colt 1",
                         controller=Controller.GROUND)
    assert reply == ""


def test_ground_clearance_readback_correct(brain):
    brain.handle("Ground, Colt 1, ready to copy clearance",
                 controller=Controller.GROUND)
    # Runway 25 assigns Exit West; a correct, complete readback is silent.
    reply = brain.handle("After departure turn right Exit West, 1500 ft or below, Colt 1",
                         controller=Controller.GROUND)
    assert reply == ""


def test_ground_taxi_readback_correct(brain):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    reply = brain.handle("Cleared taxi Sierra Echo and hold short runway 25, Colt 1",
                         controller=Controller.GROUND)
    assert reply == ""


def test_taxi_readback_tolerates_stt_clear(brain):
    # Common STT slip: "clear taxi" / "clear taxi, solo, hold short runway 25".
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    reply = brain.handle("Clear taxi, and hold short runway 25, Colt 1",
                         controller=Controller.GROUND)
    assert reply == ""


def test_clearance_readback_missing_altitude_is_flagged_incomplete(brain):
    """An incomplete readback is NOT called correct — it is flagged and continued."""
    brain.handle("Ground, Colt 1, ready to copy clearance",
                 controller=Controller.GROUND)
    # Gate ("West") is read back but the "1500 ft" altitude is not.
    reply = brain.handle("After departure turn right Exit West, Colt 1",
                         controller=Controller.GROUND)
    low = reply.lower()
    assert "readback incomplete" in low
    assert "altitude" in low
    assert "continue" in low
    assert "readback correct" not in low  # honest: it was not correct


def test_clearance_readback_incomplete_never_says_correct(brain):
    brain.handle("Ground, Colt 1, ready to copy clearance",
                 controller=Controller.GROUND)
    reply = brain.handle("After departure turn right West, Colt 1",  # miss altitude
                         controller=Controller.GROUND)
    assert "readback incomplete" in reply.lower()
    assert "readback correct" not in reply.lower()


def test_readback_tolerates_callsign_glued_to_the_runway(brain):
    """STT runs the callsign's digits into the last number ("two five" +
    "Colt 1" -> "251"); the runway must still count as read back."""
    brain.handle("Ground, Colt 1, one-ship Hornet on Ramp South",
                 controller=Controller.GROUND)
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    reply = brain.handle(
        "clear taxi, sierra echo, and hold short runway two five one, Colt 1.",
        controller=Controller.GROUND)
    assert "readback incomplete" not in reply.lower()


def test_mentioning_in_use_is_not_a_readback_before_ground_issues_it(brain):
    """A transmission that merely says "in use" must not be called a correct
    readback of runway/QNH info Ground never gave."""
    reply = brain.handle(
        "Ground, Colt 1, I was expecting the runway 25 in use call here",
        controller=Controller.GROUND)
    assert "readback correct" not in (reply or "").lower()


def test_repeated_readback_attempts_are_safe(brain):
    """A pilot who insists on readbacks must not corrupt the state machine.

    The clearance readback is idempotent: repeating it keeps re-flagging the
    miss and never advances or breaks the phase.
    """
    brain.handle("Ground, Colt 1, ready to copy clearance",
                 controller=Controller.GROUND)
    assert brain.pilots["Colt 1"].phase == Phase.CLEARANCE
    for _ in range(3):
        reply = brain.handle("After departure turn right West, Colt 1",
                             controller=Controller.GROUND)
        assert "readback incomplete" in reply.lower()
        assert brain.pilots["Colt 1"].phase == Phase.CLEARANCE  # unchanged
    # A readback that FIXES the flagged miss is confirmed ("readback correct"),
    # so the pilot knows the correction landed. A clean first readback is still
    # silent (see test_ground_clearance_readback_correct). Either way the phase
    # stays at CLEARANCE: the pilot proceeds to taxi when ready.
    reply = brain.handle("After departure turn right West, 1500 ft or below, Colt 1",
                         controller=Controller.GROUND)
    assert "readback correct" in reply.lower()
    assert brain.pilots["Colt 1"].phase == Phase.CLEARANCE


def test_corrected_readback_is_confirmed_once(brain):
    """The confirmation is for the *fix*: a repeat of the correct readback is
    silent again, because there is no longer an outstanding miss."""
    brain.handle("Ground, Colt 1, ready to copy clearance",
                 controller=Controller.GROUND)
    brain.handle("After departure turn right West, Colt 1",
                 controller=Controller.GROUND)  # missing the altitude
    corrected = brain.handle(
        "After departure turn right West, 1500 ft or below, Colt 1",
        controller=Controller.GROUND)
    assert "readback correct" in corrected.lower()
    assert brain.handle(
        "After departure turn right West, 1500 ft or below, Colt 1",
        controller=Controller.GROUND) == ""


def test_readback_numbers_accept_spoken_digits():
    from brain import AtcBrain
    assert "25" in AtcBrain._numbers("runway two five")
    assert "2992" in AtcBrain._numbers("qnh two niner niner two")
    assert "1500" in AtcBrain._numbers("one five zero zero feet")
    assert "070" in AtcBrain._numbers("heading zero seven zero")


def test_readback_numbers_accept_compound_number_words():
    # A pilot may say "angels fifteen" or "angels thirty two" instead of
    # spelling the digits. Before this, the word tables held only single digits,
    # so those folded to *nothing* and the readback looked empty (debrief
    # 2026-10-22: "angels fifteen" compared against a "15" clearance as if the
    # pilot had said nothing at all).
    from brain import AtcBrain
    assert "15" in AtcBrain._numbers("angels fifteen")
    assert "32" in AtcBrain._numbers("angels thirty two")
    assert "240" in AtcBrain._numbers("flight level two four zero")
    assert "21000" in AtcBrain._numbers("angels two one thousand")
    # The digit-by-digit form still works and agrees with the compound form.
    assert AtcBrain._numbers("angels one five") == AtcBrain._numbers("angels fifteen")


def test_compound_number_readback_is_accepted():
    # End to end: a clearance of "Angels 15" is satisfied by "angels fifteen".
    brain = AtcBrain()
    brain.handle("Colt 1, Ground, ready to copy clearance",
                 controller=Controller.GROUND)
    clearance = brain.handle("Colt 1, Ground, ready to copy clearance",
                             controller=Controller.GROUND)
    assert "15" in clearance
    reply = brain.handle(
        "After departure Exit East, 1500 ft or below, Colt 1",
        controller=Controller.GROUND)
    assert reply == ""  # complete readback: silence, not "say again"


def test_readback_ignores_the_callsign_number(brain):
    # The clearance text contains the callsign ("Colt 1, Ground, ..."); its
    # flight number must not be compared as a clearance value. A readback that
    # ends "Colt 11" (STT doubling the element) must still pass.
    brain.handle("Ground, Colt 1, ready to copy clearance",
                 controller=Controller.GROUND)
    reply = brain.handle(
        "After departure, turn right, exit west, 1500 feet or below, Colt 11",
        controller=Controller.GROUND)
    assert reply == ""


def test_taxi_readback_ignores_the_callsign_number(brain):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    reply = brain.handle("Cleared taxi whiskey, hold short runway 25, Colt 11",
                         controller=Controller.GROUND)
    assert reply == ""


def test_takeoff_readback_after_clearance_gets_roger(brain):
    # Once cleared for takeoff, reading the clearance back must NOT "say again".
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    brain.handle("Tower, Colt 1, ready for departure", controller=Controller.TOWER)
    brain.handle("Line up and wait 25, Colt 1", controller=Controller.TOWER)
    reply = brain.handle("Right turn out, cleared for takeoff, 25, Colt 1",
                         controller=Controller.TOWER)
    assert "say again" not in reply.lower()
    assert brain.pilots["Colt 1"].phase == Phase.TAKEOFF


def test_tower_lineup_readback_clears_takeoff(brain):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    brain.handle("Tower, Colt 1, ready for departure", controller=Controller.TOWER)
    reply = brain.handle("Line up and wait 25, Colt 1", controller=Controller.TOWER)
    assert "cleared for takeoff" in reply
    # Kutaisi 25's default exit (West) is straight ahead, so no turnout clause.
    assert "turnout" not in reply
    assert brain.pilots["Colt 1"].phase == Phase.TAKEOFF


def test_takeoff_turnout_follows_the_exit_gate(brain):
    # Only a RIGHT turnout is spoken (MA: a left turnout is the default and is
    # not called). A straight-out departure is told "fly runway heading".
    from brain import PilotState
    pilot = PilotState(callsign="Colt 1")
    # Kutaisi 25: Exit East is a right turn, Exit West is straight.
    pilot.exit_gate = "Exit East"
    assert "right turnout" in brain._takeoff_clearance("Colt 1", pilot)
    pilot.exit_gate = "Exit West"
    reply = brain._takeoff_clearance("Colt 1", pilot)
    assert "fly runway heading" in reply
    assert "turnout" not in reply


def test_left_turnout_is_not_spoken(brain):
    """A left turnout is the default, so it is never called (MA practice)."""
    from brain import PilotState
    pilot = PilotState(callsign="Colt 1")
    # Kutaisi 25: Exit South is a left turn.
    pilot.exit_gate = "Exit South"
    reply = brain._takeoff_clearance("Colt 1", pilot)
    assert "turnout" not in reply
    assert "fly runway heading" not in reply
    assert "cleared for takeoff" in reply


def test_takeoff_turnout_matches_the_ground_clearance_turn(brain, airfield):
    # The Ground departure clearance and the Tower takeoff clearance must agree
    # on the turnout, so a future change to the exit selection keeps them in
    # sync (both derive from the same exit gate via Airfield.exit_turn).
    brain.handle("Ground, Colt 1, ready to copy clearance",
                 controller=Controller.GROUND)
    pilot = brain.pilots["Colt 1"]
    gate = pilot.exit_gate
    assert gate  # the clearance assigned an exit
    expected = airfield.exit_turn(gate, brain.runway)
    takeoff = brain._takeoff_clearance("Colt 1", pilot)
    if expected == "right":
        assert "right turnout" in takeoff
    elif expected == "left":
        assert "turnout" not in takeoff
        assert "fly runway heading" not in takeoff
    else:
        assert "fly runway heading" in takeoff


def test_control_departure_checkin_climb(brain):
    reply = brain.handle("Control, Colt 1, at 1500 ft",
                         controller=Controller.CONTROL)
    assert "radar contact" in reply
    assert "climb to Angels" in reply


def test_departure_exit_rotates_between_flights(brain):
    """With no traffic, successive clearances rotate through the exits, so a
    pilot practises different ones instead of always the straight-out gate."""
    seen = []
    for _ in range(3):
        brain.pilots.clear()
        brain.handle("Ground, Colt 1, one-ship Hornet on Ramp South",
                     controller=Controller.GROUND)
        reply = brain.handle("Ground, Colt 1, ready to copy clearance",
                             controller=Controller.GROUND)
        seen.append(brain.pilots["Colt 1"].exit_gate)
    assert len(set(seen)) == 3  # three distinct exits


def test_departure_exit_prefers_the_freest_gate(brain, airfield):
    """With traffic, the exit with the fewest aircraft near it is chosen."""
    from tests.scenario import Unit
    # Park traffic on the straight-out gate (West for runway 25) so it is busy.
    west = airfield.gates["West"]
    traffic = [Unit("AI-1", west[0], west[1], airfield.elevation_ft + 1000)]
    brain.handle("Ground, Colt 1, one-ship Hornet on Ramp South",
                 controller=Controller.GROUND)
    brain.handle("Ground, Colt 1, ready to copy clearance",
                 controller=Controller.GROUND, traffic=traffic)
    assert brain.pilots["Colt 1"].exit_gate != "West"


def test_control_climb_issued_once_then_readback(brain):
    """A second level report is silent, not re-cleared (no loop, no roger).

    MA SOP next step is the Stingray handoff at Angels 15, so the climb
    readback has a next instruction and must not get a bare "roger".
    """
    first = brain.handle("Control, Colt 1, airborne 5 miles east climbing",
                         controller=Controller.CONTROL)
    assert "climb to Angels" in first
    assert brain.pilots["Colt 1"].climb_issued
    second = brain.handle("Climbing angels 15, Colt 1",
                          controller=Controller.CONTROL)
    assert second == ""
    third = brain.handle("Colt 1 at angels 16", controller=Controller.CONTROL)
    assert third == ""


def test_control_join_readback_descends(brain):
    brain.handle("Control, Colt 1, inbound 35 miles north",
                 controller=Controller.CONTROL)
    reply = brain.handle("150 to join via Entry East, Colt 1",
                         controller=Controller.CONTROL)
    assert "descend to 1500 feet" in reply
    assert brain.pilots["Colt 1"].descend_issued


def test_control_descent_readback_uses_the_noun(brain):
    """A readback phrased "descent of 1,500 feet" is accepted silently.

    The pilot reads the clearance back with the *noun* ("descent"), not the
    verb ("descend"). The readback regexes only matched "descend", so the
    brain answered "say again" and the arrival stalled (seen in the
    2026-10-12 sortie).
    """
    brain.handle("Control, Colt 1, inbound 35 miles north",
                 controller=Controller.CONTROL)
    brain.handle("150 to join via Entry East, Colt 1",
                 controller=Controller.CONTROL)
    reply = brain.handle("descent of 1,500 feet, Colt 1",
                         controller=Controller.CONTROL)
    assert "say again" not in (reply or "").lower()
    assert reply == ""
    assert brain.pilots["Colt 1"].phase == Phase.INBOUND
    assert brain.pilots["Colt 1"].descend_readback


def test_tower_entry_call_reports_runway_in_sight(brain):
    reply = brain.handle("Tower, Colt 1, Entry East", controller=Controller.TOWER)
    assert "report runway in sight" in reply


def test_entry_report_accepted_without_the_word_entry(brain):
    # STT often drops "entry" ("Chevy 1 and 3 East"). Once the flight is
    # inbound, a bare gate direction is accepted as the entry report.
    brain.handle("Control, Colt 1, inbound 35 miles north at Angels 12",
                 controller=Controller.CONTROL)
    assert brain.pilots["Colt 1"].phase == Phase.INBOUND
    # The pilot then checks in on Tower; the radio agency/phase advance there.
    reply = brain.handle("Tower, Colt 1, and 3 East", controller=Controller.TOWER)
    assert "report runway in sight" in reply
    assert brain.pilots["Colt 1"].phase == Phase.LANDING


def test_bare_gate_direction_ignored_before_inbound(brain):
    # A gate direction with no "entry" and no inbound phase is not an entry
    # report (e.g. a departure readback mentioning a gate) — no false trigger.
    reply = brain.handle("Tower, Colt 1, East", controller=Controller.TOWER)
    assert reply is None or "report runway in sight" not in reply


def test_ground_taxiing_variant(brain):
    # "taxiing" (not just "taxi") must match
    reply = brain.handle("Ground, Colt 1, one ship Hornets, taxiing to runway 25",
                         controller=Controller.GROUND)
    assert "cleared taxi" in reply


def test_tower_inbound_without_track(brain):
    reply = brain.handle("Tower, Colt 1, inbound", controller=Controller.TOWER)
    assert "Colt 1" in reply
    assert brain.pilots["Colt 1"].phase == Phase.INBOUND


def test_tower_airborne_hands_off_to_control(brain):
    brain._pilot("Colt 1").phase = Phase.TAKEOFF
    reply = brain.handle("Tower, Colt 1, airborne", controller=Controller.TOWER)
    assert reply == ""  # wait for radar to show the exit arc crossing
    assert brain.pilots["Colt 1"].phase == Phase.TAKEOFF


def test_tower_wheels_up_hands_off_to_control(brain):
    # "Wheels up" does not trigger an invented handoff phrase; radar controls
    # the Tower-to-Control transfer at the exit arc.
    brain._pilot("Colt 1").phase = Phase.TAKEOFF
    reply = brain.handle("Tower, Colt 1, wheels up", controller=Controller.TOWER)
    assert reply == ""
    assert brain.pilots["Colt 1"].phase == Phase.TAKEOFF


def test_tower_off_the_ground_hands_off_to_control(brain):
    brain._pilot("Colt 1").phase = Phase.TAKEOFF
    reply = brain.handle("Tower, Colt 1, off the ground",
                         controller=Controller.TOWER)
    assert reply == ""
    assert brain.pilots["Colt 1"].phase == Phase.TAKEOFF


def test_tower_wheels_up_on_the_go_is_not_departure(brain):
    # "wheels up on the go" is a touch-and-go report, not a departure: it
    # must NOT hand the flight to Control.
    reply = brain.handle("Tower, Colt 1, wheels up on the go",
                         controller=Controller.TOWER)
    assert brain.pilots["Colt 1"].phase != Phase.AIRBORNE


def test_tower_on_final_clears_to_land(brain, airfield):
    import math
    thr = airfield.runway_threshold("25")
    hdg = airfield.runway_heading("25")
    back = math.radians((hdg + 180) % 360)
    lat = thr[0] + math.degrees(5 * 1852 * math.cos(back) / 6_371_000)
    lon = thr[1] + math.degrees(5 * 1852 * math.sin(back)
                                / (6_371_000 * math.cos(math.radians(thr[0]))))
    on_final = _track(airfield, lat, lon, heading=hdg)
    reply = brain.handle("Tower, Colt 1, on final", track=on_final,
                         controller=Controller.TOWER)
    assert "cleared to land" in reply
    assert brain.pilots["Colt 1"].phase == Phase.LANDING


def test_tower_on_final_challenged_when_not_on_final(brain, airfield):
    # claiming "on final" while sitting on the ramp
    ramp = _track(airfield, airfield.ctr.center_lat, airfield.ctr.center_lon)
    reply = brain.handle("Tower, Colt 1, on final", track=ramp,
                         controller=Controller.TOWER)
    assert "negative" in reply.lower()
    assert brain.pilots["Colt 1"].phase != Phase.LANDING


def test_tower_vacated_hands_off_to_ground(brain):
    reply = brain.handle("Tower, Colt 1, runway vacated",
                         controller=Controller.TOWER)
    assert "Ground" in reply
    assert "channel 6" in reply
    assert brain.pilots["Colt 1"].phase == Phase.TAXI


def test_tower_rolling_off_hands_off_to_ground(brain):
    # MA's "rolling off runway 25" is a landing report, not a departure call:
    # it must hand off to Ground, not to Control.
    reply = brain.handle("Tower, Colt 1, rolling off runway 25",
                         controller=Controller.TOWER)
    assert "Ground" in reply
    assert "Control" not in reply
    assert brain.pilots["Colt 1"].phase == Phase.TAXI


def test_ground_accepts_rolling_off_and_vacated(brain):
    # Both MA's "rolling off" and the standard "vacated" are accepted on Ground.
    for phrase in ("Ground, Colt 1, rolling off runway 25, requesting taxi to parking",
                   "Ground, Colt 1, runway 25 vacated, requesting taxi to parking"):
        brain.pilots.clear()
        reply = brain.handle(phrase, controller=Controller.GROUND)
        assert reply and "cleared taxi" in reply, phrase


def test_ground_taxi_to_parking_named_ramp(brain, airfield):
    reply = brain.handle("Ground, Colt 1, requesting taxi to Ramp North",
                         controller=Controller.GROUND)
    assert "Ramp North" in reply
    assert "cleared taxi" in reply


def test_ground_taxi_to_parking_nearest_ramp(brain, airfield):
    # no ramp named -> nearest to the live position
    near_north = _track(airfield, 42.183, 42.490)
    reply = brain.handle("Ground, Colt 1, requesting taxi to parking",
                         track=near_north, controller=Controller.GROUND)
    assert "Ramp North" in reply


def test_parking_readback_does_not_reissue_clearance(brain, airfield):
    # The readback "cleared taxi to ramp north" contains "to ramp", which used
    # to re-match the parking request and re-issue the clearance forever.
    brain.handle("Ground, Colt 1, requesting taxi to Ramp North",
                 controller=Controller.GROUND)
    reply = brain.handle("Cleared taxi to ramp north via Alpha November, Colt 1",
                         controller=Controller.GROUND)
    assert reply == ""
    assert "cleared taxi to" not in reply.lower()


# ---------- Control ----------

def test_control_inbound_routes_via_entry_gate(brain):
    reply = brain.handle("Control, Colt 1, inbound 35 miles north",
                         controller=Controller.CONTROL)
    assert "Control" in reply
    assert "radar contact" in reply
    assert "Entry" in reply
    assert brain.pilots["Colt 1"].entry_gate


def test_control_inbound_uses_best_entry_for_runway(brain):
    # The entry is always the common-sense one for the active runway (the side
    # you land *from*), regardless of where the pilot reports from. Kutaisi
    # runway 25 -> Entry East, even for a pilot inbound "35 miles north".
    reply = brain.handle("Control, Colt 1, inbound 35 miles north",
                         controller=Controller.CONTROL)
    assert "Entry East" in reply
    assert brain.pilots["Colt 1"].entry_gate == "Entry East"


def test_control_inbound_same_entry_from_other_directions(brain):
    # A pilot reporting from the west still gets the straight-in Entry East.
    reply = brain.handle("Control, Colt 1, inbound 20 miles west",
                         controller=Controller.CONTROL)
    assert "Entry East" in reply


def test_best_entry_gate_follows_runway(airfield):
    # The best entry is on the approach side: 25 -> East, 07 -> West.
    assert airfield.best_entry_gate("25") == "East"
    assert airfield.best_entry_gate("07") == "West"


def test_control_departure_checkin_radar_contact(brain):
    # departure check-in gets radar contact, not a join-via-entry routing
    reply = brain.handle("Control, Colt 1, airborne, 5 miles east climbing",
                         controller=Controller.CONTROL)
    assert "radar contact" in reply
    assert "join via" not in reply
    assert brain.pilots["Colt 1"].phase == Phase.AIRBORNE


def test_control_hands_to_tower_on_final(brain):
    reply = brain.handle("Control, Colt 1, on final", controller=Controller.CONTROL)
    assert "contact Tower" in reply
    assert "channel 7" in reply


def test_control_hands_to_tower_overhead(brain):
    reply = brain.handle("Control, Colt 1, overhead break runway 25",
                         controller=Controller.CONTROL)
    assert "contact Tower" in reply


def test_say_again_uses_correct_agency(brain):
    assert "Ground" in brain.handle("Ground, Colt 1, banana",
                                    controller=Controller.GROUND)
    assert "Control" in brain.handle("Control, Colt 1, banana",
                                     controller=Controller.CONTROL)
    assert "Tower" in brain.handle("Tower, Colt 1, banana",
                                   controller=Controller.TOWER)


def test_help_without_flight_number(brain):
    reply = brain.handle("Trainer, Colt, help", controller=Controller.CONTROL)
    assert reply and "Apollo suggests" in reply


def test_speaker_callsign_mapping(brain):
    brain.remember_speaker("Caveman", "Colt 1")
    assert brain.callsign_for_speaker("Caveman") == "Colt 1"
    assert brain.callsign_for_speaker("caveman") == "Colt 1"
    assert brain.callsign_for_speaker("Unknown") == "Unknown"


def test_sync_speakers_seeds_from_a_slot_named_unit(brain):
    """A mission slot's unit name *is* its callsign, so seed from it.

    Seeding means a pilot is known *before* they transmit, and the slot name
    ("Chevy11") is canonicalised to the brain's key ("Chevy 1").
    """
    brain.sync_speakers([("Caveman", "Chevy11")])
    assert brain.callsign_for_speaker("Caveman") == "Chevy 1"
    assert brain.known_callsigns(["Caveman"]) == ["Chevy 1"]


def test_sync_speakers_ignores_a_unit_name_that_is_not_a_callsign(brain):
    """A dynamically spawned slot's unit name must never become a callsign.

    DCS names a dynamic spawn after the airbase, type and index
    ("Kutaisi_F/A-18C Lot 20_2- 1"), not after the flight's callsign. Seeding
    that verbatim put the unit name on the air ("Kutaisi_F/A-18C Lot 20_2- 1,
    Ground, ..."). The pilot stays unseeded until they say who they are.
    """
    brain.sync_speakers([("Caveman", "Kutaisi_F/A-18C Lot 20_2- 1")])
    assert brain.known_callsigns(["Caveman"]) == []
    assert brain.callsign_for_speaker("Caveman") == "Caveman"
    # ...and the bogus name is not learned as a flight name either.
    assert not any("Kutaisi" in name for name in brain.callsigns.names)


def test_known_callsigns_reports_only_pilots_we_can_name(brain):
    """A pilot bound by radio counts too, so both paths feed the heuristic."""
    brain.sync_speakers([("Caveman", "Chevy11"),          # slot-shaped name
                         ("Maverick", "Kutaisi_F/A-18C Lot 20_2- 1")])
    brain.remember_speaker("Maverick", "Ford 1")           # bound by radio
    assert brain.known_callsigns(["Caveman", "Maverick", "Ghost"]) == \
        ["Chevy 1", "Ford 1"]


def test_sync_speakers_binds_an_unseeded_pilot_from_what_they_say(brain):
    """A dynamic-slot pilot is identified from their own transmission.

    The unit name carries no callsign, but the pilot says one, so the normal
    learn path still binds them (keyed by their SRS name).
    """
    brain.sync_speakers([("Caveman", "Kutaisi_F/A-18C Lot 20_2- 1")])
    brain.remember_speaker("Caveman", "Ford 1")
    assert brain.callsign_for_speaker("Caveman") == "Ford 1"


def test_sync_speakers_keeps_a_radio_learned_binding_while_online(brain):
    """The 2-second poll must not wipe what the pilot said over the radio.

    Regression: the poll re-derives only slot-shaped callsigns, so a pilot on a
    dynamically spawned slot (unit name carries no callsign) would lose their
    binding on the very next poll. On the radio that looked like the tower
    addressing the raw unit name ("Caveman, Tower, you are on the runway…")
    instead of the callsign they had been using all session.
    """
    pairs = [("Caveman", "Kutaisi_F/A-18C Lot 20_2- 1")]
    brain.sync_speakers(pairs)
    brain.remember_speaker("Caveman", "Ford 1")    # the pilot checks in
    for _ in range(3):                             # ...then the poll runs again
        brain.sync_speakers(pairs)
    assert brain.callsign_for_speaker("Caveman") == "Ford 1"
    assert brain.known_callsigns(["Caveman"]) == ["Ford 1"]


def test_sync_speakers_drops_the_seed_when_the_slot_goes_away(brain):
    """A slot-derived seed is tied to the slot, so leaving clears it.

    Only the poll's own seeds are pruned: they can be re-derived on the next
    poll, so a departed slot must not keep a stale callsign recognised.
    """
    brain.sync_speakers([("Caveman", "Chevy11")])
    assert brain.callsign_for_speaker("Caveman") == "Chevy 1"
    brain.sync_speakers([("Maverick", "Chevy12")])   # Caveman's slot gone
    assert brain.callsign_for_speaker("Caveman") == "Caveman"


def test_radio_cannot_steal_a_live_slot_callsign(brain):
    """A DCS slot is the seat for this spawn; STT cannot jump the pilot.

    Changing flight requires a respawn in another slot, then a check-in.
    """
    brain.sync_speakers([("Caveman", "Chevy11")])
    assert brain.callsign_for_speaker("Caveman") == "Chevy 1"
    brain.remember_speaker("Caveman", "Ford 1")
    brain.sync_speakers([("Caveman", "Chevy11")])
    assert brain.callsign_for_speaker("Caveman") == "Chevy 1"
    assert brain.callsigns.extract("Chevy one, request taxi") == "Chevy 1"


def test_stt_over_to_does_not_become_adder_two(brain):
    """Regression: 'over to' fuzzy-matched Adder + number-word 'to' = 2."""
    brain.sync_speakers([("Caveman", "Adder11")])
    assert brain.extract_callsign("over to", "Caveman") == "Adder 1"
    extracted = brain.extract_callsign("over to", "Caveman")
    brain.remember_speaker("Caveman", extracted)
    assert brain.callsign_for_speaker("Caveman") == "Adder 1"
    brain.handle("Climbing to Angels 15, over to", speaker="Caveman",
                 controller=Controller.CONTROL)
    assert "Adder 2" not in brain.pilots


def test_respawn_in_another_flight_reseeds_callsign(brain):
    """A new DCS unit name is a new spawn; the previous seat is released."""
    brain.sync_speakers([("Caveman", "Adder11")])
    assert brain.callsign_for_speaker("Caveman") == "Adder 1"
    brain.remember_speaker("Caveman", "Ford 1")
    brain.sync_speakers([("Caveman", "Adder21")])
    assert brain.callsign_for_speaker("Caveman") == "Adder 2"


def test_radio_learned_binding_is_dropped_when_the_player_leaves(brain):
    """Leaving the server ends the spawn, including a dynamic-slot check-in."""
    brain.sync_speakers([("Caveman", "Kutaisi_F/A-18C Lot 20_2- 1")])
    brain.remember_speaker("Caveman", "Ford 1")
    brain.sync_speakers([])
    assert brain.callsign_for_speaker("Caveman") == "Caveman"


def test_a_wiped_identity_would_warn_an_empty_uncleared_record(brain):
    """Why losing the binding put the raw name on the air, not just the name.

    If the monitor's `callsign_for_speaker` falls back to the player name, it
    reads a *different*, empty `PilotState` — so checks consult a pilot who was
    never cleared and warn wrongly ("Caveman, Tower, you are on the runway
    without clearance"), even though "Ford 1" was cleared for takeoff.
    """
    brain.handle("Ford one, ready to copy clearance", speaker="Caveman",
                 controller=Controller.GROUND)
    brain.handle("Ford one, requesting taxi", speaker="Caveman",
                 controller=Controller.GROUND)
    brain.handle("Tower, Ford one at runway 07, ready for departure",
                 speaker="Caveman", controller=Controller.TOWER)
    assert brain.pilots["Ford 1"].phase is not Phase.IDLE
    # The cleared pilot is (correctly) not an incursion...
    assert brain.check_incursion("Ford 1", True) is None
    # ...but the raw player name is a different, uncleared pilot. The warning
    # needs a few consecutive on-runway polls (debounce), so call repeatedly.
    for _ in range(INCURSION_POLLS):
        warned = brain.check_incursion("Caveman", True)
    assert warned is not None


def test_sync_speakers_keeps_duplicate_callsigns_distinct(brain):
    """Two players in identically-named slots must not share one state.

    The map is keyed by player name, so each pilot resolves to their own
    callsign even when the flight names collide.
    """
    brain.sync_speakers([("Caveman", "Chevy11"), ("Maverick", "Chevy12")])
    assert brain.callsign_for_speaker("Caveman") == "Chevy 1"
    assert brain.callsign_for_speaker("Maverick") == "Chevy 1-2"


def test_sync_speakers_drops_departed_players(brain):
    brain.sync_speakers([("Caveman", "Chevy11"), ("Maverick", "Chevy12")])
    brain.sync_speakers([("Maverick", "Chevy12")])  # Caveman left
    assert brain.callsign_for_speaker("Caveman") == "Caveman"
    assert brain.callsign_for_speaker("Maverick") == "Chevy 1-2"
    assert brain.known_callsigns(["Caveman", "Maverick"]) == ["Chevy 1-2"]


def test_content_flight_name_does_not_hijack_transmission(brain):
    # "Climbing to Ford 15" is heading 150 for the *sender* (Springfield 1),
    # not flight Ford. The speaker's own callsign wins.
    brain.remember_speaker("Caveman", "Springfield 1")
    brain.handle("Springfield 1, at 1500 ft", controller=Controller.CONTROL,
                 speaker="Caveman")
    brain.handle("Climbing to Ford 15, Springfield 1",
                 controller=Controller.CONTROL, speaker="Caveman")
    assert "Springfield 1" in brain.pilots
    assert not any(k.startswith("Ford") for k in brain.pilots)


def test_comma_separated_callsign_is_attributed(brain):
    # "Ground, Springfield, 11" must extract (comma), so it is addressed to the
    # right flight instead of becoming an un-attributable ghost track.
    reply = brain.handle("Ground, Springfield, 11", controller=Controller.GROUND,
                         speaker="Caveman")
    assert reply is not None and "Springfield 1" in reply
    assert "Springfield 1" in brain.pilots


def test_known_speaker_callsign_used_when_stt_drops_it(brain):
    # Once we know who is on the radio (SRS name -> callsign), a transmission
    # that does not name the callsign is still attributed to them — STT often
    # drops it, and we should not make the pilot repeat just to satisfy STT.
    brain.remember_speaker("Caveman", "Colt 1")
    reply = brain.handle("requesting taxi", controller=Controller.GROUND,
                         speaker="Caveman")
    assert reply is not None and "Colt 1" in reply
    assert "Colt 1" in brain.pilots


def test_unknown_speaker_still_needs_a_callsign(brain):
    # A speaker we have never identified is not guessed at (multiplayer): the
    # call is ignored rather than attributed to the wrong flight.
    brain.set_pilot_count(3)
    assert brain.handle("requesting taxi", controller=Controller.GROUND,
                        speaker="Stranger") is None


def test_arrival_ready_for_departure_is_guided_not_cleared(brain):
    # An arrival never asks for departure: guide them, never clear takeoff.
    brain.handle("Control, Colt 1, inbound 30 miles south",
                 controller=Controller.CONTROL)
    assert brain.pilots["Colt 1"].phase == Phase.INBOUND
    reply = brain.handle("Tower, Colt 1, ready for departure",
                         controller=Controller.TOWER)
    assert "negative" in reply.lower()
    assert "takeoff" not in reply.lower()
    assert brain.pilots["Colt 1"].phase == Phase.LANDING


# ---------- Shared / fallback ----------

def test_unknown_request_says_again(brain):
    reply = brain.handle("Tower, Colt 1, banana banana",
                         controller=Controller.TOWER)
    assert "say again" in reply


def test_no_callsign_returns_none(brain):
    assert brain.handle("requesting taxi", controller=Controller.GROUND) is None


def test_roger_on_readback(brain):
    reply = brain.handle("Tower, Colt 1, wilco", controller=Controller.TOWER)
    assert "roger" in reply.lower()


def test_frequency_change_push_is_silent(brain):
    # The kneeboard handoff ack "Colt 1, channel 8, push" switches frequency:
    # the bot stays silent (no reply that would step on the next call).
    reply = brain.handle("Colt 1, channel 8, push", controller=Controller.TOWER)
    assert reply == ""


def test_flight_checkin_bare_callsign_is_silent(brain):
    # Master Arms SOP: after switching frequency the flight checks in *as a
    # flight* ("Colt 1") before addressing the agency. It is a flight-internal
    # call, so the controller stays silent — it must NOT be answered as an
    # agency contact (and must not get "say again").
    assert brain.handle("Colt 1", controller=Controller.TOWER) == ""
    assert brain.handle("Colt one", controller=Controller.GROUND) == ""


def test_flight_checkin_wingman_number_is_silent(brain):
    # The wingman answers the lead's flight check-in with their number: "2" for
    # a 2-ship, "3"/"4" for a 3/4-ship, and so on. A bare element number is
    # never a valid agency call, so it is silent too — in digits or words.
    for element in ("2", "3", "4", "5", "two", "three", "four"):
        assert brain.handle(element, controller=Controller.TOWER) == "", element
    # "1" is the lead, who says the flight callsign instead — not an element, so
    # it is not silently accepted (multiplayer: no callsign -> no reply).
    assert brain.handle("1", controller=Controller.TOWER) != ""


def test_agency_checkin_still_answered(brain):
    # "Ground, Colt 1" is the *agency* check-in (not the flight check-in): it
    # names the agency, so it is answered normally.
    reply = brain.handle("Ground, Colt 1", controller=Controller.GROUND)
    assert reply and "Ground" in reply


def test_agency_checkin_not_swallowed_by_agency_named_speaker(brain):
    # A player whose SRS name is literally "Ground" must not have the agency
    # stripped out of their call: "Ground, Colt 1" is still the agency check-in.
    reply = brain.handle("Ground, Colt 1", controller=Controller.GROUND,
                         speaker="Ground")
    assert reply and "Ground" in reply


def test_flight_checkin_does_not_swallow_requests(brain):
    # A call that carries a request is not a flight check-in.
    reply = brain.handle("Colt 1, requesting taxi", controller=Controller.GROUND)
    assert "taxi" in reply.lower()


def test_solo_mode_prompts_on_unknown_call(brain):
    # Single-pilot trainer: an unrecognized call (e.g. a forgotten callsign)
    # gets a spoken prompt instead of silence. The pilot is known to be online
    # (the state bridge names them), so there is a callsign to ask for.
    brain.set_pilot_count(1)
    brain.set_active_pilots(["Colt 1"])
    reply = brain.handle("requesting taxi", controller=Controller.GROUND)
    assert reply and "say again" in reply.lower()


def test_solo_mode_stays_silent_when_no_pilot_is_known(brain):
    # A dynamic slot has no callsign to learn, so prompting "say again" would
    # repeat forever. Stay silent until the pilot signs in successfully.
    brain.set_pilot_count(1)
    brain.set_active_pilots([])
    assert brain.handle("requesting taxi", controller=Controller.GROUND) is None


def test_multiplayer_stays_silent_on_unknown_call(brain):
    # With other traffic online, we do not answer calls we cannot tie to a pilot.
    brain.set_pilot_count(3)
    assert brain.handle("requesting taxi", controller=Controller.GROUND) is None


def test_radio_check(brain):
    reply = brain.handle("Tower, Colt 1, radio check", controller=Controller.TOWER)
    assert "loud and clear" in reply.lower()


def test_radio_check_on_any_agency(brain):
    for controller in (Controller.GROUND, Controller.TOWER, Controller.CONTROL):
        reply = brain.handle("Colt 1, radio check", controller=controller)
        assert "loud and clear" in reply.lower()


# ---------- Per-pilot isolation ----------

def test_two_pilots_have_independent_state(brain):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    brain.handle("Ground, Ford 2, ready to copy clearance",
                 controller=Controller.GROUND)
    assert brain.pilots["Colt 1"].phase == Phase.TAXI
    assert brain.pilots["Ford 2"].phase == Phase.CLEARANCE


# ---------- Runway / go-around ----------

def test_go_around_fires_once(brain):
    pilot = _airborne(brain, phase=Phase.LANDING)
    pilot.cleared_landing = True
    pilot.pattern_flown = True
    pilot.landing_handoff_announced = True
    first = brain.check_final("Colt 1", on_final=True, runway_occupied=True)
    second = brain.check_final("Colt 1", on_final=True, runway_occupied=True)
    assert "go around" in first
    assert second is None  # only once per approach
    assert pilot.phase == Phase.INBOUND
    assert not pilot.cleared_landing
    assert not pilot.pattern_flown
    assert not pilot.landing_handoff_announced


def test_go_around_resets_when_not_on_final(brain):
    brain.check_final("Colt 1", on_final=True, runway_occupied=True)
    brain.check_final("Colt 1", on_final=False, runway_occupied=False)
    again = brain.check_final("Colt 1", on_final=True, runway_occupied=True)
    assert "go around" in again


def test_go_around_names_traffic_when_unit_on_runway(brain, airfield):
    # A tracked unit actually on the runway -> the go-around says "traffic".
    thr = airfield.runway_threshold("25")
    blocker = _Unit("AI-1", thr[0], thr[1], airfield.elevation_ft + 10)
    reply = brain.check_final("Colt 1", on_final=True, runway_occupied=True,
                              traffic=[blocker])
    assert "go around" in reply.lower()
    assert "traffic" in reply.lower()


def test_go_around_generic_when_no_traffic_list(brain):
    # No traffic list (unknown) -> the generic "runway is occupied" wording.
    reply = brain.check_final("Colt 1", on_final=True, runway_occupied=True,
                              traffic=[])
    assert "go around" in reply.lower()
    assert "occupied" in reply.lower()


def test_go_around_generic_when_traffic_not_on_runway(brain, airfield):
    # The blocker sits on the parallel taxiway (outside the narrow corridor),
    # yet sequencing said occupied (e.g. a hold-short edge case): the call must
    # not claim "traffic on the runway" it cannot see.
    thr = airfield.runway_threshold("25")
    hdg = airfield.runway_heading("25")
    import math
    perp = math.radians((hdg + 90) % 360)
    lat = thr[0] + math.degrees(0.1 * 1852 * math.cos(perp) / 6_371_000)
    lon = thr[1] + math.degrees(0.1 * 1852 * math.sin(perp)
                                / (6_371_000 * math.cos(math.radians(thr[0]))))
    taxi_unit = _Unit("AI-1", lat, lon, airfield.elevation_ft + 10)
    reply = brain.check_final("Colt 1", on_final=True, runway_occupied=True,
                              traffic=[taxi_unit])
    assert "go around" in reply.lower()
    assert "occupied" in reply.lower()


def test_pilot_reported_go_around_rearms_arrival_and_acknowledges(brain):
    pilot = _airborne(brain, phase=Phase.LANDING)
    pilot.cleared_landing = True
    pilot.pattern_flown = True
    reply = brain.handle("Tower, Colt 1, going around", controller=Controller.TOWER)
    assert "continue climbing" in reply.lower()
    assert "downwind" in reply.lower()
    assert pilot.phase == Phase.INBOUND
    assert not pilot.cleared_landing
    assert not pilot.pattern_flown
    assert pilot.go_around_issued
    assert not pilot.landing_handoff_announced


def test_touch_and_go_request_is_cleared_and_rearms_next_circuit(brain):
    pilot = _airborne(brain, phase=Phase.LANDING)
    pilot.cleared_landing = True
    pilot.pattern_flown = True
    pilot.landing_handoff_announced = True
    reply = brain.handle("Tower, Colt 1, request touch and go",
                         controller=Controller.TOWER)
    assert "approved touch and go" in reply.lower()
    assert pilot.phase == Phase.INBOUND
    assert not pilot.cleared_landing
    assert not pilot.pattern_flown
    assert not pilot.landing_handoff_announced


def test_touch_and_go_readback_is_silent(brain):
    pilot = _airborne(brain, phase=Phase.INBOUND)
    pilot.cleared_landing = False
    reply = brain.handle("Colt 1, approved touch and go runway 25",
                         controller=Controller.TOWER)
    assert reply == ""
    assert pilot.phase == Phase.INBOUND


# ---------- Traffic sequencing (players + AI) ----------

class _Unit:
    def __init__(self, callsign, lat, lon, alt_ft):
        self.callsign = callsign
        self.lat = lat
        self.lon = lon
        self.alt_ft = alt_ft


def test_line_up_held_when_runway_occupied(brain, airfield):
    thr = airfield.runway_threshold("25")
    blocker = _Unit("AI-1", thr[0], thr[1], airfield.elevation_ft + 10)
    at_rwy = _track(airfield, thr[0], thr[1])
    reply = brain.handle("Tower, Colt 1, ready for departure", track=at_rwy,
                         controller=Controller.TOWER, traffic=[blocker])
    assert "hold short" in reply.lower()
    assert brain.pilots["Colt 1"].phase == Phase.HOLDING


def test_line_up_cleared_when_runway_clear(brain, airfield):
    thr = airfield.runway_threshold("25")
    at_rwy = _track(airfield, thr[0], thr[1])
    reply = brain.handle("Tower, Colt 1, ready for departure", track=at_rwy,
                         controller=Controller.TOWER, traffic=[])
    assert "line up and wait" in reply


def test_landing_sequenced_when_runway_occupied(brain, airfield):
    import math
    thr = airfield.runway_threshold("25")
    hdg = airfield.runway_heading("25")
    back = math.radians((hdg + 180) % 360)
    lat = thr[0] + math.degrees(5 * 1852 * math.cos(back) / 6_371_000)
    lon = thr[1] + math.degrees(5 * 1852 * math.sin(back)
                                / (6_371_000 * math.cos(math.radians(thr[0]))))
    on_final = _track(airfield, lat, lon, heading=hdg)
    blocker = _Unit("AI-1", thr[0], thr[1], airfield.elevation_ft + 10)
    reply = brain.handle("Tower, Colt 1, on final", track=on_final,
                         controller=Controller.TOWER, traffic=[blocker])
    assert "continue approach" in reply.lower()
    assert brain.pilots["Colt 1"].phase != Phase.LANDING


def test_landing_cleared_when_runway_clear(brain, airfield):
    import math
    thr = airfield.runway_threshold("25")
    hdg = airfield.runway_heading("25")
    back = math.radians((hdg + 180) % 360)
    lat = thr[0] + math.degrees(5 * 1852 * math.cos(back) / 6_371_000)
    lon = thr[1] + math.degrees(5 * 1852 * math.sin(back)
                                / (6_371_000 * math.cos(math.radians(thr[0]))))
    on_final = _track(airfield, lat, lon, heading=hdg)
    reply = brain.handle("Tower, Colt 1, on final", track=on_final,
                         controller=Controller.TOWER, traffic=[])
    assert "cleared to land" in reply


def test_holding_short_does_not_block_another_lineup(brain, airfield):
    # a pilot holding short (just before the threshold) must NOT count as
    # occupying the runway and block another aircraft's line-up.
    import math
    thr = airfield.runway_threshold("25")
    hdg = airfield.runway_heading("25")
    back = math.radians((hdg + 180) % 360)
    # ~0.1 NM before the threshold = holding short position
    lat = thr[0] + math.degrees(0.1 * 1852 * math.cos(back) / 6_371_000)
    lon = thr[1] + math.degrees(0.1 * 1852 * math.sin(back)
                                / (6_371_000 * math.cos(math.radians(thr[0]))))
    holder = _Unit("Ford-2-1", lat, lon, airfield.elevation_ft + 10)
    at_rwy = _track(airfield, thr[0], thr[1])
    reply = brain.handle("Tower, Colt 1, ready for departure", track=at_rwy,
                         controller=Controller.TOWER, traffic=[holder])
    assert "line up and wait" in reply


def test_aircraft_on_runway_still_blocks_lineup(brain, airfield):
    # an aircraft actually on the runway (past the threshold) still blocks
    import math
    thr = airfield.runway_threshold("25")
    hdg = airfield.runway_heading("25")
    fwd = math.radians(hdg)
    lat = thr[0] + math.degrees(0.3 * 1852 * math.cos(fwd) / 6_371_000)
    lon = thr[1] + math.degrees(0.3 * 1852 * math.sin(fwd)
                                / (6_371_000 * math.cos(math.radians(thr[0]))))
    on_rwy = _Unit("AI-1", lat, lon, airfield.elevation_ft + 10)
    at_rwy = _track(airfield, thr[0], thr[1])
    reply = brain.handle("Tower, Colt 1, ready for departure", track=at_rwy,
                         controller=Controller.TOWER, traffic=[on_rwy])
    assert "hold short" in reply.lower()


def test_challenge_asks_for_position_confirmation(brain, airfield):
    # A challenged report stays in the flight flow and asks the pilot to
    # confirm their position, without introducing reset/cancel instructions.
    ramp = _track(airfield, *airfield.parking_areas["Ramp West"])
    reply = brain.handle("Tower, Colt 1, ready for departure",
                         track=ramp, controller=Controller.TOWER)
    assert "negative" in reply.lower()
    assert "confirm your position" in reply.lower()
    assert "reset" not in reply.lower() and "cancel" not in reply.lower()


def test_control_inbound_with_angels_is_not_departure(brain):
    # "inbound ... at Angels 12" must be read as an arrival, not a departure
    # check-in (which would wrongly answer "climb to Angels 15").
    reply = brain.handle("Kutaisi Control, Colt 1, inbound 35 miles north at Angels 12",
                         controller=Controller.CONTROL)
    assert "join via" in reply
    assert "climb" not in reply.lower()
    assert brain.pilots["Colt 1"].phase == Phase.INBOUND


def test_control_checking_in_is_departure(brain):
    reply = brain.handle("Control, Colt 1, checking in",
                         controller=Controller.CONTROL)
    assert "radar contact" in reply
    assert brain.pilots["Colt 1"].phase == Phase.AIRBORNE


# ---------- Wind / runway ----------

def test_set_wind_and_runway(brain):
    # DCS reports m/s; Tower/Ground speak knots (5 m/s → 10 kt).
    brain.set_wind(270, 5)
    assert brain.wind == "270, 10 knots"  # MA SOP form
    brain.set_wind(0, 0)
    assert brain.wind == "calm"
    brain.set_runway("07")
    assert brain.runway == "07"


# ---------- Help (trainer aid) ----------

def test_help_idle_points_to_ground(brain):
    reply = brain.handle("Trainer, Colt 1, help", controller=Controller.TOWER)
    assert "Ground" in reply
    assert "clearance" in reply.lower()


def test_help_after_taxi_points_to_hold_short(brain):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    reply = brain.handle("Trainer, Colt 1, help", controller=Controller.GROUND)
    assert "holding short" in reply.lower()


def test_help_holding_points_to_tower(brain):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    brain.handle("Colt 1, holding short runway 25", controller=Controller.GROUND)
    reply = brain.handle("Trainer, Colt 1, help", controller=Controller.GROUND)
    assert "Tower" in reply
    assert "ready for departure" in reply.lower()


def test_help_works_on_any_controller(brain):
    # help is a shared intent, valid regardless of which frequency it arrives on
    for controller in (Controller.GROUND, Controller.TOWER, Controller.CONTROL):
        reply = brain.handle("Trainer, Colt 1, help", controller=controller)
        assert reply and "Colt 1" in reply


def test_escape_hatches_work_on_every_agency_and_phase(brain):
    # help / reset / cancel must work on ALL agencies in ALL states — a pilot
    # must never be stuck because they are on the "wrong" frequency or phase.
    from brain import PilotState
    for controller in (Controller.GROUND, Controller.TOWER, Controller.CONTROL):
        for phase in Phase:
            brain.pilots.clear()
            brain.pilots["Colt 1"] = PilotState(callsign="Colt 1", phase=phase)
            help_reply = brain.handle("Trainer, Colt 1, help", controller=controller)
            reset_reply = brain.handle("Colt 1 reset", controller=controller)
            cancel_reply = brain.handle("Colt 1 cancel", controller=controller)
            assert help_reply and "Apollo suggests" in help_reply, (controller, phase)
            assert reset_reply and "state reset" in reset_reply, (controller, phase)
            assert cancel_reply and "cancelled" in cancel_reply, (controller, phase)


def test_help_requires_callsign(brain):
    assert brain.handle("Trainer, help me please", controller=Controller.TOWER) is None


# ---------- Trainer commands (per-pilot, any frequency) ----------

def test_trainer_echo_on_off_toggles_per_pilot(brain):
    reply = brain.handle("Trainer, Colt 1, echo on", controller=Controller.GROUND)
    assert reply and "echo on" in reply
    assert brain.mirror_enabled("Colt 1") is True
    # A different pilot is unaffected (per-pilot state).
    assert brain.mirror_enabled("Ford 2") is False
    reply = brain.handle("Trainer, Colt 1, echo off", controller=Controller.TOWER)
    assert reply and "echo off" in reply
    assert brain.mirror_enabled("Colt 1") is False


def test_trainer_echo_without_callsign_uses_speaker(brain):
    # "Trainer, echo on" with no callsign: the known speaker is used.
    brain.remember_speaker("Caveman", "Colt 1")
    reply = brain.handle("Trainer, echo on", controller=Controller.TOWER,
                         speaker="Caveman")
    assert reply and "echo on" in reply
    assert brain.mirror_enabled("Colt 1") is True


def test_trainer_works_on_every_agency_and_phase(brain):
    from brain import PilotState
    for controller in (Controller.GROUND, Controller.TOWER, Controller.CONTROL):
        for phase in Phase:
            brain.pilots.clear()
            brain.pilots["Colt 1"] = PilotState(callsign="Colt 1", phase=phase)
            reply = brain.handle("Trainer, Colt 1, echo on", controller=controller)
            assert reply and "echo on" in reply, (controller, phase)
            assert brain.mirror_enabled("Colt 1") is True, (controller, phase)


def test_trainer_unknown_command_hints(brain):
    reply = brain.handle("Trainer, Colt 1, do something", controller=Controller.TOWER)
    assert reply and "trainer commands" in reply
    assert brain.mirror_enabled("Colt 1") is False


def test_trainer_volume_sets_and_clamps(brain):
    assert brain.tx_volume is None  # default: use --tx-volume
    reply = brain.handle("Trainer, Colt 1, volume 0.3", controller=Controller.TOWER)
    assert reply and "0.30" in reply
    assert brain.tx_volume == 0.3
    # Out-of-range values are clamped to 0..1.
    brain.handle("Trainer, Colt 1, volume 2", controller=Controller.TOWER)
    assert brain.tx_volume == 1.0
    brain.handle("Trainer, Colt 1, volume 0", controller=Controller.TOWER)
    assert brain.tx_volume == 0.0


def test_trainer_volume_without_a_number_hints(brain):
    reply = brain.handle("Trainer, Colt 1, volume", controller=Controller.TOWER)
    assert reply and "trainer commands" in reply
    assert brain.tx_volume is None


def test_trainer_does_not_change_phase(brain):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    phase = brain.pilots["Colt 1"].phase
    brain.handle("Trainer, Colt 1, echo on", controller=Controller.GROUND)
    assert brain.pilots["Colt 1"].phase == phase


def test_trainer_mode_switches_globally(brain):
    # Mode is GLOBAL (one bot persona), not per pilot: a different flight is
    # affected by Colt 1's mode command.
    reply = brain.handle("Trainer, Colt 1, casual mode", controller=Controller.TOWER)
    assert reply and "casual mode" in reply
    assert brain.mode == "casual"
    brain.handle("Trainer, Ford 2, teaching mode", controller=Controller.GROUND)
    assert brain.mode == "coach"


def test_trainer_mode_is_the_default(brain):
    # Coach is the MasterArms teaching persona; no command needed at start.
    assert brain.mode == "coach"


def test_trainer_mode_without_a_word_hints(brain):
    # "trainer, mode" with no coach/casual in it must not change anything and
    # must give the usage hint.
    reply = brain.handle("Trainer, Colt 1, mode", controller=Controller.TOWER)
    assert reply and "trainer commands" in reply
    assert brain.mode == "coach"


def test_trainer_mode_does_not_change_phase(brain):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    phase = brain.pilots["Colt 1"].phase
    brain.handle("Trainer, Colt 1, casual mode", controller=Controller.GROUND)
    assert brain.pilots["Colt 1"].phase == phase


def test_trainer_mode_checks_readback_values_in_casual_too():
    # Semantic standards do not change with the mode: a missing readback item
    # is flagged in CASUAL exactly as in COACH. (Wording coaching has no
    # mechanism yet; when it lands, casual must silence it but not this.)
    from brain import AtcBrain
    b = AtcBrain()
    b.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    clearance = b.pilots["Colt 1"].last_clearance or b.pilots["Colt 1"].last_reply
    b.mode = "casual"
    # A readback omitting the clearance's numbers is still incomplete.
    reply = b.handle("Colt 1, roger, hold short", controller=Controller.GROUND)
    assert reply and "readback incomplete" in reply.lower(), (clearance, reply)


def test_trainer_mode_works_on_every_agency(brain):
    from brain import PilotState
    for controller in (Controller.GROUND, Controller.TOWER, Controller.CONTROL):
        for mode_word, expected in (("casual mode", "casual"),
                                    ("coach mode", "coach")):
            brain.pilots.clear()
            brain.pilots["Colt 1"] = PilotState(callsign="Colt 1")
            reply = brain.handle(f"Trainer, Colt 1, {mode_word}",
                                 controller=controller)
            assert reply and expected in reply, (controller, mode_word, reply)
            assert brain.mode == expected, (controller, mode_word)


# ---------- Escape hatches (never get stuck in a wrong state) ----------

def test_reset_returns_to_idle(brain):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    brain.handle("Tower, Colt 1, ready for departure", controller=Controller.TOWER)
    assert brain.pilots["Colt 1"].phase == Phase.LINEUP
    reply = brain.handle("Colt 1, reset", controller=Controller.TOWER)
    assert "state reset" in reply
    assert brain.pilots["Colt 1"].phase == Phase.IDLE
    assert brain.pilots["Colt 1"].exit_gate == ""


def test_cancel_steps_back_one_phase(brain):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    brain.handle("Tower, Colt 1, ready for departure", controller=Controller.TOWER)
    assert brain.pilots["Colt 1"].phase == Phase.LINEUP
    reply = brain.handle("Colt 1, cancel", controller=Controller.TOWER)
    assert "cancelled" in reply
    assert brain.pilots["Colt 1"].phase == Phase.HOLDING


def test_abort_is_accepted_as_synonym(brain):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    brain.handle("Tower, Colt 1, ready for departure", controller=Controller.TOWER)
    reply = brain.handle("Colt 1, abort", controller=Controller.TOWER)
    assert "cancelled" in reply
    assert brain.pilots["Colt 1"].phase == Phase.HOLDING


def test_cancel_from_landing_goes_around(brain):
    brain.handle("Tower, Colt 1, on final", controller=Controller.TOWER)
    assert brain.pilots["Colt 1"].phase == Phase.LANDING
    brain.handle("Colt 1, cancel", controller=Controller.TOWER)
    assert brain.pilots["Colt 1"].phase == Phase.INBOUND


def test_say_again_replays_last_clearance(brain):
    first = brain.handle("Ground, Colt 1, requesting taxi",
                         controller=Controller.GROUND)
    reply = brain.handle("Colt 1, say again", controller=Controller.GROUND)
    assert reply == first


def test_say_again_without_history(brain):
    reply = brain.handle("Colt 1, say again", controller=Controller.TOWER)
    assert "say again" in reply


def test_never_mind_stays_silent_without_cancelling_clearance(brain):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    pilot = brain.pilots["Colt 1"]
    reply = brain.handle("Colt 1, never mind", controller=Controller.GROUND)
    assert reply == ""
    assert pilot.phase == Phase.TAXI


def test_help_mentions_escape_hatches(brain):
    reply = brain.handle("Trainer, Colt 1, help", controller=Controller.TOWER)
    assert "reset" in reply.lower()
    assert "cancel" in reply.lower()


# ---------- Position cross-check (trainer: challenge bad reports) ----------

def test_holding_short_challenged_when_on_ramp(brain, airfield):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    # pilot is still on the ramp (far from the runway) but claims holding short
    ramp = _track(airfield, *airfield.parking_areas["Ramp West"])
    reply = brain.handle("Colt 1, holding short runway 25",
                         track=ramp, controller=Controller.GROUND)
    assert "negative" in reply.lower()
    assert "confirm" in reply.lower()
    # state must NOT advance to HOLDING
    assert brain.pilots["Colt 1"].phase == Phase.TAXI


def test_holding_short_accepted_when_at_runway(brain, airfield):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    thr = airfield.runway_threshold("25")
    at_rwy = _track(airfield, thr[0], thr[1])
    reply = brain.handle("Colt 1, holding short runway 25",
                         track=at_rwy, controller=Controller.GROUND)
    assert "contact" in reply.lower()
    assert brain.pilots["Colt 1"].phase == Phase.HOLDING


def test_holding_short_hands_to_tower(brain, airfield):
    # The Master Arms kneeboard hands the pilot to Tower without naming the
    # holding position ("contact Tower on channel 7").
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    # sit at Holding C (P1) and report holding short
    p1 = airfield.holding_points["Holding C"]
    at_p1 = _track(airfield, p1[0], p1[1])
    reply = brain.handle("Colt 1, holding short runway 25",
                         track=at_p1, controller=Controller.GROUND)
    assert "contact Tower" in reply
    assert brain.pilots["Colt 1"].phase == Phase.HOLDING


def test_ready_for_departure_challenged_when_not_at_runway(brain, airfield):
    ramp = _track(airfield, *airfield.parking_areas["Ramp West"])
    reply = brain.handle("Tower, Colt 1, ready for departure",
                         track=ramp, controller=Controller.TOWER)
    assert "negative" in reply.lower()
    assert brain.pilots["Colt 1"].phase != Phase.TAKEOFF


def test_ready_for_departure_accepted_at_runway(brain, airfield):
    thr = airfield.runway_threshold("25")
    at_rwy = _track(airfield, thr[0], thr[1])
    reply = brain.handle("Tower, Colt 1, ready for departure",
                         track=at_rwy, controller=Controller.TOWER)
    assert "line up and wait" in reply
    assert brain.pilots["Colt 1"].phase == Phase.LINEUP


def test_no_track_trusts_pilot(brain):
    # without live state, the bot trusts the report (offline behaviour)
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    reply = brain.handle("Colt 1, holding short runway 25",
                         controller=Controller.GROUND)
    assert "contact" in reply.lower()
    assert brain.pilots["Colt 1"].phase == Phase.HOLDING


# ---------- Directions (trainer aid) ----------

def test_directions_to_named_gate(brain, airfield):
    # a position well south of the field, asking for bearing/distance to Entry East
    south = _track(airfield, airfield.ctr.center_lat - 0.3,
                   airfield.ctr.center_lon)
    reply = brain.handle("Control, Colt 1, request bearing and distance to Entry East",
                         track=south, controller=Controller.CONTROL)
    assert "Entry East" in reply
    assert "bearing" in reply
    assert "distance" in reply


def test_directions_defaults_to_nearest_gate(brain, airfield):
    east = airfield.gates["East"]
    near_east = _track(airfield, east[0], east[1])
    reply = brain.handle("Control, Colt 1, request bearing and distance",
                         track=near_east, controller=Controller.CONTROL)
    assert "Entry East" in reply


def test_directions_without_track(brain):
    reply = brain.handle("Control, Colt 1, request bearing and distance",
                         controller=Controller.CONTROL)
    assert "unable" in reply.lower()


def test_directions_bearing_is_plausible(brain, airfield):
    # due west of the field, Entry East should bear roughly east (045-135)
    west = _track(airfield, airfield.ctr.center_lat,
                  airfield.ctr.center_lon - 0.3)
    reply = brain.handle("Control, Colt 1, bearing and distance to Entry East",
                         track=west, controller=Controller.CONTROL)
    import re
    bearing = int(re.search(r"bearing (\d{3})", reply).group(1))
    assert 45 <= bearing <= 135


def test_directions_defaults_to_best_entry_not_nearest(brain, airfield):
    # A generic "directions to the field" gives the gate that sets up the
    # landing (Entry East for runway 25), not the gate nearest the aircraft.
    north = _track(airfield, airfield.ctr.center_lat + 0.3,
                   airfield.ctr.center_lon)
    reply = brain.handle("Control, Colt 1, request directions to the field",
                         track=north, controller=Controller.CONTROL)
    assert "Entry East" in reply


# ---------- Explicit entry requests ----------

def test_requested_entry_is_approved_and_remembered(brain):
    reply = brain.handle("Control, Colt 1, request Entry North",
                         controller=Controller.CONTROL)
    assert "approved" in reply.lower()
    assert "Entry North" in reply
    assert brain.pilots["Colt 1"].entry_gate == "Entry North"


def test_requested_entry_survives_followup_inbound(brain):
    brain.handle("Control, Colt 1, request Entry North",
                 controller=Controller.CONTROL)
    # A later inbound call must keep the approved gate, not reset to best entry.
    reply = brain.handle("Control, Colt 1, inbound 20 miles north",
                         controller=Controller.CONTROL)
    assert "Entry North" in reply
    assert brain.pilots["Colt 1"].entry_gate == "Entry North"


def test_bare_position_report_is_not_an_entry_request(brain):
    # "35 miles north" in an inbound call is a position, not a gate request.
    reply = brain.handle("Control, Colt 1, inbound 35 miles north",
                         controller=Controller.CONTROL)
    assert "Entry East" in reply
    assert brain.pilots["Colt 1"].entry_gate == "Entry East"


# ---------- Speaker identity ("Caveman" <-> "Springfield 1-1") ----------

def test_speaker_mapping_holds_both_identities(brain):
    brain.remember_speaker("Caveman", "Springfield 1-1")
    # The SRS/DCS player name and the flight callsign are the same pilot; the
    # SRS name resolves to the callsign, and the callsign maps to itself.
    assert brain.callsign_for_speaker("Caveman") == "Springfield 1-1"
    assert brain.callsign_for_speaker("Springfield 1-1") == "Springfield 1-1"


def test_speaker_mapping_tolerates_dcs_duplicate_suffix(brain):
    # Separators and case are insignificant ("caveman" == "CAVEMAN"), and a
    # flight-name speaker ("Springfield") also matches DCS's numbered form
    # ("Springfield1"). But two DCS-uniquified *players* ("Caveman" vs
    # "Caveman-1") must stay distinct — the trailing digit is significant.
    brain.remember_speaker("Caveman", "Springfield 1-1")
    assert brain.callsign_for_speaker("caveman") == "Springfield 1-1"
    assert brain.callsign_for_speaker("CAVEMAN") == "Springfield 1-1"
    assert brain.callsign_for_speaker("Caveman_1") == "Caveman_1"  # different pilot


def test_two_pilots_with_shared_callsign_stay_distinct(brain):
    # The TTI hazard: several pilots share the "Springfield11" slot name. Each
    # speaker must map to the gate/sign they actually used, not the callsign.
    brain.remember_speaker("Alice", "Springfield 1-1")
    brain.remember_speaker("Bob", "Springfield 1-2")
    assert brain.callsign_for_speaker("Alice") == "Springfield 1-1"
    assert brain.callsign_for_speaker("Bob") == "Springfield 1-2"
    assert brain.callsign_for_speaker("Carol") == "Carol"  # unknown -> unchanged


# ---------- Vectors (approach vector, real phraseology) ----------

def test_vectors_gives_heading(brain, airfield):
    # 20 NM south of the field, vectors for runway 25
    south = _track(airfield, airfield.ctr.center_lat - 0.3,
                   airfield.ctr.center_lon)
    reply = brain.handle("Control, Colt 1, request vectors for runway 25",
                         track=south, controller=Controller.CONTROL)
    assert "fly heading" in reply
    assert "vectors for runway 25" in reply


def test_vectors_heading_points_at_approach(brain, airfield):
    # from the south, the vector should point roughly north (toward the field)
    south = _track(airfield, airfield.ctr.center_lat - 0.3,
                   airfield.ctr.center_lon)
    reply = brain.handle("Control, Colt 1, request vectors",
                         track=south, controller=Controller.CONTROL)
    import re
    heading = int(re.search(r"fly heading (\d{3})", reply).group(1))
    assert heading <= 45 or heading >= 315  # roughly north


def test_vectors_without_track(brain):
    reply = brain.handle("Control, Colt 1, request vectors",
                         controller=Controller.CONTROL)
    assert "unable" in reply.lower()


def test_azimuth_request_gives_bearing(brain, airfield):
    # "azimuth" is a synonym for bearing/directions (trainer aid).
    south = _track(airfield, airfield.ctr.center_lat - 0.3,
                   airfield.ctr.center_lon)
    reply = brain.handle("Control, Colt 1, request azimuth to Entry East",
                         track=south, controller=Controller.CONTROL)
    assert "bearing" in reply.lower()
    assert "Entry East" in reply


def test_heading_request_gives_vector_not_handoff(brain, airfield):
    # A *request* for a heading is a trainer aid (a vector), NOT a readback of
    # the join clearance — it must not hand the flight to Tower.
    south = _track(airfield, airfield.ctr.center_lat - 0.3,
                   airfield.ctr.center_lon)
    brain.handle("Control, Colt 1, inbound 35 miles south",
                 track=south, controller=Controller.CONTROL)
    reply = brain.handle("Control, Colt 1, request heading to the field",
                         track=south, controller=Controller.CONTROL)
    assert "fly heading" in reply.lower()
    assert "contact Tower" not in reply
    assert brain.pilots["Colt 1"].phase == Phase.INBOUND


# ---------- Computed join heading (not hardcoded) ----------

def test_control_join_heading_is_computed(brain, airfield):
    # from the south, the join heading toward Entry East (north-east of the
    # field) should point into the north-east quadrant
    south = _track(airfield, airfield.ctr.center_lat - 0.3,
                   airfield.ctr.center_lon)
    reply = brain.handle("Control, Colt 1, inbound 35 miles south",
                         track=south, controller=Controller.CONTROL)
    import re
    heading = int(re.search(r"heading (\d{3})", reply).group(1))
    assert 0 <= heading <= 90  # roughly north-east toward Entry East


def test_control_join_heading_varies_with_position(brain, airfield):
    # two different positions must not produce the same canned heading
    south = _track(airfield, airfield.ctr.center_lat - 0.3,
                   airfield.ctr.center_lon)
    west = _track(airfield, airfield.ctr.center_lat,
                  airfield.ctr.center_lon - 0.3)
    import re
    r1 = brain.handle("Control, Colt 1, inbound 35 miles south",
                      track=south, controller=Controller.CONTROL)
    brain.pilots.clear()
    r2 = brain.handle("Control, Colt 1, inbound 35 miles west",
                      track=west, controller=Controller.CONTROL)
    h1 = int(re.search(r"heading (\d{3})", r1).group(1))
    h2 = int(re.search(r"heading (\d{3})", r2).group(1))
    assert h1 != h2


def test_control_join_without_track_has_no_heading(brain):
    reply = brain.handle("Control, Colt 1, inbound 35 miles north",
                         controller=Controller.CONTROL)
    assert "join via Entry East" in reply
    assert "heading" not in reply


def test_control_join_turn_is_computed_not_canned(brain, airfield):
    # The join turn follows the aircraft's current heading: pointing away from
    # the gate gives a turn, pointing at it gives "fly heading" (no turn).
    import re
    south = _track(airfield, airfield.ctr.center_lat - 0.3,
                   airfield.ctr.center_lon)
    reply = brain.handle("Control, Colt 1, inbound 35 miles south",
                         track=south, controller=Controller.CONTROL)
    heading = int(re.search(r"heading (\d{3})", reply).group(1))
    # The track's heading is 0 (north); the join heading is north-east, so a
    # right turn is expected.
    assert "turn right heading" in reply
    assert heading > 0


def test_control_join_straight_when_already_pointing_at_the_gate(brain, airfield):
    # A pilot already flying the join heading gets "fly heading X" (no turn).
    import re
    south = _track(airfield, airfield.ctr.center_lat - 0.3,
                   airfield.ctr.center_lon)
    # First learn the join heading, then re-issue with the aircraft aligned.
    reply = brain.handle("Control, Colt 1, inbound 35 miles south",
                         track=south, controller=Controller.CONTROL)
    heading = int(re.search(r"heading (\d{3})", reply).group(1))
    brain.pilots.clear()
    aligned = _track(airfield, airfield.ctr.center_lat - 0.3,
                     airfield.ctr.center_lon, heading=heading)
    reply = brain.handle("Control, Colt 1, inbound 35 miles south",
                         track=aligned, controller=Controller.CONTROL)
    assert "fly heading" in reply
    assert "turn" not in reply


# ---------- Self-exclusion from runway occupancy ----------

class _PlayerUnit:
    def __init__(self, callsign, player, lat, lon, alt_ft):
        self.callsign = callsign
        self.player = player
        self.lat = lat
        self.lon = lon
        self.alt_ft = alt_ft


def test_pilot_does_not_block_own_lineup(brain, airfield):
    # the transmitting pilot sitting on the runway must not count as traffic
    thr = airfield.runway_threshold("25")
    me = _PlayerUnit("Colt-1-1", "Caveman", thr[0], thr[1],
                     airfield.elevation_ft + 10)
    at_rwy = _track(airfield, thr[0], thr[1])
    reply = brain.handle("Tower, Colt 1, ready for departure", track=at_rwy,
                         controller=Controller.TOWER, traffic=[me],
                         speaker="Caveman")
    assert "line up and wait" in reply
    assert brain.pilots["Colt 1"].phase == Phase.LINEUP


def test_other_player_still_blocks_lineup(brain, airfield):
    thr = airfield.runway_threshold("25")
    other = _PlayerUnit("Ford-2-1", "Someone", thr[0], thr[1],
                        airfield.elevation_ft + 10)
    at_rwy = _track(airfield, thr[0], thr[1])
    reply = brain.handle("Tower, Colt 1, ready for departure", track=at_rwy,
                         controller=Controller.TOWER, traffic=[other],
                         speaker="Caveman")
    assert "hold short" in reply.lower()


# ---------- Line-up readback variants ----------

def test_lined_up_and_waiting_clears_takeoff(brain):
    brain.handle("Tower, Colt 1, ready for departure", controller=Controller.TOWER)
    assert brain.pilots["Colt 1"].phase == Phase.LINEUP
    reply = brain.handle("Tower, Colt 1, lined up and waiting",
                         controller=Controller.TOWER)
    assert "cleared for takeoff" in reply
    assert brain.pilots["Colt 1"].phase == Phase.TAKEOFF


def test_ready_again_while_lined_up_clears_takeoff(brain):
    # a pilot who reports "ready for departure" again (instead of reading back
    # "line up and wait") must still get the takeoff clearance, not be stuck.
    brain.handle("Tower, Colt 1, ready for departure", controller=Controller.TOWER)
    assert brain.pilots["Colt 1"].phase == Phase.LINEUP
    reply = brain.handle("Tower, Colt 1, ready for departure",
                         controller=Controller.TOWER)
    assert "cleared for takeoff" in reply
    assert brain.pilots["Colt 1"].phase == Phase.TAKEOFF


def test_bare_ready_while_lined_up_clears_takeoff(brain):
    brain.handle("Tower, Colt 1, ready for departure", controller=Controller.TOWER)
    reply = brain.handle("Tower, Colt 1, ready", controller=Controller.TOWER)
    assert "cleared for takeoff" in reply
    assert brain.pilots["Colt 1"].phase == Phase.TAKEOFF


def test_lineup_help_mentions_readback(brain):
    brain.handle("Tower, Colt 1, ready for departure", controller=Controller.TOWER)
    reply = brain.handle("Trainer, Colt 1, help", controller=Controller.TOWER)
    assert "read back" in reply.lower() or "readback" in reply.lower()


def test_taking_off_hands_off_to_control(brain):
    reply = brain.handle("Tower, Colt 1, taking off",
                         controller=Controller.TOWER)
    assert "contact Control" in reply
    assert brain.pilots["Colt 1"].phase == Phase.AIRBORNE


# ---------- Passing the entry point (Control -> Tower) ----------

def test_control_passing_entry_hands_to_tower(brain):
    brain.handle("Control, Colt 1, inbound 35 miles east",
                 controller=Controller.CONTROL)
    assert brain.pilots["Colt 1"].phase == Phase.INBOUND
    reply = brain.handle("Control, Colt 1, passing entry east, inbound",
                         controller=Controller.CONTROL)
    assert "contact Tower" in reply
    assert brain.pilots["Colt 1"].phase == Phase.INBOUND
    assert brain.pilots["Colt 1"].pending_handoff_to == Controller.TOWER.value
    brain.handle("Tower, Colt 1", controller=Controller.TOWER)
    assert brain.pilots["Colt 1"].phase == Phase.LANDING


def test_extract_callsign_uses_only_the_known_speakers_errors(brain):
    """Once DCS/radio has named the speaker, other flights cannot steal STT."""
    brain.remember_speaker("Caveman", "Ford 1")
    brain.set_active_pilots(["Ford 1"])
    assert brain.extract_callsign("ground, forward one", "Caveman") == "Ford 1"
    assert brain.extract_callsign("ground, forward, one", "Caveman") == "Ford 1"
    assert brain.stt_flight_names() == ["Ford"]


def test_control_never_reissues_join_after_handoff(brain):
    # After the full join -> descend -> handoff, an "entry" mention must NOT
    # re-issue the join or pull the flight back from Tower (live-log regression).
    brain.handle("Control, Colt 1, inbound 35 miles southeast",
                 controller=Controller.CONTROL)
    brain.handle("150 to join via Entry East, Colt 1", controller=Controller.CONTROL)
    reply = brain.handle("Descend to 1500 feet, Colt 1", controller=Controller.CONTROL)
    assert reply == ""
    assert brain.pilots["Colt 1"].phase == Phase.INBOUND
    # A valid entry report requests the Tower transfer, never a new join.
    reply = brain.handle("Colt 1, entry southeast", controller=Controller.CONTROL)
    assert "join via" not in reply
    assert "contact Tower" in reply
    assert brain.pilots["Colt 1"].phase == Phase.INBOUND
    assert brain.handle("Control, Colt 1, inbound", controller=Controller.CONTROL)
    assert brain.pilots["Colt 1"].phase == Phase.INBOUND


# ---------- Regressions from the 2026-10-18 pancake debrief ----------

def test_bare_control_checkin_after_handoff_does_not_rearm_join(brain):
    # A bare "Control, Adder 1" right after the Tower handoff is a
    # frequency-change check-in, not an inbound call: it must not flip
    # Landing -> Inbound and re-arm the join flow (12:55:33 in the debrief).
    brain.handle("Control, Colt 1, inbound 35 miles southeast",
                 controller=Controller.CONTROL)
    brain.handle("150 to join via Entry East, Colt 1",
                 controller=Controller.CONTROL)
    brain.handle("Descend to 1500 feet, Colt 1", controller=Controller.CONTROL)
    assert brain.pilots["Colt 1"].phase == Phase.INBOUND
    brain.handle("Control, Colt 1, entry east", controller=Controller.CONTROL)
    brain.handle("Tower, Colt 1", controller=Controller.TOWER)
    assert brain.pilots["Colt 1"].phase == Phase.LANDING
    reply = brain.handle("Control, Colt 1", controller=Controller.CONTROL)
    assert reply and "Control" in reply
    assert brain.pilots["Colt 1"].phase == Phase.LANDING


def test_first_bare_control_checkin_still_starts_inbound(brain):
    # The guard must not break the legitimate first check-in: a bare
    # "Kutaisi Control, Colt 1" before any join starts the arrival flow.
    reply = brain.handle("Control, Colt 1", controller=Controller.CONTROL)
    assert reply and "Control" in reply
    assert brain.pilots["Colt 1"].phase == Phase.INBOUND


def test_no_vacate_warning_while_holding(brain):
    # The Ground->Tower handoff fires while the pilot is still at the holding
    # point; taxiing forward from there clips the runway pavement. A pilot in
    # Holding is not an incursion — no "vacate immediately" (12:51:21).
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    brain.handle("Colt 1, holding short runway 25", controller=Controller.GROUND)
    assert brain.pilots["Colt 1"].phase == Phase.HOLDING
    for _ in range(INCURSION_POLLS):
        warned = brain.check_incursion("Colt 1", on_runway=True)
    assert warned is None


def test_real_incursion_still_warns(brain):
    # The Holding exemption must not mask a genuine incursion: an uncleared
    # pilot (never taxied, never cleared) parked on the runway still gets the
    # warning after the debounce streak.
    for _ in range(INCURSION_POLLS):
        warned = brain.check_incursion("Colt 1", on_runway=True)
    assert warned is not None
    assert "vacate" in warned.lower()


def test_mangled_runway_in_sight_readback_acknowledged(brain):
    # STT drops the "in": "report runway sight" is the readback of "report
    # runway in sight", not a position report — acknowledge, not "say again"
    # (13:06:55 in the debrief).
    brain.handle("Tower, Colt 1, at entry west", controller=Controller.TOWER)
    reply = brain.handle("report runway sight, Colt 1",
                         controller=Controller.TOWER)
    assert reply and "roger" in reply.lower()
    # The exact form still reads back the same way.
    reply = brain.handle("report runway in sight, Colt 1",
                         controller=Controller.TOWER)
    assert reply and "roger" in reply.lower()


def test_repeat_after_wrong_exit_replays_the_clearance(brain):
    # A wrong-exit readback gets "say again" — but that reply must not replace
    # the stored clearance, or a follow-up "repeat copy clearance" replays the
    # "say again" itself and the pilot can never recover (12:44:32).
    brain.handle("Ground, Colt 1, ready to copy clearance",
                 controller=Controller.GROUND)
    brain.handle("Ground, Colt 1, after departure, turn right, exit north,"
                 " 1000 feet or below", controller=Controller.GROUND)
    reply = brain.handle("Repeat copy clearance, Colt 1",
                         controller=Controller.GROUND)
    assert "exit" in reply.lower()
    assert "say again" not in reply.lower()


def test_say_again_still_replays_clearance(brain):
    # The meta-reply guard must not break the normal path: "say again" with a
    # real clearance on record still replays that clearance.
    brain.handle("Ground, Colt 1, ready to copy clearance",
                 controller=Controller.GROUND)
    reply = brain.handle("say again, Colt 1", controller=Controller.GROUND)
    assert "exit" in reply.lower()


def test_say_again_replays_automatic_transmission(brain):
    automatic = "Colt 1, Tower, contact Control on channel 8."
    brain.remember_transmission("Colt 1", automatic)
    assert brain.handle("Colt 1, say again", controller=Controller.TOWER) == automatic


def test_automatic_meta_transmission_does_not_replace_replay(brain):
    pilot = brain._pilot("Colt 1")
    pilot.last_reply = "Colt 1, Ground, cleared taxi via Whiskey."
    brain.remember_transmission("Colt 1", "Colt 1, Tower, say again.")
    brain.remember_transmission("Colt 1", "readback incomplete")
    assert pilot.last_reply == "Colt 1, Ground, cleared taxi via Whiskey."


# ---------- Zones-based landing clearance (radar-driven) ----------

import math as _math

from ctr import AircraftTrack as _Track


def _track_on(airfield, bearing_from_thr, dist_nm, alt_ft=1500, heading=None):
    """A live track at a polar position around the active runway threshold."""
    thr = airfield.runway_threshold(airfield.active_runway)
    hdg = airfield.runway_heading(airfield.active_runway)
    rad = _math.radians(bearing_from_thr)
    lat = thr[0] + _math.degrees(dist_nm * 1852 * _math.cos(rad) / 6_371_000)
    lon = thr[1] + _math.degrees(
        dist_nm * 1852 * _math.sin(rad)
        / (6_371_000 * _math.cos(_math.radians(thr[0]))))
    return _Track(callsign="Colt 1", inside=True, lat=lat, lon=lon,
                  alt_ft=alt_ft, distance_nm=dist_nm, relative="x",
                  heading=heading if heading is not None else hdg)


def _inbound_bearing(airfield):
    """Bearing from the threshold that points straight down final."""
    return (airfield.runway_heading(airfield.active_runway) + 180) % 360


def test_pattern_turn_slices_reach_the_final_cone(airfield):
    # The slices (abeam ±90° to ±30°) must meet the final cone exactly: a
    # position 29° off the inbound course is inside a slice, 31° is not —
    # and 31° is inside the tight established-final cone (±30°... boundary).
    # Use clearly separated angles instead of the exact boundary.
    inbound = _inbound_bearing(airfield)
    assert airfield.in_pattern_turn(0, 0) is False  # no airfield position
    # abeam (90°) and base (45°) are in the turn; final (0°) is not.
    t_abeam = _track_on(airfield, (inbound + 90) % 360, 2.0)
    t_base = _track_on(airfield, (inbound + 45) % 360, 2.0)
    t_final = _track_on(airfield, inbound, 4.0)
    assert airfield.in_pattern_turn(t_abeam.lat, t_abeam.lon)
    assert airfield.in_pattern_turn(t_base.lat, t_base.lon)
    assert not airfield.in_pattern_turn(t_final.lat, t_final.lon)
    # Either side counts (STT mangles left/right).
    t_left = _track_on(airfield, (inbound - 45) % 360, 2.0)
    assert airfield.in_pattern_turn(t_left.lat, t_left.lon)
    # Outside the slice radius: not in the turn.
    t_far = _track_on(airfield, (inbound + 45) % 360, 10.0)
    assert not airfield.in_pattern_turn(t_far.lat, t_far.lon)


def test_established_final_is_tighter_than_is_on_final(airfield):
    # The displayed final wedge and positional check both use ±30°. The voice
    # report allows a wider heading tolerance (±50°) than the radar's
    # established-final check (±30°).
    inbound = _inbound_bearing(airfield)
    hdg = airfield.runway_heading(airfield.active_runway)
    t = _track_on(airfield, inbound, 4.0)
    t.heading = (hdg + 45) % 360
    assert airfield.is_on_final(t.lat, t.lon, t.heading)
    assert not airfield.is_established_final(t.lat, t.lon, t.heading)
    # Dead ahead: established.
    t = _track_on(airfield, inbound, 4.0)
    assert airfield.is_established_final(t.lat, t.lon, hdg)


def test_radar_clears_to_land_once_established(brain, airfield):
    # The OHB and pattern calls are status; the clearance comes from radar
    # once the aircraft has flown the turn (observed in a slice) and rolled
    # out established on final.
    inbound = _inbound_bearing(airfield)
    brain.handle("Tower, Colt 1, at entry west", controller=Controller.TOWER)
    brain.handle("Tower, Colt 1, runway in sight", controller=Controller.TOWER)
    assert brain.pilots["Colt 1"].phase == Phase.LANDING
    assert not brain.pilots["Colt 1"].cleared_landing
    # Abeam / base turn: no clearance yet, but the radar OBSERVES the turn.
    t = _track_on(airfield, (inbound + 90) % 360, 2.0)
    assert brain.check_landing_clearance("Colt 1", t) is None
    t = _track_on(airfield, (inbound + 45) % 360, 2.0)
    assert brain.check_landing_clearance("Colt 1", t) is None
    assert not brain.pilots["Colt 1"].cleared_landing
    assert brain.pilots["Colt 1"].pattern_flown
    # Established on final: the radar holds off (the pilot gets the chance
    # to say "on final" first) until inside the backstop range.
    t = _track_on(airfield, inbound, 4.0)
    assert brain.check_landing_clearance("Colt 1", t) is None
    t = _track_on(airfield, inbound, 2.9)
    reply = brain.check_landing_clearance("Colt 1", t)
    assert reply and "cleared to land" in reply.lower()
    assert brain.pilots["Colt 1"].cleared_landing
    # Fires once.
    assert brain.check_landing_clearance("Colt 1", t) is None


def test_radar_clearance_waits_for_a_free_runway(brain, airfield):
    # Runway occupied: the radar holds the clearance (the go-around monitor
    # handles the occupied-runway warning); it fires once the runway clears.
    inbound = _inbound_bearing(airfield)
    brain.handle("Tower, Colt 1, at entry west", controller=Controller.TOWER)
    brain.handle("Tower, Colt 1, runway in sight", controller=Controller.TOWER)
    # Fly the pattern first (the radar must observe the turn).
    t = _track_on(airfield, (inbound + 90) % 360, 2.0)
    brain.check_landing_clearance("Colt 1", t)
    thr = airfield.runway_threshold(airfield.active_runway)
    blocker = _Unit("AI-1", thr[0], thr[1], airfield.elevation_ft + 10)
    t = _track_on(airfield, inbound, 2.9)
    assert brain.check_landing_clearance("Colt 1", t, traffic=[blocker]) is None
    assert not brain.pilots["Colt 1"].cleared_landing
    reply = brain.check_landing_clearance("Colt 1", t, traffic=[])
    assert reply and "cleared to land" in reply.lower()


def test_premature_on_final_while_turning_base_challenged(brain, airfield):
    # A pilot calling "on final" while still in the turn (inside a slice) is
    # reporting a position the radar does not show: the SOP answer is the
    # position challenge ("negative. I show you ..."), not a clearance.
    inbound = _inbound_bearing(airfield)
    brain.handle("Tower, Colt 1, at entry west", controller=Controller.TOWER)
    brain.handle("Tower, Colt 1, runway in sight", controller=Controller.TOWER)
    t = _track_on(airfield, (inbound + 45) % 360, 2.0)
    reply = brain.handle("Colt 1, on final", t, controller=Controller.TOWER)
    assert "negative" in reply.lower()
    assert "cleared" not in reply.lower()
    assert not brain.pilots["Colt 1"].cleared_landing


def test_on_final_after_radar_clearance_is_silent(brain, airfield):
    # After the radar has cleared the flight, the pilot's "on final" is a
    # status report — no duplicate clearance.
    inbound = _inbound_bearing(airfield)
    brain.handle("Tower, Colt 1, at entry west", controller=Controller.TOWER)
    brain.handle("Tower, Colt 1, runway in sight", controller=Controller.TOWER)
    t = _track_on(airfield, (inbound + 90) % 360, 2.0)
    brain.check_landing_clearance("Colt 1", t)  # fly the pattern
    t = _track_on(airfield, inbound, 2.9)
    assert brain.check_landing_clearance("Colt 1", t)
    assert brain.handle("Colt 1, on final", t,
                        controller=Controller.TOWER) == ""


def test_straight_in_on_final_still_cleared_by_voice(brain, airfield):
    # A pilot who skips the OHB and flies straight-in never enters a slice:
    # their "on final" call (established position) still gets the clearance.
    inbound = _inbound_bearing(airfield)
    brain.handle("Tower, Colt 1, at entry west", controller=Controller.TOWER)
    t = _track_on(airfield, inbound, 4.0)
    reply = brain.handle("Colt 1, on final", t, controller=Controller.TOWER)
    assert "cleared to land" in reply.lower()
    assert brain.pilots["Colt 1"].cleared_landing


def test_radar_clearance_requires_the_break_clearance(brain, airfield):
    # Debrief 2026-10-18 19:21:08: an arrival from Entry West sits on the
    # extended centreline, so "established final" was true at the handoff and
    # the radar cleared to land 13 s later — before the overhead break. The
    # radar clearance is for the OHB flow only: it must not fire until Tower
    # has cleared the pilot for the break AND the radar has observed the
    # pattern turn (a slice passage).
    inbound = _inbound_bearing(airfield)
    brain.handle("Tower, Colt 1, at entry west", controller=Controller.TOWER)
    t = _track_on(airfield, inbound, 12.0)
    assert brain.check_landing_clearance("Colt 1", t) is None
    assert not brain.pilots["Colt 1"].cleared_landing
    # Break clearance alone is not enough: on the centreline, established,
    # but the radar has not seen the turn yet.
    brain.handle("Tower, Colt 1, runway in sight", controller=Controller.TOWER)
    assert brain.check_landing_clearance("Colt 1", t) is None
    # Fly the pattern (slice passage), roll out: the clearance fires at the
    # backstop range.
    t = _track_on(airfield, (inbound + 90) % 360, 2.0)
    brain.check_landing_clearance("Colt 1", t)
    t = _track_on(airfield, inbound, 2.9)
    reply = brain.check_landing_clearance("Colt 1", t)
    assert reply and "cleared to land" in reply.lower()


def test_landing_geometry_uses_the_active_runway(brain, airfield):
    # The brain's runway (set by wind/ATIS or the mission) may differ from
    # airfield.active_runway: every positional check must use brain.runway.
    # Debrief 2026-10-18: the flight flew runway 07 while active_runway was
    # 25, so a default-runway check silently looked at the wrong runway end.
    brain.set_runway("07")
    inbound = _inbound_bearing(airfield)  # helper uses active_runway: rebuild
    thr = airfield.runway_threshold("07")
    hdg = airfield.runway_heading("07")
    inbound7 = (hdg + 180) % 360
    rad = _math.radians(inbound7)
    lat = thr[0] + _math.degrees(4 * 1852 * _math.cos(rad) / 6_371_000)
    lon = thr[1] + _math.degrees(
        4 * 1852 * _math.sin(rad)
        / (6_371_000 * _math.cos(_math.radians(thr[0]))))
    t = _Track(callsign="Colt 1", inside=True, lat=lat, lon=lon,
               alt_ft=1500, distance_nm=4, relative="x", heading=hdg)
    assert airfield.is_established_final(lat, lon, hdg, "07")
    assert not airfield.is_established_final(lat, lon, hdg)  # default = 25
    # The brain's radar monitor uses brain.runway: established on 07.
    brain.handle("Tower, Colt 1, at entry west", controller=Controller.TOWER)
    brain.handle("Tower, Colt 1, runway in sight", controller=Controller.TOWER)
    # Fly the pattern (slice passage) before the established-final tick.
    rad_s = _math.radians(inbound7 + 90)
    lat_s = thr[0] + _math.degrees(2 * 1852 * _math.cos(rad_s) / 6_371_000)
    lon_s = thr[1] + _math.degrees(
        2 * 1852 * _math.sin(rad_s)
        / (6_371_000 * _math.cos(_math.radians(thr[0]))))
    t_slice = _Track(callsign="Colt 1", inside=True, lat=lat_s, lon=lon_s,
                     alt_ft=1500, distance_nm=2, relative="x", heading=hdg)
    brain.check_landing_clearance("Colt 1", t_slice)
    t_close = _Track(callsign="Colt 1", inside=True, lat=lat, lon=lon,
                     alt_ft=1500, distance_nm=1.9, relative="x", heading=hdg)
    reply = brain.check_landing_clearance("Colt 1", t_close)
    assert reply and "runway 07" in reply.lower()


def test_break_clearance_covers_wingmen(brain, airfield):
    # Master Arms: the break clearance is valid for the whole flight. The
    # wingman's radar flow (check_landing_clearance) arms on break_cleared,
    # so the lead's break clearance must mark same-flight wingmen too —
    # via either the OHB request or the "runway in sight" path.
    inbound = _inbound_bearing(airfield)
    brain.set_active_pilots(["Colt 1", "Colt 2"])
    brain.handle("Tower, Colt 2, at entry west", controller=Controller.TOWER)
    brain.handle("Tower, Colt 1, at entry west", controller=Controller.TOWER)
    brain.handle("Tower, Colt 1, runway in sight", controller=Controller.TOWER)
    assert brain.pilots["Colt 2"].break_cleared
    # The lead's landing clearance covers the wing: their "on final" is
    # silent (kneeboard RTB dialog: the wing only follows).
    t_lead = _track_on(airfield, inbound, 4.0)
    assert brain.handle("Colt 1, on final", t_lead,
                        controller=Controller.TOWER)
    t_wing = _track_on(airfield, inbound, 4.5)
    assert brain.handle("Colt 2, on final", t_wing,
                        controller=Controller.TOWER) == ""
    assert brain.pilots["Colt 2"].cleared_landing


def test_radar_clearance_not_fired_at_rwis_distance(brain, airfield):
    # Debrief 2026-10-18 20:49:55: the pilot reported "runway in sight" 8 NM
    # out on the centreline (the entry-to-field line IS the final corridor)
    # and the radar cleared to land one second later — before the break.
    # The radar must have OBSERVED the pattern turn: no slice passage, no
    # clearance, no matter how perfect the final geometry looks.
    inbound = _inbound_bearing(airfield)
    brain.handle("Tower, Colt 1, at entry west", controller=Controller.TOWER)
    t = _track_on(airfield, inbound, 8.2)
    brain.handle("Colt 1, runway in sight", t, controller=Controller.TOWER)
    assert brain.pilots["Colt 1"].break_cleared
    assert brain.check_landing_clearance("Colt 1", t) is None
    # Even closer, still on the centreline, no slice passage: no clearance.
    t = _track_on(airfield, inbound, 3.0)
    assert brain.check_landing_clearance("Colt 1", t) is None
    # Fly through a slice, roll out: the clearance fires at the backstop.
    t = _track_on(airfield, (inbound + 90) % 360, 2.0)
    brain.check_landing_clearance("Colt 1", t)
    t = _track_on(airfield, inbound, 2.9)
    reply = brain.check_landing_clearance("Colt 1", t)
    assert reply and "cleared to land" in reply.lower()


def test_final_descent_waits_for_the_step_down_altitude(brain, airfield):
    # Debrief 2026-10-18 20:46:38: the pilot got "descend to 1500" while still
    # at 8000 ft — the distance gate had shrunk but the step-down to 5000 was
    # never completed. The final descent waits for the altitude.
    gate = airfield.gates["West"]
    far = _Track(callsign="Colt 1", inside=False,
                 lat=gate[0] + _math.degrees(20 * 1852 / 6_371_000),
                 lon=gate[1], alt_ft=15000, distance_nm=20, relative="x")
    brain.handle("Control, Colt 1, one ship Hornet, inbound, 25 miles north",
                 far, controller=Controller.CONTROL)
    brain.handle("turn heading 213 to join via entry west, Colt 1", far,
                 controller=Controller.CONTROL)
    # The checkin assigns the best gate for the position; pin the spoken one
    # so the descent distances are measured against the gate we fly to.
    brain.pilots["Colt 1"].entry_gate = "West"
    assert brain.pilots["Colt 1"].descend_intermediate
    # Readback while still high (8000 ft): silent, no final descent — but the
    # readback is remembered so the handoff monitor can fire at pattern alt.
    near_high = _Track(callsign="Colt 1", inside=False,
                       lat=gate[0] + _math.degrees(10 * 1852 / 6_371_000),
                       lon=gate[1], alt_ft=8000, distance_nm=10, relative="x")
    assert brain.handle("descend and maintain 5000 feet, Colt 1", near_high,
                        controller=Controller.CONTROL) == ""
    assert not brain.pilots["Colt 1"].descend_issued
    assert brain.pilots["Colt 1"].descend_readback
    # At 5000 ft the monitor issues the final descent.
    near_ok = _Track(callsign="Colt 1", inside=False,
                     lat=gate[0] + _math.degrees(10 * 1852 / 6_371_000),
                     lon=gate[1], alt_ft=5000, distance_nm=10, relative="x")
    reply = brain.check_arrival_descent("Colt 1", near_ok)
    assert reply and "1500" in reply
    assert brain.pilots["Colt 1"].descend_issued


def test_radar_backstop_gives_the_voice_call_a_window(brain, airfield):
    # The radar clearance is the backstop, not the opener: after rollout it
    # holds off so the pilot can say "on final" (the SOP call) and get the
    # clearance from the voice path. Only inside RADAR_CLEAR_NM does the
    # radar speak — so a silent pilot is still cleared before short final.
    inbound = _inbound_bearing(airfield)
    brain.handle("Tower, Colt 1, at entry west", controller=Controller.TOWER)
    brain.handle("Tower, Colt 1, runway in sight", controller=Controller.TOWER)
    brain.check_landing_clearance(
        "Colt 1", _track_on(airfield, (inbound + 90) % 360, 2.0))
    # Established at 3.5 NM: the radar waits.
    t = _track_on(airfield, inbound, 3.5)
    assert brain.check_landing_clearance("Colt 1", t) is None
    assert not brain.pilots["Colt 1"].cleared_landing
    # The pilot says "on final": cleared immediately by voice.
    reply = brain.handle("Colt 1, on final", t, controller=Controller.TOWER)
    assert "cleared to land" in reply.lower()
    # The radar backstop never fires afterwards.
    assert brain.check_landing_clearance("Colt 1", t) is None


def test_radar_backstop_covers_a_silent_pilot(brain, airfield):
    # A pilot who never says "on final" is still cleared before short final.
    inbound = _inbound_bearing(airfield)
    brain.handle("Tower, Colt 1, at entry west", controller=Controller.TOWER)
    brain.handle("Tower, Colt 1, runway in sight", controller=Controller.TOWER)
    brain.check_landing_clearance(
        "Colt 1", _track_on(airfield, (inbound + 90) % 360, 2.0))
    for dist in (3.5,):
        assert brain.check_landing_clearance(
            "Colt 1", _track_on(airfield, inbound, dist)) is None
    reply = brain.check_landing_clearance(
        "Colt 1", _track_on(airfield, inbound, 2.9))
    assert reply and "cleared to land" in reply.lower()


def test_step_down_issued_when_high_even_inside_the_gate(brain, airfield):
    # Debrief 2026-10-18 21:17:59: the pilot checked in 6.5 NM north at
    # Angels 14 — abeam the entry gate, so the straight-line gate distance
    # (11 NM) was inside the final-descent gate and Control skipped the
    # 5000 step-down entirely. The step-down is gated on distance AND
    # altitude: too high -> 5000 first, regardless of the distance.
    thr = airfield.runway_threshold(airfield.active_runway)
    lat_n = thr[0] + _math.degrees(6.5 * 1852 / 6_371_000)
    t = _Track(callsign="Colt 1", inside=True, lat=lat_n, lon=thr[1],
               alt_ft=14000, distance_nm=6.5, relative="6 miles north")
    brain.handle("Control, Colt 1, one ship Hornet, inbound, seven miles"
                 " north of Kutaisi, at angels 14", t,
                 controller=Controller.CONTROL)
    # The checkin assigns the best gate for the position; the scenario flies
    # to Entry West, so pin it (the pilot reads back the assigned gate).
    brain.pilots["Colt 1"].entry_gate = "West"
    reply = brain.handle("turn left heading 215 to join via entry west,"
                         " Colt 1", t, controller=Controller.CONTROL)
    assert "5000" in reply
    assert brain.pilots["Colt 1"].descend_intermediate
    assert not brain.pilots["Colt 1"].descend_issued
    # At the gate and down to 5000: the monitor issues the final descent.
    gate = airfield.gates["West"]
    t2 = _Track(callsign="Colt 1", inside=False, lat=gate[0], lon=gate[1],
                alt_ft=5000, distance_nm=14, relative="at entry west")
    reply = brain.check_arrival_descent("Colt 1", t2)
    assert reply and "1500" in reply
    assert brain.pilots["Colt 1"].descend_issued
    # The final descent readback alone does not hand off to Tower.
    t3 = _Track(callsign="Colt 1", inside=True, lat=gate[0], lon=gate[1],
                alt_ft=1500, distance_nm=14, relative="at entry west")
    reply = brain.handle("descend to 1500 feet, Colt 1", t3,
                         controller=Controller.CONTROL)
    assert reply == ""
    assert brain.pilots["Colt 1"].phase == Phase.INBOUND


def test_final_descent_direct_when_low_and_close(brain, airfield):
    # The altitude gate must not break the normal close-and-low case: an
    # arrival inside the gate at/below 5000 gets the final descent directly
    # on the join readback (the documented MA example).
    gate = airfield.gates["West"]
    t = _Track(callsign="Colt 1", inside=False, lat=gate[0], lon=gate[1],
               alt_ft=5000, distance_nm=10, relative="at entry west")
    brain.handle("Control, Colt 1, one ship Hornet, inbound, 10 miles west",
                 t, controller=Controller.CONTROL)
    brain.pilots["Colt 1"].entry_gate = "West"
    reply = brain.handle("turn heading 213 to join via entry west, Colt 1",
                         t, controller=Controller.CONTROL)
    assert "1500" in reply
    assert brain.pilots["Colt 1"].descend_issued


# ---------- Regressions from the 2026-10-22 Mags debrief ----------
#
# The live sortie logged 30 bare "say again" replies out of 61 ATC
# transmissions, and the pilot was never cleared to land. Each test below pins
# one fix for that sortie, written against the *recorded* pilot phrases so the
# exact failures cannot come back.

def _airborne(brain, callsign="Colt 1", phase=Phase.AIRBORNE):
    """Put a known pilot in a phase, creating the state if needed."""
    pilot = brain._pilot(callsign)
    pilot.phase = phase
    return pilot


def test_bare_tower_checkin_is_acknowledged(brain):
    # Seven consecutive "Tower, Epic 4" calls were answered "say again" in the
    # sortie, so the pilot could not even establish contact on Tower.
    reply = brain.handle("Tower, Colt 1", controller=Controller.TOWER)
    assert reply and "Tower" in reply
    assert "say again" not in reply.lower()


def test_bare_tower_checkin_with_full_name_is_acknowledged(brain):
    reply = brain.handle("Kutaisi Tower, Colt 1", controller=Controller.TOWER)
    assert reply and "Tower" in reply


def test_tower_checkin_does_not_swallow_a_request(brain):
    # The acknowledgement must not steal a real call: "ready for departure"
    # still gets the line-up.
    reply = brain.handle("Tower, Colt 1, ready for departure",
                         controller=Controller.TOWER)
    assert "line up and wait" in reply.lower()


def test_spoken_channel_readback_is_silent(brain):
    # STT writes "channel seven", never "channel 7": the old digit-only guard
    # let every real frequency-change readback fall through to "say again".
    for text in ("Colt 1, channel seven, push",
                 "Stingray, channel three, Colt 1",
                 "Colt 1 pushing channel three",
                 "Colt 1, switching to channel 3",
                 "Colt 1, channel 8, push"):
        assert brain.handle(text, controller=Controller.CONTROL) == "", text


def test_spoken_channel_readback_is_silent_in_handed_off_phases(brain):
    # The silent-ack guard used to sit *after* controller dispatch, so Ground's
    # and Control's "re-issue the handoff" branches answered first and repeated
    # "contact Tower on channel 7" over the pilot's acknowledgement — twice in a
    # row in the 2026-10-22 sortie (22:06:45 and 22:06:55). The guard now runs
    # before dispatch, so a readback in a handed-off phase stays silent.
    for controller, phase, text in (
            (Controller.GROUND, Phase.HOLDING, "Colt 1, channel seven push"),
            (Controller.GROUND, Phase.HOLDING, "Colt 1, channel seven, push."),
            (Controller.TOWER, Phase.AIRBORNE, "Colt 1, channel 8, push"),
            (Controller.CONTROL, Phase.AIRBORNE, "Colt 1, channel three, push"),
            (Controller.CONTROL, Phase.LANDING, "Colt 1, channel seven, push"),
            (Controller.TOWER, Phase.LANDING, "Colt 1, channel six, push")):
        _airborne(brain, callsign="Colt 1", phase=phase)
        assert brain.handle(text, controller=controller) == "", text


def test_channel_change_request_still_answered_in_handed_off_phase(brain):
    # Moving the guard must not swallow a genuine request for a frequency
    # change: "request push" is a question and still gets the handoff.
    _airborne(brain, callsign="Colt 1", phase=Phase.HOLDING)
    reply = brain.handle("Colt 1, request push", controller=Controller.GROUND)
    assert "Tower" in reply
    assert "say again" not in reply.lower()


def test_channel_change_request_gets_the_handoff(brain):
    # "request Stingray" was answered with a bare ack six times in a row. The
    # pilot was airborne with Control, so the answer is the Stingray push.
    _airborne(brain)
    reply = brain.handle("Control, Colt 1, request Stingray",
                         controller=Controller.CONTROL)
    assert "Stingray" in reply
    assert "say again" not in reply.lower()


def test_channel_request_for_an_arrival_gets_tower(brain):
    _airborne(brain, phase=Phase.INBOUND)
    reply = brain.handle("Control, Colt 1, request channel change",
                         controller=Controller.CONTROL)
    assert "Tower" in reply


def test_help_is_reachable_without_the_trainer_prefix(brain):
    # Help calls in the sortie were swallowed as check-ins or "say again".
    # With a callsign it always works; a bare "help" needs the pilot to be
    # placeable (solo mode), which the trainer enables for a single pilot.
    for text in ("Control, Colt 1, help", "Colt 1, help"):
        reply = brain.handle(text, controller=Controller.CONTROL)
        assert reply and "suggests" in reply.lower(), text


def test_bare_help_works_for_a_placeable_pilot(brain):
    # A solo pilot who says just "help" is attributed to their own callsign.
    brain.solo_mode = True
    brain.set_active_pilots(["Colt 1"])
    brain.remember_speaker("Caveman", "Colt 1")
    reply = brain.handle("help", controller=Controller.CONTROL, speaker="Caveman")
    assert reply and "suggests" in reply.lower()


def test_help_still_works_with_the_trainer_prefix(brain):
    reply = brain.handle("trainer, Colt 1, help", controller=Controller.GROUND)
    assert reply and "suggests" in reply.lower()


def test_help_on_control_does_not_say_contact_control(brain):
    # help_airborne told a pilot already on Control to "contact Control".
    _airborne(brain)
    reply = brain.handle("Control, Colt 1, help", controller=Controller.CONTROL)
    assert "contact Control" not in reply
    assert "Stingray" in reply


def test_help_airborne_on_tower_still_points_at_control(brain):
    _airborne(brain)
    reply = brain.handle("Tower, Colt 1, help", controller=Controller.TOWER)
    assert "Control" in reply


def test_control_does_not_reissue_join_after_handing_off(brain):
    # Once Control has handed the flight to Tower, a later Control call must
    # repeat the handoff, not re-clear a join (which undid the handoff).
    pilot = _airborne(brain, phase=Phase.LANDING)
    reply = brain.handle("Control, Colt 1, inbound, Kutaisi",
                         controller=Controller.CONTROL)
    assert "contact Tower" in reply
    assert "join via" not in reply
    assert pilot.phase == Phase.LANDING


def test_departure_checkin_with_control_stays_airborne(brain):
    # The post-takeoff check-in must not flip a departure into Inbound.
    pilot = _airborne(brain, phase=Phase.LINEUP)
    brain.handle("Control, Colt 1, established Angels 15",
                 controller=Controller.CONTROL)
    assert pilot.phase == Phase.AIRBORNE


def test_request_landing_on_control_gets_tower_handoff(brain):
    # "request landing" got "say again" while "on final" got the handoff.
    reply = brain.handle("Control, Colt 1, request landing",
                         controller=Controller.CONTROL)
    assert "Tower" in reply


def test_exit_note_is_silent_for_a_departure_tracking_its_gate(brain, airfield):
    # The note fired on EVERY departure: the handoff happens seconds after
    # liftoff, when the assigned gate is 5-8 NM away by definition.
    from ctr import AircraftTrack
    pilot = _airborne(brain)
    pilot.exit_gate = "East"
    target = airfield.gates["East"]
    track = AircraftTrack(callsign="Colt 1", inside=False, lat=target[0],
                          lon=target[1], alt_ft=2000.0, distance_nm=8.0,
                          relative="8 miles east", heading=90.0)
    assert brain._exit_deviation_note(pilot, track) == ""


def test_exit_note_fires_when_heading_for_a_different_gate(brain, airfield):
    from ctr import AircraftTrack
    pilot = _airborne(brain)
    pilot.exit_gate = "East"
    # Over the *West* gate while cleared for Exit East: genuinely off track.
    target = airfield.gates["West"]
    track = AircraftTrack(callsign="Colt 1", inside=False, lat=target[0],
                          lon=target[1], alt_ft=2000.0, distance_nm=8.0,
                          relative="8 miles west", heading=270.0)
    assert "Exit East" in brain._exit_deviation_note(pilot, track)


def test_entry_report_requires_crossing_the_matching_arc(brain, airfield):
    from airspace import bearing_distance
    from scenario import _bearing_range_point
    gate = "East"
    center = (airfield.ctr.center_lat, airfield.ctr.center_lon)
    bearing, radius = bearing_distance(*center, *airfield.gates[gate])
    inside = _bearing_range_point(*center, bearing, radius - 1.0)
    assert brain._at_entry(_track(airfield, *inside), "Entry East")

    # Close to the gate point but outside its angular window is not an entry.
    off_arc = _bearing_range_point(*center, bearing + 25, radius - 1.0)
    assert not brain._at_entry(_track(airfield, *off_arc), "Entry East")

    # Being just outside the entry radius is too late, even on the right radial.
    outside = _bearing_range_point(*center, bearing, radius + 0.1)
    assert not brain._at_entry(_track(airfield, *outside), "Entry East")


def test_arrival_completes_after_control_handoff(brain, airfield):
    """End-to-end: the sortie that ended with no landing clearance now closes.

    Reproduces the 2026-10-22 arrival: Control hands off, the pilot misses the
    first Tower call, and Tower must still acknowledge and clear them to land.
    """
    from scenario import Scenario
    sc = Scenario(airfield, brain, callsign="Colt 1", speaker="Caveman")
    sc.on_final("25", nm=2.0)
    sc.set_alt(1400.0)
    pilot = _airborne(brain, phase=Phase.INBOUND)
    pilot.entry_gate = "East"
    pilot.descend_issued = True
    pilot.descend_readback = True

    # Control hands the arrival to Tower.
    reply = brain.handle("Control, Colt 1, request landing", sc._track(),
                         controller=Controller.CONTROL)
    assert "contact Tower" in reply
    assert pilot.phase == Phase.INBOUND
    assert pilot.pending_handoff_to == Controller.TOWER.value

    # Tower acknowledges the check-in and prompts for the position.
    assert brain.handle("Tower, Colt 1", sc._track(),
                        controller=Controller.TOWER) == (
                            "Colt 1, Tower, report your position.")
    assert pilot.phase == Phase.LANDING

    # "on final" completes the arrival.
    reply = brain.handle("Tower, Colt 1, on final", sc._track(),
                         controller=Controller.TOWER)
    assert "cleared to land" in reply
    assert pilot.cleared_landing


# ---------- Phrasing tolerance across agencies (2026-10-22 follow-up) ----------
#
# A pilot does not read the manual. The debrief showed the bot only understood
# one canonical wording per request ("requesting taxi", not "ready to taxi"),
# which produced "say again" for perfectly normal phraseology on every agency.
# These pin the wordings a real controller answers without hesitation.

def test_ground_accepts_ready_to_taxi(brain):
    # The MA kneeboard says "requesting taxi", but "ready to taxi" is just as
    # standard and was answered "say again" (or a bare check-in ack).
    brain.handle("Ground, Colt 1, ready to copy clearance",
                 controller=Controller.GROUND)
    for text in ("Ground, Colt 1, ready to taxi", "Colt 1, ready to taxi",
                 "Colt 1, ready for taxi", "Colt 1, we're ready to taxi"):
        brain._pilot("Colt 1").phase = Phase.CLEARANCE
        reply = brain.handle(text, controller=Controller.GROUND)
        assert "cleared taxi" in reply.lower(), text


def test_ground_accepts_start_up_and_bare_taxi(brain):
    for text, expected in (("Colt 1, ready for start up", "taxi"),
                           ("Colt 1, request start up", "taxi"),
                           ("Colt 1, taxi", "taxi")):
        brain._pilot("Colt 1").phase = Phase.CLEARANCE
        reply = brain.handle(text, controller=Controller.GROUND)
        assert expected in reply.lower(), (text, reply)


def test_request_taxi_clearance_gets_the_taxi_clearance(brain):
    # "clearance" must not hijack it into a fresh departure clearance.
    brain.handle("Ground, Colt 1, ready to copy clearance",
                 controller=Controller.GROUND)
    reply = brain.handle("Colt 1, request taxi clearance",
                         controller=Controller.GROUND)
    assert "cleared taxi" in reply.lower()
    assert "after departure" not in reply.lower()


def test_tower_answers_holding_short_report(brain, airfield):
    # A pilot who switches to Tower and reports holding short gets the line-up,
    # not "say again".
    from scenario import Scenario
    sc = Scenario(airfield, brain, callsign="Colt 1", speaker="Caveman")
    sc.holding("Holding A/N")
    brain._pilot("Colt 1").phase = Phase.HOLDING
    reply = brain.handle("Tower, Colt 1, holding short runway 25", sc._track(),
                         controller=Controller.TOWER)
    assert "line up and wait" in reply.lower()


def test_tower_accepts_landing_variants(brain):
    for text in ("Tower, Colt 1, request to land", "Tower, Colt 1, ready to land",
                 "Tower, Colt 1, requesting landing", "Tower, Colt 1, request landing"):
        brain._pilot("Colt 1").phase = Phase.INBOUND
        reply = brain.handle(text, controller=Controller.TOWER)
        assert reply and "say again" not in reply.lower(), text


def test_control_accepts_climb_and_rtb_variants(brain):
    # Each call on a fresh pilot: the second call in a sequence is a readback of
    # the first (correctly silent), which would mask what we are testing here.
    for text in ("Control, Colt 1, request climb",
                 "Control, Colt 1, ready to climb",
                 "Control, Colt 1, climbing to Angels 15"):
        brain.pilots.pop("Colt 1", None)
        brain._pilot("Colt 1").phase = Phase.AIRBORNE
        reply = brain.handle(text, controller=Controller.CONTROL)
        assert reply is not None and "say again" not in reply.lower(), text
    for text in ("Control, Colt 1, RTB", "Colt 1, returning to base"):
        brain.pilots.pop("Colt 1", None)
        brain._pilot("Colt 1").phase = Phase.AIRBORNE
        reply = brain.handle(text, controller=Controller.CONTROL)
        assert reply is not None and "say again" not in reply.lower(), text


def test_control_acknowledges_a_direction_report(brain):
    # "marking bull, 077 for 41" is a picture call, not a clearance request.
    brain._pilot("Colt 1").phase = Phase.AIRBORNE
    reply = brain.handle("Control, Colt 1, marking bull, 077 for 41",
                         controller=Controller.CONTROL)
    assert reply and "say again" not in reply.lower()


def test_ground_accepts_parking_variants(brain):
    for text in ("Ground, Colt 1, request taxi to parking",
                 "Colt 1, request parking", "Colt 1, taxi to the ramp"):
        brain._pilot("Colt 1").phase = Phase.TAXI
        brain._pilot("Colt 1").parking_cleared = False
        reply = brain.handle(text, controller=Controller.GROUND)
        assert "taxi" in reply.lower(), text
        assert "say again" not in reply.lower(), text


def test_departure_handoff_is_radar_driven_not_a_pilot_report(brain, airfield):
    """Tower pushes a departure to Control on radar, with no pilot call.

    MA SOP: after takeoff the pilot "expect[s] instructions to switch to
    control" — Tower initiates it. The phase only advances once the aircraft is
    actually off the runway (a departure readback alone does not do it).
    """
    from ctr import AircraftTrack
    pilot = brain._pilot("Colt 1")
    pilot.phase = Phase.TAKEOFF
    pilot.exit_gate = "West"
    pilot.checked_in_at = Controller.TOWER.value
    # Still over the runway: no handoff yet (a readback is not airborne).
    thr = airfield.runway_threshold("25")
    on_rwy = AircraftTrack(callsign="Colt 1", inside=True, lat=thr[0], lon=thr[1],
                           alt_ft=airfield.elevation_ft + 10, distance_nm=0.2,
                           relative="on runway", heading=250.0)
    assert brain.check_departure_handoff("Colt 1", on_rwy) is None
    assert pilot.phase == Phase.TAKEOFF
    # Passing the assigned Exit West arc fires the handoff unprompted.
    sc_track = AircraftTrack(
        callsign="Colt 1", inside=False, lat=airfield.gates["West"][0],
        lon=airfield.gates["West"][1], alt_ft=2000.0, distance_nm=8.0,
        relative="8 miles west", heading=250.0)
    reply = brain.check_departure_handoff("Colt 1", sc_track)
    assert reply is not None and "contact Control" in reply
    assert "did not exit" not in reply
    assert pilot.phase == Phase.AIRBORNE
    assert pilot.checked_in_at == Controller.TOWER.value
    assert pilot.pending_handoff_to == Controller.CONTROL.value


def test_departure_hands_off_and_reports_wrong_configured_exit(brain, airfield):
    pilot = brain._pilot("Colt 1")
    pilot.phase = Phase.TAKEOFF
    pilot.exit_gate = "West"
    north = airfield.gates["North"]
    reply = brain.check_departure_handoff(
        "Colt 1", _track(airfield, *north, alt_ft=2000.0))
    assert reply and "did not exit through Exit West" in reply
    assert "contact Control" in reply
    assert pilot.phase == Phase.AIRBORNE


def test_departure_leaving_ctr_outside_all_arcs_does_not_handoff(brain, airfield):
    pilot = brain._pilot("Colt 1")
    pilot.phase = Phase.TAKEOFF
    pilot.exit_gate = "West"
    # East is the arrival gate for runway 25, so it is outside every permitted
    # departure arc. Crossing the CTR boundary alone is not a gate handoff.
    east = airfield.gates["East"]
    assert not airfield.ctr.contains_horizontal(*east)
    assert brain.check_departure_handoff(
        "Colt 1", _track(airfield, *east, alt_ft=2000.0)) is None
    assert pilot.phase == Phase.TAKEOFF


def test_departure_inside_ctr_does_not_handoff_before_an_exit(brain, airfield):
    pilot = brain._pilot("Colt 1")
    pilot.phase = Phase.TAKEOFF
    pilot.exit_gate = "West"
    lat, lon = airfield.parking_areas["Ramp South"]
    assert airfield.ctr.contains_horizontal(lat, lon)
    assert brain.check_departure_handoff(
        "Colt 1", _track(airfield, lat, lon, alt_ft=1000.0)) is None
    assert pilot.phase == Phase.TAKEOFF


def test_early_control_request_does_not_bypass_exit_arc(brain, airfield):
    pilot = brain._pilot("Colt 1")
    pilot.phase = Phase.TAKEOFF
    pilot.exit_gate = "West"
    lat, lon = airfield.parking_areas["Ramp South"]
    reply = brain.handle("Tower, Colt 1, request Control",
                         _track(airfield, lat, lon, alt_ft=1000.0),
                         controller=Controller.TOWER)
    assert reply == ""
    assert pilot.phase == Phase.TAKEOFF
    assert pilot.last_controller == Controller.TOWER.value


def test_control_does_not_climb_until_departure_checks_in(brain, airfield):
    pilot = brain._pilot("Colt 1")
    pilot.phase = Phase.AIRBORNE
    pilot.departure_handoff_announced = True
    track = _track(airfield, *airfield.gates["West"], alt_ft=airfield.elevation_ft + 1200)
    assert brain.check_departure_climb("Colt 1", track) is None
    assert not pilot.climb_issued

    reply = brain.handle("Control, Colt 1, at 1200 feet", track,
                         controller=Controller.CONTROL)
    assert "radar contact" in reply
    assert "climb to Angels" in reply
    assert pilot.control_checkin_received
    assert pilot.climb_issued


# ---------- Stingray (AWACS) — Master Arms SOP, kneeboard page 3 ----------
# The tactical leg: check in, give a bullseye picture, then RTB and hand back to
# Control. Written against the recorded SOP phrases so the flow cannot regress.

def _stingray(brain, callsign="Colt 1"):
    """Put a known pilot on the Stingray leg."""
    pilot = brain._pilot(callsign)
    pilot.phase = Phase.STINGRAY
    return pilot


def test_stingray_checkin_is_acknowledged(brain):
    # "Stingray, Adder11" -> "Adder11, Stingray" (SOP page 3).
    _stingray(brain)
    reply = brain.handle("Stingray, Colt 1", controller=Controller.STINGRAY)
    assert reply and "Stingray" in reply
    assert "say again" not in reply.lower()


def test_stingray_bullseye_call_acknowledged_without_a_bullseye(brain):
    # No mission bullseye (bare brain): acknowledge, never invent a position.
    _stingray(brain)
    reply = brain.handle(
        "Stingray, Colt 1, bullseye 065 for 40, angels 15, as fragged",
        controller=Controller.STINGRAY)
    assert "radar contact" in reply.lower()
    assert "negative" not in reply.lower()


def test_stingray_bullseye_call_verified_against_the_mission(brain, airfield):
    # With the mission bullseye set, a correct call is acknowledged...
    from airspace import bearing_distance
    from ctr import AircraftTrack
    blat, blon = 42.0, 42.5
    brain.set_bullseye(blat, blon)
    _stingray(brain)
    alat, alon = 42.6667, 42.5  # 40 NM north of the bullseye
    bearing, distance = bearing_distance(blat, blon, alat, alon)
    bearing = (bearing + brain.cockpit_bearing_offset_deg) % 360
    track = AircraftTrack(callsign="Colt 1", inside=False, lat=alat, lon=alon,
                          alt_ft=15000.0, distance_nm=40.0, relative="north",
                          heading=0.0)
    reply = brain.handle(
        f"Stingray, Colt 1, bullseye {bearing:03.0f} for {distance:.0f}",
        controller=Controller.STINGRAY, track=track)
    assert "radar contact" in reply.lower()
    assert "negative" not in reply.lower()


def test_cockpit_bearing_convention_is_applied(brain, airfield):
    # Kutaisi's cockpit displays bearings ~13° counterclockwise of geodesic
    # truth (magnetic variation 7°E + DCS map rotation 6°, configured in
    # airspace.json). Measured in the 2026-10-25 test flight: the pilot called
    # "bullseye 056" where the bot's geodesic math says 070 — and 056 was the
    # instrument value. The bot must verify and answer in these cockpit units,
    # while its internal math and challenge stay in the same units the pilot
    # reads.
    from airspace import bearing_distance
    from ctr import AircraftTrack
    assert (airfield.magnetic_variation_deg, airfield.map_rotation_deg) == \
        (7.0, 6.0)
    blat, blon = 42.2000, 41.6417  # the mission bullseye of the test flight
    brain.set_bullseye(blat, blon)
    _stingray(brain)
    alat, alon = 42.40428, 42.40491  # 16:12:12 position of the test sortie
    bearing, distance = bearing_distance(blat, blon, alat, alon)
    assert abs(bearing - 70.0) < 0.5  # geodesic truth (the bot's math)
    cockpit = (bearing + brain.cockpit_bearing_offset_deg) % 360
    assert abs(cockpit - 57.0) < 0.5  # what the pilot's instruments showed
    track = AircraftTrack(callsign="Colt 1", inside=False, lat=alat, lon=alon,
                          alt_ft=15000.0, distance_nm=36.0, relative="northeast",
                          heading=70.0)
    # The pilot reads the instrument value -> acknowledged...
    reply = brain.handle(
        "Stingray, Colt 1, bullseye 057 for 36, angels 15",
        controller=Controller.STINGRAY, track=track)
    assert "radar contact" in reply.lower(), reply
    # ...and the challenge speaks the same units (057, the cockpit display).
    reply = brain.handle(
        "Stingray, Colt 1, bullseye 200 for 10",
        controller=Controller.STINGRAY, track=track)
    assert "negative" in reply.lower() and "057" in reply, reply
    # A spoken heading is also given in cockpit units (true − 13).
    assert abs((70.0 + brain.cockpit_bearing_offset_deg) % 360 - 57.0) < 0.1


def test_stingray_bullseye_call_off_is_coached_not_blocked(brain):
    # ...and a clearly wrong call gets the real bearing/range, but the flight
    from ctr import AircraftTrack
    brain.set_bullseye(42.0, 42.5)
    pilot = _stingray(brain)
    track = AircraftTrack(callsign="Colt 1", inside=False, lat=42.6667,
                          lon=42.5, alt_ft=15000.0, distance_nm=40.0,
                          relative="north", heading=0.0)
    reply = brain.handle("Stingray, Colt 1, bullseye 200 for 10",
                         controller=Controller.STINGRAY, track=track)
    assert "negative" in reply.lower()
    assert "bullseye" in reply.lower()
    assert pilot.phase == Phase.STINGRAY  # not advanced, not blocked


def test_stingray_rtb_triggers(brain):
    # Every way a flight says "I am going home" gets "copy, RTB" — the reason
    # is never questioned (a trainer flight may be a strike, a fuel check, or
    # just a pilot who wants to head back).
    for text in ("Colt 1 Miller Time, requesting RTB",
                 "Colt 1, bingo",
                 "Colt 1, joker",
                 "Colt 1, bugout",
                 "Colt 1, bug out",
                 "Colt 1, winchester",
                 "Colt 1, requesting RTB",
                 "Colt 1, returning to base"):
        pilot = _stingray(brain)
        pilot.rtb_requested = False
        reply = brain.handle(text, controller=Controller.STINGRAY)
        assert "RTB" in reply, text
        assert pilot.rtb_requested, text


def test_repeated_stingray_rtb_does_not_repeat_ack(brain):
    _stingray(brain)
    first = brain.handle("Colt 1, requesting RTB", controller=Controller.STINGRAY)
    second = brain.handle("Colt 1, requesting RTB", controller=Controller.STINGRAY)
    assert "RTB" in first
    assert second == ""


def test_stingray_waits_before_rtb_push_when_already_in_range(brain,
                                                              monkeypatch):
    import brain as brain_module

    clock = [100.0]
    monkeypatch.setattr(brain_module.time, "monotonic", lambda: clock[0])
    pilot = _stingray(brain)
    near = AircraftTrack(callsign="Colt 1", inside=False, lat=42.0, lon=42.0,
                         alt_ft=15000, distance_nm=49, relative="49 miles")

    reply = brain.handle("Colt 1, requesting RTB", near,
                         controller=Controller.STINGRAY)
    assert "RTB" in reply
    assert brain.check_stingray_rtb_handoff("Colt 1", near) is None

    clock[0] += 4.9
    assert brain.check_stingray_rtb_handoff("Colt 1", near) is None
    clock[0] += 0.1
    handoff = brain.check_stingray_rtb_handoff("Colt 1", near)
    assert handoff and "contact Control" in handoff
    assert pilot.rtb_control_handoff_announced


def test_stingray_rtb_push_is_one_shot_and_a_missed_push_is_recoverable(brain):
    from ctr import AircraftTrack

    pilot = _stingray(brain)
    pilot.rtb_requested = True
    far = AircraftTrack(callsign="Colt 1", inside=False, lat=42.0, lon=42.0,
                        alt_ft=15000, distance_nm=55, relative="55 miles")
    assert brain.check_stingray_rtb_handoff("Colt 1", far) is None

    near = AircraftTrack(callsign="Colt 1", inside=False, lat=42.0, lon=42.0,
                         alt_ft=15000, distance_nm=49, relative="49 miles")
    reply = brain.check_stingray_rtb_handoff("Colt 1", near)
    assert reply and "contact Control" in reply
    assert pilot.pending_handoff_to == Controller.CONTROL.value
    assert pilot.phase == Phase.STINGRAY
    assert brain.check_stingray_rtb_handoff("Colt 1", near) is None

    # If the pilot misses the push, a direct check-in still completes it.
    brain.handle("Control, Colt 1", near, controller=Controller.CONTROL)
    assert pilot.phase == Phase.INBOUND
    assert not pilot.pending_handoff_to


def test_stingray_hands_back_to_control(brain):
    # "request Control" re-enters the normal arrival flow (Control inbound).
    pilot = _stingray(brain)
    reply = brain.handle("Stingray, Colt 1, request Control",
                         controller=Controller.STINGRAY)
    assert "Control" in reply
    assert "channel 8" in reply
    assert pilot.phase == Phase.STINGRAY
    assert pilot.pending_handoff_to == Controller.CONTROL.value
    brain.handle("Control, Colt 1", controller=Controller.CONTROL)
    assert pilot.phase == Phase.INBOUND


def test_stingray_does_not_break_other_controllers(brain):
    # The fourth controller must not change Ground/Tower/Control behaviour.
    assert "Ground" in brain.handle("Ground, Colt 1",
                                    controller=Controller.GROUND)
    assert "Tower" in brain.handle("Tower, Colt 1", controller=Controller.TOWER)
    assert "Control" in brain.handle("Control, Colt 1",
                                     controller=Controller.CONTROL)


def test_stingray_package_reports_are_silent(brain):
    # "Adder1 pushing" and "one away" are SOP package-channel reports, not calls
    # to the controller. Answering "say again" would train the pilot to stop
    # making them, so Stingray stays silent.
    for text in ("Colt 1 pushing", "Colt 1, one away", "Colt 1, rifle",
                 "Colt 1, fox two", "Colt 1, magnum", "Colt 1, copy that",
                 "Colt 1, as fragged"):
        _stingray(brain)
        assert brain.handle(text, controller=Controller.STINGRAY) == "", text


def test_stingray_air_start_can_check_in_directly(brain):
    # An air-start (or a pilot practising) must be able to go straight to
    # Stingray without flying a full departure first — otherwise testing the
    # bullseye flow needs a takeoff every time.
    pilot = brain._pilot("Colt 1")
    assert pilot.phase == Phase.IDLE
    reply = brain.handle("Stingray, Colt 1", controller=Controller.STINGRAY)
    assert "Stingray" in reply
    assert pilot.phase == Phase.STINGRAY


def test_stingray_bullseye_calls_are_repeatable(brain):
    # A pilot flying around practising bullseye calls can make as many as they
    # like; each is checked on its own merits.
    from ctr import AircraftTrack
    brain.set_bullseye(42.05, 42.40)
    track = AircraftTrack(callsign="Colt 1", inside=False, lat=42.6, lon=42.5,
                          alt_ft=15000.0, distance_nm=33.0, relative="north",
                          heading=0.0)
    # True (instrument) position is 355 for 33; all three are within tolerance (±10° / ±3 NM).
    for text in ("Stingray, Colt 1, bullseye 355 for 33",
                 "Stingray, Colt 1, bullseye 358 for 34",
                 "Stingray, Colt 1, bullseye 354 for 32"):
        reply = brain.handle(text, controller=Controller.STINGRAY, track=track)
        assert "radar contact" in reply.lower(), text
    # A clearly wrong call is still coached, not blocked.
    reply = brain.handle("Stingray, Colt 1, bullseye 200 for 10",
                         controller=Controller.STINGRAY, track=track)
    assert "negative" in reply.lower()


def test_stingray_help_is_tactical_not_idle(brain):
    # A pilot on Stingray asking for help must NOT be told to "contact Ground
    # for clearance and taxi" — that is the idle hint and is actively wrong on
    # the tactical leg. Both a checked-in pilot and an air-start get the
    # Stingray hint.
    for phase in (Phase.STINGRAY, Phase.IDLE):
        _airborne(brain, callsign="Colt 1", phase=phase)
        reply = brain.handle("Stingray, Colt 1, help",
                             controller=Controller.STINGRAY)
        assert "bullseye" in reply.lower(), phase
        assert "Ground" not in reply, phase


def test_stingray_pilot_can_self_move_to_control(brain):
    # A pilot on the tactical leg may ask to come home at any time — the trainer
    # never makes them wait for the controller to offer the handoff. Every way
    # of asking gives the Control handoff and re-enters the arrival flow.
    for text in ("Stingray, Colt 1, request Control",
                 "Stingray, Colt 1, request channel change",
                 "Stingray, Colt 1, request channel 8",
                 "Stingray, Colt 1, request landing",
                 "Stingray, Colt 1, request to land",
                 "Stingray, Colt 1, request approach",
                 "Stingray, Colt 1, inbound",
                 "Stingray, Colt 1, can I go to Control"):
        pilot = _stingray(brain)
        reply = brain.handle(text, controller=Controller.STINGRAY)
        assert "Control" in reply and "channel 8" in reply, text
        assert pilot.phase == Phase.STINGRAY, text
        brain.handle("Control, Colt 1", controller=Controller.CONTROL)
        assert pilot.phase == Phase.INBOUND, text


def test_stingray_channel_request_names_stingray_not_control(brain):
    # A generic frequency request on Stingray must not be answered with
    # "Colt 1, Control." (the old bug: the fallback used the Control ack, so
    # Stingray appeared to answer as Control).
    _stingray(brain)
    reply = brain.handle("Stingray, Colt 1, request channel change",
                         controller=Controller.STINGRAY)
    assert "Control on channel 8" in reply
    assert reply.strip() != "Colt 1, Control."


def test_pilot_can_self_move_between_any_agencies(brain):
    # This is a trainer: we do not know what the pilot wants to practise, so a
    # pilot may ask to move to ANY agency from ANY agency. Every combination
    # must name the target agency and its channel — never "say again", and never
    # the agency they are already on.
    agencies = {
        Controller.GROUND: "Ground",
        Controller.TOWER: "Tower",
        Controller.CONTROL: "Control",
        Controller.STINGRAY: "Stingray",
    }
    phases = [Phase.IDLE, Phase.TAXI, Phase.HOLDING, Phase.AIRBORNE,
              Phase.STINGRAY, Phase.INBOUND, Phase.LANDING]
    for controller, name in agencies.items():
        for phase in phases:
            for target, target_name in agencies.items():
                _airborne(brain, callsign="Colt 1", phase=phase)
                text = f"{name}, Colt 1, request {target_name}"
                reply = brain.handle(text, controller=controller)
                assert reply, f"{text} (from {controller.value}) got no reply"
                assert "say again" not in reply.lower(), text
                if target is not controller:
                    assert f"contact {target_name}" in reply, text
                    assert f"channel" in reply, text


def test_self_move_sets_the_phase_for_the_new_agency(brain):
    # Moving to an agency must set the phase to match, or the new agency treats
    # the pilot as if they had never left (e.g. a self-move to Control must be
    # seen as inbound, so the arrival flow works).
    pilot = _airborne(brain, callsign="Colt 1", phase=Phase.STINGRAY)
    brain.handle("Stingray, Colt 1, request Control",
                 controller=Controller.STINGRAY)
    assert pilot.phase == Phase.STINGRAY
    brain.handle("Control, Colt 1, inbound 35 miles north at Angels 12",
                 controller=Controller.CONTROL)
    assert pilot.phase == Phase.INBOUND
    # And the arrival flow then works on Control.
    reply = brain.handle("Control, Colt 1, inbound 35 miles north at Angels 12",
                         controller=Controller.CONTROL)
    assert "radar contact" in reply.lower()


def test_stingray_help_states_the_real_bullseye(brain):
    # The help hint states the pilot's *actual* bullseye position, so they can
    # check their numbers before calling them out — and the number it states
    # must be accepted by the check (otherwise the aid would mislead).
    import re
    from ctr import AircraftTrack
    brain.set_bullseye(42.05, 42.40)
    _stingray(brain)
    track = AircraftTrack(callsign="Colt 1", inside=False, lat=42.6, lon=42.5,
                          alt_ft=15000.0, distance_nm=33.0, relative="north",
                          heading=0.0)
    hint = brain.handle("Stingray, Colt 1, help",
                        controller=Controller.STINGRAY, track=track)
    match = re.search(r"bullseye (\d{3}) for (\d+)", hint)
    assert match, hint
    # Calling back exactly what help stated is accepted.
    reply = brain.handle(
        f"Stingray, Colt 1, bullseye {match.group(1)} for {match.group(2)}",
        controller=Controller.STINGRAY, track=track)
    assert "radar contact" in reply.lower()


def test_stingray_help_without_a_bullseye_uses_the_example(brain):
    # No mission bullseye (or no live position): the hint falls back to the
    # generic example rather than reading "you are —".
    brain.bullseye = None
    _stingray(brain)
    hint = brain.handle("Stingray, Colt 1, help",
                        controller=Controller.STINGRAY)
    assert "bullseye 065 for 40" in hint
    assert "you are —" not in hint


def test_stingray_bullseye_angels_is_checked(brain):
    # A picture call states the altitude too ("angels 15"), so it is checked
    # with the same tolerance as bearing/range — a wrong altitude is coached,
    # not silently accepted.
    from ctr import AircraftTrack
    brain.set_bullseye(42.05, 42.40)
    _stingray(brain)
    track = AircraftTrack(callsign="Colt 1", inside=False, lat=42.6, lon=42.5,
                          alt_ft=15000.0, distance_nm=33.0, relative="north",
                          heading=0.0)
    # Correct (and within ±1 angel) -> acknowledged.
    for angels in (15, 14, 16):
        reply = brain.handle(
            f"Stingray, Colt 1, bullseye 355 for 33, angels {angels}",
            controller=Controller.STINGRAY, track=track)
        assert "radar contact" in reply.lower(), angels
    # Clearly wrong -> coached, and the real angels is stated.
    reply = brain.handle("Stingray, Colt 1, bullseye 355 for 33, angels 25",
                         controller=Controller.STINGRAY, track=track)
    assert "negative" in reply.lower()
    assert "angels 15" in reply


def test_stingray_bullseye_angels_is_optional(brain):
    # The SOP call includes angels, but a call without it is still valid (the
    # bearing/range is the position report).
    from ctr import AircraftTrack
    brain.set_bullseye(42.05, 42.40)
    _stingray(brain)
    track = AircraftTrack(callsign="Colt 1", inside=False, lat=42.6, lon=42.5,
                          alt_ft=15000.0, distance_nm=33.0, relative="north",
                          heading=0.0)
    reply = brain.handle("Stingray, Colt 1, bullseye 355 for 33",
                         controller=Controller.STINGRAY, track=track)
    assert "radar contact" in reply.lower()


def test_stingray_help_states_angels_too(brain):
    # The help hint states the altitude as well, so the whole call can be
    # checked before making it.
    import re
    from ctr import AircraftTrack
    brain.set_bullseye(42.05, 42.40)
    _stingray(brain)
    track = AircraftTrack(callsign="Colt 1", inside=False, lat=42.6, lon=42.5,
                          alt_ft=15000.0, distance_nm=33.0, relative="north",
                          heading=0.0)
    hint = brain.handle("Stingray, Colt 1, help",
                        controller=Controller.STINGRAY, track=track)
    match = re.search(r"bullseye (\d{3}) for (\d+), angels (\d+)", hint)
    assert match, hint
    reply = brain.handle(
        f"Stingray, Colt 1, bullseye {match.group(1)} for {match.group(2)}, "
        f"angels {match.group(3)}",
        controller=Controller.STINGRAY, track=track)
    assert "radar contact" in reply.lower()


def test_a_challenge_never_traps_the_pilot(brain):
    # A coaching challenge ("negative, I show you ...") must never block the
    # pilot: they can ignore it and ask to move on, which is exactly what a
    # trainee does. This is the "never get stuck" rule applied to the bullseye
    # and position cross-checks.
    from ctr import AircraftTrack
    brain.set_bullseye(42.05, 42.40)
    _stingray(brain)
    track = AircraftTrack(callsign="Colt 1", inside=False, lat=42.6, lon=42.5,
                          alt_ft=15000.0, distance_nm=33.0, relative="north",
                          heading=0.0)
    # A clearly wrong call is challenged...
    reply = brain.handle("Stingray, Colt 1, bullseye 200 for 10",
                         controller=Controller.STINGRAY, track=track)
    assert "negative" in reply.lower()
    # ...and the pilot can then ignore it and ask to go home.
    for text in ("Stingray, Colt 1, request Control",
                 "Stingray, Colt 1, request push to Control",
                 "Stingray, Colt 1, request channel 8",
                 "Stingray, Colt 1, bugout"):
        _stingray(brain)
        brain.handle("Stingray, Colt 1, bullseye 200 for 10",
                     controller=Controller.STINGRAY, track=track)
        reply = brain.handle(text, controller=Controller.STINGRAY, track=track)
        assert "say again" not in reply.lower(), text
        assert "Control" in reply or "RTB" in reply, text


# --- Spoken figures in picture calls (STT writes words, not digits) ----------
# The debrief's STT output is words ("bullseye zero six five for four zero"), so
# the fold has to happen before the parsers or a spoken call is silently
# unhandled. These pin the fold, the parsers, and the end-to-end reply.

def test_spoken_figure_handles_tens_and_teens():
    # Regression: the fold used `_ONES` for every word, so a teen or a trailing
    # tens word raised KeyError and the worker swallowed the reply entirely —
    # the pilot heard *nothing* ("heading one forty" is a very common call).
    assert _spoken_figure("four forty") == "440"
    assert _spoken_figure("one forty") == "140"
    assert _spoken_figure("fifteen") == "15"
    assert _spoken_figure("fifteen seven zero three") == "15703"
    assert _spoken_figure("thirty two") == "32"
    assert _spoken_figure("two one thousand") == "21000"
    assert _spoken_figure("zero seven zero") == "070"


def test_a_heading_with_a_tens_word_does_not_crash(brain):
    # The crash was reachable through handle(): the worker catches the exception
    # and the pilot hears nothing, which is exactly the "stuck" symptom.
    brain._pilot("Colt 1").phase = Phase.AIRBORNE
    reply = brain.handle("Colt 1, heading four forty",
                         controller=Controller.CONTROL)
    assert reply is not None


def test_spoken_bullseye_call_is_folded(brain):
    # "bullseye zero six five for four zero" -> "bullseye 065 for 40".
    assert parse_bullseye_call(
        _fold_bullseye_figures(_fold_spoken_figures(
            "bullseye zero six five for four zero"))) == (65.0, 40.0)


def test_stt_commas_and_joined_four_are_verified_against_track(brain):
    from scenario import _bearing_range_point
    brain.set_bullseye(42.05, 42.40)
    lat, lon = _bearing_range_point(42.05, 42.40, 70, 40)
    track = _track(brain.airfield, lat, lon, alt_ft=18000)
    _stingray(brain)
    reply = brain.handle(
        "Stingray, Colt 1, Bullseye, 457, 440, Angels, 18, as fragged",
        controller=Controller.STINGRAY, track=track)
    assert "radar contact" in reply.lower(), reply


def test_stt_joined_four_is_repaired_for_named_marking_point(brain, airfield):
    from scenario import _bearing_range_point
    lat, lon = _bearing_range_point(airfield.ctr.center_lat,
                                    airfield.ctr.center_lon, 70, 40)
    track = _track(airfield, lat, lon, alt_ft=18000)
    _stingray(brain)
    reply = brain.handle(
        "Stingray, Colt 1, marking Kutasi, 457, 440, angels, 18",
        controller=Controller.STINGRAY, track=track)
    assert "radar contact" in reply.lower(), reply


def test_stt_joined_four_is_not_repaired_when_track_disagrees(brain):
    from scenario import _bearing_range_point
    brain.set_bullseye(42.05, 42.40)
    lat, lon = _bearing_range_point(42.05, 42.40, 220, 25)
    track = _track(brain.airfield, lat, lon, alt_ft=18000)
    _stingray(brain)
    reply = brain.handle(
        "Stingray, Colt 1, Bullseye, 457, 440, Angels, 18, as fragged",
        controller=Controller.STINGRAY, track=track)
    assert "negative" in reply.lower(), reply


def test_stt_joined_four_repairs_one_digit_bearing_and_range(brain, airfield):
    from scenario import _bearing_range_point

    # "for 5" can become "45" in either BRA field. Position verification
    # disambiguates that from a literal 045-degree bearing / 45-NM range.
    brain.set_bullseye(42.05, 42.40)
    bull_lat, bull_lon = _bearing_range_point(42.05, 42.40, 18, 5)
    bull_track = _track(airfield, bull_lat, bull_lon, alt_ft=18000)
    mark_lat, mark_lon = _bearing_range_point(
        airfield.ctr.center_lat, airfield.ctr.center_lon, 18, 5)
    mark_track = _track(airfield, mark_lat, mark_lon, alt_ft=18000)

    _stingray(brain)
    for text, track in (
        ("Stingray, Colt 1, Bullseye, 45, 45, Angels, 18", bull_track),
        ("Stingray, Colt 1, marking Kutaisi, 45, 45, Angels, 18",
         mark_track),
    ):
        reply = brain.handle(text, controller=Controller.STINGRAY, track=track)
        assert "radar contact" in reply.lower(), reply


def test_spoken_marking_call_is_folded(brain):
    # "marking Kutaisi two zero zero for twenty" -> "marking kutaisi 200 for 20".
    folded = _fold_bullseye_figures(_fold_spoken_figures(
        "marking kutaisi two zero zero for twenty"))
    assert parse_marking_call(folded) == ("kutaisi", 200.0, 20.0)


def test_marking_bull_means_the_bullseye(brain):
    # "marking bull, 077, 440" — a comma instead of "for", and "bull" for the
    # bullseye itself (Mags' habit; the trainer accepts it).
    folded = _fold_bullseye_figures(_fold_spoken_figures(
        "marking bull, zero seven seven, four forty"))
    assert parse_marking_call(folded) == ("bullseye", 77.0, 440.0)


def test_marking_bulls_stt_four_is_repaired_as_for(brain):
    from scenario import _bearing_range_point

    # Whisper rendered "for" as a separate 4 in the debrief:
    # "marking bulls, 63, 4, 46". The extra group must not make 4 NM the range.
    brain.cockpit_bearing_offset_deg = 0
    bull_lat, bull_lon = 42.05, 42.40
    brain.set_bullseye(bull_lat, bull_lon)
    lat, lon = _bearing_range_point(bull_lat, bull_lon, 63, 46)
    track = _track(brain.airfield, lat, lon, alt_ft=15000)
    _stingray(brain)

    reply = brain.handle(
        "Stingray, Colt 1, marking bulls, 63, 4, 46, angels, 15, as flagged",
        controller=Controller.STINGRAY, track=track)
    assert "radar contact" in reply.lower(), reply


def test_stt_mocking_bulls_is_normalized_to_marking_bull(brain):
    _stingray(brain)
    reply = brain.handle(
        "Stingray, Colt 1, mocking bulls, zero seven seven, four forty, "
        "angels fifteen, as fragged",
        controller=Controller.STINGRAY)
    assert reply and "radar contact" in reply.lower()
    assert "say again" not in reply.lower()


def test_marking_a_named_point_is_verified_against_the_airfield(brain):
    # The SOP example names the field: "marking Kutaisi two zero zero for
    # twenty". The point resolves to the served airfield's reference point.
    from airspace import bearing_distance
    from ctr import AircraftTrack
    _stingray(brain)
    clat = brain.airfield.ctr.center_lat
    clon = brain.airfield.ctr.center_lon
    alat, alon = 42.0, 42.2  # somewhere south-west of Kutaisi
    bearing, distance = bearing_distance(clat, clon, alat, alon)
    bearing = (bearing + brain.cockpit_bearing_offset_deg) % 360
    track = AircraftTrack(callsign="Colt 1", inside=False, lat=alat, lon=alon,
                          alt_ft=5000.0, distance_nm=distance,
                          relative="southwest", heading=200.0)
    reply = brain.handle(
        f"Stingray, Colt 1, marking Kutaisi {bearing:03.0f} for {distance:.0f}, "
        f"angels 5",
        controller=Controller.STINGRAY, track=track)
    assert "radar contact" in reply.lower(), reply
    # A wrong bearing is coached, and names the point it checked against.
    reply = brain.handle(
        "Stingray, Colt 1, marking Kutaisi 090 for 40, angels 5",
        controller=Controller.STINGRAY, track=track)
    assert "negative" in reply.lower()
    assert "Kutaisi" in reply


def test_marking_an_unknown_point_is_acknowledged_not_guessed(brain):
    # A point the trainer does not know (another airfield) is acknowledged
    # without a check — verifying against a guess would teach the wrong number.
    from ctr import AircraftTrack
    _stingray(brain)
    track = AircraftTrack(callsign="Colt 1", inside=False, lat=42.0, lon=42.2,
                          alt_ft=5000.0, distance_nm=20.0,
                          relative="southwest", heading=200.0)
    reply = brain.handle("Stingray, Colt 1, marking Batumi 200 for 20, angels 5",
                         controller=Controller.STINGRAY, track=track)
    assert "radar contact" in reply.lower()
    assert "negative" not in reply.lower()


def test_exact_altitude_is_coached_to_round(brain):
    # "angels 15703" / "15703 tf" read the digits off the altimeter; the SOP
    # wants whole thousands. The trainer coaches the correct call (Mags: "Apollo
    # blir irriterad om man läser siffrorna exakt").
    from ctr import AircraftTrack
    brain.set_bullseye(42.05, 42.40)
    _stingray(brain)
    track = AircraftTrack(callsign="Colt 1", inside=False, lat=42.6, lon=42.5,
                          alt_ft=15703.0, distance_nm=33.0, relative="north",
                          heading=0.0)
    for text in ("Stingray, Colt 1, bullseye 008 for 33, angels 15703",
                 "Stingray, Colt 1, bullseye 008 for 33, 15703 tf",
                 "Stingray, Colt 1, bullseye 008 for 33, 15703 feet"):
        reply = brain.handle(text, controller=Controller.STINGRAY, track=track)
        assert "negative" in reply.lower(), text
        assert "angels 16" in reply, text
        assert "radar contact" not in reply.lower(), text


def test_exact_altitude_with_a_bad_position_coaches_both(brain):
    # Both errors in one transmission get one combined reply — two "negative"s
    # in a row would not be said on the radio.
    from ctr import AircraftTrack
    brain.set_bullseye(42.05, 42.40)
    _stingray(brain)
    track = AircraftTrack(callsign="Colt 1", inside=False, lat=42.6, lon=42.5,
                          alt_ft=15703.0, distance_nm=33.0, relative="north",
                          heading=0.0)
    reply = brain.handle("Stingray, Colt 1, bullseye 200 for 10, angels 15703",
                         controller=Controller.STINGRAY, track=track)
    assert reply.lower().count("negative") == 1, reply
    assert "angels 16" in reply
    assert "bullseye" in reply.lower()


def test_rounded_angels_are_still_accepted(brain):
    # The correct call ("angels 16" for 15,703 ft) is acknowledged — the coaching
    # must not reject the very form it asks for.
    from ctr import AircraftTrack
    brain.set_bullseye(42.05, 42.40)
    _stingray(brain)
    track = AircraftTrack(callsign="Colt 1", inside=False, lat=42.6, lon=42.5,
                          alt_ft=15703.0, distance_nm=33.0, relative="north",
                          heading=0.0)
    reply = brain.handle("Stingray, Colt 1, bullseye 355 for 33, angels 16",
                         controller=Controller.STINGRAY, track=track)
    assert "radar contact" in reply.lower()


# ---------- Authentication (RAMROD) — not implemented, but never a dead end ----
# A pilot who tries to authenticate must not hear a bare "say again": that reads
# as "you said it wrong" and invites them to keep retrying something that can
# never work. Answer in ATC style, on every frequency, and leave a way forward.

def test_authentication_is_answered_in_atc_style_on_every_controller(brain):
    for controller in (Controller.GROUND, Controller.TOWER,
                       Controller.CONTROL, Controller.STINGRAY):
        for text in ("Colt 1, authenticate Golf Alpha",
                     "Colt 1, authenticating Romeo",
                     "Colt 1, RAMROD"):
            reply = brain.handle(text, controller=controller)
            assert reply, (controller, text)
            assert "say again" not in reply.lower(), (controller, text, reply)
            assert "not implemented" in reply.lower(), (controller, text, reply)
            assert "Colt 1" in reply, (controller, text, reply)


def test_pilot_can_continue_after_authentication_attempt(brain):
    # The reply must not advance or wedge the state machine: the very next
    # normal call is handled as if the authentication attempt never happened.
    _stingray(brain)
    reply = brain.handle("Stingray, Colt 1, RAMROD",
                         controller=Controller.STINGRAY)
    assert "not implemented" in reply.lower()
    reply = brain.handle("Stingray, Colt 1", controller=Controller.STINGRAY)
    assert reply and "Stingray" in reply
    assert "say again" not in reply.lower()


# --- Robustness: never a false ack, never a bare "say again" -----------------

def test_stingray_does_not_false_ack_an_unknown_call(brain):
    # The old catch-all acked ANY call containing "stingray"/"awacs", so an
    # unrecognised call got a false "Colt 1, Stingray." ack. Only a genuine
    # bare check-in may be acked; a call with content must be handled (or fall
    # through), never acked as if it were a check-in.
    _stingray(brain)
    reply = brain.handle("Stingray, Colt 1, request weather",
                         controller=Controller.STINGRAY)
    assert reply != "Colt 1, Stingray."
    assert "say again" in reply.lower()


def test_stingray_bare_checkin_still_acked(brain):
    _stingray(brain)
    reply = brain.handle("Stingray, Colt 1", controller=Controller.STINGRAY)
    assert reply == "Colt 1, Stingray."


def test_request_vector_to_bullseye_is_not_an_approach_vector(brain, airfield):
    # "request vector to bullseye" is a picture request, not an approach
    # vector: it must not be answered with a runway heading. With a track
    # present the vector aid WOULD answer, so this proves the exclusion.
    south = _track(airfield, airfield.ctr.center_lat - 0.3,
                   airfield.ctr.center_lon)
    _airborne(brain, callsign="Colt 1", phase=Phase.AIRBORNE)
    reply = brain.handle("Control, Colt 1, request vector to bullseye",
                         track=south, controller=Controller.CONTROL)
    assert "fly heading" not in reply.lower()
    assert "vectors for runway" not in reply.lower()


def test_capability_calls_answer_not_implemented_on_every_frequency(brain):
    # PICTURE / DECLARE / BOGEY DOPE / SNAP are C2 (AWACS) functions we do not
    # simulate. On every frequency they must get an honest ATC-style reply —
    # never a false ack, never a bare "say again".
    for controller in Controller:
        for text in ("Colt 1, picture", "Colt 1, declare",
                     "Colt 1, bogey dope", "Colt 1, snap"):
            _airborne(brain, callsign="Colt 1", phase=Phase.AIRBORNE)
            reply = brain.handle(text, controller=controller)
            assert reply, (controller, text)
            assert "say again" not in reply.lower(), (controller, text, reply)
            assert "not implemented" in reply.lower(), (controller, text, reply)


def test_emergency_never_gets_say_again_on_every_frequency(brain):
    # A pilot who declares an emergency must never be told "say again" — that
    # reads as "you said it wrong". Acknowledge in ATC style on every frequency.
    for controller in Controller:
        for text in ("Colt 1, mayday", "Colt 1, pan-pan",
                     "Colt 1, declaring an emergency"):
            _airborne(brain, callsign="Colt 1", phase=Phase.AIRBORNE)
            reply = brain.handle(text, controller=controller)
            assert reply, (controller, text)
            assert "say again" not in reply.lower(), (controller, text, reply)
            assert "emergency" in reply.lower(), (controller, text, reply)


def test_self_move_to_tower_from_airborne_is_an_arrival(brain):
    # The request creates a pending transfer. Arrival state changes only once
    # the pilot actually checks in on Tower.
    for phase in (Phase.AIRBORNE, Phase.STINGRAY, Phase.INBOUND,
                  Phase.LANDING):
        pilot = _airborne(brain, callsign="Colt 1", phase=phase)
        pilot.checked_in_at = Controller.CONTROL.value
        brain.handle("Control, Colt 1, request Tower",
                     controller=Controller.CONTROL)
        assert pilot.phase == phase
        brain.handle("Tower, Colt 1", controller=Controller.TOWER)
        assert pilot.phase == Phase.LANDING, phase


def test_self_move_to_ground_from_airborne_is_taxi(brain):
    # Same for Ground: an airborne pilot asking for Ground wants ground
    # handling, so the phase must become Taxi (Ground has no airborne handler).
    for phase in (Phase.AIRBORNE, Phase.STINGRAY):
        pilot = _airborne(brain, callsign="Colt 1", phase=phase)
        pilot.checked_in_at = Controller.CONTROL.value
        brain.handle("Control, Colt 1, request Ground",
                     controller=Controller.CONTROL)
        assert pilot.phase == phase
        brain.handle("Ground, Colt 1", controller=Controller.GROUND)
        assert pilot.phase == Phase.TAXI, phase
