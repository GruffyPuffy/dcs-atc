"""System tests: fly full MA dialogs through the brain with faked radar.

These roll the complete request/answer exchanges (the kind on the kneeboard)
through the real `AtcBrain`, with faked positions and traffic — no SRS, no DCS.
They catch integration bugs the unit tests miss (state carried across
controllers, position cross-checks, sequencing against traffic).

Scenario harness: `tests/scenario.py`.
"""

import pytest

from brain import Controller, Phase
from scenario import Scenario


def _scenario(airfield, brain):
    """A fresh scenario + brain (each test flies its own flight)."""
    return Scenario(airfield, brain)


# ---------- Full departure: 2-ship, Ramp South -> takeoff ----------

def test_full_departure_two_ship(airfield, brain):
    sc = _scenario(airfield, brain).park("Ramp South")

    # Ground: check-in with ATIS, clearance, readback, taxi, readback, hold.
    assert "Ground" in sc.say("Ground, Colt 1, two-ship Hornets on Ramp South "
                              "with information Charlie", Controller.GROUND)
    r = sc.say("Colt 1, ready to copy clearance", Controller.GROUND)
    assert "after departure" in r.lower() and "1500 ft or below" in r
    assert "readback correct" in sc.say(
        "After departure Exit West, 1500 ft or below, Colt 1", Controller.GROUND)
    r = sc.say("Colt 1, requesting taxi", Controller.GROUND)
    assert "cleared taxi" in r.lower()
    assert "readback correct" in sc.say(
        "Cleared taxi Sierra Echo and hold short runway 25, Colt 1",
        Controller.GROUND)

    # Now at the threshold: holding short -> Tower.
    sc.at_threshold("25")
    r = sc.say("Colt 1, holding short runway 25", Controller.GROUND)
    assert "contact" in r.lower() and "Tower" in r
    assert sc.phase() == Phase.HOLDING

    # Tower: line up, readback (triggers takeoff), then airborne -> Control.
    r = sc.say("Tower, Colt 1, at runway 25, ready for departure",
               Controller.TOWER)
    assert "line up and wait" in r.lower()
    r = sc.say("Line up and wait 25, Colt 1", Controller.TOWER)
    assert "cleared for takeoff" in r.lower()
    assert sc.phase() == Phase.DEPARTURE

    r = sc.say("Tower, Colt 1, airborne", Controller.TOWER)
    assert "contact Control" in r
    assert sc.phase() == Phase.AIRBORNE


# ---------- Full arrival: inbound -> break -> land -> park ----------

def test_full_arrival_two_ship(airfield, brain):
    sc = _scenario(airfield, brain)
    sc.say("Ground, Colt 1, two-ship Hornets on Ramp South", Controller.GROUND)

    # Control: inbound, join, readback (descend), then hand to Tower.
    sc.at_gate("North")
    r = sc.say("Kutaisi Control, Colt 1, inbound 35 miles north at Angels 12",
               Controller.CONTROL)
    assert "join via Entry East" in r
    r = sc.say("150 to join via Entry East, Colt 1", Controller.CONTROL)
    assert "descend to 1500 feet" in r.lower()

    # Tower: entry, runway in sight (break), on final (land).
    r = sc.say("Tower, Colt 1, Entry East", Controller.TOWER)
    assert "report runway in sight" in r.lower()
    r = sc.say("Tower, Colt 1, runway in sight", Controller.TOWER)
    assert "overhead break" in r.lower()
    assert "2-ship Colt 1" in r  # formation remembered from the ground call

    sc.on_final("25")
    r = sc.say("Tower, Colt 1, on final", Controller.TOWER)
    assert "cleared to land" in r.lower()
    assert "2-ship Colt 1" in r
    assert sc.phase() == Phase.LANDING

    # Vacated -> Ground -> parking.
    r = sc.say("Tower, Colt 1, runway vacated", Controller.TOWER)
    assert "contact Ground" in r
    r = sc.say("Ground, Colt 1, requesting taxi to parking", Controller.GROUND)
    assert "cleared taxi" in r.lower()


