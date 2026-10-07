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

    @property
    def ceiling_ft_msl(self) -> float:
        return self.airfield_elevation_ft + self.ceiling_ft_agl

    def contains(self, lat: float, lon: float, alt_ft_msl: float) -> bool:
        """True if the position is inside the CTR horizontally and vertically."""
        if alt_ft_msl > self.ceiling_ft_msl:
            return False
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
    taxi_routes: dict[str, tuple[float, float]] = field(default_factory=dict)
    parking_areas: dict[str, tuple[float, float]] = field(default_factory=dict)

    def nearest_taxi_route(self, lat: float, lon: float) -> str | None:
        """Name of the taxi route whose reference point is nearest, or None.

        DCS does not expose taxiway names, so routes are configured per airfield
        in `airspace.json` (`taxi_routes`: name -> [lat, lon] of a representative
        point, e.g. a parking area). The nearest one to the aircraft is used.
        """
        if not self.taxi_routes:
            return None
        return min(self.taxi_routes, key=lambda r: _haversine_nm(
            lat, lon, self.taxi_routes[r][0], self.taxi_routes[r][1]))

    def nearest_parking_area(self, lat: float, lon: float) -> str | None:
        """Name of the parking area (ramp) nearest a position, or None."""
        if not self.parking_areas:
            return None
        return min(self.parking_areas, key=lambda p: _haversine_nm(
            lat, lon, self.parking_areas[p][0], self.parking_areas[p][1]))

    def runway_threshold(self, runway: str | None = None) -> tuple[float, float] | None:
        """Threshold (lat, lon) for a runway (default: the active runway)."""
        return self.runways.get(runway or self.active_runway)

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

    def is_on_final(self, lat: float, lon: float, heading: float,
                    runway: str | None = None, max_nm: float = 8.0,
                    max_offset_deg: float = 30.0) -> bool:
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
                        half_width_nm: float = 0.12,
                        max_alt_ft_agl: float = 500.0) -> bool:
        """True if any unit is on the runway (within a corridor around it).

        `units` is any iterable of objects with `.lat`, `.lon`, `.alt_ft` and
        optionally `.callsign`. `exclude` skips one callsign (e.g. the aircraft
        we are clearing, so it does not count itself).
        """
        thr = self.runway_threshold(runway)
        rwy_hdg = self.runway_heading(runway)
        if thr is None or rwy_hdg is None:
            return False
        # The runway extends from the threshold in the landing direction.
        along_dir = math.radians(rwy_hdg)
        ax, ay = math.sin(along_dir), math.cos(along_dir)  # east, north
        for unit in units:
            if exclude is not None and getattr(unit, "callsign", None) == exclude:
                continue
            if unit.alt_ft - self.elevation_ft > max_alt_ft_agl:
                continue
            # offset from threshold in local metres (east, north)
            ex, ny = _project(unit.lat, unit.lon, thr[0], thr[1])
            along = ex * ax + ny * ay
            across = ex * ay - ny * ax
            if 0.0 <= along <= length_nm * M_PER_NM and abs(across) <= half_width_nm * M_PER_NM:
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
    else:
        # circular CTR around the airfield reference point
        ref = spec.get("reference", spec.get("runways", {}).get("25", {}).get("threshold"))
        if not ref:
            raise ValueError(f"airfield {name!r} needs a ctr.polygon or a reference point")
        center_lat, center_lon = float(ref[0]), float(ref[1])
        radius_nm = float(ctr_spec.get("radius_nm", defaults.get("ctr_radius_nm", 5.0)))
        radius_m = radius_nm * M_PER_NM
        polygon = Point(0.0, 0.0).buffer(radius_m, quad_segs=64)

    gates = {g: (float(v[0]), float(v[1])) for g, v in spec.get("gates", {}).items()}
    runways = {r: (float(v["threshold"][0]), float(v["threshold"][1]))
               for r, v in spec.get("runways", {}).items()}
    taxi_routes = {r: (float(v[0]), float(v[1]))
                   for r, v in spec.get("taxi_routes", {}).items()}
    parking_areas = {p: (float(v[0]), float(v[1]))
                     for p, v in spec.get("parking_areas", {}).items()}

    ctr = ControlZone(
        name=f"{name} CTR",
        ceiling_ft_agl=ceiling_agl,
        airfield_elevation_ft=elevation,
        polygon=polygon,
        center_lat=center_lat,
        center_lon=center_lon,
        gates=gates,
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
        parking_areas=parking_areas,
    )
