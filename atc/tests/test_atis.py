"""ATIS: information letter, QNH, wind-driven runway, broadcast text."""

import datetime

from atis import (AtisReport, build_atis, choose_active_runway,
                  information_letter, qnh_inhg, wind_components)


def test_information_letter_by_hour():
    assert information_letter(0) == "Alpha"
    assert information_letter(2) == "Charlie"
    assert information_letter(14) == "Oscar"
    assert information_letter(25) == "Bravo"  # wraps


def test_qnh_conversion():
    assert abs(qnh_inhg(760.0) - 29.92) < 0.01


def test_wind_components_headwind():
    # wind from 250, runway 250 -> full headwind, no crosswind
    head, cross = wind_components(250, 10, 250)
    assert abs(head - 10) < 0.01
    assert abs(cross) < 0.01


def test_wind_components_crosswind():
    # wind from 340, runway 250 -> mostly crosswind
    head, cross = wind_components(340, 10, 250)
    assert abs(head) < 5
    assert abs(cross) > 5


def test_choose_active_runway_into_wind(airfield):
    # wind from 250 favours runway 25
    assert choose_active_runway(airfield, 250, 10) == "25"
    # wind from 070 favours runway 07
    assert choose_active_runway(airfield, 70, 10) == "07"


def test_choose_active_runway_calm_falls_back(airfield):
    assert choose_active_runway(airfield, 0, 0) == airfield.active_runway


def test_build_atis_cavok(airfield):
    weather = {"wind_dir": 250, "wind_speed_ms": 5, "qnh_mmhg": 760,
               "visibility_m": 10000, "clouds_base_m": 2000,
               "temperature_c": 20}
    report = build_atis(airfield, weather,
                        now=datetime.datetime(2026, 10, 7, 14, 0))
    assert report.information == "Oscar"
    assert report.active_runway == "25"
    assert report.cavok
    text = report.broadcast()
    assert "information Oscar" in text
    assert "CAVOK" in text
    assert "QNH 29.92" in text


def test_build_atis_non_cavok(airfield):
    weather = {"wind_dir": 250, "wind_speed_ms": 5, "qnh_mmhg": 760,
               "visibility_m": 3000, "clouds_base_m": 300,
               "temperature_c": 20}
    report = build_atis(airfield, weather)
    assert not report.cavok
    assert "visibility" in report.broadcast()


def test_build_atis_calm_wind(airfield):
    weather = {"wind_dir": 0, "wind_speed_ms": 0, "qnh_mmhg": 760,
               "visibility_m": 10000, "clouds_base_m": 2000,
               "temperature_c": 20}
    report = build_atis(airfield, weather)
    assert "wind calm" in report.broadcast()
