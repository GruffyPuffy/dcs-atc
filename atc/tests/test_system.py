"""System tests: fly full MA dialogs through the brain with faked radar.

These roll the complete request/answer exchanges (the kind on the kneeboard)
through the real `AtcBrain`, with faked positions and traffic — no SRS, no DCS.
They catch integration bugs the unit tests miss (state carried across
controllers, position cross-checks, sequencing against traffic).

Scenario harness: `tests/scenario.py`.
"""

import pytest

from brain import Controller, INCURSION_POLLS, Phase
from scenario import Scenario


def _scenario(airfield, brain):
    """A fresh scenario + brain (each test flies its own flight)."""
    return Scenario(airfield, brain)


def _pass_departure_exit(sc, brain, airfield):
    """Advance the radar track through the assigned outbound gate arc."""
    gate = brain.pilots["Colt 1"].exit_gate or "West"
    sc.at_gate(gate).set_alt(airfield.elevation_ft + 1500)
    return brain.check_departure_handoff("Colt 1", sc._track())


# ---------- Light path (ATC.md §12a / kneeboard page 4) ----------
# The documented short version: straight to Tower (no Ground), report "on final"
# (no formal break handoff). This guards the doc/card against drifting.

def test_light_path_departure(airfield, brain):
    sc = _scenario(airfield, brain).at_threshold("25")
    r = sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    assert "line up and wait" in r.lower()
    r = sc.say("Line up and wait 25, Colt 1", Controller.TOWER)
    assert "cleared for takeoff" in r.lower()
    r = sc.say("Tower, Colt 1, airborne", Controller.TOWER)
    assert r == ""  # no extra phrase; radar triggers the handoff at the exit
    assert brain.pilots["Colt 1"].phase == Phase.TAKEOFF
    r = _pass_departure_exit(sc, brain, airfield)
    assert r and "contact Control" in r
    # Clear of the CTR (the realistic point to check in with Control).
    sc.at_gate("East").set_alt(airfield.elevation_ft + 1500)
    r = sc.say("Control, Colt 1, at 1500 ft", Controller.CONTROL)
    assert "climb to Angels" in r


def test_control_checkin_inside_ctr_does_not_clear_climb(airfield, brain):
    # A departure that checks in with Control while still inside the CTR (below
    # the ceiling) is on the wrong frequency: Tower owns the zone. Control must
    # NOT clear them up through it — it tells them to stay low until clear.
    sc = _scenario(airfield, brain).at_threshold("25")
    sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    sc.say("Line up and wait 25, Colt 1", Controller.TOWER)   # -> Departure
    sc.say("Tower, Colt 1, airborne", Controller.TOWER)       # -> Airborne
    # Still inside the CTR, low: check in with Control too early.
    sc.at_runway("25", along_nm=0.5).set_alt(airfield.elevation_ft + 700)
    r = sc.say("Control, Colt 1, at 700 feet", Controller.CONTROL)
    assert "climb to Angels" not in r
    assert "remain below" in r.lower()
    assert not brain.pilots["Colt 1"].climb_issued


def test_control_checkin_outside_ctr_clears_climb(airfield, brain):
    # Once clear of the CTR, the normal departure check-in clears the climb.
    sc = _scenario(airfield, brain).at_threshold("25")
    sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    sc.say("Line up and wait 25, Colt 1", Controller.TOWER)
    sc.say("Tower, Colt 1, airborne", Controller.TOWER)
    sc.at_gate("East").set_alt(airfield.elevation_ft + 1500)  # outside the CTR
    r = sc.say("Control, Colt 1, at 1500 ft", Controller.CONTROL)
    assert "climb to Angels 15" in r
    assert brain.pilots["Colt 1"].climb_issued


def test_tower_hands_departure_to_control_at_the_exit_arc(airfield, brain):
    # The kneeboard sequence hands off at the departure exit, not at the CTR
    # boundary or on an airborne radio report.
    sc = _scenario(airfield, brain).at_threshold("25")
    sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    sc.say("Line up and wait 25, Colt 1", Controller.TOWER)
    assert brain.pilots["Colt 1"].phase == Phase.TAKEOFF
    # Still over the runway: no handoff yet (a little time to settle).
    sc.at_runway("25", along_nm=0.5).set_alt(airfield.elevation_ft + 300)
    assert brain.check_departure_handoff("Colt 1", sc._track()) is None
    # Past the runway end is still short of the exit arc.
    sc.at_runway("25", along_nm=2.0).set_alt(airfield.elevation_ft + 800)
    assert brain.check_departure_handoff("Colt 1", sc._track()) is None
    # Crossing the assigned exit arc completes the automatic handoff.
    sc.at_gate("West").set_alt(airfield.elevation_ft + 1500)
    call = brain.check_departure_handoff("Colt 1", sc._track())
    assert call and "contact Control" in call
    assert brain.pilots["Colt 1"].phase == Phase.AIRBORNE
    # Fires once.
    assert brain.check_departure_handoff("Colt 1", sc._track()) is None


def test_control_climbs_departure_on_radar_once_clear_of_ctr(airfield, brain):
    # A departure that checked in too early is held below the ceiling, then
    # receives the climb after clearing the CTR.
    sc = _scenario(airfield, brain).at_threshold("25")
    sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    sc.say("Line up and wait 25, Colt 1", Controller.TOWER)
    sc.say("Tower, Colt 1, airborne", Controller.TOWER)
    sc.at_runway("25", along_nm=0.5).set_alt(airfield.elevation_ft + 700)
    sc.say("Control, Colt 1, at 700 feet", Controller.CONTROL)  # too early
    assert not brain.pilots["Colt 1"].climb_issued
    # Still inside the CTR: no climb yet.
    assert brain.check_departure_climb("Colt 1", sc._track()) is None
    # Now clear of the CTR: Control issues the climb after the check-in.
    sc.at_gate("East").set_alt(airfield.elevation_ft + 1500)
    call = brain.check_departure_climb("Colt 1", sc._track())
    assert call and "climb to Angels 15" in call
    assert brain.pilots["Colt 1"].climb_issued
    # Fires once.
    assert brain.check_departure_climb("Colt 1", sc._track()) is None


