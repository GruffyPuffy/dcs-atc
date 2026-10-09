"""Airspace geometry: load CTR definitions and test aircraft containment.

CTRs are defined in `airspace.json` as lat/lon polygons (or a radius around the
airfield). Shapely works in a planar coordinate system, so we project lat/lon to
a local metric plane (equirectangular, centred on the airfield) — accurate to
well under a metre over a 5 NM control zone.

DCS gives us positions as lat/lon (via the state bridge), so everything here is
in WGS84 degrees and feet.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from shapely.geometry import Point, Polygon

EARTH_RADIUS_M = 6_371_000.0
M_PER_NM = 1852.0
FT_PER_M = 3.280839895

DEFAULT_CONFIG = Path(__file__).with_name("airspace.json")


def _project(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    """Equirectangular projection to local metres around (lat0, lon0)."""
    x = math.radians(lon - lon0) * math.cos(math.radians(lat0)) * EARTH_RADIUS_M
    y = math.radians(lat - lat0) * EARTH_RADIUS_M
    return x, y


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a)) / M_PER_NM


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, degrees true (0-360)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _compass(bearing: float) -> str:
    """8-point compass name for a bearing in degrees."""
    names = ["north", "northeast", "east", "southeast",
             "south", "southwest", "west", "northwest"]
    return names[int((bearing + 22.5) % 360 // 45)]


def _offset(lat: float, lon: float, north_m: float,
            east_m: float) -> tuple[float, float]:
    """Offset a lat/lon by metres north/east (local flat-earth approximation)."""
    dlat = math.degrees(north_m / EARTH_RADIUS_M)
    dlon = math.degrees(east_m / (EARTH_RADIUS_M * math.cos(math.radians(lat))))
    return lat + dlat, lon + dlon


def bearing_distance(lat1: float, lon1: float, lat2: float,
                     lon2: float) -> tuple[float, float]:
    """Bearing (degrees true) and distance (NM) from point 1 to point 2."""
    return _bearing(lat1, lon1, lat2, lon2), _haversine_nm(lat1, lon1, lat2, lon2)


@dataclass
class ControlZone:
    """A control zone: a horizontal polygon with a vertical ceiling (AGL)."""

    name: str
    ceiling_ft_agl: float
    airfield_elevation_ft: float
    polygon: Polygon
    center_lat: float
    center_lon: float
    gates: dict[str, tuple[float, float]] = field(default_factory=dict)
    # Original CTR outline in lat/lon (for map display); the shapely `polygon`
    # is projected to local metres and not directly usable as WGS84.
    polygon_latlon: list[tuple[float, float]] = field(default_factory=list)

    @property
    def ceiling_ft_msl(self) -> float:
        return self.airfield_elevation_ft + self.ceiling_ft_agl

    def contains(self, lat: float, lon: float, alt_ft_msl: float) -> bool:
        """True if the position is inside the CTR horizontally and vertically."""
        if alt_ft_msl > self.ceiling_ft_msl:
            return False
        return self.contains_horizontal(lat, lon)

    def contains_horizontal(self, lat: float, lon: float) -> bool:
        """True if the position is inside the CTR outline, ignoring altitude.

        Used for the altitude-bust check: an aircraft inside the CTR footprint
        but above the ceiling has left the CTR vertically.
        """
        x, y = _project(lat, lon, self.center_lat, self.center_lon)
        return self.polygon.contains(Point(x, y))

    def distance_nm(self, lat: float, lon: float) -> float:
        """Distance from the airfield reference point, nautical miles."""
        return _haversine_nm(self.center_lat, self.center_lon, lat, lon)

    def bearing_from_airfield(self, lat: float, lon: float) -> float:
        return _bearing(self.center_lat, self.center_lon, lat, lon)

    def relative_position(self, lat: float, lon: float) -> str:
        """e.g. '4 miles north' — distance + compass from the airfield."""
        distance = self.distance_nm(lat, lon)
        unit = "mile" if round(distance) == 1 else "miles"
        return f"{distance:.0f} {unit} {_compass(self.bearing_from_airfield(lat, lon))}"

    def nearest_gate(self, lat: float, lon: float) -> str | None:
        """Name of the entry/exit gate closest to a position, or None."""
        if not self.gates:
            return None
        return min(self.gates, key=lambda g: _haversine_nm(
            lat, lon, self.gates[g][0], self.gates[g][1]))


@dataclass
class Airfield:
    name: str
    tower: str
    frequency_mhz: float
    elevation_ft: float
    active_runway: str
    ctr: ControlZone
    atis_frequency_mhz: float = 0.0
    ground: str = ""
    ground_frequency_mhz: float = 0.0
    control: str = ""
    control_frequency_mhz: float = 0.0
    runways: dict[str, tuple[float, float]] = field(default_factory=dict)
    gates: dict[str, tuple[float, float]] = field(default_factory=dict)
    # Taxi routes to the runway, per runway and per starting ramp:
    #   {runway: {ramp_name: "Sierra Echo"}}. The ramp is chosen by the
    #   aircraft's live position (nearest parking area).
    taxi_routes: dict[str, dict[str, str]] = field(default_factory=dict)
    # Taxi route from the runway back to a ramp (post-landing):
    #   {ramp_name: "Whiskey"}.
    parking_routes: dict[str, str] = field(default_factory=dict)
    parking_areas: dict[str, tuple[float, float]] = field(default_factory=dict)
    # Named runway holding positions (P1..P4 on the MA chart), lat/lon.
    holding_points: dict[str, tuple[float, float]] = field(default_factory=dict)
    # Radio preset channel numbers, per agency (Master Arms SOP). These are
    # per-airfield (a preset is a property of the airfield's radio plan), so
    # they live here rather than in the global phraseology variables.
    channels: dict[str, str] = field(default_factory=dict)

    def taxi_route(self, lat: float | None, lon: float | None,
                   runway: str | None = None) -> str | None:
        """Taxi route to the runway from the aircraft's position.

        Routes are configured per runway and per starting ramp
        (`taxi_routes`: {runway: {ramp: route}}). The ramp is the parking area
        nearest the aircraft; without a position the first ramp is used.
        """
        rwy = runway or self.active_runway
        routes = self.taxi_routes.get(rwy)
        if not routes:
            return None
        if lat is not None and lon is not None and self.parking_areas:
            ramp = self.nearest_parking_area(lat, lon)
            if ramp in routes:
                return routes[ramp]
        return next(iter(routes.values()))

    def parking_route(self, lat: float | None, lon: float | None) -> str | None:
        """Taxi route from the runway to the nearest ramp (post-landing)."""
        if not self.parking_routes:
            return None
        if lat is not None and lon is not None and self.parking_areas:
            ramp = self.nearest_parking_area(lat, lon)
            if ramp in self.parking_routes:
                return self.parking_routes[ramp]
        return next(iter(self.parking_routes.values()))

    def nearest_parking_area(self, lat: float, lon: float) -> str | None:
        """Name of the parking area (ramp) nearest a position, or None."""
        if not self.parking_areas:
            return None
        return min(self.parking_areas, key=lambda p: _haversine_nm(
            lat, lon, self.parking_areas[p][0], self.parking_areas[p][1]))

    def nearest_holding_point(self, lat: float, lon: float) -> str | None:
        """Name of the runway holding position nearest a position, or None."""
        if not self.holding_points:
            return None
        return min(self.holding_points, key=lambda h: _haversine_nm(
            lat, lon, self.holding_points[h][0], self.holding_points[h][1]))

    def default_exit_gate(self, runway: str | None = None) -> str | None:
        """Exit gate that best matches the departure direction for a runway.

        After takeoff you fly the runway heading, so the natural exit is the
        gate whose bearing from the airfield is closest to the runway heading.
        Returns the gate name (e.g. "East"), or None if no gates are configured.
        """
        if not self.gates:
            return None
        rwy_hdg = self.runway_heading(runway)
        if rwy_hdg is None:
            return next(iter(self.gates))
        lat0, lon0 = self.ctr.center_lat, self.ctr.center_lon

        def offset(name: str) -> float:
            g = self.gates[name]
            bearing = _bearing(lat0, lon0, g[0], g[1])
            return abs((bearing - rwy_hdg + 180) % 360 - 180)

        return min(self.gates, key=offset)

    def exit_turn(self, gate: str, runway: str | None = None) -> str:
        """Turn from the runway heading to an exit gate: "left"/"right"/"".

        Empty string means the gate is roughly straight ahead (within 20°), so
        the clearance reads "after departure Exit West" rather than a turn.
        """
        rwy_hdg = self.runway_heading(runway)
        target = self.gates.get(gate.split()[-1]) if gate else None
        if rwy_hdg is None or target is None:
            return "right"
        lat0, lon0 = self.ctr.center_lat, self.ctr.center_lon
        bearing = _bearing(lat0, lon0, target[0], target[1])
        delta = (bearing - rwy_hdg + 540) % 360 - 180
        if abs(delta) < 20:
            return ""
        return "right" if delta > 0 else "left"

    def holding_zone_geometry(self, runway: str | None = None,
                              thr_nm: float = 0.6,
                              point_nm: float = 0.2) -> list[dict]:
        """The areas where a 'holding short' report is accepted, as map circles.

        Mirrors `is_holding_short`: a circle at the threshold plus one at each
        named holding point. Returns [{center, radius_nm, label}, ...].
        """
        zones: list[dict] = []
        thr = self.runway_threshold(runway)
        if thr is not None:
            zones.append({"center": [thr[0], thr[1]], "radius_nm": thr_nm,
                          "label": "threshold"})
        for name, (hlat, hlon) in self.holding_points.items():
            zones.append({"center": [hlat, hlon], "radius_nm": point_nm,
                          "label": name})
        return zones

    def final_zone_geometry(self, runway: str | None = None,
                            max_nm: float = 12.0,
                            max_offset_deg: float = 30.0) -> dict | None:
        """The final-approach area, as a wedge (sector) for the map.

        Mirrors `is_on_final`: within `max_nm` of the threshold, on the approach
        side, within `max_offset_deg` of the runway centreline. Returns a
        Leaflet-friendly {center, radius_nm, start_deg, end_deg} or None.
        """
        thr = self.runway_threshold(runway)
        rwy_hdg = self.runway_heading(runway)
        if thr is None or rwy_hdg is None:
            return None
        # The approach comes from the reciprocal of the landing direction.
        inbound = (rwy_hdg + 180) % 360
        return {
            "runway": runway or self.active_runway,
            "center": [thr[0], thr[1]],
            "radius_nm": max_nm,
            "start_deg": (inbound - max_offset_deg) % 360,
            "end_deg": (inbound + max_offset_deg) % 360,
            "heading": rwy_hdg,
        }

    def runway_corridor_geometry(self, runway: str | None = None,
                                 length_nm: float = 1.6,
                                 half_width_nm: float = 0.02,
                                 margin_nm: float = 0.25) -> dict | None:
        """The runway-occupancy corridor as a rectangle for the map.

        Mirrors `runway_occupied`: the full runway length plus `margin_nm` at
        both ends. Returns {corners: [[lat,lon], ...]} or None.
        """
        thr = self.runway_threshold(runway)
        rwy_hdg = self.runway_heading(runway)
        if thr is None or rwy_hdg is None:
            return None
        length = self.runway_length_nm(runway) or length_nm
        along = math.radians(rwy_hdg)
        ax, ay = math.sin(along), math.cos(along)      # east, north
        px, py = ay, -ax                               # perpendicular
        start_m = -margin_nm * M_PER_NM
        end_m = (length + margin_nm) * M_PER_NM
        half_m = half_width_nm * M_PER_NM
        corners = []
        for along_m, across_m in ((start_m, -half_m), (end_m, -half_m),
                                  (end_m, half_m), (start_m, half_m)):
            north_m = along_m * ay + across_m * py
            east_m = along_m * ax + across_m * px
            corners.append(list(_offset(thr[0], thr[1], north_m, east_m)))
        return {"corners": corners}

    def runway_threshold(self, runway: str | None = None) -> tuple[float, float] | None:
        """Threshold (lat, lon) for a runway (default: the active runway)."""
        return self.runways.get(runway or self.active_runway)

    def set_active_runway(self, runway: str) -> None:
        """Update the active runway (e.g. when the wind shifts).

        The brain and the airfield must agree, or the go-around / final /
        occupancy checks would use a different runway than the clearances.
        """
        if runway and runway in self.runways:
            self.active_runway = runway

    def runway_length_nm(self, runway: str | None = None) -> float | None:
        """Runway length in NM (threshold to opposite threshold), or None."""
        rwy = runway or self.active_runway
        opposite = self._opposite_runway(rwy)
        thr = self.runways.get(rwy)
        opp_thr = self.runways.get(opposite) if opposite else None
        if thr and opp_thr:
            return _haversine_nm(thr[0], thr[1], opp_thr[0], opp_thr[1])
        return None

    def runway_heading(self, runway: str | None = None) -> float | None:
        """Runway heading in degrees true.

        Prefers the actual bearing between the two thresholds (accurate); falls
        back to the runway number (magnetic, rounded to 10°) if the opposite
        threshold is not defined.
        """
        rwy = runway or self.active_runway
        if not rwy:
            return None
        opposite = self._opposite_runway(rwy)
        thr = self.runways.get(rwy)
        opp_thr = self.runways.get(opposite) if opposite else None
        if thr and opp_thr:
            return _bearing(thr[0], thr[1], opp_thr[0], opp_thr[1])
        if rwy[:2].isdigit():
            return (int(rwy[:2]) * 10) % 360
        return None

    def _opposite_runway(self, runway: str) -> str | None:
        """The reciprocal runway designator (25 <-> 07), if defined."""
        if not runway[:2].isdigit():
            return None
        number = int(runway[:2])
        opposite = (number + 18) % 36 or 36
        candidate = f"{opposite:02d}"
        for name in self.runways:
            if name[:2] == candidate:
                return name
        return None

    def distance_to_threshold_nm(self, lat: float, lon: float,
                                 runway: str | None = None) -> float | None:
        """Distance from a position to the runway threshold, nautical miles."""
        thr = self.runway_threshold(runway)
        if thr is None:
            return None
        return _haversine_nm(lat, lon, thr[0], thr[1])

    def nearest_gate(self, lat: float, lon: float) -> str | None:
        """Name of the entry/exit gate closest to a position, or None."""
        return self.ctr.nearest_gate(lat, lon)

    def is_at_runway(self, lat: float, lon: float, runway: str | None = None,
                     max_nm: float = 0.6) -> bool:
        """True if the position is at/near the runway threshold (holding point).

        Used to cross-check a pilot's "holding short" / "ready for departure"
        report against their live position.
        """
        thr = self.runway_threshold(runway)
        if thr is None:
            return False
        return _haversine_nm(lat, lon, thr[0], thr[1]) <= max_nm

    def is_holding_short(self, lat: float, lon: float,
                         runway: str | None = None,
                         point_nm: float = 0.2, thr_nm: float = 0.6) -> bool:
        """True if the position is at a runway holding position.

        A pilot may hold at the threshold itself or at any named holding point
        (P1..P4 on the MA chart), which can be over a mile from the threshold —
        so we accept proximity to either.
        """
        if self.is_at_runway(lat, lon, runway, thr_nm):
            return True
        for hlat, hlon in self.holding_points.values():
            if _haversine_nm(lat, lon, hlat, hlon) <= point_nm:
                return True
        return False

    def vector_heading(self, lat: float, lon: float, runway: str | None = None,
                       intercept_nm: float = 10.0) -> float | None:
        """Heading to fly to intercept the extended centreline of a runway.

        Returns the bearing from the aircraft to a point `intercept_nm` before
        the threshold on the approach side — i.e. the heading a controller would
        give as "fly heading X, vectors for runway Y".
        """
        thr = self.runway_threshold(runway)
        rwy_hdg = self.runway_heading(runway)
        if thr is None or rwy_hdg is None:
            return None
        # The approach comes from the reciprocal of the landing direction, so
        # the intercept point sits `intercept_nm` back along (rwy_hdg + 180).
        back = math.radians((rwy_hdg + 180) % 360)
        north_m = intercept_nm * M_PER_NM * math.cos(back)
        east_m = intercept_nm * M_PER_NM * math.sin(back)
        ip_lat = thr[0] + math.degrees(north_m / EARTH_RADIUS_M)
        ip_lon = thr[1] + math.degrees(
            east_m / (EARTH_RADIUS_M * math.cos(math.radians(thr[0]))))
        bearing, _ = bearing_distance(lat, lon, ip_lat, ip_lon)
        return bearing

    def join_heading(self, lat: float, lon: float, gate: str) -> float | None:
        """Heading to fly to join the CTR via a named gate, from the pilot's
        live position. Used for the Control join clearance ("turn heading X to
        join via Entry East") so the heading is computed, not canned."""
        target = self.gates.get(gate.split()[-1]) if gate else None
        if target is None:
            return None
        bearing, _ = bearing_distance(lat, lon, target[0], target[1])
        return bearing

    def is_on_final(self, lat: float, lon: float, heading: float,
                    runway: str | None = None, max_nm: float = 12.0,
                    max_offset_deg: float = 40.0) -> bool:
        """True if the aircraft looks like it is on final approach.

        Heuristic: within `max_nm` of the threshold, heading roughly aligned
        with the runway (within `max_offset_deg`), and on the approach side.
        """
        thr = self.runway_threshold(runway)
        rwy_hdg = self.runway_heading(runway)
        if thr is None or rwy_hdg is None:
            return False
        dist = _haversine_nm(lat, lon, thr[0], thr[1])
        if dist > max_nm:
            return False
        # bearing from aircraft to threshold should match the runway heading
        bearing_to_thr = _bearing(lat, lon, thr[0], thr[1])
        offset = abs((bearing_to_thr - rwy_hdg + 180) % 360 - 180)
        if offset > max_offset_deg:
            return False
        # aircraft heading should also be roughly aligned with the runway
        hdg_offset = abs((heading - rwy_hdg + 180) % 360 - 180)
        return hdg_offset <= max_offset_deg

    def runway_occupied(self, units, runway: str | None = None,
                        exclude: str | None = None, length_nm: float = 1.6,
                        half_width_nm: float = 0.02,
                        max_alt_ft_agl: float = 500.0,
                        margin_nm: float = 0.25,
                        on_runway: bool = True) -> bool:
        """True if any unit is on the runway (within a corridor around it).

        The corridor runs the length of the runway and extends `margin_nm`
        beyond **both** ends, so an aircraft just entering or rolling out past
        the far threshold still counts as occupying the runway.

        The half-width is the **real runway half-width** (~30 m for Kutaisi's
        60 m runway), not a generous margin: a wider corridor would swallow the
        parallel taxiway holding positions (P1/P2 are only ~50 m from the
        centreline) and wrongly block landing for a pilot holding short.

        `on_runway=True` (default) additionally requires the unit to be between
        the two thresholds — i.e. actually on the runway, not in the overrun
        margin. This is what sequencing uses, so a pilot **holding short** (just
        before the threshold) does not count as occupying the runway and block
        another aircraft's line-up or landing.

        `units` is any iterable of objects with `.lat`, `.lon`, `.alt_ft` and
        optionally `.callsign`/`.player`. `exclude` skips one aircraft (e.g. the
        aircraft we are clearing, so it does not count itself) — matched against
        both the unit name and the player name, since the brain works in flight
        callsigns while the live units are keyed by DCS unit name.
        """
        thr = self.runway_threshold(runway)
        rwy_hdg = self.runway_heading(runway)
        if thr is None or rwy_hdg is None:
            return False
        # The runway extends from the threshold in the landing direction; use
        # the real threshold-to-threshold length when both ends are known.
        length = self.runway_length_nm(runway) or length_nm
        along_dir = math.radians(rwy_hdg)
        ax, ay = math.sin(along_dir), math.cos(along_dir)  # east, north
        for unit in units:
            if exclude is not None and exclude in (
                    getattr(unit, "callsign", None),
                    getattr(unit, "player", None)):
                continue
            if unit.alt_ft - self.elevation_ft > max_alt_ft_agl:
                continue
            # offset from threshold in local metres (east, north)
            ex, ny = _project(unit.lat, unit.lon, thr[0], thr[1])
            along = ex * ax + ny * ay
            across = ex * ay - ny * ax
            if on_runway and not (0.0 <= along <= length * M_PER_NM):
                continue  # in the overrun margin, not on the runway proper
            if (-margin_nm * M_PER_NM <= along
                    <= (length + margin_nm) * M_PER_NM
                    and abs(across) <= half_width_nm * M_PER_NM):
                return True
        return False


class Airspace:
    """All airfields/CTRs loaded from airspace.json."""

    def __init__(self, airfields: dict[str, Airfield]):
        self.airfields = airfields

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CONFIG) -> "Airspace":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        defaults = data.get("defaults", {})
        airfields: dict[str, Airfield] = {}
        for name, spec in data.get("airfields", {}).items():
            airfields[name] = _build_airfield(name, spec, defaults)
        return cls(airfields)

    def get(self, name: str) -> Airfield | None:
        return self.airfields.get(name)

    def nearest(self, lat: float, lon: float) -> Airfield | None:
        """Airfield whose reference point is closest to the given position."""
        if not self.airfields:
            return None
        return min(self.airfields.values(),
                   key=lambda a: a.ctr.distance_nm(lat, lon))