# ---------- Sequencing: runway occupied ----------

def test_departure_held_when_runway_occupied(airfield, brain):
    sc = _scenario(airfield, brain).at_threshold("25").traffic_on_runway("25")
    r = sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    assert "hold short" in r.lower() and "occupied" in r.lower()
    assert sc.phase() == Phase.HOLDING

    # Once the runway clears, the same call gets through.
    sc.clear_traffic()
    r = sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    assert "line up and wait" in r.lower()


def test_landing_sequenced_when_runway_occupied(airfield, brain):
    sc = _scenario(airfield, brain).on_final("25").traffic_on_runway("25")
    r = sc.say("Tower, Colt 1, on final", Controller.TOWER)
    assert "continue approach" in r.lower()
    assert sc.phase() != Phase.LANDING

    sc.clear_traffic()
    r = sc.say("Tower, Colt 1, on final", Controller.TOWER)
    assert "cleared to land" in r.lower()
    assert sc.phase() == Phase.LANDING


# ---------- Position cross-checks (faked radar) ----------

def test_ready_for_departure_challenged_on_ramp(airfield, brain):
    sc = _scenario(airfield, brain).park("Ramp South")
    r = sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    assert "negative" in r.lower()
    # the challenge offers a way out
    assert "reset" in r.lower() and "cancel" in r.lower()
    assert sc.phase() != Phase.LINEUP


def test_holding_short_challenged_on_ramp(airfield, brain):
    sc = _scenario(airfield, brain).park("Ramp South")
    # get into Taxi first, then falsely report holding short
    sc.say("Ground, Colt 1, requesting taxi", Controller.GROUND)
    r = sc.say("Colt 1, holding short runway 25", Controller.GROUND)
    assert "negative" in r.lower()
    assert sc.phase() == Phase.TAXI  # did not advance


def test_holding_short_accepted_at_named_point(airfield, brain):
    sc = _scenario(airfield, brain).holding("Holding B")
    sc.say("Ground, Colt 1, requesting taxi", Controller.GROUND)
    r = sc.say("Colt 1, holding short runway 25", Controller.GROUND)
    assert "Holding B" in r
    assert sc.phase() == Phase.HOLDING


# ---------- Line-up lock regression (the one that trapped a real flight) ----------

def test_lineup_never_traps_the_pilot(airfield, brain):
    sc = _scenario(airfield, brain).at_threshold("25")
    sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    assert sc.phase() == Phase.LINEUP
    # repeating "ready for departure" must still yield a takeoff clearance
    r = sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    assert "cleared for takeoff" in r.lower()
    assert sc.phase() == Phase.DEPARTURE


# ---------- Diversity: single ship vs 4-ship ----------

def test_single_ship_has_no_formation_prefix(airfield, brain):
    sc = _scenario(airfield, brain).on_final("25")
    r = sc.say("Tower, Colt 1, on final", Controller.TOWER)
    assert "ship" not in r
    assert "cleared to land" in r.lower()


def test_four_ship_prefix_on_landing(airfield, brain):
    sc = _scenario(airfield, brain)
    sc.say("Control, Colt 1, 4-ship Hornets inbound", Controller.CONTROL)
    sc.on_final("25")
    r = sc.say("Tower, Colt 1, on final", Controller.TOWER)
    assert "4-ship Colt 1" in r


# ---------- Escape hatches mid-flight ----------

def test_reset_and_fly_again(airfield, brain):
    sc = _scenario(airfield, brain).park("Ramp South")
    sc.say("Ground, Colt 1, requesting taxi", Controller.GROUND)
    assert sc.phase() == Phase.TAXI
    r = sc.say("Colt 1, reset", Controller.GROUND)
    assert "reset" in r.lower()
    assert sc.phase() == Phase.IDLE


def test_cancel_steps_back(airfield, brain):
    sc = _scenario(airfield, brain).at_threshold("25")
    sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    assert sc.phase() == Phase.LINEUP
    r = sc.say("Colt 1, cancel", Controller.TOWER)
    assert "cancel" in r.lower()
    assert sc.phase() == Phase.HOLDING