def test_stingray_handoff_once_at_angels_15(airfield, brain):
    # MA SOP: after the climb, Control pushes to Stingray on channel 3 once
    # the aircraft is at Angels 15. We do not simulate AWACS, but the call
    # still fires. The climb readback is silent (next instruction is this).
    sc = _scenario(airfield, brain).at_threshold("25")
    sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    sc.say("Line up and wait 25, Colt 1", Controller.TOWER)
    sc.say("Tower, Colt 1, airborne", Controller.TOWER)
    sc.at_gate("East").set_alt(airfield.elevation_ft + 1500)
    r = sc.say("Control, Colt 1, at 1500 ft", Controller.CONTROL)
    assert "climb to Angels 15" in r
    assert sc.say("Climbing to Angels 15, Colt 1", Controller.CONTROL) == ""
    assert brain.check_stingray_handoff("Colt 1", sc._track()) is None
    sc.set_alt(12000)
    assert brain.check_stingray_handoff("Colt 1", sc._track()) is None
    sc.set_alt(15000)
    call = brain.check_stingray_handoff("Colt 1", sc._track())
    assert call and "contact Stingray" in call and "channel 3" in call
    assert brain.pilots["Colt 1"].stingray_handoff_announced
    assert brain.check_stingray_handoff("Colt 1", sc._track()) is None


def test_light_path_return_and_landing(airfield, brain):
    sc = _scenario(airfield, brain).at_gate("North")
    r = sc.say("Kutaisi Control, Colt 1, inbound 35 miles north at Angels 12",
               Controller.CONTROL)
    assert "join via Entry East" in r           # best entry for runway 25
    r = sc.say("150 to join via Entry East, Colt 1", Controller.CONTROL)
    assert "descend to 1500 feet" in r.lower()
    # Straight to Tower (no formal Control→Tower handoff required).
    r = sc.say("Tower, Colt 1, Entry East", Controller.TOWER)
    assert "report runway in sight" in r.lower()
    r = sc.say("Colt 1, runway in sight", Controller.TOWER)
    assert "overhead break" in r.lower()
    assert sc.phase() == Phase.LANDING
    sc.on_final("25")
    r = sc.say("Colt 1, on final", Controller.TOWER)
    assert "cleared to land" in r.lower()
    assert sc.phase() == Phase.LANDING
    r = sc.say("Colt 1, runway vacated", Controller.TOWER)
    assert "contact Ground" in r


# ---------- Full departure: 2-ship, Ramp South -> takeoff ----------

def test_full_departure_two_ship(airfield, brain):
    sc = _scenario(airfield, brain).park("Ramp South")

    # Ground: check-in with ATIS, clearance, readback, taxi, readback, hold.
    assert "Ground" in sc.say("Ground, Colt 1, two-ship Hornets on Ramp South "
                              "with information Charlie", Controller.GROUND)
    r = sc.say("Colt 1, ready to copy clearance", Controller.GROUND)
    assert "after departure" in r.lower() and "1500 ft or below" in r
    # A correct readback with a next step (taxi) is silent.
    assert sc.say(
        "After departure Exit West, 1500 ft or below, Colt 1", Controller.GROUND) == ""
    r = sc.say("Colt 1, requesting taxi", Controller.GROUND)
    assert "cleared taxi" in r.lower()
    assert sc.say(
        "Cleared taxi Sierra Echo and hold short runway 25, Colt 1",
        Controller.GROUND) == ""

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
    assert sc.phase() == Phase.TAKEOFF

    r = sc.say("Tower, Colt 1, airborne", Controller.TOWER)
    assert r == ""
    r = _pass_departure_exit(sc, brain, airfield)
    assert r and "contact Control" in r
    assert sc.phase() == Phase.AIRBORNE

    # Control: check in, climb, then the radar handoff to Stingray at Angels 15.
    # The pilot must be clear of the CTR for the climb (MA: "remain below 1500
    # ft until clear of the control zone"), so move them out to the exit gate.
    sc.at_gate("East").set_alt(1500)
    r = sc.say("Control, Colt 1, at 1500 ft", Controller.CONTROL)
    assert "climb to Angels 15" in r
    assert sc.say("Climbing to Angels 15, Colt 1", Controller.CONTROL) == ""
    sc.set_alt(15000)  # at Angels 15, still clear of the CTR
    r = brain.check_stingray_handoff("Colt 1", sc._track())
    assert r and "contact Stingray" in r and "channel 3" in r

    # Stingray: check in, bullseye picture, then RTB and hand back to Control.
    r = sc.say("Stingray, Colt 1", Controller.STINGRAY)
    assert r == "Colt 1, Stingray."  # the check-in ack, not "say again"
    assert sc.phase() == Phase.STINGRAY
    bearing, distance = sc.cockpit_bullseye()
    r = sc.say(sc.bullseye_call(bearing, distance), Controller.STINGRAY)
    assert "radar contact" in r.lower()
    r = sc.say("Colt 1, bugout", Controller.STINGRAY)
    assert "RTB" in r
    r = sc.say("Stingray, Colt 1, request Control", Controller.STINGRAY)
    assert "contact Control" in r and "channel 8" in r
    assert sc.phase() == Phase.STINGRAY
    assert brain.pilots["Colt 1"].pending_handoff_to == Controller.CONTROL.value


# ---------- Full arrival: inbound -> break -> land -> park ----------

