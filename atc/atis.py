"""ATIS: automatic terminal information service.

Master Arms broadcasts ATIS on a separate frequency (UHF 270.500, per-airfield).
It gives the active runway, QNH, weather, and an **information letter** that
changes with the hour ("Charlie", "Delta", ...). Pilots listen before contacting
Ground and say "with information Charlie".

This module builds that broadcast from live DCS weather (the bridge's `weather`
op) and the airfield config. The active runway is chosen from the wind: the
runway whose heading is most into wind.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from airspace import Airfield

# NATO phonetic alphabet, used for the information identifier.
PHONETIC = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf",
            "Hotel", "India", "Juliett", "Kilo", "Lima", "Mike", "November",
            "Oscar", "Papa", "Quebec", "Romeo", "Sierra", "Tango", "Uniform",
            "Victor", "Whiskey", "Xray", "Yankee", "Zulu"]

MMHG_TO_INHG = 0.0393701


def information_letter(hour: int) -> str:
    """ATIS information identifier for a given hour (0-23 -> Alpha..Xray)."""
    return PHONETIC[hour % 24]


def qnh_inhg(qnh_mmhg: float) -> float:
    """Convert QNH from mmHg (DCS) to inches of mercury (aviation)."""
    return qnh_mmhg * MMHG_TO_INHG


def wind_components(wind_dir: float, wind_speed: float,
                    runway_heading: float) -> tuple[float, float]:
    """Return (headwind, crosswind) in the same units as wind_speed.

    Positive headwind = into wind; positive crosswind = from the right.
    """
    import math
    angle = math.radians(wind_dir - runway_heading)
    headwind = wind_speed * math.cos(angle)
    crosswind = wind_speed * math.sin(angle)
    return headwind, crosswind


@dataclass
class AtisReport:
    airfield: str
    information: str
    active_runway: str
    wind_dir: float
    wind_speed: float
    qnh_mmhg: float
    qnh_inhg: float
    temperature_c: float
    visibility_m: float
    clouds_base_m: float
    cavok: bool

    def broadcast(self) -> str:
        """The spoken ATIS text (single pass)."""
        wind = (f"wind {self.wind_dir:03.0f} degrees, {self.wind_speed:.0f} "
                f"meters per second" if self.wind_speed > 0.5 else "wind calm")
        if self.cavok:
            weather = "CAVOK"
        else:
            weather = (f"visibility {self.visibility_m / 1000:.0f} kilometers, "
                       f"clouds {self.clouds_base_m:.0f} meters")
        return (f"{self.airfield} information {self.information}. "
                f"{self.active_runway} in use. {wind}. "
                f"QNH {self.qnh_inhg:.2f}. {weather}. "
                f"Temperature {self.temperature_c:.0f}. "
                f"Advise on initial contact you have information {self.information}.")


def choose_active_runway(airfield: Airfield, wind_dir: float,
                         wind_speed: float) -> str:
    """Pick the runway most into wind (highest headwind component).

    Falls back to the configured active runway if wind is calm or no runways
    are defined.
    """
    if wind_speed < 0.5 or not airfield.runways:
        return airfield.active_runway
    best, best_headwind = airfield.active_runway, float("-inf")
    for runway in airfield.runways:
        heading = airfield.runway_heading(runway)
        if heading is None:
            continue
        headwind, _ = wind_components(wind_dir, wind_speed, heading)
        if headwind > best_headwind:
            best, best_headwind = runway, headwind
    return best


def build_atis(airfield: Airfield, weather: dict,
               now: datetime.datetime | None = None) -> AtisReport:
    """Build an ATIS report from live weather and the airfield config."""
    now = now or datetime.datetime.now()
    wind_dir = float(weather.get("wind_dir", 0.0))
    wind_speed = float(weather.get("wind_speed_ms", 0.0))
    qnh_mmhg = float(weather.get("qnh_mmhg", 760.0))
    visibility_m = float(weather.get("visibility_m", 10000.0))
    clouds_base_m = float(weather.get("clouds_base_m", 0.0))
    temperature_c = float(weather.get("temperature_c", 15.0))

    # CAVOK: visibility >= 10 km, no significant cloud below 5000 ft, no precip
    cavok = visibility_m >= 10000 and clouds_base_m >= 1500

    return AtisReport(
        airfield=airfield.name,
        information=information_letter(now.hour),
        active_runway=choose_active_runway(airfield, wind_dir, wind_speed),
        wind_dir=wind_dir,
        wind_speed=wind_speed,
        qnh_mmhg=qnh_mmhg,
        qnh_inhg=qnh_inhg(qnh_mmhg),
        temperature_c=temperature_c,
        visibility_m=visibility_m,
        clouds_base_m=clouds_base_m,
        cavok=cavok,
    )