def test_say_again_replays_last_clearance(airfield, brain):
    sc = _scenario(airfield, brain)
    first = sc.say("Ground, Colt 1, requesting taxi", Controller.GROUND)
    again = sc.say("Colt 1, say again", Controller.GROUND)
    assert again == first


# ---------- Geometry: precise runway-relative placements ----------

def test_traffic_mid_runway_blocks_lineup(airfield, brain):
    # a unit actually on the runway (mid-length) blocks a line-up
    sc = _scenario(airfield, brain).at_threshold("25")
    sc.traffic_at_runway("25", along_nm=0.8)  # ~mid-runway
    r = sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    assert "hold short" in r.lower()


def test_traffic_before_threshold_does_not_block(airfield, brain):
    # a unit in the overrun margin (before the threshold) is NOT on the runway
    sc = _scenario(airfield, brain).at_threshold("25")
    sc.traffic_at_runway("25", along_nm=-0.15)  # just before the threshold
    r = sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    assert "line up and wait" in r.lower()


def test_traffic_on_parallel_taxiway_does_not_block(airfield, brain):
    # a unit on the parallel taxiway (~0.03 NM off centreline) is not on the runway
    sc = _scenario(airfield, brain).at_threshold("25")
    sc.traffic_at_runway("25", along_nm=0.5, across_nm=0.03)
    r = sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    assert "line up and wait" in r.lower()


def test_traffic_high_above_runway_does_not_block(airfield, brain):
    # an aircraft overflying the runway above 500 ft AGL is not occupying it
    sc = _scenario(airfield, brain).at_threshold("25")
    sc.traffic_at_runway("25", along_nm=0.5,
                         alt_ft=airfield.elevation_ft + 2000)
    r = sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    assert "line up and wait" in r.lower()


def test_explicit_latlon_traffic(airfield, brain):
    # the rawest form: place a unit by lat/lon directly
    thr = airfield.runway_threshold("25")
    sc = _scenario(airfield, brain).at_threshold("25")
    sc.add_traffic("AI-9", thr[0], thr[1], airfield.elevation_ft + 10)
    r = sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    assert "hold short" in r.lower()


def test_pilot_off_centreline_is_challenged(airfield, brain):
    # a pilot claiming "on final" but well abeam the runway is challenged
    sc = _scenario(airfield, brain)
    sc.at_runway("25", along_nm=-3.0, across_nm=5.0)  # ~59 deg off centreline
    sc.set_heading(airfield.runway_heading("25"))
    r = sc.say("Tower, Colt 1, on final", Controller.TOWER)
    assert "negative" in r.lower()


def test_pilot_aligned_on_final_is_cleared(airfield, brain):
    sc = _scenario(airfield, brain).on_final("25", nm=5.0)
    r = sc.say("Tower, Colt 1, on final", Controller.TOWER)
    assert "cleared to land" in r.lower()


# ---------- Help at every phase ----------

@pytest.mark.parametrize("setup,controller,expect", [
    (lambda sc: sc.park("Ramp South"), Controller.GROUND, "ground"),
    (lambda sc: sc.park("Ramp South"), Controller.TOWER, "ground"),
])
def test_help_on_the_ground(airfield, brain, setup, controller, expect):
    sc = _scenario(airfield, brain)
    setup(sc)
    r = sc.say("Colt 1, help", controller)
    assert "Apollo suggests" in r
    assert expect in r.lower()


def test_help_after_taxi_points_to_hold_short(airfield, brain):
    sc = _scenario(airfield, brain).park("Ramp South")
    sc.say("Ground, Colt 1, requesting taxi", Controller.GROUND)
    r = sc.say("Colt 1, help", Controller.GROUND)
    assert "holding short" in r.lower()


def test_help_while_holding_points_to_tower(airfield, brain):
    sc = _scenario(airfield, brain).at_threshold("25")
    sc.say("Ground, Colt 1, requesting taxi", Controller.GROUND)
    sc.say("Colt 1, holding short runway 25", Controller.GROUND)
    r = sc.say("Colt 1, help", Controller.GROUND)
    assert "ready for departure" in r.lower()


