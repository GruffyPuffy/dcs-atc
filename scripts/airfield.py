#!/usr/bin/env python3
"""Generate an `airspace.json` stub for a DCS airbase from the live bridge.

DCS exposes the runway ends and parking spots for every airbase, so we can seed
most of an airfield entry automatically. What DCS does **not** expose is taxiway
names and holding positions — those come from the aerodrome chart and must be
filled in by hand (the stub marks them clearly).

Usage:
    python3 scripts/airfield.py Gudauta                 # print a JSON stub
    python3 scripts/airfield.py Gudauta --radius-nm 5   # circular CTR radius
    python3 scripts/airfield.py Gudauta --merge         # merge into airspace.json

The stub uses a **circular CTR** (reference + radius) so it works without a
hand-drawn polygon; replace it with a `polygon` if you have the chart outline.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from state_client import exchange  # noqa: E402

EARTH_RADIUS_M = 6_371_000.0
M_PER_NM = 1852.0
DEFAULT_AIRSPACE = Path(__file__).resolve().parent.parent / "atc" / "airspace.json"

# Master Arms UHF channel plan (per-airfield presets). Defaults; edit per field.
DEFAULT_CHANNELS = {"ground": "6", "tower": "7", "control": "8"}

# DCS terrain Radio.lua (ATC frequencies per airfield). The bot uses the UHF
# value for tower (Master Arms convention; cf. Kutaisi 263.000).
RADIO_LUA_CANDIDATES = [
    Path("/data/dcs-atc/config/.wine/drive_c/Program Files/Eagle Dynamics/"
         "DCS World Server/Mods/terrains/Caucasus/Radio.lua"),
]


def _terrain_uhf(airbase: str) -> float | None:
    """UHF ATC frequency for an airbase from the terrain Radio.lua, if found."""
    import re
    for path in RADIO_LUA_CANDIDATES:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        # Each block: "-- Name" ... [UHF] = {MODULATIONTYPE_AM, <hz>}
        for block in text.split("-- "):
            first = block.splitlines()[0].strip() if block.splitlines() else ""
            if first.lower() != airbase.lower():
                continue
            m = re.search(r"\[UHF\]\s*=\s*\{[^,]+,\s*([0-9.]+)", block)
            if m:
                return float(m.group(1)) / 1e6
    return None


def _nm(a: tuple[float, float], b: tuple[float, float]) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = math.radians(b[0] - a[0])
    dl = math.radians(b[1] - a[1])
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(x)) / M_PER_NM


def _offset(lat: float, lon: float, north_m: float,
            east_m: float) -> tuple[float, float]:
    dlat = math.degrees(north_m / EARTH_RADIUS_M)
    dlon = math.degrees(east_m / (EARTH_RADIUS_M * math.cos(math.radians(lat))))
    return lat + dlat, lon + dlon


def _far_threshold(lat: float, lon: float, course_rad: float,
                   length_m: float) -> tuple[float, float]:
    """The opposite threshold, `length_m` along the runway course."""
    north = length_m * math.cos(course_rad)
    east = length_m * math.sin(course_rad)
    return _offset(lat, lon, north, east)


def _reciprocal(name: str) -> str:
    """The opposite runway number (25 -> 07, 15 -> 33)."""
    n = int(name)
    return f"{(n + 18) % 36 or 36:02d}"


def _thresholds_from_centre(lat: float, lon: float, name: str,
                            length_m: float) -> tuple[tuple[float, float],
                                                     tuple[float, float]]:
    """Approximate the two thresholds from the runway centre.

    DCS reports the runway **centre** (verified: for Kutaisi it is equidistant
    from both known thresholds), plus the length. The runway number gives the
    heading (magnetic, ~10 deg resolution), so we place the named threshold
    `length/2` back along that heading and the reciprocal at the far end. This
    is approximate — verify against the aerodrome chart.
    """
    heading = math.radians(int(name) * 10)
    half = length_m / 2
    # The named threshold is behind the centre, opposite the landing direction.
    thr = _offset(lat, lon, -half * math.cos(heading), -half * math.sin(heading))
    far = _offset(lat, lon, half * math.cos(heading), half * math.sin(heading))
    return thr, far


def _cluster(points: list[tuple[float, float]],
             threshold_nm: float = 0.6) -> list[list[tuple[float, float]]]:
    """Single-link clustering of parking spots into ramp areas."""
    clusters: list[list[tuple[float, float]]] = []
    for p in points:
        for c in clusters:
            if any(_nm(p, q) < threshold_nm for q in c):
                c.append(p)
                break
        else:
            clusters.append([p])
    return clusters


def _centroid(points: list[tuple[float, float]]) -> tuple[float, float]:
    return (sum(p[0] for p in points) / len(points),
            sum(p[1] for p in points) / len(points))


def _compass_name(lat: float, lon: float, clat: float, clon: float) -> str:
    """Rough compass name of a point relative to the field centre."""
    north = (lat - clat) * EARTH_RADIUS_M
    east = (lon - clon) * math.cos(math.radians(clat)) * EARTH_RADIUS_M
    bearing = (math.degrees(math.atan2(east, north)) + 360) % 360
    names = ["North", "Northeast", "East", "Southeast",
             "South", "Southwest", "West", "Northwest"]
    return names[int((bearing + 22.5) % 360 // 45)]


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """True bearing (deg) from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = (math.cos(p1) * math.sin(p2)
         - math.sin(p1) * math.cos(p2) * math.cos(dl))
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _default_gates(lat: float, lon: float, axis_hdg: float | None,
                   radius_nm: float) -> dict[str, list[float]]:
    """Entry/exit gates on the CTR circle, named by compass.

    For a circular (default) CTR there is no chart to read entry points from, so
    place four gates on the boundary: two **aligned with the runway axis** (the
    upwind/downwind ends — the natural departure exit and straight-in entry) and
    two **perpendicular** (abeam — the base/crosswind entry). Each is named by
    its compass bearing from the field ("East", "Southwest", ...).

    Falls back to true N/E/S/W when the runway heading is unknown.
    """
    if axis_hdg is None:
        bearings = (0.0, 90.0, 180.0, 270.0)
    else:
        b = axis_hdg % 360
        bearings = (b, (b + 90) % 360, (b + 180) % 360, (b + 270) % 360)
    gates: dict[str, list[float]] = {}
    for bearing in bearings:
        rad = math.radians(bearing)
        g = _offset(lat, lon, radius_nm * M_PER_NM * math.cos(rad),
                    radius_nm * M_PER_NM * math.sin(rad))
        name = _compass_name(g[0], g[1], lat, lon)
        # disambiguate if two gates land on the same compass sector
        if name in gates:
            name = f"{name} 2"
        gates[name] = [round(g[0], 5), round(g[1], 5)]
    return gates


