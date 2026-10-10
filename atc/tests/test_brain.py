"""Brain: intent recognition, controller routing, per-pilot state machine."""

import dataclasses

from brain import AtcBrain, Controller, Phase, Phraseology
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
    assert brain.pilots["Colt 1"].phase == Phase.DEPARTURE


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


def test_tower_in_the_break_acknowledged(brain):
    reply = brain.handle("Tower, Colt 1, in the break",
                         controller=Controller.TOWER)
    assert "report on final" in reply


# ---------- Master Arms Mission Procedures flow ----------

def test_ground_checkin_acknowledged(brain):
    # "Ground, Adder11" -> "Adder11, Ground"
    reply = brain.handle("Ground, Colt 1", controller=Controller.GROUND)
    assert reply == "Colt 1, Ground."


def test_ground_gives_runway_and_qnh_without_atis(brain):
    # no "with information X" -> Ground passes runway + QNH
    reply = brain.handle("Ground, Colt 1, two-ship Hornets on Ramp South",
                         controller=Controller.GROUND)
    assert "runway 25 in use" in reply
    assert "QNH" in reply


def test_ground_acknowledges_with_information(brain):
    reply = brain.handle(
        "Ground, Colt 1, two-ship Hornets on Ramp South with information Charlie",
        controller=Controller.GROUND)
    assert reply == "Colt 1, Ground."


def test_ground_runway_qnh_readback(brain):
    # The kneeboard line "25 in use, QNH 2992, Adder11" must be answered with
    # "readback correct, advise when ready for clearance" — not "say again".
    brain.handle("Ground, Colt 1, two-ship Hornets on Ramp South",
                 controller=Controller.GROUND)
    reply = brain.handle("25 in use, QNH 2992, Colt 1",
                         controller=Controller.GROUND)
    assert "readback correct" in reply
    assert "ready for clearance" in reply


def test_ground_clearance_readback_without_exit_word(brain):
    # STT may drop the "Exit" prefix; the gate name alone must still read back.
    brain.handle("Ground, Colt 1, ready to copy clearance",
                 controller=Controller.GROUND)
    reply = brain.handle("After departure turn right West 1500 ft or below, Colt 1",
                         controller=Controller.GROUND)
    assert "readback correct" in reply


def test_ground_clearance_readback_correct(brain):
    brain.handle("Ground, Colt 1, ready to copy clearance",
                 controller=Controller.GROUND)
    # Runway 25 assigns Exit West; a correct, complete readback is confirmed.
    reply = brain.handle("After departure turn right Exit West, 1500 ft or below, Colt 1",
                         controller=Controller.GROUND)
    assert "readback correct" in reply


def test_ground_taxi_readback_correct(brain):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    reply = brain.handle("Cleared taxi Sierra Echo and hold short runway 25, Colt 1",
                         controller=Controller.GROUND)
    assert "readback correct" in reply


def test_taxi_readback_tolerates_stt_clear(brain):
    # Common STT slip: "clear taxi" / "clear taxi, solo, hold short runway 25".
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    reply = brain.handle("Clear taxi, and hold short runway 25, Colt 1",
                         controller=Controller.GROUND)
    assert "readback correct" in reply


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
    # A correct readback then confirms and still leaves the phase at CLEARANCE
    # (the pilot proceeds to taxi when ready).
    reply = brain.handle("After departure turn right West, 1500 ft or below, Colt 1",
                         controller=Controller.GROUND)
    assert "readback correct" in reply.lower()
    assert brain.pilots["Colt 1"].phase == Phase.CLEARANCE


def test_readback_numbers_accept_spoken_digits():
    from brain import AtcBrain
    assert "25" in AtcBrain._numbers("runway two five")
    assert "2992" in AtcBrain._numbers("qnh two niner niner two")
    assert "1500" in AtcBrain._numbers("one five zero zero feet")
    assert "070" in AtcBrain._numbers("heading zero seven zero")


def test_takeoff_readback_after_clearance_gets_roger(brain):
    # Once cleared for takeoff, reading the clearance back must NOT "say again".
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    brain.handle("Tower, Colt 1, ready for departure", controller=Controller.TOWER)
    brain.handle("Line up and wait 25, Colt 1", controller=Controller.TOWER)
    reply = brain.handle("Right turn out, cleared for takeoff, 25, Colt 1",
                         controller=Controller.TOWER)
    assert "say again" not in reply.lower()
    assert brain.pilots["Colt 1"].phase == Phase.DEPARTURE