def test_help_while_lined_up_mentions_readback(airfield, brain):
    sc = _scenario(airfield, brain).at_threshold("25")
    sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    r = sc.say("Colt 1, help", Controller.TOWER)
    assert "read back" in r.lower() or "readback" in r.lower()


def test_help_always_mentions_escape_hatches(airfield, brain):
    sc = _scenario(airfield, brain).park("Ramp South")
    r = sc.say("Colt 1, help", Controller.GROUND)
    assert "reset" in r.lower() and "cancel" in r.lower()


def test_help_works_on_any_controller(airfield, brain):
    for controller in (Controller.GROUND, Controller.TOWER, Controller.CONTROL):
        sc = _scenario(airfield, brain).park("Ramp South")
        r = sc.say("Colt 1, help", controller)
        assert "Apollo suggests" in r


# ---------- Interleaved flights (two aircraft, one radar picture) ----------

def test_two_flights_independent_state(airfield, brain):
    shared: list = []
    colt = Scenario(airfield, brain, callsign="Colt 1", speaker="Caveman",
                    traffic=shared)
    ford = Scenario(airfield, brain, callsign="Ford 2", speaker="Maverick",
                    traffic=shared)

    colt.park("Ramp South")
    ford.park("Ramp North")

    colt.say("Ground, Colt 1, requesting taxi", Controller.GROUND)
    ford.say("Ground, Ford 2, requesting taxi", Controller.GROUND)

    assert colt.phase() == Phase.TAXI
    assert ford.phase() == Phase.TAXI
    # independent: Colt's taxi route is from Ramp South, Ford's from Ramp North
    assert brain.pilots["Colt 1"].phase == Phase.TAXI
    assert brain.pilots["Ford 2"].phase == Phase.TAXI


def test_interleaved_departure_and_arrival(airfield, brain):
    # one flight departing, one arriving, talking on different frequencies
    shared: list = []
    dep = Scenario(airfield, brain, callsign="Colt 1", speaker="Caveman",
                   traffic=shared)
    arr = Scenario(airfield, brain, callsign="Ford 2", speaker="Maverick",
                   traffic=shared)

    dep.at_threshold("25")
    arr.at_gate("North")

    # departure lines up while arrival checks in with Control
    dep.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    arr.say("Kutaisi Control, Ford 2, inbound 35 miles north",
            Controller.CONTROL)

    assert dep.phase() == Phase.LINEUP
    assert arr.phase() == Phase.INBOUND
    # the two states never interfere
    assert brain.pilots["Colt 1"].phase == Phase.LINEUP
    assert brain.pilots["Ford 2"].phase == Phase.INBOUND


def test_departing_flight_blocks_arriving_landing(airfield, brain):
    # a flight lined up on the runway blocks another flight's landing
    shared: list = []
    dep = Scenario(airfield, brain, callsign="Colt 1", speaker="Caveman",
                   traffic=shared)
    arr = Scenario(airfield, brain, callsign="Ford 2", speaker="Maverick",
                   traffic=shared)

    dep.at_threshold("25")
    arr.on_final("25", nm=5.0)

    # Colt lines up and is now ON the runway (mid-length)
    dep.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    dep.traffic_at_runway("25", along_nm=0.8, callsign="Colt-1-1",
                          player="Caveman")

    r = arr.say("Tower, Ford 2, on final", Controller.TOWER)
    assert "continue approach" in r.lower()
    assert arr.phase() != Phase.LANDING


def test_own_flight_does_not_block_itself(airfield, brain):
    # the transmitting pilot on the runway must not count as their own traffic
    shared: list = []
    sc = Scenario(airfield, brain, callsign="Colt 1", speaker="Caveman",
                  traffic=shared)
    sc.at_threshold("25")
    sc.traffic_at_runway("25", along_nm=0.8, callsign="Colt-1-1",
                         player="Caveman")
    r = sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    assert "line up and wait" in r.lower()


# ---------- Edge cases ----------