def test_full_arrival_two_ship(airfield, brain):
    sc = _scenario(airfield, brain)
    sc.say("Ground, Colt 1, two-ship Hornets on Ramp South", Controller.GROUND)

    # Stingray: the tactical leg first — check in, bullseye picture, RTB, then
    # the handoff back to Control (MA SOP "returning to base").
    sc.at_bullseye(90, 40).set_alt(15000)
    r = sc.say("Stingray, Colt 1", Controller.STINGRAY)
    assert r == "Colt 1, Stingray."  # the check-in ack, not "say again"
    bearing, distance = sc.cockpit_bullseye()
    r = sc.say(sc.bullseye_call(bearing, distance), Controller.STINGRAY)
    assert "radar contact" in r.lower()
    r = sc.say("Colt 1 Miller Time, requesting RTB", Controller.STINGRAY)
    assert "RTB" in r
    r = sc.say("Stingray, Colt 1, request Control", Controller.STINGRAY)
    assert "contact Control" in r and "channel 8" in r
    assert sc.phase() == Phase.STINGRAY
    assert brain.pilots["Colt 1"].pending_handoff_to == Controller.CONTROL.value

    # Control: inbound, join, readback (descend), then hand to Tower.
    # Reset the altitude: the Stingray leg left the fake track at Angels 15, and
    # the descent step is altitude-aware (high -> step down to 5000 first).
    sc.at_gate("North").set_alt(1000)
    r = sc.say("Kutaisi Control, Colt 1, inbound 35 miles north at Angels 12",
               Controller.CONTROL)
    assert "join via Entry East" in r
    r = sc.say("150 to join via Entry East, Colt 1", Controller.CONTROL)
    assert "descend to 1500 feet" in r.lower()
    # Readback of the descent -> Control hands off to Tower (MA flow, kn. page 2)
    r = sc.say("Descend to 1500 feet, Colt 1", Controller.CONTROL)
    assert r == ""
    assert sc.phase() == Phase.INBOUND
    # The monitor hands off only after the final descent readback and CTR entry.
    r = brain.check_arrival_tower_handoff("Colt 1", sc._track())
    if r is None:
        # Move into the CTR after the final descent is read back.
        sc.lat, sc.lon = airfield.ctr.center_lat, airfield.ctr.center_lon
        r = brain.check_arrival_tower_handoff("Colt 1", sc._track())
    assert r and "contact Tower" in r
    assert sc.phase() == Phase.INBOUND
    sc.say("Tower, Colt 1", Controller.TOWER)
    assert sc.phase() == Phase.LANDING

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


def test_parking_readback_is_checked_against_the_parking_clearance(airfield, brain):
    # A parking readback that names only the ramp ("Ramp North, Alpha November")
    # must be confirmed — not checked against the earlier departure taxi
    # clearance (which named a runway and made it look incomplete).
    sc = _scenario(airfield, brain).park("Ramp South")
    sc.say("Ground, Colt 1, requesting taxi", Controller.GROUND)
    sc.say("Cleared taxi Sierra Echo and hold short runway 25, Colt 1",
           Controller.GROUND)
    sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    sc.say("Line up and wait 25, Colt 1", Controller.TOWER)
    sc.say("Tower, Colt 1, airborne", Controller.TOWER)
    sc.say("Tower, Colt 1, runway vacated", Controller.TOWER)
    r = sc.say("Ground, Colt 1, requesting taxi to parking", Controller.GROUND)
    assert "cleared taxi" in r.lower()
    r = sc.say("Ramp North, Alpha November, Colt 1", Controller.GROUND)
    assert r == ""  # correct readback, nothing follows -> silent


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


def test_held_departure_is_called_back_when_runway_clears(airfield, brain):
    # Real ATC calls a held pilot back — the pilot shouldn't have to re-ask.
    sc = _scenario(airfield, brain).at_threshold("25").traffic_on_runway("25")
    r = sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    assert "hold short" in r.lower()
    assert sc.phase() == Phase.HOLDING

    # Runway still occupied: no unprompted call.
    assert brain.check_runway_clear("Colt 1", runway_occupied=True) is None
    # Cleared: the *real* clearance is issued (line up and wait), unprompted.
    call = brain.check_runway_clear("Colt 1", runway_occupied=False)
    assert call and "line up and wait" in call.lower()
    assert sc.phase() == Phase.LINEUP
    # Fires once — an idle pilot is not spammed.
    assert brain.check_runway_clear("Colt 1", runway_occupied=False) is None


def test_held_landing_is_recleared_when_runway_clears(airfield, brain):
    sc = _scenario(airfield, brain).on_final("25").traffic_on_runway("25")
    r = sc.say("Tower, Colt 1, on final", Controller.TOWER)
    assert "continue approach" in r.lower()

    assert brain.check_runway_clear("Colt 1", runway_occupied=True) is None
    call = brain.check_runway_clear("Colt 1", runway_occupied=False)
    assert call and "cleared to land" in call.lower()
    assert sc.phase() == Phase.LANDING
    assert brain.check_runway_clear("Colt 1", runway_occupied=False) is None


# ---------- Post-landing handoff to Ground (MA SOP) ----------

def test_landed_flight_is_pushed_to_ground(airfield, brain):
    # MA SOP: "after landing, tower will push you to the Ground channel" — the
    # pilot does not report "vacated" to Tower. The push happens at TOUCHDOWN
    # (while still rolling out), so the pilot is on Ground before vacating.
    sc = _scenario(airfield, brain).on_final("25")
    r = sc.say("Tower, Colt 1, on final", Controller.TOWER)
    assert "cleared to land" in r.lower()
    assert sc.phase() == Phase.LANDING
    # On approach (not yet on the runway): no handoff.
    assert brain.check_landing_handoff("Colt 1", on_runway=False) is None
    # Touched down (on the runway, rolling out): Tower pushes to Ground.
    call = brain.check_landing_handoff("Colt 1", on_runway=True)
    assert call and "contact Ground" in call
    assert sc.phase() == Phase.TAXI
    # Fires once — an idle pilot is not spammed.
    assert brain.check_landing_handoff("Colt 1", on_runway=True) is None


