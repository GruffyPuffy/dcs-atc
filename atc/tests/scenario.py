"""System-test scenario harness: fly full dialogs through the brain.

This is a **system test without SRS or DCS**. It drives the real `AtcBrain`
through complete pilot/controller exchanges while we fake the live radar: the
pilot's own position (for position cross-checks, final approach, etc.) and the
traffic list (for runway occupancy / sequencing).

Example:

    sc = Scenario(airfield, brain, callsign="Colt 1", speaker="Caveman")
    sc.park("Ramp South")
    sc.say("Ground, Colt 1, two-ship Hornets on Ramp South", GROUND)
    sc.say("Adder11...")  # ...

Positions are set with `park`, `at_threshold`, `on_final`, `holding`, etc.;
traffic with `traffic_on_runway`. `say(...)` returns the reply and records it.
"""

from __future__ import annotations

import math

from ctr import AircraftTrack

EARTH_RADIUS_M = 6_371_000.0
M_PER_NM = 1852.0


def _runway_point(airfield, runway: str, along_nm: float,
                  across_nm: float = 0.0) -> tuple[float, float]:
    """A lat/lon offset from a runway threshold.

    `along_nm` runs in the landing direction (negative = before the threshold);
    `across_nm` is perpendicular (positive = right of the runway heading).
    """
    thr = airfield.runway_threshold(runway)
    hdg = airfield.runway_heading(runway)
    along = math.radians(hdg)
    across = math.radians(hdg + 90)
    north_m = along_nm * M_PER_NM * math.cos(along) \
        + across_nm * M_PER_NM * math.cos(across)
    east_m = along_nm * M_PER_NM * math.sin(along) \
        + across_nm * M_PER_NM * math.sin(across)
    lat = thr[0] + math.degrees(north_m / EARTH_RADIUS_M)
    lon = thr[1] + math.degrees(
        east_m / (EARTH_RADIUS_M * math.cos(math.radians(thr[0]))))
    return lat, lon


def _bearing_range_point(lat: float, lon: float, bearing: float,
                         range_nm: float) -> tuple[float, float]:
    """A lat/lon at `bearing`/`range_nm` from a reference point.

    The inverse of `bearing_distance`, so a position built from a bullseye
    bearing/range reads back as that same bearing/range.
    """
    rad = math.radians(bearing)
    north_m = range_nm * M_PER_NM * math.cos(rad)
    east_m = range_nm * M_PER_NM * math.sin(rad)
    out_lat = lat + math.degrees(north_m / EARTH_RADIUS_M)
    out_lon = lon + math.degrees(
        east_m / (EARTH_RADIUS_M * math.cos(math.radians(lat))))
    return out_lat, out_lon


class Unit:
    """A fake live unit (player or AI) for traffic / occupancy checks."""

    def __init__(self, callsign: str, lat: float, lon: float, alt_ft: float,
                 player: str = ""):
        self.callsign = callsign
        self.player = player
        self.lat = lat
        self.lon = lon
        self.alt_ft = alt_ft