def test_unknown_request_says_again(airfield, brain):
    sc = _scenario(airfield, brain).park("Ramp South")
    r = sc.say("Tower, Colt 1, banana banana", Controller.TOWER)
    assert "say again" in r.lower()


def test_no_callsign_is_ignored(airfield, brain):
    sc = _scenario(airfield, brain).park("Ramp South")
    assert sc.say("requesting taxi", Controller.GROUND) is None


def test_radio_check_any_frequency(airfield, brain):
    for controller in (Controller.GROUND, Controller.TOWER, Controller.CONTROL):
        sc = _scenario(airfield, brain).park("Ramp South")
        r = sc.say("Colt 1, radio check", controller)
        assert "loud and clear" in r.lower()


def test_wrong_controller_still_answers(airfield, brain):
    # a pilot who calls Ground for a Tower matter still gets a sensible reply
    sc = _scenario(airfield, brain).at_threshold("25")
    r = sc.say("Ground, Colt 1, ready for departure", Controller.GROUND)
    assert r is not None  # Ground answers (does not go silent)


def test_bearing_and_distance_request(airfield, brain):
    sc = _scenario(airfield, brain).at_gate("North")
    r = sc.say("Control, Colt 1, request bearing and distance to Entry East",
               Controller.CONTROL)
    assert "bearing" in r.lower() and "distance" in r.lower()


def test_vectors_request(airfield, brain):
    sc = _scenario(airfield, brain).at_gate("North")
    r = sc.say("Control, Colt 1, request vectors for runway 25",
               Controller.CONTROL)
    assert "fly heading" in r.lower()


def test_vectors_without_radar_contact(airfield, brain):
    # no live position -> unable
    r = brain.handle("Control, Colt 1, request vectors",
                     track=None, controller=Controller.CONTROL)
    assert "unable" in r.lower()


# ---------- Full lifecycle: kneeboard page 1 -> 3 -> 2 in one flight ----------

def test_full_lifecycle_all_pages(airfield, brain):
    """One flight, start to parking, walking the kneeboard pages in order.

    Page 1 (start/takeoff) -> page 3 (airborne/AWACS, not handled by the ATC
    bot) -> page 2 (RTB/landing). Asserts the phase at every step, so a
    regression anywhere in the chain fails here.
    """
    sc = _scenario(airfield, brain).park("Ramp South")

    # --- Page 1: START / TAKEOFF ---
    # Ground: check-in (2-ship), clearance, readback, taxi, readback, hold.
    assert "Ground" in sc.say(
        "Ground, Colt 1, two-ship Hornets on Ramp South with information Charlie",
        Controller.GROUND)
    assert sc.phase() == Phase.IDLE

    r = sc.say("Colt 1, ready to copy clearance", Controller.GROUND)
    assert "after departure" in r.lower()
    assert sc.phase() == Phase.CLEARANCE
    assert "readback correct" in sc.say(
        "After departure Exit West, 1500 ft or below, Colt 1", Controller.GROUND)

    r = sc.say("Colt 1, requesting taxi", Controller.GROUND)
    assert "cleared taxi" in r.lower()
    assert sc.phase() == Phase.TAXI
    assert "readback correct" in sc.say(
        "Cleared taxi Sierra Echo and hold short runway 25, Colt 1",
        Controller.GROUND)

    sc.at_threshold("25")
    r = sc.say("Colt 1, holding short runway 25", Controller.GROUND)
    assert "contact" in r.lower() and "Tower" in r
    assert sc.phase() == Phase.HOLDING

    # Tower: line up, readback (takeoff), airborne -> Control.
    r = sc.say("Tower, Colt 1, at runway 25, ready for departure",
               Controller.TOWER)
    assert "line up and wait" in r.lower()
    assert sc.phase() == Phase.LINEUP
    r = sc.say("Line up and wait 25, Colt 1", Controller.TOWER)
    assert "cleared for takeoff" in r.lower()
    assert sc.phase() == Phase.DEPARTURE

    r = sc.say("Tower, Colt 1, airborne", Controller.TOWER)
    assert "contact Control" in r
    assert sc.phase() == Phase.AIRBORNE

    # Control: departure check-in -> climb.
    r = sc.say("Control, Colt 1, at 1500 ft", Controller.CONTROL)
    assert "climb to Angels 15" in r
    assert sc.phase() == Phase.AIRBORNE

    # --- Page 3: AIRBORNE (AWACS) — not handled by the ATC bot ---
    # The bot is ATC only; an AWACS call is not a known callsign, so it is
    # ignored (returns None) rather than mis-answered.
    assert sc.say("Stingray, Adder11", Controller.CONTROL) is None

    # --- Page 2: RTB / LANDING ---
    # Control: inbound, join, readback (descend), hand to Tower.
    sc.at_gate("North")
    r = sc.say("Kutaisi Control, Colt 1, inbound 35 miles north at Angels 12",
               Controller.CONTROL)
    assert "join via Entry East" in r
    assert sc.phase() == Phase.INBOUND
    r = sc.say("150 to join via Entry East, Colt 1", Controller.CONTROL)
    assert "descend to 1500 feet" in r.lower()

    # Tower: entry, runway in sight (break), on final (land).
    r = sc.say("Tower, Colt 1, Entry East", Controller.TOWER)
    assert "report runway in sight" in r.lower()
    r = sc.say("Tower, Colt 1, runway in sight", Controller.TOWER)
    assert "overhead break" in r.lower()
    assert "2-ship Colt 1" in r  # formation carried from page 1

    sc.on_final("25")
    r = sc.say("Tower, Colt 1, on final", Controller.TOWER)
    assert "cleared to land" in r.lower()
    assert sc.phase() == Phase.LANDING

    # Vacated -> Ground -> parking.
    r = sc.say("Tower, Colt 1, runway vacated", Controller.TOWER)
    assert "contact Ground" in r
    assert sc.phase() == Phase.TAXI
    r = sc.say("Ground, Colt 1, requesting taxi to parking", Controller.GROUND)
    assert "cleared taxi" in r.lower()