def test_vacated_report_hands_to_ground_once(airfield, brain):
    # A pilot who reports "vacated" first still gets the handoff, and the
    # proactive check must not repeat it.
    sc = _scenario(airfield, brain).on_final("25")
    sc.say("Tower, Colt 1, on final", Controller.TOWER)
    r = sc.say("Colt 1, runway 25 vacated", Controller.TOWER)
    assert "contact Ground" in r
    assert sc.phase() == Phase.TAXI
    assert brain.check_landing_handoff("Colt 1", on_runway=True) is None


def test_landing_handoff_rearms_on_next_approach(airfield, brain):
    # A go-around / touch-and-go (back on approach) re-arms the handoff for the
    # next landing.
    sc = _scenario(airfield, brain).on_final("25")
    sc.say("Tower, Colt 1, on final", Controller.TOWER)
    brain.check_landing_handoff("Colt 1", on_runway=True)
    assert brain.pilots["Colt 1"].landing_handoff_announced is True
    # Back on approach (go-around): re-arm.
    brain.pilots["Colt 1"].phase = Phase.INBOUND
    brain.check_landing_handoff("Colt 1", on_runway=False)
    assert brain.pilots["Colt 1"].landing_handoff_announced is False


def test_no_handoff_without_landing_clearance(airfield, brain):
    # A pilot in the Landing phase who was never *cleared to land* (e.g. an
    # overhead break) is not pushed to Ground.
    sc = _scenario(airfield, brain).on_final("25")
    sc.say("Tower, Colt 1, overhead break", Controller.TOWER)
    assert sc.phase() == Phase.LANDING
    assert brain.pilots["Colt 1"].cleared_landing is False
    assert brain.check_landing_handoff("Colt 1", on_runway=True) is None


def test_landing_rollout_gate_holds_off_a_touch_and_go():
    # The handoff only fires once the aircraft is on the ground AND slow enough
    # to be a rollout; a touch-and-go (still fast) stays with Tower.
    from atc_bot import is_landing_rollout
    assert is_landing_rollout(on_runway=True, speed_kt=30.0) is True
    assert is_landing_rollout(on_runway=True, speed_kt=58.0) is True   # gauge floor
    assert is_landing_rollout(on_runway=True, speed_kt=140.0) is False  # touch-and-go
    assert is_landing_rollout(on_runway=False, speed_kt=0.0) is False   # on approach


def test_landing_rollout_gate_requires_being_on_the_ground():
    # `runway_occupied` accepts anything below 500 ft AGL, so a low, slow
    # aircraft crossing the threshold was pushed to Ground while still airborne.
    from atc_bot import is_landing_rollout
    assert is_landing_rollout(on_runway=True, speed_kt=60.0, agl_ft=6.0) is True
    assert is_landing_rollout(on_runway=True, speed_kt=60.0, agl_ft=300.0) is False
    assert is_landing_rollout(on_runway=True, speed_kt=60.0, agl_ft=18.0) is True


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
    # Holding at a named point (B) is accepted (not challenged) and hands the
    # pilot to Tower, matching the Master Arms kneeboard wording.
    sc = _scenario(airfield, brain).holding("Holding B")
    sc.say("Ground, Colt 1, requesting taxi", Controller.GROUND)
    r = sc.say("Colt 1, holding short runway 25", Controller.GROUND)
    assert "contact Tower" in r
    assert sc.phase() == Phase.HOLDING


def test_entry_report_far_from_gate_is_challenged(airfield, brain):
    # A pilot who reports "at the entry" while still far away is challenged and
    # NOT handed to Tower (they would arrive unannounced).
    sc = _scenario(airfield, brain).at_gate("North")  # far from Entry West
    sc.say("Kutaisi Control, Colt 1, inbound 35 miles north at Angels 12",
           Controller.CONTROL)
    sc.say("150 to join via Entry East, Colt 1", Controller.CONTROL)
    # Still far from Entry West, but claims to be there (before the descend
    # readback would hand the flight off).
    r = sc.say("Control, Colt 1, entry west", Controller.CONTROL)
    assert "negative" in r.lower()
    assert "contact Tower" not in r
    assert sc.phase() == Phase.INBOUND  # did not advance


def test_entry_report_at_gate_hands_to_tower(airfield, brain):
    # A pilot actually at the entry point is handed to Tower.
    sc = _scenario(airfield, brain).at_gate("West")
    sc.say("Kutaisi Control, Colt 1, inbound 35 miles west at Angels 12",
           Controller.CONTROL)
    sc.say("150 to join via Entry East, Colt 1", Controller.CONTROL)
    r = sc.say("Control, Colt 1, entry west", Controller.CONTROL)
    assert "contact Tower" in r
    assert sc.phase() == Phase.INBOUND
    assert brain.pilots["Colt 1"].pending_handoff_to == Controller.TOWER.value
    sc.say("Tower, Colt 1", Controller.TOWER)
    assert sc.phase() == Phase.LANDING


# ---------- Stepped arrival descent (distance-based) ----------

def test_far_inbound_gets_intermediate_descent(airfield, brain):
    # 35 miles out: Control steps the flight down (5000 ft), not straight to
    # 1500 ft (a real controller does not drop an arrival to the final altitude
    # 35 miles out).
    sc = _scenario(airfield, brain)
    sc.lat = airfield.ctr.center_lat + 0.58  # ~35 NM north of the field
    sc.lon = airfield.ctr.center_lon
    sc.say("Kutaisi Control, Colt 1, inbound 35 miles north at Angels 12",
           Controller.CONTROL)
    r = sc.say("150 to join via Entry East, Colt 1", Controller.CONTROL)
    assert "descend and maintain 5000" in r.lower()
    assert brain.pilots["Colt 1"].descend_intermediate is True
    assert brain.pilots["Colt 1"].descend_issued is False


