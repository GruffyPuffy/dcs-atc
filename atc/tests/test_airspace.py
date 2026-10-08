"""Airspace geometry: CTR containment, runway heading, gates, occupancy."""

from dataclasses import dataclass

from airspace import Airspace


def test_loads_kutaisi(airfield):
    assert airfield.name == "Kutaisi"
    assert airfield.frequency_mhz == 263.0
    assert airfield.ground_frequency_mhz == 250.0
    assert airfield.control_frequency_mhz == 257.0
    assert airfield.active_runway == "25"


def test_ctr_contains_airfield(airfield):
    # the airfield reference point is inside its own CTR at low altitude
    assert airfield.ctr.contains(airfield.ctr.center_lat,
                                 airfield.ctr.center_lon, 1000)


def test_ctr_excludes_high_altitude(airfield):
    ceiling = airfield.ctr.ceiling_ft_msl
    assert not airfield.ctr.contains(airfield.ctr.center_lat,
                                     airfield.ctr.center_lon, ceiling + 500)


def test_runway_heading_uses_thresholds(airfield):
    # 25/07 thresholds are defined, so heading comes from the bearing
    heading = airfield.runway_heading("25")
    assert heading is not None
    assert 240 <= heading <= 280


def test_opposite_runway(airfield):
    assert airfield._opposite_runway("25") == "07"
    assert airfield._opposite_runway("07") == "25"


def test_nearest_gate(airfield):
    # a point near the East gate should resolve to East
    east = airfield.gates["East"]
    assert airfield.nearest_gate(east[0], east[1]) == "East"


def test_distance_to_threshold(airfield):
    thr = airfield.runway_threshold("25")
    assert airfield.distance_to_threshold_nm(thr[0], thr[1], "25") < 0.1


@dataclass
class FakeUnit:
    callsign: str
    lat: float
    lon: float
    alt_ft: float


def test_runway_occupied_detects_unit_on_threshold(airfield):
    thr = airfield.runway_threshold("25")
    on_rwy = FakeUnit("Bogey", thr[0], thr[1], airfield.elevation_ft + 10)
    assert airfield.runway_occupied([on_rwy])


def test_runway_occupied_excludes_self(airfield):
    thr = airfield.runway_threshold("25")
    on_rwy = FakeUnit("Colt 1", thr[0], thr[1], airfield.elevation_ft + 10)
    assert not airfield.runway_occupied([on_rwy], exclude="Colt 1")


def test_runway_occupied_ignores_high_aircraft(airfield):
    thr = airfield.runway_threshold("25")
    high = FakeUnit("Over", thr[0], thr[1], airfield.elevation_ft + 5000)
    assert not airfield.runway_occupied([high])


def test_runway_occupied_covers_both_ends(airfield):
    # a unit just past the far (07) threshold still occupies the runway
    far = airfield.runway_threshold("07")
    unit = FakeUnit("Bogey", far[0], far[1], airfield.elevation_ft + 10)
    assert airfield.runway_occupied([unit])


def test_runway_occupied_uses_real_length(airfield):
    # the corridor spans the whole runway, not a fixed 1.6 NM
    assert airfield.runway_length_nm("25") > 1.0


def test_holding_short_does_not_block_landing(airfield):
    # a pilot holding short at a parallel taxiway holding point is NOT on the
    # runway, so must not block a landing clearance
    for name, (lat, lon) in airfield.holding_points.items():
        unit = FakeUnit(name, lat, lon, airfield.elevation_ft + 10)
        assert not airfield.runway_occupied([unit]), name


def test_parked_aircraft_does_not_block_landing(airfield):
    for name, (lat, lon) in airfield.parking_areas.items():
        unit = FakeUnit(name, lat, lon, airfield.elevation_ft + 10)
        assert not airfield.runway_occupied([unit]), name


def test_set_active_runway_switches_checks(airfield):
    # with 07 active, a unit at the 07 threshold occupies; at 25 it does not
    thr07 = airfield.runway_threshold("07")
    unit = FakeUnit("Bogey", thr07[0], thr07[1], airfield.elevation_ft + 10)
    airfield.set_active_runway("07")
    try:
        assert airfield.runway_occupied([unit])
        assert airfield.final_zone_geometry()["center"] == list(thr07)
    finally:
        airfield.set_active_runway("25")


def test_set_active_runway_ignores_unknown(airfield):
    airfield.set_active_runway("99")
    assert airfield.active_runway == "25"


def test_nearest_airfield():
    airspace = Airspace.load()
    af = airspace.get("Kutaisi")
    nearest = airspace.nearest(af.ctr.center_lat, af.ctr.center_lon)
    assert nearest.name == "Kutaisi"


def test_relative_position_singular_mile(airfield):
    # a point ~1 NM from the reference should read "1 mile", not "1 miles"
    import math
    lat = airfield.ctr.center_lat + (1.0 * 1852.0) / 111_320.0
    text = airfield.ctr.relative_position(lat, airfield.ctr.center_lon)
    assert text.startswith("1 mile ")
    assert "miles" not in text


def test_relative_position_plural_miles(airfield):
    lat = airfield.ctr.center_lat + (4.0 * 1852.0) / 111_320.0
    text = airfield.ctr.relative_position(lat, airfield.ctr.center_lon)
    assert text.startswith("4 miles ")


# ---------- Holding points + check zones ----------

def test_holding_points_loaded(airfield):
    # P1..P4 from the MA aerodrome chart
    assert set(airfield.holding_points) == {
        "Holding C", "Holding B", "Holding A/N", "Holding S/W"}


def test_nearest_holding_point(airfield):
    p1 = airfield.holding_points["Holding C"]
    assert airfield.nearest_holding_point(p1[0], p1[1]) == "Holding C"


def test_check_zone_holding(airfield):
    thr = airfield.runway_threshold("25")
    assert airfield.is_holding_short(thr[0], thr[1])
    # on the ramp, far from the runway -> not holding short
    assert not airfield.is_holding_short(42.183, 42.472)


def test_is_holding_short_accepts_named_points(airfield):
    # P2/P3/P4 are 1-1.5 NM from the threshold; holding there must be accepted
    for name, (lat, lon) in airfield.holding_points.items():
        assert airfield.is_holding_short(lat, lon), name


def test_holding_zone_geometry_covers_points(airfield):
    zones = airfield.holding_zone_geometry()
    labels = {z["label"] for z in zones}
    assert "threshold" in labels
    assert set(airfield.holding_points) <= labels


def test_final_zone_geometry_is_a_wedge(airfield):
    wedge = airfield.final_zone_geometry()
    assert wedge is not None
    assert wedge["radius_nm"] == 12.0
    assert wedge["runway"] == airfield.active_runway
    # the wedge should straddle the approach direction (reciprocal of runway)
    inbound = (airfield.runway_heading("25") + 180) % 360
    assert abs(((wedge["start_deg"] + wedge["end_deg"]) / 2 - inbound + 180) % 360 - 180) < 1


def test_runway_corridor_geometry(airfield):
    corridor = airfield.runway_corridor_geometry()
    assert corridor is not None
    assert len(corridor["corners"]) == 4