# ---------- Go-around (runway occupied on final) ----------

def test_go_around_fires_once(airfield, brain):
    # the background monitor calls check_final; it must fire once per approach
    first = brain.check_final("Colt 1", on_final=True, runway_occupied=True)
    assert first is not None and "go around" in first.lower()
    again = brain.check_final("Colt 1", on_final=True, runway_occupied=True)
    assert again is None  # not repeated
    # once off final, it re-arms
    brain.check_final("Colt 1", on_final=False, runway_occupied=False)
    rearmed = brain.check_final("Colt 1", on_final=True, runway_occupied=True)
    assert rearmed is not None


# ---------- CTR entry warning ----------

def test_ctr_entry_without_clearance_warns(airfield, brain):
    from ctr import CtrEvent
    sc = _scenario(airfield, brain).at_gate("North")
    warning = brain.on_ctr_event("Colt 1", CtrEvent.ENTERED, sc._track())
    assert warning is not None and "without clearance" in warning.lower()


def test_ctr_entry_after_inbound_is_silent(airfield, brain):
    from ctr import CtrEvent
    sc = _scenario(airfield, brain).at_gate("North")
    sc.say("Kutaisi Control, Colt 1, inbound 35 miles north", Controller.CONTROL)
    warning = brain.on_ctr_event("Colt 1", CtrEvent.ENTERED, sc._track())
    assert warning is None  # already cleared inbound


# ---------- Wind-based runway switch mid-flight ----------

def test_runway_switch_changes_taxi_route(airfield, brain):
    sc = _scenario(airfield, brain).park("Ramp South")
    r25 = sc.say("Ground, Colt 1, requesting taxi", Controller.GROUND)
    assert "Sierra Echo" in r25  # Ramp South -> 25
    brain.set_runway("07")
    brain.pilots.clear()
    sc2 = _scenario(airfield, brain).park("Ramp South")
    r07 = sc2.say("Ground, Colt 1, requesting taxi", Controller.GROUND)
    assert "Whiskey" in r07  # Ramp South -> 07


# ---------- Repeated / redundant calls ----------