def test_close_inbound_gets_final_descent(airfield, brain):
    # At the entry point: Control clears the final descent to 1500 ft.
    sc = _scenario(airfield, brain).at_gate("East")
    sc.say("Kutaisi Control, Colt 1, inbound 10 miles east at Angels 12",
           Controller.CONTROL)
    r = sc.say("150 to join via Entry East, Colt 1", Controller.CONTROL)
    assert "descend to 1500 feet" in r.lower()
    assert brain.pilots["Colt 1"].descend_issued is True


def test_final_descent_issued_when_close_after_step(airfield, brain):
    # After the far-out step-down, the monitor issues the final descent once the
    # aircraft reaches the entry area.
    sc = _scenario(airfield, brain)
    sc.lat = airfield.ctr.center_lat + 0.58  # ~35 NM north of the field
    sc.lon = airfield.ctr.center_lon
    sc.say("Kutaisi Control, Colt 1, inbound 35 miles north at Angels 12",
           Controller.CONTROL)
    sc.say("150 to join via Entry East, Colt 1", Controller.CONTROL)
    assert brain.pilots["Colt 1"].descend_intermediate is True
    # Still far: no final descent yet.
    assert brain.check_arrival_descent("Colt 1", sc._track()) is None
    # Now at the entry: the final descent is issued, once.
    sc.at_gate("East")
    call = brain.check_arrival_descent("Colt 1", sc._track())
    assert call and "descend to 1500 feet" in call.lower()
    assert brain.check_arrival_descent("Colt 1", sc._track()) is None


# ---------- Line-up lock regression (the one that trapped a real flight) ----------

def test_lineup_never_traps_the_pilot(airfield, brain):
    sc = _scenario(airfield, brain).at_threshold("25")
    sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    assert sc.phase() == Phase.LINEUP
    # repeating "ready for departure" must still yield a takeoff clearance
    r = sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    assert "cleared for takeoff" in r.lower()
    assert sc.phase() == Phase.TAKEOFF


def test_takeoff_waits_until_actually_lined_up(airfield, brain):
    """Reading back "line up and wait" must NOT clear takeoff on its own.

    The pilot has only acknowledged the instruction; they may still be taxiing
    onto the runway. The clearance follows once the monitor sees them lined up
    (at the threshold, pointing down the runway, stationary).
    """
    sc = _scenario(airfield, brain).holding("Holding C")
    sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    assert sc.phase() == Phase.LINEUP

    # Readback while still at the holding point: acknowledged, not cleared.
    r = sc.say("Line up and wait 25, Colt 1", Controller.TOWER)
    assert "cleared for takeoff" not in r.lower()
    assert sc.phase() == Phase.LINEUP
    # The monitor sees them still holding short -> no clearance yet.
    assert brain.check_lineup("Colt 1", sc._track()) is None

    # Now they have taxied on and are lined up: the monitor clears takeoff.
    sc.at_threshold("25")
    call = brain.check_lineup("Colt 1", sc._track())
    assert call and "cleared for takeoff" in call.lower()
    assert sc.phase() == Phase.TAKEOFF
    # Fires once.
    assert brain.check_lineup("Colt 1", sc._track()) is None


def test_takeoff_withheld_while_still_rolling(airfield, brain):
    """A pilot lined up but still moving is not yet cleared for takeoff."""
    sc = _scenario(airfield, brain).at_threshold("25")
    sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    sc.speed = 40.0  # still rolling onto the runway
    r = sc.say("Line up and wait 25, Colt 1", Controller.TOWER)
    assert "cleared for takeoff" not in r.lower()
    assert brain.check_lineup("Colt 1", sc._track()) is None
    sc.speed = 0.0  # stopped, lined up
    assert brain.check_lineup("Colt 1", sc._track()) is not None


def test_lineup_readback_is_silent(airfield, brain):
    """Line-up readback is not in the SOP as a 'roger' — stay quiet until lined up."""
    sc = _scenario(airfield, brain).holding("Holding C")
    sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    r = sc.say("Line up and wait 25, Colt 1", Controller.TOWER)
    assert r == ""


def test_takeoff_readback_is_silent(airfield, brain):
    """Takeoff-clearance readback has no SOP reply — next call is the airborne handoff."""
    sc = _scenario(airfield, brain).at_threshold("25")
    sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    sc.say("Line up and wait 25, Colt 1", Controller.TOWER)
    r = sc.say("cleared for takeoff runway 25, Colt 1", Controller.TOWER)
    assert r == ""


def test_ctr_handoff_waits_for_final_descent_readback(airfield, brain):
    """CTR entry alone cannot skip the final descent/readback in the SOP."""
    sc = _scenario(airfield, brain).set_alt(5000.0)
    pilot = brain._pilot("Colt 1")
    pilot.phase = Phase.INBOUND
    pilot.checked_in_at = Controller.CONTROL.value
    pilot.entry_gate = "East"
    pilot.descend_intermediate = True
    pilot.descend_readback = True  # readback of the 5000 ft step only

    # The aircraft is in the CTR. A prior intermediate readback isn't enough.
    assert brain.check_arrival_tower_handoff("Colt 1", sc._track()) is None

    # Control issues the final 1500 ft clearance; still wait for its readback.
    descend = brain.check_arrival_descent("Colt 1", sc._track())
    assert descend and "1500" in descend
    assert not pilot.descend_readback
    assert brain.check_arrival_tower_handoff("Colt 1", sc._track()) is None

    # The pilot reads it back. Tower can now be offered because the aircraft
    # is already inside the chosen "near the field" boundary.
    assert sc.say("Descend to 1500 feet, Colt 1", Controller.CONTROL) == ""
    call = brain.check_arrival_tower_handoff("Colt 1", sc._track())
    assert call and "contact Tower" in call
    assert pilot.phase == Phase.INBOUND
    assert pilot.pending_handoff_to == Controller.TOWER.value
    assert brain.check_arrival_tower_handoff("Colt 1", sc._track()) is None
    # A direct Tower check-in completes the handoff.
    sc.say("Tower, Colt 1", Controller.TOWER)
    assert pilot.phase == Phase.LANDING
    assert not pilot.pending_handoff_to