def test_tower_lineup_readback_clears_takeoff(brain):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    brain.handle("Tower, Colt 1, ready for departure", controller=Controller.TOWER)
    reply = brain.handle("Line up and wait 25, Colt 1", controller=Controller.TOWER)
    assert "cleared for takeoff" in reply
    assert "turnout" in reply
    assert brain.pilots["Colt 1"].phase == Phase.DEPARTURE


def test_control_departure_checkin_climb(brain):
    reply = brain.handle("Control, Colt 1, at 1500 ft",
                         controller=Controller.CONTROL)
    assert "radar contact" in reply
    assert "climb to Angels" in reply


def test_control_climb_issued_once_then_readback(brain):
    """A second level report is acknowledged, not re-cleared (no loop)."""
    first = brain.handle("Control, Colt 1, airborne 5 miles east climbing",
                         controller=Controller.CONTROL)
    assert "climb to Angels" in first
    assert brain.pilots["Colt 1"].climb_issued
    second = brain.handle("Climbing angels 15, Colt 1",
                          controller=Controller.CONTROL)
    assert "climb to Angels" not in second
    assert "readback correct" in second
    third = brain.handle("Colt 1 at angels 16", controller=Controller.CONTROL)
    assert "climb to Angels" not in third


def test_control_join_readback_descends(brain):
    brain.handle("Control, Colt 1, inbound 35 miles north",
                 controller=Controller.CONTROL)
    reply = brain.handle("150 to join via Entry East, Colt 1",
                         controller=Controller.CONTROL)
    assert "descend to 1500 feet" in reply
    assert brain.pilots["Colt 1"].descend_issued


def test_tower_entry_call_reports_runway_in_sight(brain):
    reply = brain.handle("Tower, Colt 1, Entry East", controller=Controller.TOWER)
    assert "report runway in sight" in reply


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
    reply = brain.handle("Tower, Colt 1, airborne", controller=Controller.TOWER)
    assert "Control" in reply
    assert "channel 8" in reply
    assert brain.pilots["Colt 1"].phase == Phase.AIRBORNE


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
    reply = brain.handle("Colt help", controller=Controller.CONTROL)
    assert reply and "Apollo suggests" in reply


def test_speaker_callsign_mapping(brain):
    brain.remember_speaker("Caveman", "Colt 1")
    assert brain.callsign_for_speaker("Caveman") == "Colt 1"
    assert brain.callsign_for_speaker("caveman") == "Colt 1"
    assert brain.callsign_for_speaker("Unknown") == "Unknown"


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


def test_solo_mode_prompts_on_unknown_call(brain):
    # Single-pilot trainer: an unrecognized call (e.g. a forgotten callsign)
    # gets a spoken prompt instead of silence.
    brain.set_pilot_count(1)
    reply = brain.handle("requesting taxi", controller=Controller.GROUND)
    assert reply and "say again" in reply.lower()


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
    first = brain.check_final("Colt 1", on_final=True, runway_occupied=True)
    second = brain.check_final("Colt 1", on_final=True, runway_occupied=True)
    assert "go around" in first
    assert second is None  # only once per approach


def test_go_around_resets_when_not_on_final(brain):
    brain.check_final("Colt 1", on_final=True, runway_occupied=True)
    brain.check_final("Colt 1", on_final=False, runway_occupied=False)
    again = brain.check_final("Colt 1", on_final=True, runway_occupied=True)
    assert "go around" in again


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


def test_challenge_offers_a_way_out(brain, airfield):
    # a challenged report must not leave the pilot stuck: the reply reminds
    # them of the escape hatches.
    ramp = _track(airfield, *airfield.parking_areas["Ramp West"])
    reply = brain.handle("Tower, Colt 1, ready for departure",
                         track=ramp, controller=Controller.TOWER)
    assert "negative" in reply.lower()
    assert "reset" in reply.lower() and "cancel" in reply.lower()


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
    brain.set_wind(270, 5)
    assert brain.wind == "270 at 5"
    brain.set_wind(0, 0)
    assert brain.wind == "calm"
    brain.set_runway("07")
    assert brain.runway == "07"


# ---------- Help (trainer aid) ----------

