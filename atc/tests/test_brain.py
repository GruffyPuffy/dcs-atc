"""Brain: intent recognition, controller routing, per-pilot state machine."""

from brain import Controller, Phase
from ctr import AircraftTrack


def _track(airfield, lat, lon, alt_ft=1000.0, heading=0.0):
    """Build an AircraftTrack for a raw position (no tracker state)."""
    return AircraftTrack(
        callsign="x", inside=airfield.ctr.contains(lat, lon, alt_ft),
        lat=lat, lon=lon, alt_ft=alt_ft,
        distance_nm=airfield.ctr.distance_nm(lat, lon),
        relative=airfield.ctr.relative_position(lat, lon), heading=heading)


# ---------- Ground ----------

def test_ground_clearance_assigns_exit_gate(brain):
    reply = brain.handle("Ground, Colt 1, ready to copy clearance",
                         controller=Controller.GROUND)
    assert "Colt 1" in reply
    assert "Ground" in reply
    assert brain.pilots["Colt 1"].phase == Phase.CLEARANCE
    assert brain.pilots["Colt 1"].exit_gate


def test_ground_taxi(brain):
    reply = brain.handle("Ground, Colt 1, requesting taxi",
                         controller=Controller.GROUND)
    assert "taxi to runway" in reply
    assert brain.pilots["Colt 1"].phase == Phase.TAXI


def test_ground_taxi_route_by_position(brain, airfield):
    # west apron -> alpha, east apron -> bravo (routes configured per airfield)
    west = _track(airfield, 42.182, 42.470)
    reply = brain.handle("Ground, Colt 1, requesting taxi", track=west,
                         controller=Controller.GROUND)
    assert "via alpha" in reply
    brain.pilots.clear()
    east = _track(airfield, 42.183, 42.490)
    reply = brain.handle("Ground, Colt 1, requesting taxi", track=east,
                         controller=Controller.GROUND)
    assert "via bravo" in reply


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
    assert "cleared for takeoff" in reply
    assert brain.pilots["Colt 1"].phase == Phase.DEPARTURE


def test_tower_runway_in_sight_overhead(brain):
    reply = brain.handle("Tower, Colt 1, runway in sight",
                         controller=Controller.TOWER)
    assert "overhead break" in reply


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


def test_control_inbound_uses_spoken_direction(brain):
    # the direction the pilot says should win over the live position
    reply = brain.handle("Control, Colt 1, inbound 35 miles north",
                         controller=Controller.CONTROL)
    assert "Entry North" in reply
    assert brain.pilots["Colt 1"].entry_gate == "Entry North"


def test_control_inbound_spoken_west(brain):
    reply = brain.handle("Control, Colt 1, inbound 20 miles west",
                         controller=Controller.CONTROL)
    assert "Entry West" in reply


def test_control_departure_checkin_radar_contact(brain):
    # departure check-in gets radar contact, not a join-via-entry routing
    reply = brain.handle("Control, Colt 1, airborne, 5 miles east climbing",
                         controller=Controller.CONTROL)
    assert "radar contact" in reply
    assert "join via" not in reply
    assert brain.pilots["Colt 1"].phase == Phase.AIRBORNE


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


def test_help_requires_callsign(brain):
    assert brain.handle("help me please", controller=Controller.TOWER) is None


# ---------- Position cross-check (trainer: challenge bad reports) ----------

def test_holding_short_challenged_when_on_ramp(brain, airfield):
    brain.handle("Ground, Colt 1, requesting taxi", controller=Controller.GROUND)
    # pilot is still on the ramp (far from the runway) but claims holding short
    ramp = _track(airfield, airfield.ctr.center_lat, airfield.ctr.center_lon)
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


def test_ready_for_departure_challenged_when_not_at_runway(brain, airfield):
    ramp = _track(airfield, airfield.ctr.center_lat, airfield.ctr.center_lon)
    reply = brain.handle("Tower, Colt 1, ready for departure",
                         track=ramp, controller=Controller.TOWER)
    assert "negative" in reply.lower()
    assert brain.pilots["Colt 1"].phase != Phase.DEPARTURE


def test_ready_for_departure_accepted_at_runway(brain, airfield):
    thr = airfield.runway_threshold("25")
    at_rwy = _track(airfield, thr[0], thr[1])
    reply = brain.handle("Tower, Colt 1, ready for departure",
                         track=at_rwy, controller=Controller.TOWER)
    assert "cleared for takeoff" in reply
    assert brain.pilots["Colt 1"].phase == Phase.DEPARTURE


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