def test_holding_point_beside_runway_is_not_lined_up(airfield):
    """A pilot at a holding point is NOT lined up, even if it is near the
    threshold.

    The position test must be a runway corridor, not a radius: at Kutaisi,
    Holding A/N is only 387 m from the 07 threshold but 387 m *sideways*, and
    Holding B is 565 m down the runway on a taxiway. A radius would clear them
    for takeoff while they are still off the runway.
    """
    for runway in ("25", "07"):
        hdg = airfield.runway_heading(runway)
        for name, point in airfield.holding_points.items():
            assert not airfield.is_lined_up(point[0], point[1], hdg, runway,
                                            speed_kt=0.0), f"{runway} {name}"


def test_lined_up_accepts_threshold_and_rejects_mid_runway(airfield):
    """Lined up = on the centreline at the threshold end, not mid-runway."""
    for runway in ("25", "07"):
        hdg = airfield.runway_heading(runway)
        thr = airfield.runway_threshold(runway)
        assert airfield.is_lined_up(thr[0], thr[1], hdg, runway, speed_kt=0.0)
        # The far threshold is 1.3 nm down the runway: not the line-up point.
        far = airfield.runway_threshold(
            airfield._opposite_runway(runway))
        assert not airfield.is_lined_up(far[0], far[1], hdg, runway,
                                        speed_kt=0.0)


# ---------- Overhead-break direction (per airfield, per runway) ----------

def test_overhead_break_direction_follows_runway(airfield, brain):
    """Kutaisi's break is on the south side: left for 25, right for 07."""
    assert airfield.break_direction("25") == "left"
    assert airfield.break_direction("07") == "right"

    sc = _scenario(airfield, brain).on_final("25")
    r = sc.say("Tower, Colt 1, runway in sight", Controller.TOWER)
    assert "left overhead break" in r.lower()

    brain.set_runway("07")
    sc = _scenario(airfield, brain).on_final("07")
    r = sc.say("Tower, Colt 1, runway in sight", Controller.TOWER)
    assert "right overhead break" in r.lower()


def test_overhead_break_direction_defaults_when_unconfigured(airfield):
    """A runway with no configured direction falls back to the default."""
    assert airfield.break_direction("99") == airfield.default_break_direction


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
    r = sc.say("Trainer, Colt 1, help", controller)
    assert "Apollo suggests" in r
    assert expect in r.lower()


def test_help_after_taxi_points_to_hold_short(airfield, brain):
    sc = _scenario(airfield, brain).park("Ramp South")
    sc.say("Ground, Colt 1, requesting taxi", Controller.GROUND)
    r = sc.say("Trainer, Colt 1, help", Controller.GROUND)
    assert "holding short" in r.lower()


def test_help_while_holding_points_to_tower(airfield, brain):
    sc = _scenario(airfield, brain).at_threshold("25")
    sc.say("Ground, Colt 1, requesting taxi", Controller.GROUND)
    sc.say("Colt 1, holding short runway 25", Controller.GROUND)
    r = sc.say("Trainer, Colt 1, help", Controller.GROUND)
    assert "ready for departure" in r.lower()


def test_help_while_lined_up_mentions_readback(airfield, brain):
    sc = _scenario(airfield, brain).at_threshold("25")
    sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    r = sc.say("Trainer, Colt 1, help", Controller.TOWER)
    assert "read back" in r.lower() or "readback" in r.lower()


def test_help_always_mentions_escape_hatches(airfield, brain):
    sc = _scenario(airfield, brain).park("Ramp South")
    r = sc.say("Trainer, Colt 1, help", Controller.GROUND)
    assert "reset" in r.lower() and "cancel" in r.lower()


def test_help_works_on_any_controller(airfield, brain):
    for controller in (Controller.GROUND, Controller.TOWER, Controller.CONTROL):
        sc = _scenario(airfield, brain).park("Ramp South")
        r = sc.say("Trainer, Colt 1, help", controller)
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
    assert sc.say(
        "After departure Exit West, 1500 ft or below, Colt 1", Controller.GROUND) == ""

    r = sc.say("Colt 1, requesting taxi", Controller.GROUND)
    assert "cleared taxi" in r.lower()
    assert sc.phase() == Phase.TAXI
    assert sc.say(
        "Cleared taxi Sierra Echo and hold short runway 25, Colt 1",
        Controller.GROUND) == ""

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
    assert sc.phase() == Phase.TAKEOFF

    r = sc.say("Tower, Colt 1, airborne", Controller.TOWER)
    assert r == ""
    r = _pass_departure_exit(sc, brain, airfield)
    assert r and "contact Control" in r
    assert sc.phase() == Phase.AIRBORNE

    # Control: departure check-in -> climb (clear of the CTR).
    sc.at_gate("East").set_alt(airfield.elevation_ft + 1500)
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

def test_landing_clearance_readback_is_silent(airfield, brain):
    # A pilot reading back "cleared to land" is busy landing. Master Arms
    # (Landing Procedures §Comms): after the landing clearance and readback
    # "there's generally no need for additional comms" — so the bot stays
    # silent (no "roger", no "say again").
    sc = _scenario(airfield, brain).on_final("25")
    sc.say("Tower, Colt 1, on final", Controller.TOWER)
    assert sc.phase() == Phase.LANDING
    r = sc.say("Runway 25, cleared to land, Colt 1", Controller.TOWER)
    assert r == ""  # deliberate silence (no "roger", no "say again")


# ---------- Live-test regressions (Kutaisi 2026-10-11) ----------