class Scenario:
    """Fly a pilot through the brain with faked radar + traffic."""

    def __init__(self, airfield, brain, callsign: str = "Colt 1",
                 speaker: str = "Caveman", traffic: list | None = None):
        self.airfield = airfield
        self.brain = brain
        self.callsign = callsign
        self.speaker = speaker
        self.lat = airfield.ctr.center_lat
        self.lon = airfield.ctr.center_lon
        self.heading = 0.0
        self.alt = 1000.0
        self.speed = 0.0
        # `traffic` may be a shared list so several scenarios see one radar
        # picture (interleaved flights); otherwise each scenario owns its own.
        self._traffic: list[Unit] = traffic if traffic is not None else []
        self.log: list[tuple[str, str | None]] = []

    # ---- position helpers (all return self, so they chain) ----

    def park(self, ramp: str) -> "Scenario":
        self.lat, self.lon = self.airfield.parking_areas[ramp]
        return self

    def at_gate(self, gate: str) -> "Scenario":
        self.lat, self.lon = self.airfield.gates[gate]
        return self

    def at_threshold(self, runway: str = "25") -> "Scenario":
        self.lat, self.lon = self.airfield.runway_threshold(runway)
        # A pilot at the threshold is pointing down the runway, stationary.
        self.heading = self.airfield.runway_heading(runway) or 0.0
        self.speed = 0.0
        return self

    def holding(self, name: str) -> "Scenario":
        self.lat, self.lon = self.airfield.holding_points[name]
        return self

    def on_final(self, runway: str = "25", nm: float = 5.0) -> "Scenario":
        """Place the aircraft `nm` from the threshold on the approach side,
        aligned with the runway heading."""
        self.lat, self.lon = _runway_point(self.airfield, runway, -nm)
        self.heading = self.airfield.runway_heading(runway)
        return self

    def set_heading(self, heading: float) -> "Scenario":
        self.heading = heading
        return self

    def set_alt(self, alt_ft: float) -> "Scenario":
        self.alt = alt_ft
        return self

    def at_runway(self, runway: str = "25", along_nm: float = 0.0,
                  across_nm: float = 0.0) -> "Scenario":
        """Place the pilot at a runway-relative offset.

        `along_nm` is distance from the threshold in the landing direction
        (negative = before the threshold, i.e. the overrun/holding side);
        `across_nm` is perpendicular (positive = right of the runway heading).
        """
        self.lat, self.lon = _runway_point(self.airfield, runway,
                                           along_nm, across_nm)
        return self

    def at_bullseye(self, bearing: float, range_nm: float) -> "Scenario":
        """Place the pilot at a bullseye-relative offset.

        `bearing`/`range_nm` are from the mission bullseye, exactly as a pilot
        would read them off the HSI — so a bullseye call built from these
        values is a *correct* call. Requires the brain to have a bullseye set
        (`brain.set_bullseye`).
        """
        blat, blon = self.brain.bullseye
        self.lat, self.lon = _bearing_range_point(blat, blon, bearing, range_nm)
        return self

    def bullseye_call(self, bearing: float, range_nm: float) -> str:
        """A bullseye picture call for the pilot's current position.

        Reads the true bearing/range from the mission bullseye, so the call is
        correct by construction (use `at_bullseye` first, or pass explicit
        values to make a deliberately wrong call).
        """
        return (f"Stingray, {self.callsign}, bullseye {bearing:03.0f} for "
                f"{range_nm:.0f}, Angels 15, as fragged")

    def true_bullseye(self) -> tuple[float, float]:
        """The geodesic-true (bearing, range) from the bullseye to the pilot."""
        from airspace import bearing_distance
        blat, blon = self.brain.bullseye
        return bearing_distance(blat, blon, self.lat, self.lon)

    def cockpit_bullseye(self) -> tuple[float, float]:
        """(bearing, range) as the pilot's cockpit displays them (true minus
        the airfield's cockpit bearing convention — see airspace.py); this is
        the number the pilot would actually say."""
        bearing, distance = self.true_bullseye()
        return ((bearing + self.brain.cockpit_bearing_offset_deg) % 360,
                distance)

    # ---- traffic helpers ----

    def add_traffic(self, callsign: str, lat: float, lon: float,
                    alt_ft: float, player: str = "") -> "Scenario":
        """Add a live unit at an explicit lat/lon (the rawest form)."""
        self._traffic.append(Unit(callsign, lat, lon, alt_ft, player))
        return self

    def traffic_at_runway(self, runway: str = "25", along_nm: float = 0.0,
                          across_nm: float = 0.0, alt_ft: float | None = None,
                          callsign: str = "AI-1",
                          player: str = "") -> "Scenario":
        """Add traffic at a runway-relative offset (see `at_runway`).

        Lets a test place a unit precisely: on the runway (`along_nm` between 0
        and the length), in the overrun margin (negative), or on the parallel
        taxiway (`across_nm` ~0.03).
        """
        lat, lon = _runway_point(self.airfield, runway, along_nm, across_nm)
        if alt_ft is None:
            alt_ft = self.airfield.elevation_ft + 10
        return self.add_traffic(callsign, lat, lon, alt_ft, player)

    def traffic_on_runway(self, runway: str = "25",
                          callsign: str = "AI-1", player: str = "") -> "Scenario":
        # mid-runway, on the centreline
        length = self.airfield.runway_length_nm(runway) or 1.6
        return self.traffic_at_runway(runway, along_nm=length / 2,
                                      callsign=callsign, player=player)

    def traffic_on_final(self, runway: str = "25", nm: float = 3.0,
                         callsign: str = "AI-2") -> "Scenario":
        return self.traffic_at_runway(runway, along_nm=-nm,
                                      alt_ft=self.airfield.elevation_ft + 600,
                                      callsign=callsign)

    def clear_traffic(self) -> "Scenario":
        self._traffic = []
        return self

    # ---- the radio ----

    def _track(self) -> AircraftTrack:
        return AircraftTrack(
            callsign=self.callsign,
            inside=self.airfield.ctr.contains(self.lat, self.lon, self.alt),
            lat=self.lat, lon=self.lon, alt_ft=self.alt,
            distance_nm=self.airfield.ctr.distance_nm(self.lat, self.lon),
            relative=self.airfield.ctr.relative_position(self.lat, self.lon),
            heading=self.heading, speed_kt=self.speed)

    def say(self, text: str, controller) -> str | None:
        """Transmit `text` to `controller`; return (and log) the reply."""
        reply = self.brain.handle(
            text, self._track(), controller=controller,
            traffic=self._traffic, speaker=self.speaker)
        self.log.append((text, reply))
        return reply

    def phase(self):
        return self.brain.pilots[self.callsign].phase