def test_help_idle_points_to_ground(brain):
    reply = brain.handle("Colt 1 help", controller=Controller.TOWER)
    assert "Ground" in reply
    assert "clearance" in reply.lower()


def test_help_after_taxi_points_to_hold_short(brain):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    reply = brain.handle("Colt 1 help", controller=Controller.GROUND)
    assert "holding short" in reply.lower()


def test_help_holding_points_to_tower(brain):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    brain.handle("Colt 1, holding short runway 25", controller=Controller.GROUND)
    reply = brain.handle("Colt 1 help", controller=Controller.GROUND)
    assert "Tower" in reply
    assert "ready for departure" in reply.lower()


def test_help_works_on_any_controller(brain):
    # help is a shared intent, valid regardless of which frequency it arrives on
    for controller in (Controller.GROUND, Controller.TOWER, Controller.CONTROL):
        reply = brain.handle("Colt 1 help", controller=controller)
        assert reply and "Colt 1" in reply


def test_escape_hatches_work_on_every_agency_and_phase(brain):
    # help / reset / cancel must work on ALL agencies in ALL states — a pilot
    # must never be stuck because they are on the "wrong" frequency or phase.
    from brain import PilotState
    for controller in (Controller.GROUND, Controller.TOWER, Controller.CONTROL):
        for phase in Phase:
            brain.pilots.clear()
            brain.pilots["Colt 1"] = PilotState(callsign="Colt 1", phase=phase)
            help_reply = brain.handle("Colt 1 help", controller=controller)
            reset_reply = brain.handle("Colt 1 reset", controller=controller)
            cancel_reply = brain.handle("Colt 1 cancel", controller=controller)
            assert help_reply and "Apollo suggests" in help_reply, (controller, phase)
            assert reset_reply and "state reset" in reset_reply, (controller, phase)
            assert cancel_reply and "cancelled" in cancel_reply, (controller, phase)


def test_help_requires_callsign(brain):
    assert brain.handle("help me please", controller=Controller.TOWER) is None


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


def test_help_mentions_escape_hatches(brain):
    reply = brain.handle("Colt 1 help", controller=Controller.TOWER)
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


def test_holding_short_names_the_holding_position(brain, airfield):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    # sit at Holding C (P1) and report holding short
    p1 = airfield.holding_points["Holding C"]
    at_p1 = _track(airfield, p1[0], p1[1])
    reply = brain.handle("Colt 1, holding short runway 25",
                         track=at_p1, controller=Controller.GROUND)
    assert "Holding C" in reply
    assert brain.pilots["Colt 1"].phase == Phase.HOLDING


def test_ready_for_departure_challenged_when_not_at_runway(brain, airfield):
    ramp = _track(airfield, *airfield.parking_areas["Ramp West"])
    reply = brain.handle("Tower, Colt 1, ready for departure",
                         track=ramp, controller=Controller.TOWER)
    assert "negative" in reply.lower()
    assert brain.pilots["Colt 1"].phase != Phase.DEPARTURE


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
    assert brain.pilots["Colt 1"].phase == Phase.DEPARTURE


def test_ready_again_while_lined_up_clears_takeoff(brain):
    # a pilot who reports "ready for departure" again (instead of reading back
    # "line up and wait") must still get the takeoff clearance, not be stuck.
    brain.handle("Tower, Colt 1, ready for departure", controller=Controller.TOWER)
    assert brain.pilots["Colt 1"].phase == Phase.LINEUP
    reply = brain.handle("Tower, Colt 1, ready for departure",
                         controller=Controller.TOWER)
    assert "cleared for takeoff" in reply
    assert brain.pilots["Colt 1"].phase == Phase.DEPARTURE


def test_bare_ready_while_lined_up_clears_takeoff(brain):
    brain.handle("Tower, Colt 1, ready for departure", controller=Controller.TOWER)
    reply = brain.handle("Tower, Colt 1, ready", controller=Controller.TOWER)
    assert "cleared for takeoff" in reply
    assert brain.pilots["Colt 1"].phase == Phase.DEPARTURE


def test_lineup_help_mentions_readback(brain):
    brain.handle("Tower, Colt 1, ready for departure", controller=Controller.TOWER)
    reply = brain.handle("Colt 1, help", controller=Controller.TOWER)
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
    assert brain.pilots["Colt 1"].phase == Phase.LANDING