def test_departure_readback_does_not_flip_to_inbound(airfield, brain):
    # A departure readback ("remain below 1500 ft until clear of the control
    # zone via Exit East") contains "control zone" and "via". It must NOT be
    # read as an arrival check-in (the bare "control" branch) nor as a join
    # readback — the phase must stay Airborne.
    sc = _scenario(airfield, brain).at_gate("East").set_alt(2000)
    sc.say("Control, Colt 1 airborne, 600 feet", Controller.CONTROL)
    assert sc.phase() == Phase.AIRBORNE
    r = sc.say("Remaining below 1500 feet until clear of the control zone "
               "via exit east Colt 1", Controller.CONTROL)
    assert sc.phase() == Phase.AIRBORNE
    assert "say again" not in r.lower()
    # A later exit-point report is acknowledged, still Airborne.
    r = sc.say("Colt 1, exit east", Controller.CONTROL)
    assert sc.phase() == Phase.AIRBORNE
    assert "say again" not in r.lower()


def test_touchdown_handoff_is_not_a_runway_incursion(airfield, brain):
    # The Tower->Ground handoff fires at touchdown (phase becomes Taxi while
    # still rolling out). That must not be flagged as an uncleared incursion.
    sc = _scenario(airfield, brain).on_final("25").set_alt(500)
    sc.say("Tower, Colt 1, on final", Controller.TOWER)
    assert sc.phase() == Phase.LANDING
    handoff = brain.check_landing_handoff("Colt 1", on_runway=True)
    assert handoff is not None and "contact Ground" in handoff
    assert sc.phase() == Phase.TAXI
    assert brain.check_incursion("Colt 1", on_runway=True) is None


def test_runway_vacated_on_ground_is_acknowledged(airfield, brain):
    # Kneeboard RTB page: "runway 25 vacated, requesting taxi to parking" is
    # said on GROUND (after the handoff). A bare "vacated" gets a roger; the
    # combined call gets the taxi clearance.
    sc = _scenario(airfield, brain).on_final("25").set_alt(500)
    sc.say("Tower, Colt 1, on final", Controller.TOWER)
    brain.check_landing_handoff("Colt 1", on_runway=True)
    r = sc.say("Colt 1, runway vacated", Controller.GROUND)
    assert "say again" not in r.lower()
    r = sc.say("Colt 1, runway 25 vacated, requesting taxi to parking",
               Controller.GROUND)
    assert "cleared taxi" in r.lower()


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
    assert "Tower" in warning
    assert brain.working_controller("Colt 1") == Controller.TOWER


def test_ctr_warning_uses_working_agency(airfield, brain):
    # After Stingray / Control, the pilot is not on Tower. The CTR warning
    # must identify as Control so it matches the frequency they hear it on.
    from ctr import CtrEvent
    sc = _scenario(airfield, brain).at_gate("North")
    sc.say("Kutaisi Control, Colt 1, radio check", Controller.CONTROL)
    warning = brain.on_ctr_event("Colt 1", CtrEvent.ENTERED, sc._track())
    assert warning is not None and "without clearance" in warning.lower()
    assert "Control" in warning
    assert brain.working_controller("Colt 1") == Controller.CONTROL


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
    assert sc.phase() == Phase.TAKEOFF
    r = sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    assert r is not None  # answered, not silent
    assert sc.phase() == Phase.TAKEOFF  # still cleared


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
    call = None
    for _ in range(INCURSION_POLLS):
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
    call = None
    for _ in range(INCURSION_POLLS):
        call = brain.check_incursion("Colt 1", on_runway=True)
    assert call is not None
    assert brain.check_incursion("Colt 1", on_runway=True) is None
    brain.check_incursion("Colt 1", on_runway=False)  # re-arm
    call = None
    for _ in range(INCURSION_POLLS):
        call = brain.check_incursion("Colt 1", on_runway=True)
    assert call is not None


def test_runway_incursion_silent_after_airborne_call(airfield, brain):
    # A departing aircraft that has called "airborne" (phase Airborne) is
    # climbing out over the runway corridor — NOT an incursion. This fired a
    # false "vacate immediately" right after takeoff in a live test.
    sc = _scenario(airfield, brain).at_threshold("25")
    sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    sc.say("Line up and wait 25, Colt 1", Controller.TOWER)  # -> Departure
    sc.say("Tower, Colt 1, airborne", Controller.TOWER)      # report only
    assert brain.pilots["Colt 1"].phase == Phase.TAKEOFF
    assert _pass_departure_exit(sc, brain, airfield)
    assert brain.pilots["Colt 1"].phase == Phase.AIRBORNE
    assert brain.check_incursion("Colt 1", on_runway=True) is None


# ---------- Debrief regressions: 2026-10-18 ma--pancake Adder-1 sortie ----------
# Every test here maps to a transmission that botched a real training flight
# (see the debrief comms log at 12:50-12:56). Do not fold in new behaviour.

def test_debrief_descending_and_maintaining_5000_is_accepted(airfield, brain):
    # "Colt 1, descending and maintaining 5,000 feet" (STT: inflected verb,
    # comma in the figure) was rejected -> two "say again" loops.
    sc = _scenario(airfield, brain)
    sc.lat = airfield.ctr.center_lat + 0.58
    sc.lon = airfield.ctr.center_lon
    sc.say("Kutaisi Control, Colt 1, inbound 35 miles north at Angels 12",
           Controller.CONTROL)
    r = sc.say("150 to join via Entry East, Colt 1", Controller.CONTROL)
    assert "descend and maintain 5000" in r.lower()
    reply = sc.say("descending and maintaining 5,000 feet, Colt 1",
                   Controller.CONTROL)
    assert "say again" not in (reply or "").lower()
    # Still stepped down (intermediate); the final descent follows on radar.
    assert brain.pilots["Colt 1"].descend_intermediate is True


def test_debrief_right_overhead_brake_readback_is_silent(airfield, brain):
    # "right overhead brake, Colt 1" (STT homophone) — and the pilot said no
    # "cleared" — was answered "say again" three times.
    sc = _scenario(airfield, brain).at_gate("East")
    sc.say("Tower, Colt 1, runway in sight", Controller.TOWER)
    assert sc.say("right overhead brake, Colt 1", Controller.TOWER) == ""