def _build_airfield(name: str, spec: dict, defaults: dict) -> Airfield:
    ctr_spec = spec.get("ctr", {})
    elevation = float(spec.get("elevation_ft", 0.0))
    ceiling_agl = float(ctr_spec.get("ceiling_ft_agl",
                                      defaults.get("ctr_ceiling_ft_agl", 1500)))

    polygon_pts = ctr_spec.get("polygon")
    if polygon_pts:
        # centre = centroid of the polygon (used for projection + distance)
        center_lat = sum(p[0] for p in polygon_pts) / len(polygon_pts)
        center_lon = sum(p[1] for p in polygon_pts) / len(polygon_pts)
        projected = [_project(p[0], p[1], center_lat, center_lon) for p in polygon_pts]
        polygon = Polygon(projected)
        polygon_latlon = [(float(p[0]), float(p[1])) for p in polygon_pts]
    else:
        # circular CTR around the airfield reference point
        ref = spec.get("reference", spec.get("runways", {}).get("25", {}).get("threshold"))
        if not ref:
            raise ValueError(f"airfield {name!r} needs a ctr.polygon or a reference point")
        center_lat, center_lon = float(ref[0]), float(ref[1])
        radius_nm = float(ctr_spec.get("radius_nm", defaults.get("ctr_radius_nm", 5.0)))
        radius_m = radius_nm * M_PER_NM
        polygon = Point(0.0, 0.0).buffer(radius_m, quad_segs=64)
        # Sample the circle back to lat/lon for the map.
        polygon_latlon = []
        for i in range(64):
            angle = 2 * math.pi * i / 64
            dlat = math.degrees(radius_m * math.cos(angle) / EARTH_RADIUS_M)
            dlon = math.degrees(radius_m * math.sin(angle)
                                / (EARTH_RADIUS_M * math.cos(math.radians(center_lat))))
            polygon_latlon.append((center_lat + dlat, center_lon + dlon))

    gates = {g: (float(v[0]), float(v[1])) for g, v in spec.get("gates", {}).items()}
    runways = {r: (float(v["threshold"][0]), float(v["threshold"][1]))
               for r, v in spec.get("runways", {}).items()}
    taxi_routes = {r: dict(v) for r, v in spec.get("taxi_routes", {}).items()}
    parking_routes = dict(spec.get("parking_routes", {}))
    parking_areas = {p: (float(v[0]), float(v[1]))
                     for p, v in spec.get("parking_areas", {}).items()}
    holding_points = {h: (float(v[0]), float(v[1]))
                      for h, v in spec.get("holding_points", {}).items()}

    ctr = ControlZone(
        name=f"{name} CTR",
        ceiling_ft_agl=ceiling_agl,
        airfield_elevation_ft=elevation,
        polygon=polygon,
        center_lat=center_lat,
        center_lon=center_lon,
        gates=gates,
        polygon_latlon=polygon_latlon,
    )
    return Airfield(
        name=name,
        tower=spec.get("tower", f"{name} Tower"),
        frequency_mhz=float(spec.get("frequency_mhz", 0.0)),
        elevation_ft=elevation,
        active_runway=str(spec.get("active_runway", "")),
        ctr=ctr,
        atis_frequency_mhz=float(spec.get("atis_frequency_mhz", 0.0)),
        ground=spec.get("ground", f"{name} Ground"),
        ground_frequency_mhz=float(spec.get("ground_frequency_mhz", 0.0)),
        control=spec.get("control", f"{name} Control"),
        control_frequency_mhz=float(spec.get("control_frequency_mhz", 0.0)),
        runways=runways,
        gates=gates,
        taxi_routes=taxi_routes,
        parking_routes=parking_routes,
        parking_areas=parking_areas,
        holding_points=holding_points,
        channels={k: str(v) for k, v in spec.get("channels", {}).items()},
    )