def build_stub(host: str, port: int, airbase: str, radius_nm: float) -> dict:
    status = exchange(host, port, "status")
    if not status.get("ok"):
        raise SystemExit(f"bridge error: {status.get('error')}")
    base = next((b for b in status["result"]["airbases"]
                 if b["name"].lower() == airbase.lower()), None)
    if base is None:
        raise SystemExit(f"airbase {airbase!r} not found in the mission")

    clat, clon = float(base["lat"]), float(base["lon"])
    elevation = float(base["alt"]) * 3.280839895

    # Runway ends (name, centre, length). DCS reports the runway CENTRE, so we
    # derive the two thresholds from the runway number + length.
    rwy = exchange(host, port, "runways", airbase=airbase)
    runways: dict[str, dict] = {}
    runway_meta: list[dict] = []
    axis_hdg: float | None = None
    for r in rwy.get("result", {}).get("runways", []):
        name = str(r["name"])
        clat_r, clon_r = float(r["lat"]), float(r["lon"])
        length = float(r["length"])
        thr, far = _thresholds_from_centre(clat_r, clon_r, name, length)
        recip = _reciprocal(name)
        runways[name] = {"threshold": [round(thr[0], 5), round(thr[1], 5)]}
        runways.setdefault(recip, {"threshold": [round(far[0], 5),
                                                 round(far[1], 5)]})
        if axis_hdg is None:
            axis_hdg = _bearing(thr[0], thr[1], far[0], far[1])
        runway_meta.append({"name": name, "recip": recip,
                            "length_m": round(length), "width_m": round(r["width"]),
                            "centre": [round(clat_r, 5), round(clon_r, 5)]})

    # Parking areas (clustered spots -> named ramps).
    park = exchange(host, port, "parking", airbase=airbase)
    spots = [(float(s["lat"]), float(s["lon"]))
             for s in park.get("result", {}).get("spots", [])]
    parking_areas: dict[str, list[float]] = {}
    for cluster in _cluster(spots):
        c = _centroid(cluster)
        name = f"Ramp {_compass_name(c[0], c[1], clat, clon)}"
        # disambiguate if two clusters land on the same compass name
        if name in parking_areas:
            name = f"{name} 2"
        parking_areas[name] = [round(c[0], 5), round(c[1], 5)]

    # Gates: on the CTR circle, aligned with the runway axis + abeam, named by
    # compass (edit to match the chart when one has printed entry points).
    gates = _default_gates(clat, clon, axis_hdg, radius_nm)

    active = next(iter(runways), "25")
    tower_freq = _terrain_uhf(airbase) or 0.0
    return {
        "tower": f"{airbase} Tower",
        "frequency_mhz": tower_freq,
        "atis_frequency_mhz": 0.0,
        "ground": f"{airbase} Ground",
        "ground_frequency_mhz": 0.0,
        "control": f"{airbase} Control",
        "control_frequency_mhz": 0.0,
        "elevation_ft": round(elevation),
        "active_runway": active,
        "default_runway": active,
        "ctr": {"ceiling_ft_agl": 1500, "radius_nm": radius_nm,
                "reference": [round(clat, 5), round(clon, 5)]},
        "runways": runways,
        "gates": gates,
        "taxi_routes": {
            active: {ramp: "TODO" for ramp in parking_areas},
        },
        "parking_routes": {ramp: "TODO" for ramp in parking_areas},
        "parking_areas": parking_areas,
        "holding_points": {"Holding A": [round(clat, 5), round(clon, 5)]},
        "channels": dict(DEFAULT_CHANNELS),
        "_meta": {
            "runways": runway_meta,
            "note": "TODO: set frequencies, taxi_routes, parking_routes and "
                    "holding_points from the aerodrome chart; verify "
                    "default_runway matches what DCS/the mission uses (the calm-wind "
                    "runway). CTR is a circle; replace with a polygon if you have "
                    "the chart outline.",
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("airbase", help="airbase name as DCS reports it (e.g. Gudauta)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=10309)
    ap.add_argument("--radius-nm", type=float, default=5.0,
                    help="circular CTR radius (default 5); gates sit on this circle")
    ap.add_argument("--merge", action="store_true",
                    help="merge the stub into atc/airspace.json (else print it)")
    ap.add_argument("--airspace", default=str(DEFAULT_AIRSPACE))
    args = ap.parse_args()

    stub = build_stub(args.host, args.port, args.airbase, args.radius_nm)

    if not args.merge:
        print(json.dumps({args.airbase: stub}, indent=2))
        return

    path = Path(args.airspace)
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("airfields", {})[args.airbase] = stub
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"merged {args.airbase!r} into {path}")


if __name__ == "__main__":
    main()