def test_debrief_bare_overhead_brake_without_cleared_is_accepted(airfield, brain):
    # The MA reply is "cleared ... overhead break"; the pilot's readback is
    # often just "right overhead break" — no "cleared". Not a new request.
    sc = _scenario(airfield, brain).at_gate("East")
    sc.say("Tower, Colt 1, runway in sight", Controller.TOWER)
    reply = sc.say("cleared for the right overhead brake, Colt 1",
                   Controller.TOWER)
    assert reply == ""


def test_debrief_runway_seven_taxi_readback_is_correct(airfield, brain):
    # "runway seven" (words, no leading zero) failed the check against
    # clearance "runway 07" -> "readback incomplete, I did not get your runway".
    sc = _scenario(airfield, brain).park("Ramp South")
    brain.set_runway("07")
    sc.say("Ground, Colt 1, requesting taxi", Controller.GROUND)
    r = sc.say("runway 7, Colt 1", Controller.GROUND)
    assert "say again" not in (r or "").lower()
    assert "did not get" not in (r or "").lower()


def test_debrief_turning_base_is_silent(airfield, brain):
    # "turning base" was met with "say again" every leg of the pattern.
    sc = _scenario(airfield, brain).at_gate("East")
    sc.say("Tower, Colt 1, runway in sight", Controller.TOWER)
    sc.say("right overhead break, Colt 1", Controller.TOWER)
    assert sc.say("turning base, Colt 1", Controller.TOWER) == ""
    assert sc.say("downwind, Colt 1", Controller.TOWER) == ""


def test_debrief_wrong_exit_say_again_not_did_not_get(airfield, brain):
    # A readback that names a DIFFERENT exit must not be answered "I did not
    # get your exit" — the pilot did read back an exit.
    sc = _scenario(airfield, brain).park("Ramp South")
    sc.say("Ground, Colt 1, ready to copy clearance", Controller.GROUND)
    clearance = brain.pilots["Colt 1"].last_clearance
    # swap the exit gate in the readback for another configured gate
    gates = [g for g in brain.gates
             if g.lower() not in (clearance or "").lower()]
    assume = gates[0] if gates else "North"
    text = f"after departure, turn right, exit {assume.lower()}, 1500, Colt 1"
    r = sc.say(text, Controller.GROUND)
    assert "did not get" not in (r or "").lower()
    assert "say again" in (r or "").lower()


def test_debrief_bare_control_checkin_after_takeoff_stays_airborne(airfield,
                                                                   brain):
    # "Control, Adder one" right after departure flipped Airborne -> Inbound,
    # killing the climb clearance.
    sc = _scenario(airfield, brain).at_threshold("25")
    sc.say("Tower, Colt 1, ready for departure", Controller.TOWER)
    sc.say("Line up and wait 25, Colt 1", Controller.TOWER)   # -> Departure
    sc.say("right turn out, cleared for takeoff 25, Colt 1", Controller.TOWER)
    sc.say("Tower, Colt 1, airborne", Controller.TOWER)       # -> Airborne
    sc.say("Control, Colt 1, at 1500 feet", Controller.CONTROL)
    assert brain.pilots["Colt 1"].phase == Phase.AIRBORNE
    # A BARE check-in after the climb was issued must NOT flip to Inbound.
    sc.say("Control, Colt 1", Controller.CONTROL)
    assert brain.pilots["Colt 1"].phase == Phase.AIRBORNE


def test_debrief_wrong_frequency_ready_call_gets_handoff_again(airfield, brain):
    # "Kutaisi Tower, ready to takeoff" on GROUND's frequency (pilot never
    # switched) got "say again" twice; a real ground controller re-issues the
    # handoff.
    sc = _scenario(airfield, brain).park("Ramp South")
    sc.say("Ground, Colt 1, requesting taxi", Controller.GROUND)
    sc.holding("Holding A/N")  # taxi out to the holding point first
    sc.say("Ground, Colt 1, holding short runway 25", Controller.GROUND)
    r = sc.say("Kutaisi Tower, Colt 1, ready to takeoff", Controller.GROUND)
    assert "contact tower" in (r or "").lower()


# ---------- Regressions from the 2026-10-22 Mags debrief ----------

def test_departure_handoff_has_no_bogus_exit_note(airfield, brain):
    """The "not near Exit X" note must not fire on a normal departure.

    The handoff happens seconds after liftoff, when the assigned gate is 5-8 NM
    away by definition. Measuring the gate distance against a 1.5 NM radius made
    the note fire on *every* departure (debrief 2026-10-22: "note: you are not
    near Exit East" 47 s after takeoff, 1.1 NM from the runway).
    """
    sc = _scenario(airfield, brain).park("Ramp South")
    sc.say("Colt 1, ready to copy clearance", Controller.GROUND)
    gate = brain.pilots["Colt 1"].exit_gate
    sc.say(f"After departure Exit {gate}, 1500 ft or below, Colt 1",
           Controller.GROUND)
    sc.say("Colt 1, requesting taxi", Controller.GROUND)
    sc.at_threshold("25")
    sc.say("Colt 1, holding short runway 25", Controller.GROUND)
    sc.say("Tower, Colt 1, at runway 25, ready for departure",
           Controller.TOWER)
    sc.say("Line up and wait 25, Colt 1", Controller.TOWER)
    # The pilot does NOT report "airborne": Tower's handoff monitor drives the
    # transition (as in the 2026-10-22 sortie), which is where the note fired.
    sc.at_gate(gate).set_alt(airfield.elevation_ft + 1500)
    reply = brain.check_departure_handoff("Colt 1", sc._track())
    assert reply is not None
    assert "contact Control" in reply
    assert "not near" not in reply.lower()