def test_repeated_ready_for_departure_after_takeoff(airfield, brain):
    # once cleared for takeoff, a further "ready" must not re-clear or trap
    sc = _scenario(airfield, brain).at_threshold("25")
    sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    sc.say("Line up and wait 25, Colt 1", Controller.TOWER)
    assert sc.phase() == Phase.DEPARTURE
    r = sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    assert r is not None  # answered, not silent
    assert sc.phase() == Phase.DEPARTURE  # still cleared


def test_checking_in_acknowledged(airfield, brain):
    sc = _scenario(airfield, brain).park("Ramp South")
    r = sc.say("Tower, Colt 1, checking in", Controller.TOWER)
    assert "roger" in r.lower()


# ---------- Automatic checks: altitude bust & runway incursion ----------

def test_altitude_bust_warns_under_tower_control(airfield, brain):
    # a pilot still in a Tower phase who climbs above the CTR ceiling
    sc = _scenario(airfield, brain).at_threshold("25")
    sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    sc.say("Line up and wait 25, Colt 1", Controller.TOWER)  # -> Departure
    sc.set_alt(airfield.ctr.ceiling_ft_msl + 500)
    call = brain.check_altitude("Colt 1", sc._track())
    assert call is not None and "control zone" in call.lower()


def test_altitude_bust_fires_once(airfield, brain):
    sc = _scenario(airfield, brain).at_threshold("25")
    sc.set_alt(airfield.ctr.ceiling_ft_msl + 500)
    first = brain.check_altitude("Colt 1", sc._track())
    assert first is not None
    assert brain.check_altitude("Colt 1", sc._track()) is None  # not repeated
    # back below the ceiling re-arms it
    sc.set_alt(airfield.elevation_ft + 500)
    brain.check_altitude("Colt 1", sc._track())
    sc.set_alt(airfield.ctr.ceiling_ft_msl + 500)
    assert brain.check_altitude("Colt 1", sc._track()) is not None


def test_altitude_bust_exempts_control_handoff(airfield, brain):
    # a pilot already talking to Control is cleared above the CTR
    sc = _scenario(airfield, brain).at_gate("North")
    sc.say("Control, Colt 1, at 1500 ft", Controller.CONTROL)  # -> Airborne
    sc.set_alt(airfield.ctr.ceiling_ft_msl + 5000)
    assert brain.check_altitude("Colt 1", sc._track()) is None


def test_runway_incursion_warns_when_uncleared(airfield, brain):
    # a pilot on the runway who never got a takeoff/landing clearance
    sc = _scenario(airfield, brain).park("Ramp South")
    sc.say("Ground, Colt 1, requesting taxi", Controller.GROUND)  # -> Taxi
    sc.at_runway("25", along_nm=0.5)  # now sitting on the runway
    call = brain.check_incursion("Colt 1", on_runway=True)
    assert call is not None and "without clearance" in call.lower()


def test_runway_incursion_silent_when_cleared(airfield, brain):
    sc = _scenario(airfield, brain).at_threshold("25")
    sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    sc.say("Line up and wait 25, Colt 1", Controller.TOWER)  # -> Departure
    assert brain.check_incursion("Colt 1", on_runway=True) is None


def test_runway_incursion_silent_while_lined_up(airfield, brain):
    # "line up and wait" explicitly puts the pilot on the runway: no incursion.
    sc = _scenario(airfield, brain).at_threshold("25")
    sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    assert brain.pilots["Colt 1"].phase == Phase.LINEUP
    assert brain.check_incursion("Colt 1", on_runway=True) is None


def test_runway_incursion_fires_once(airfield, brain):
    sc = _scenario(airfield, brain).park("Ramp South")
    sc.say("Ground, Colt 1, requesting taxi", Controller.GROUND)
    assert brain.check_incursion("Colt 1", on_runway=True) is not None
    assert brain.check_incursion("Colt 1", on_runway=True) is None
    brain.check_incursion("Colt 1", on_runway=False)  # re-arm
    assert brain.check_incursion("Colt 1", on_runway=True) is not None
