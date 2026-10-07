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


def test_nearest_airfield():
    airspace = Airspace.load()
    af = airspace.get("Kutaisi")
    nearest = airspace.nearest(af.ctr.center_lat, af.ctr.center_lon)
    assert nearest.name == "Kutaisi"
