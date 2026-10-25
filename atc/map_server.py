"""Live map view for the ATC trainer (stdlib HTTP server + Leaflet).

Serves a single-page map showing the airfield (CTR, gates, runways, taxi
routes, parking) and every player aircraft with its callsign, flight phase and
controller. Data comes from the same live sources the bot uses:

- positions: the DCS state bridge (`state_client.StateClient`)
- phases:    the shared `AtcBrain` (per-callsign `PilotState`)
- geometry:  `airspace.json` (via `airspace.Airfield`)

No third-party Python dependencies: `http.server` for the server, Leaflet from
a CDN for the map. Run it from the bot with `--map-port`, or standalone:

    uv run map_server.py --airfield Kutaisi --port 8080
    uv run map_server.py --airfield Gudauta --replay debrief/tracks_*.json
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from airspace import Airfield
from brain import AtcBrain, EXIT_REPORT_RADIUS_NM
from state_client import StateClient
from topgun import BUST_RADIUS_M

# Debrief saving is best-effort, but a *silent* failure is what let a whole
# sortie go missing. `MapService` takes the bot's own `log()` callable and
# dumps every save problem (with a traceback) through it, so the next
# occurrence shows up in the bot log instead of vanishing.
def _noop_log(_line: str) -> None:
    pass


WEB = Path(__file__).with_name("web")
OVERLAYS = WEB / "overlays"

# AI aircraft are sent within this radius of the airfield (bounds the payload);
# the map page then filters to the current viewport, so zoom/pan declutters.
AI_RADIUS_NM = 150.0

# How many chatter-log entries to keep for the map's log drawer.
CHATTER_LIMIT = 200

# Flight-path trails. A full flight (inbound call -> parked) can run 15-30 min,
# and this is a *debug* tool, so keep a very long history: at the ~2 s poll,
# 3600 points is ~2 hours. Nothing is time-expired — the trail stays until the
# aircraft leaves the mission (or the server restarts).
TRACK_MAX_POINTS = 3600
# Stop recording a trail once the aircraft is this far from the field (it is
# off-station; the trail would only clutter the map and waste memory).
TRACK_RADIUS_NM = 60.0
# Don't add a point unless the aircraft has moved at least this far, so a parked
# or orbiting aircraft does not fill the trail with near-identical points.
TRACK_MIN_MOVE_NM = 0.02
# How many radio calls to keep per aircraft (comm markers on the path).
COMM_MAX = 1000
# How often (seconds of wall clock) to autosave the track history when a
# `tracks_file` is configured. Cheap (a few KB of JSON at most).
TRACKS_SAVE_INTERVAL = 10.0

# Where saved sortie snapshots ("debriefs") live, and their filename pattern:
#   <DEBRIEF_DIR>/tracks_<airfield>_<player>_<callsign>_<YYYYMMDD-HHMMSS>.json
# A fresh file per pilot spawn, so a tester can later pick the exact sortie.
DEBRIEF_DIR = Path(__file__).with_name("debrief")

# Process exit code the bot uses to ask its supervisor (run_server.sh) to
# relaunch it (e.g. after a web-driven airfield switch). Any other code stops
# the supervisor, so a normal exit or a crash never triggers a restart loop.
RESTART_EXIT_CODE = 75


class ControlPlane:
    """Web-driven restart / airfield selection.

    The map server runs *inside* the bot process, so it cannot restart the bot
    itself. Instead this writes the requested airfield to a small state file and
    asks the bot to exit with `RESTART_EXIT_CODE`; the outer `run_server.sh`
    loop reads the state file and relaunches the bot with the new airfield. The
    split keeps the dangerous part (spawning a process) out of the web server.

    Airfields are validated against the loaded config (an allowlist), so the
    endpoint can never be made to launch an arbitrary `--airfield`.
    """

    def __init__(self, airfields: list[str], state_file: Path | str,
                 on_exit: Callable[[str, str, bool], None], current: str,
                 debug: bool = False):
        self.airfields = sorted(airfields)
        self.state_file = Path(state_file)
        self.on_exit = on_exit
        self.current = current
        # Current session's debug state, so a restart can preserve it (the web
        # echoes this back; a restart must not silently drop --debug).
        self.debug = bool(debug)
        self._pending: tuple[str, str, bool] | None = None

    def payload(self) -> dict:
        return {"airfields": self.airfields, "current": self.current,
                "debug": self.debug}

    def restart(self, airfield: str, save_debrief: bool) -> dict:
        """Record a restart request. Returns a result; the actual exit happens
        via `run_pending()` *after* the HTTP response has been flushed."""
        if airfield not in self.airfields:
            return {"ok": False, "error": f"unknown airfield {airfield!r}"}
        # Plain key=value so run_server.sh can read it with sed (no JSON shell).
        self.state_file.write_text(
            f"ATC_AIRFIELD={airfield}\nATC_DEBUG={1 if save_debrief else 0}\n",
            encoding="utf-8")
        self._pending = ("restart", airfield, save_debrief)
        return {"ok": True, "restarting": True, "airfield": airfield}

    def stop(self) -> dict:
        """Ask the bot to exit *without* relaunch (the supervisor stops too)."""
        self._pending = ("stop", self.current, False)
        return {"ok": True, "stopping": True}

    def run_pending(self) -> None:
        """Fire the pending action (exits the process). Called after the
        response is sent so the browser sees the result, not a reset."""
        if self._pending is not None:
            action, airfield, save = self._pending
            self._pending = None
            self.on_exit(action, airfield, save)




def _debrief_slug(text: str) -> str:
    """Filename-safe slug (alnum kept, everything else becomes a hyphen)."""
    return "".join(c if c.isalnum() else "-" for c in (text or "").lower()).strip("-")


def debrief_filename(airfield: str, when: float | None = None,
                     player: str = "", callsign: str = "") -> str:
    """A timestamped debrief filename (one file per pilot spawn).

    ``tracks_<airfield>_<player>_<callsign>_<YYYYMMDD-HHMMSS>.json``.
    Player/callsign are omitted when empty so a session-wide or replay name
    still works (``tracks_gudauta_20261010-105236.json``).
    """
    stamp = (time.strftime("%Y%m%d-%H%M%S", time.localtime(when))
             if when is not None else time.strftime("%Y%m%d-%H%M%S"))
    parts = ["tracks", _debrief_slug(airfield)]
    if player:
        parts.append(_debrief_slug(player))
    if callsign:
        parts.append(_debrief_slug(callsign))
    parts.append(stamp)
    return "_".join(p for p in parts if p) + ".json"


class TrackHistory:
    """Per-callsign flight-path trails + comm markers for the map.

    Keeps a bounded deque of recent positions per aircraft, plus the radio calls
    made at those positions (so the map can show *where* a pilot called and
    *where* the reply came). Recording stops beyond `radius_nm` (off-station)
    and points closer than `min_move_nm` are skipped, so the trail is a clean
    path rather than a cloud of dots.

    Trails are **retained** after an aircraft leaves the mission (or the pilot
    quits the slot): this is a debrief tool, so a completed sortie must stay
    reviewable — a pilot who logs off should still see their whole flight.
    Retained tracks are marked `active=False` in the snapshot and drawn dimmed;
    they are only ever removed by a `reset`, a server restart without a
    `tracks_file`, or (optionally) loading a saved file. See `save`/`load`.
    """

    def __init__(self, radius_nm: float = TRACK_RADIUS_NM,
                 max_points: int = TRACK_MAX_POINTS,
                 min_move_nm: float = TRACK_MIN_MOVE_NM,
                 comm_max: int = COMM_MAX,
                 log: Callable[[str], None] | None = None):
        self.radius_nm = radius_nm
        self.max_points = max_points
        self.min_move_nm = min_move_nm
        self.comm_max = comm_max
        # Where to report save failures (the bot's log()); no-op by default.
        self.log = log or _noop_log
        self._tracks: dict[str, deque[tuple[float, float, float]]] = {}
        self._comms: dict[str, deque[dict]] = {}
        self._active: set[str] = set()
        # Last known position per callsign, even when off-station (where we do
        # not record a trail point). Used to pin a radio call to a position when
        # the aircraft is outside the recording radius.
        self._last_pos: dict[str, tuple[float, float]] = {}

    def update(self, callsign: str, lat: float, lon: float,
               distance_nm: float, alt_ft: float = 0.0) -> None:
        """Record a position for a callsign (skips off-station / tiny moves)."""
        self._active.add(callsign)
        self._last_pos[callsign] = (lat, lon)
        if distance_nm > self.radius_nm:
            return  # off-station: stop recording
        track = self._tracks.get(callsign)
        if track is None:
            track = deque(maxlen=self.max_points)
            self._tracks[callsign] = track
        if track:
            plat, plon, _ = track[-1]
            if _nm_between(plat, plon, lat, lon) < self.min_move_nm:
                return  # barely moved
        track.append((lat, lon, alt_ft))

    def add_comm(self, callsign: str, lat: float, lon: float, kind: str,
                 text: str, controller: str = "", t: str = "") -> None:
        """Attach a radio call (or state event) to a position on the path."""
        comms = self._comms.get(callsign)
        if comms is None:
            comms = deque(maxlen=self.comm_max)
            self._comms[callsign] = comms
        comms.append({"lat": lat, "lon": lon, "kind": kind, "text": text,
                      "controller": controller, "t": t})

    def last_position(self, callsign: str) -> tuple[float, float] | None:
        track = self._tracks.get(callsign)
        if track:
            return (track[-1][0], track[-1][1])
        # Off-station (no trail point recorded): fall back to the last known
        # position so a call is still pinned somewhere on the map.
        return self._last_pos.get(callsign)

    def path(self, callsign: str) -> list[list[float]]:
        return [[lat, lon, alt] for lat, lon, alt in self._tracks.get(callsign, ())]

    def comms(self, callsign: str) -> list[dict]:
        return list(self._comms.get(callsign, ()))

    def is_active(self, callsign: str) -> bool:
        return callsign in self._active

    def callsigns(self) -> list[str]:
        """Every callsign with a recorded trail (active or retained)."""
        return list(self._tracks)

    def point_count(self) -> int:
        """Total recorded points across every trail."""
        return sum(len(t) for t in self._tracks.values())

    def is_empty(self) -> bool:
        """True if no trail has any points (nothing worth saving)."""
        return self.point_count() == 0

    def forget(self, callsign: str) -> None:
        self._tracks.pop(callsign, None)
        self._comms.pop(callsign, None)
        self._active.discard(callsign)

    def set_active(self, active: set[str]) -> None:
        """Mark which callsigns are currently in the mission.

        Trails are **not** dropped for the ones no longer present — they are
        retained for debrief. This only flips the `active` flag used to dim
        completed trails on the map.
        """
        self._active = set(active)

    def to_json(self, callsigns: set[str] | None = None) -> dict:
        """Serialisable snapshot of every trail + comm marker.

        Pass `callsigns` to keep only those aircraft (one pilot's spawn).
        """
        if callsigns is None:
            track_items = self._tracks.items()
            comm_items = self._comms.items()
        else:
            track_items = ((cs, pts) for cs, pts in self._tracks.items()
                           if cs in callsigns)
            comm_items = ((cs, calls) for cs, calls in self._comms.items()
                          if cs in callsigns)
        return {
            "tracks": {cs: [list(p) for p in pts] for cs, pts in track_items},
            "comms": {cs: list(c) for cs, c in comm_items},
        }

    def load_json(self, data: dict) -> None:
        """Merge a `to_json()` snapshot (used to restore a saved debrief)."""
        if not isinstance(data, dict):
            return
        for cs, pts in (data.get("tracks") or {}).items():
            track = deque(maxlen=self.max_points)
            for p in pts:
                try:
                    track.append((float(p[0]), float(p[1]),
                                  float(p[2]) if len(p) > 2 else 0.0))
                except (TypeError, ValueError, IndexError):
                    continue
            if track:
                self._tracks[cs] = track
        for cs, calls in (data.get("comms") or {}).items():
            comms = deque(maxlen=self.comm_max)
            for c in calls:
                if isinstance(c, dict):
                    comms.append(c)
            if comms:
                self._comms[cs] = comms

    def save(self, path: Path) -> None:
        """Write the tracks to `path` atomically (temp file + rename)."""
        self.save_json(self.to_json(), path)

    def save_json(self, data: dict, path: Path) -> None:
        """Write a debrief payload (tracks + optional chatter) atomically.

        Best-effort: a debrief file must never take the map/bot down. But a
        failure is *logged* (with a traceback) rather than swallowed, so a
        missing sortie is diagnosable. The payload is sanitised first, so a
        stray non-serialisable value (e.g. an enum that leaked into a field)
        degrades to a string instead of aborting the whole save.
        """
        try:
            text = json.dumps(_json_safe(data))
        except (TypeError, ValueError) as error:
            self.log(f"[!] debrief serialise failed for {path}: {error}\n"
                     f"{traceback.format_exc()}")
            return
        try:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(text)
            os.replace(tmp, path)
        except OSError as error:
            self.log(f"[!] debrief write failed for {path}: {error}\n"
                     f"{traceback.format_exc()}")
        except Exception as error:  # noqa: BLE001 - never lose the trail silently
            self.log(f"[!] debrief save failed for {path}: {error}\n"
                     f"{traceback.format_exc()}")

    def load(self, path: Path) -> None:
        """Restore tracks from `path`, if it exists and parses."""
        try:
            if path.exists():
                self.load_json(json.loads(path.read_text()))
        except (OSError, ValueError):
            pass

    @staticmethod
    def read_chatter(path: Path) -> list[dict]:
        """The saved radio log from a debrief file (empty if none/absent)."""
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return []
        return [c for c in (data.get("chatter") or []) if isinstance(c, dict)]

    @staticmethod
    def read_bullseye(path: Path) -> list[float] | None:
        """The saved coalition bullseye from a debrief file, or None.

        Saved with the sortie so the viewer can draw it without a live bridge.
        """
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        bullseye = data.get("bullseye")
        if isinstance(bullseye, list) and len(bullseye) == 2:
            return [float(bullseye[0]), float(bullseye[1])]
        return None


# Keep at most this many debrief files (one file per pilot spawn, so a
# multi-ship session writes several). Oldest are pruned after a save.
DEBRIEF_KEEP = 40


def prune_debriefs(keep: int = DEBRIEF_KEEP, directory: Path | None = None) -> None:
    """Delete the oldest debrief files, keeping the newest `keep`."""
    directory = directory or DEBRIEF_DIR
    try:
        files = sorted(directory.glob("tracks_*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for path in files[keep:]:
        try:
            path.unlink()
        except OSError:
            pass


def _nm_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in NM (small helper; avoids importing airspace)."""
    import math
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a)) / 1852.0


def _json_safe(value):
    """Coerce a value into something `json.dumps` can always serialise.

    The debrief payload is built from live data (positions, brain state, STT
    text). A single stray non-serialisable value (an enum, a set, a custom
    object) would otherwise abort the *entire* save. This walks the structure
    and replaces anything unknown with its `str()`, so the trail is still
    written. `json.dumps` already escapes control characters and non-ASCII
    (including lone surrogates from odd STT bytes), so text needs no special
    handling here.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset, deque)):
        return [_json_safe(v) for v in value]
    return str(value)


def _overlay_payload(airfield: Airfield) -> dict | None:
    """Georeferenced chart overlay for an airfield, if one has been generated.

    `georef.py` writes `<name>.png` (north-up) + `<name>.json` (lat/lon bounds)
    into `web/overlays/`. Returns the URL + bounds for a Leaflet imageOverlay.
    """
    name = airfield.name.lower()
    png = OVERLAYS / f"{name}.png"
    meta = OVERLAYS / f"{name}.json"
    if not (png.exists() and meta.exists()):
        return None
    bounds = json.loads(meta.read_text())
    return {
        "url": f"/overlays/{name}.png",
        "bounds": [[bounds["south"], bounds["west"]],
                   [bounds["north"], bounds["east"]]],
    }


def _airspace_payload(airfield: Airfield, basemap: str | None = None) -> dict:
    """Static geometry for the map (CTR, gates, runways, taxi, parking)."""
    return {
        "name": airfield.name,
        "tower": airfield.tower,
        "elevation_ft": airfield.elevation_ft,
        "active_runway": airfield.active_runway,
        "center": [airfield.ctr.center_lat, airfield.ctr.center_lon],
        "ctr": {
            "ceiling_ft_agl": airfield.ctr.ceiling_ft_agl,
            "polygon": [list(p) for p in airfield.ctr.polygon_latlon],
        },
        "gates": {name: list(pos) for name, pos in airfield.gates.items()},
        "runways": {name: list(pos) for name, pos in airfield.runways.items()},
        "taxi_routes": airfield.taxi_routes,
        "parking_routes": airfield.parking_routes,
        "parking_areas": {name: list(pos) for name, pos in airfield.parking_areas.items()},
        "holding_points": {name: list(pos) for name, pos in airfield.holding_points.items()},
        # The areas the bot actually checks, derived from the same parameters
        # the brain uses (so the map can't drift from the logic).
        "checks": {
            "holding": airfield.holding_zone_geometry(),
            "final": airfield.final_zone_geometry(),
            "runway": airfield.runway_corridor_geometry(),
            "lineup": airfield.lineup_zone_geometry(),
            "exit": airfield.exit_zone_geometry(radius_nm=EXIT_REPORT_RADIUS_NM),
            "pattern": airfield.pattern_zones_geometry(),
        },
        "overlay": _overlay_payload(airfield),
        # Base map layer (OSM or DCS tiles) chosen by config/CLI.
        "basemap": _basemap_payload(airfield, basemap),
        # Agencies for the chatter-log filter checkboxes (shown even before
        # they have transmitted, so the filter list is stable).
        "agencies": _agencies(airfield),
    }


def _midfield_pos(airfield: Airfield) -> list[float]:
    """Fallback wind-arrow origin when DCS has not given a tower position."""
    if airfield.runways:
        lats = [p[0] for p in airfield.runways.values()]
        lons = [p[1] for p in airfield.runways.values()]
        return [sum(lats) / len(lats), sum(lons) / len(lons)]
    return [airfield.ctr.center_lat, airfield.ctr.center_lon]


KT_PER_MS = 1.943844  # DCS weather is m/s; the map shows knots


def _wind_payload(brain: AtcBrain) -> dict:
    """Live wind for the map arrow (dir = meteorological FROM, speed in kt)."""
    ms = float(getattr(brain, "wind_speed", 0.0) or 0.0)
    return {
        "dir": round(float(getattr(brain, "wind_dir", 0.0) or 0.0), 1),
        "speed": round(ms * KT_PER_MS),
        "text": getattr(brain, "wind", "calm"),
    }


def _basemap_payload(airfield: Airfield, choice: str | None) -> dict:
    """The base-map tile layer for the map.

    Reads `airspace.json` `map.layers` (with a fallback), picks `choice`
    (CLI --base-map) or `map.base` (config) or 'osm'. Returns the Leaflet
    tileLayer options the page needs: url, attribution, maxZoom,
    maxNativeZoom, tms.
    """
    cfg = airfield.map_config or {}
    layers = cfg.get("layers") or {}
    name = choice or cfg.get("base") or "osm"
    spec = layers.get(name) or _FALLBACK_LAYERS.get(name) or _FALLBACK_LAYERS["osm"]
    payload = {
        "name": name,
        "url": spec["url"],
        "attribution": spec.get("attribution", ""),
        "maxZoom": spec.get("maxZoom", 19),
        "maxNativeZoom": spec.get("maxNativeZoom", spec.get("maxZoom", 19)),
        "tms": bool(spec.get("tms", False)),
    }
    wms = spec.get("wms") or {}
    if wms.get("url") and wms.get("layers"):
        payload["wms"] = {"url": wms["url"], "layers": wms["layers"]}
    return payload


# Used when airspace.json has no `map` section.
_FALLBACK_LAYERS = {
    "osm": {"url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
            "attribution": "&copy; OpenStreetMap contributors", "maxZoom": 19},
    "dcs": {"url": "http://dcsmaps.com/caucasus/{z}/{x}/{y}.png",
            "attribution": "&copy; dcsmaps.com", "maxZoom": 16,
            "maxNativeZoom": 12, "tms": True,
            "wms": {"url": "http://dcsmaps.com/cgi-bin/mapserv?map=CAUCASUS_MAPFILE",
                    "layers": "Airbases,Cities,Towns,Roads,Railroad,Landmarks,"
                              "TACAN,VOR,NDB,DME,DB"}},
}


def _agencies(airfield: Airfield) -> list[str]:
    """Radio agencies for the chatter filter: ground/tower/control/stingray/atis."""
    out = []
    if airfield.ground_frequency_mhz:
        out.append("ground")
    if airfield.frequency_mhz:
        out.append("tower")
    if airfield.control_frequency_mhz:
        out.append("control")
    if airfield.stingray_frequency_mhz:
        out.append("stingray")
    if airfield.atis_frequency_mhz:
        out.append("atis")
    return out


class MapService:
    """Builds the JSON the map page polls, from live state + brain phases."""

    def __init__(self, airfield: Airfield, brain: AtcBrain,
                 state: StateClient | None, lock: threading.RLock | None = None,
                 basemap: str | None = None,
                 tracks_file: Path | str | None = None,
                 replay: bool = False,
                 control: "ControlPlane | None" = None,
                 log: Callable[[str], None] | None = None,
                 debrief_dir: Path | str | None = None):
        self.airfield = airfield
        self.brain = brain
        self.state = state
        self.lock = lock or threading.RLock()
        self.basemap = basemap  # base-map layer name override (else config)
        # Web-driven restart/airfield selection (None when running standalone).
        self.control = control
        # The bot's own log() callable, used to report save failures (with a
        # traceback) instead of swallowing them. Defaults to a no-op so the
        # standalone map server / tests need no logger.
        self.log = log or _noop_log
        # Chatter log: recent radio traffic (rx + tx) for the map's log drawer.
        self._chatter: deque[dict] = deque(maxlen=CHATTER_LIMIT)
        self._chatter_lock = threading.Lock()
        # Flight-path trails (per callsign), for the map's path overlay.
        self.tracks = TrackHistory(log=self.log)
        # Optional debrief file: restore saved trails on startup and autosave
        # them periodically, so a sortie survives a bot/map restart. When set
        # and the file already exists it is a *replay*: we restore it and, by
        # default, stop recording/overwriting, so a tester can review a saved
        # event (see `replay`).
        self.tracks_file = Path(tracks_file) if tracks_file else None
        self.debrief_dir = Path(debrief_dir) if debrief_dir else None
        # player name -> current spawn file / callsign (one debrief per spawn)
        self._spawn_files: dict[str, Path] = {}
        self._spawn_callsigns: dict[str, str] = {}
        self._last_saved = 0.0
        existed = self.tracks_file is not None and self.tracks_file.is_file()
        if existed:
            self.tracks.load(self.tracks_file)
            # Restore the saved radio log too, so the chatter window mirrors a
            # replayed sortie (older files simply have none).
            self._chatter.extend(TrackHistory.read_chatter(self.tracks_file))
        # `replay` true forces read-only review; when a tracks_file names an
        # existing file we default to replay too (don't clobber a saved sortie).
        self.replay = bool(replay or existed)
        # Last seen phase per callsign, to record phase transitions on the path.
        self._last_phase: dict[str, str] = {}
        # Live DCS tower/dispatcher lat/lon (TopGun uses the same bridge op).
        self._tower_pos: list[float] | None = None

    def save_tracks(self) -> None:
        """Write current trails to per-spawn files, or `tracks_file`.

        An empty history is **not** written, so a run that captured no trail
        never leaves an empty debrief file behind (which would show up in the
        map's load dropdown as an unusable entry).
        """
        if self.replay:
            return
        if self._spawn_files:
            with self.lock:
                for player, path in list(self._spawn_files.items()):
                    self._write_spawn(player, path)
            prune_debriefs(directory=self.debrief_dir)
            return
        if self.tracks_file is not None:
            with self.lock:
                if self.tracks.is_empty():
                    return
                data = self.tracks.to_json()
                # Include the radio log, so a replay restores the chatter
                # window too (not just the trails).
                with self._chatter_lock:
                    data["chatter"] = list(self._chatter)
                if self.brain.bullseye:
                    data["bullseye"] = list(self.brain.bullseye)
                self.tracks.save_json(data, self.tracks_file)
            # Debriefs are saved every session now; keep the newest few only.
            prune_debriefs()

    def _spawn_track_keys(self, player: str) -> set[str]:
        """Callsigns (and raw player name) whose trail belongs to this spawn."""
        known = self.brain.callsign_for_speaker(player)
        if known and known != player:
            self._spawn_callsigns[player] = known
        keys = {player}
        cs = self._spawn_callsigns.get(player) or ""
        if cs:
            keys.add(cs)
        return keys

    def _filter_chatter(self, player: str, callsign: str) -> list[dict]:
        names = {player.lower()}
        if callsign:
            names.add(callsign.lower())
        return [e for e in self._chatter
                if (e.get("who") or "").lower() in names
                or (e.get("callsign") or "").lower() in names]

    def _write_spawn(self, player: str, path: Path) -> None:
        keys = self._spawn_track_keys(player)
        data = self.tracks.to_json(keys)
        if not data.get("tracks"):
            return
        callsign = self._spawn_callsigns.get(player) or ""
        data["player"] = player
        data["callsign"] = callsign
        with self._chatter_lock:
            data["chatter"] = self._filter_chatter(player, callsign)
        if self.brain.bullseye:
            data["bullseye"] = list(self.brain.bullseye)
        self.tracks.save_json(data, path)

    def begin_spawn(self, player: str, callsign: str = "",
                    unit: str = "") -> None:
        """Open a new debrief file for this player's DCS spawn."""
        if self.replay or self.debrief_dir is None or not player:
            return
        cs = callsign if callsign and callsign != player else ""
        name = debrief_filename(self.airfield.name, player=player, callsign=cs)
        path = self.debrief_dir / name
        if path.exists():
            stem, n = path.stem, 2
            while True:
                cand = path.with_name(f"{stem}-{n}.json")
                if not cand.exists():
                    path = cand
                    break
                n += 1
        extra = f" unit={unit!r}" if unit else ""
        with self.lock:
            if player in self._spawn_files:
                self._end_spawn_locked(player)
            self._spawn_files[player] = path
            self._spawn_callsigns[player] = cs
        self.log(f"[*] Debrief spawn: {path.name}  player={player}"
                 f"  callsign={cs or '-'}{extra}")

    def end_spawn(self, player: str) -> None:
        """Flush and close this player's current spawn debrief."""
        if not player:
            return
        with self.lock:
            self._end_spawn_locked(player)

    def _end_spawn_locked(self, player: str) -> None:
        path = self._spawn_files.pop(player, None)
        keys = self._spawn_track_keys(player)
        if path is not None:
            self._write_spawn(player, path)
            for key in keys:
                self.tracks.forget(key)
                self._last_phase.pop(key, None)
        self._spawn_callsigns.pop(player, None)

    def _maybe_save_tracks(self) -> None:
        """Autosave the trails at most every `TRACKS_SAVE_INTERVAL` seconds."""
        if self.replay:
            return
        if self.tracks_file is None and not self._spawn_files:
            return
        now = time.monotonic()
        if now - self._last_saved < TRACKS_SAVE_INTERVAL:
            return
        self._last_saved = now
        self.save_tracks()

    def start_autosave(self, interval: float = TRACKS_SAVE_INTERVAL) -> None:
        """Save the trails on a wall-clock timer, independent of any caller.

        `record()`/`snapshot()` already autosave, but only while they are being
        called (a live monitor tick or an open map page). This timer guarantees
        the debrief file is refreshed on a fixed cadence regardless, so a
        missed/blocked caller — or a crash — still leaves a recent file on disk.
        No-op for a replay or when neither `tracks_file` nor `debrief_dir`
        is configured.
        """
        if self.replay or (self.tracks_file is None and self.debrief_dir is None):
            return

        def _loop() -> None:
            while True:
                time.sleep(interval)
                try:
                    self.save_tracks()
                except Exception as error:  # noqa: BLE001 - keep the timer alive
                    self.log(f"[!] periodic debrief save failed: {error}\n"
                             f"{traceback.format_exc()}")

        threading.Thread(target=_loop, daemon=True,
                         name="atc-debrief-autosave").start()

    def record(self, players) -> None:
        """Record positions + phase events for the live players (no map needed).

        Called on the bot's monitor tick so the debrief trail is captured whether
        or not a browser is polling the map. `players` is the list of live
        `Aircraft`. Idempotent with `snapshot`, which records the same way when
        the map *is* open. A no-op while reviewing a saved replay.
        """
        if self.replay:
            return
        with self.lock:
            for ac in players:
                callsign = self.brain.callsign_for_speaker(ac.player)
                pilot = self.brain.pilots.get(callsign)
                distance = self.airfield.ctr.distance_nm(ac.lat, ac.lon)
                self.tracks.update(callsign, ac.lat, ac.lon, distance, ac.alt_ft)
                phase = pilot.phase.value if pilot else "Unknown"
                if pilot is not None and self._last_phase.get(callsign) != phase:
                    self._last_phase[callsign] = phase
                    self.tracks.add_comm(
                        callsign, ac.lat, ac.lon, "state", phase,
                        pilot.last_controller, time.strftime("%H:%M:%S"))
            self.tracks.set_active(
                {self.brain.callsign_for_speaker(ac.player) for ac in players})
        self._maybe_save_tracks()

    def log_chatter(self, kind: str, freq_mhz: float, who: str,
                    text: str, controller: str = "") -> None:
        """Record one radio event (kind: 'rx' pilot, 'tx' ATC, 'coach' note)."""
        if self.replay:
            return  # reviewing a saved sortie: keep its log, don't add live ones
        # Resolve the flight callsign for a pilot transmission (the brain learns
        # SRS-name -> callsign from what the pilot says), so the log can show
        # "Colt 1" instead of the raw DCS/SRS name. Empty for ATC/system lines.
        callsign = ""
        if kind == "rx" and who:
            with self.lock:
                callsign = self.brain.callsign_for_speaker(who)
                if callsign == who:
                    # Not learned yet (the first call, before the worker maps
                    # the speaker): fall back to the callsign named in the text,
                    # so the first exchange is not split onto a ghost trail.
                    callsign = self.brain.callsigns.extract(text) or ""
        elif kind == "tx":
            # An ATC reply always opens with the callsign; use it to pin the
            # reply to the pilot's position on the path.
            with self.lock:
                callsign = self.brain.callsigns.extract(text) or ""
        elif kind == "coach" and who:
            # A wording-coach note goes to ONE pilot (log-only, not radio).
            # `who` is that flight's callsign — the note is pinned to the
            # pilot's position and lands in their per-spawn debrief, so the
            # post-flight review shows what was taught (and where).
            callsign = who
        stamp = time.strftime("%H:%M:%S")
        entry = {
            "t": stamp,
            "kind": kind,
            "freq": round(freq_mhz, 3),
            "who": who,
            "callsign": callsign,
            "text": text,
            "controller": controller,
        }
        with self._chatter_lock:
            self._chatter.append(entry)
        # Attach the call to the aircraft's path, so the map can show *where*
        # the pilot called and *where* the reply came. ATC replies (tx) are
        # pinned to the pilot's last known position.
        #
        # A pilot transmission that could not be tied to a callsign (the first
        # call before the brain learned the speaker, or a garbled/understood-less
        # call) is still attached — under the raw speaker name, which is how the
        # trail is keyed until the mapping is learned. This keeps *every* spoken
        # transmission on the trail, not just the recognised ones.
        key = callsign
        if not key and kind == "rx" and who:
            key = who
        if key:
            pos = self.tracks.last_position(key)
            if pos is None and who and who != key:
                pos = self.tracks.last_position(who)
            if pos is None:
                # No recorded position yet (e.g. the very first exchange, before
                # the trail has a point): ask the live state for it, so no call
                # is ever dropped from the trail.
                pos = self._live_position(who or key)
            if pos is not None:
                self.tracks.add_comm(key, pos[0], pos[1], kind, text,
                                     controller, stamp)

    def _live_position(self, who: str) -> tuple[float, float] | None:
        """Live lat/lon for a pilot (SRS/DCS name), or None."""
        if self.state is None or not who:
            return None
        try:
            for ac in self.state.aircraft():
                if ac.player == who or ac.callsign == who:
                    return (ac.lat, ac.lon)
        except (OSError, RuntimeError):
            return None
        return None

    def chatter(self) -> list[dict]:
        with self._chatter_lock:
            return list(self._chatter)

    def list_debriefs(self) -> list[dict]:
        """Saved debrief files that contain a trail, newest first.

        Empty files (no recorded points) are skipped: they are unusable as a
        replay and would only clutter the map's load dropdown.
        """
        out: list[dict] = []
        for path in DEBRIEF_DIR.glob("tracks_*.json"):
            try:
                if not json.loads(path.read_text()).get("tracks"):
                    continue
                out.append({"name": path.name,
                            "mtime": path.stat().st_mtime})
            except (OSError, ValueError):
                continue
        out.sort(key=lambda d: d["mtime"], reverse=True)
        for d in out:
            d["time"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                      time.localtime(d["mtime"]))
        return out

    def load_debrief(self, name: str) -> dict:
        """Load a saved debrief by filename and switch to read-only replay.

        Only a bare filename inside `DEBRIEF_DIR` is accepted (no path
        traversal), so the web endpoint can never read an arbitrary file.
        Returns a small status dict; on error, `ok` is False and nothing
        changes.
        """
        base = Path(name).name  # strip any directory components
        if not base or base != name or not base.endswith(".json"):
            return {"ok": False, "error": "invalid name"}
        path = (DEBRIEF_DIR / base).resolve()
        if path.parent != DEBRIEF_DIR.resolve() or not path.is_file():
            return {"ok": False, "error": "not found"}
        loaded = TrackHistory(log=self.log)
        loaded.load(path)
        if loaded.is_empty():
            return {"ok": False, "error": "empty debrief — no trail was recorded"}
        # Restore the saved radio log too, so the chatter window mirrors the
        # replayed sortie (falls back to empty for older files without it).
        chatter = TrackHistory.read_chatter(path)
        with self.lock:
            self.tracks = loaded
            self._last_phase = {}
            with self._chatter_lock:
                self._chatter.clear()
                self._chatter.extend(chatter)
        self.tracks_file = path
        self.replay = True  # freeze: don't record or overwrite the saved file
        return {"ok": True, "name": base}

    def unload_debrief(self) -> dict:
        """Leave replay and resume live recording (clears the loaded trail)."""
        with self.lock:
            self.tracks = TrackHistory(log=self.log)
            self._last_phase = {}
            with self._chatter_lock:
                self._chatter.clear()
        self.tracks_file = None
        self.replay = False
        return {"ok": True}

    @staticmethod
    def _retained_entry(callsign: str, path: list, comms: list) -> dict:
        """A trail shown with no live aircraft (completed, or a replay)."""
        return {
            "callsign": callsign,
            "player": "",
            "type": "",
            "lat": None,
            "lon": None,
            "alt_ft": None,
            "heading": None,
            "coalition": 0,
            "phase": "",
            "controller": "",
            "entry_gate": "",
            "exit_gate": "",
            "active": False,
            "path": path,
            "comms": comms,
        }

    def _live_tower_pos(self) -> list[float]:
        """Chart TWR if configured; else live DCS; else mid-runway.

        DCS `getDispatcherTowerPos` often geocodes to the Caucasus origin
        (Crimea); those points are rejected. Chart coords come from the
        overlay TWR star (`airspace.json` `tower_pos`).
        """
        if self.airfield.tower_pos:
            return [self.airfield.tower_pos[0], self.airfield.tower_pos[1]]
        if self._tower_pos is not None:
            return self._tower_pos
        getter = getattr(self.state, "tower", None) if self.state else None
        if getter is not None:
            try:
                info = getter(self.airfield.name)
            except (OSError, RuntimeError):
                info = None
            if info and info.get("lat") is not None and info.get("lon") is not None:
                lat, lon = float(info["lat"]), float(info["lon"])
                if self.airfield.ctr.distance_nm(lat, lon) <= 3.0:
                    self._tower_pos = [lat, lon]
                    return self._tower_pos
        return _midfield_pos(self.airfield)

    def snapshot(self) -> dict:
        """One map frame: airspace geometry + live aircraft with their state."""
        aircraft: list[dict] = []
        ai_air: list[dict] = []
        error = None
        active: set[str] = set()
        if self.state is not None and not self.replay:
            try:
                players = self.state.aircraft()
                all_units = self.state.all_units()
            except (OSError, RuntimeError) as exc:
                players, all_units = [], []
                error = str(exc)
            with self.lock:
                for ac in players:
                    callsign = self.brain.callsign_for_speaker(ac.player)
                    pilot = self.brain.pilots.get(callsign)
                    distance = self.airfield.ctr.distance_nm(ac.lat, ac.lon)
                    self.tracks.update(callsign, ac.lat, ac.lon, distance,
                                       ac.alt_ft)
                    phase = pilot.phase.value if pilot else "Unknown"
                    # Record a phase transition as a timeline event, so the path
                    # shows *when* the brain changed state (not just the calls).
                    if pilot is not None and self._last_phase.get(callsign) != phase:
                        self._last_phase[callsign] = phase
                        self.tracks.add_comm(
                            callsign, ac.lat, ac.lon, "state", phase,
                            pilot.last_controller, time.strftime("%H:%M:%S"))
                    aircraft.append({
                        "callsign": callsign,
                        "player": ac.player,
                        "type": ac.type,
                        "lat": ac.lat,
                        "lon": ac.lon,
                        "alt_ft": round(ac.alt_ft),
                        "heading": round(ac.heading),
                        "coalition": ac.coalition,
                        "phase": phase,
                        "controller": pilot.last_controller if pilot else "",
                        "entry_gate": pilot.entry_gate if pilot else "",
                        "exit_gate": pilot.exit_gate if pilot else "",
                        "active": True,
                        "path": self.tracks.path(callsign),
                        "comms": self.tracks.comms(callsign),
                    })
                # Mark which aircraft are present, so the map can dim the rest.
                # Trails are *retained* (not pruned) for debrief after a pilot
                # leaves the mission or quits the slot.
                active = {self.brain.callsign_for_speaker(ac.player)
                          for ac in players}
                self.tracks.set_active(active)
                for callsign in list(self._last_phase):
                    if callsign not in active:
                        del self._last_phase[callsign]
            # AI air traffic (planes/helicopters) near the field, for
            # situational awareness. The map page filters these to the current
            # viewport, so zoom/pan declutters without a server-side radius.
            for ac in all_units:
                if ac.player or ac.category not in (0, 1):
                    continue
                if self.airfield.ctr.distance_nm(ac.lat, ac.lon) > AI_RADIUS_NM:
                    continue
                ai_air.append({
                    "callsign": ac.callsign,
                    "type": ac.type,
                    "lat": ac.lat,
                    "lon": ac.lon,
                    "alt_ft": round(ac.alt_ft),
                    "heading": round(ac.heading),
                    "coalition": ac.coalition,
                })
        # Retained trails: completed sorties (still visible after a pilot logs
        # off) and — with no live state — a loaded replay, so a saved event can
        # be reviewed on the map without DCS running.
        with self.lock:
            for callsign in self.tracks.callsigns():
                if callsign in active:
                    continue
                aircraft.append(self._retained_entry(
                    callsign, self.tracks.path(callsign),
                    self.tracks.comms(callsign)))
        self._maybe_save_tracks()  # keep the debrief file reasonably fresh
        airfield = _airspace_payload(self.airfield, self.basemap)
        airfield["tower_pos"] = self._live_tower_pos()
        airfield["wind"] = _wind_payload(self.brain)
        airfield["topgun"] = {"radius_m": BUST_RADIUS_M}
        # Coalition bullseye from the mission (None until the bridge supplies
        # it). Drawn on the map so a bullseye call can be checked by eye.
        airfield["bullseye"] = (list(self.brain.bullseye)
                                if self.brain.bullseye else None)
        return {
            "airfield": airfield,
            "aircraft": aircraft,
            "ai_air": ai_air,
            "chatter": self.chatter(),
            "error": error,
            "replay": self.replay,
            "replay_name": (self.tracks_file.name
                            if self.replay and self.tracks_file else ""),
            # Web-driven restart/airfield selection (None when not wired).
            "control": self.control.payload() if self.control else None,
        }


def make_handler(service: MapService):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # keep the bot log clean
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # The browser closed the tab / refreshed mid-response. Normal
                # for a polling client; nothing to do but drop the connection.
                self.close_connection = True

        def do_GET(self) -> None:
            try:
                self._route()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True  # client hung up; not an error

        def do_POST(self) -> None:
            try:
                self._route_post()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def _json_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, TypeError):
                return {}

        def _route_post(self) -> None:
            path = urlsplit(self.path).path
            restart = False
            if path == "/api/debrief/load":
                result = service.load_debrief(self._json_body().get("name", ""))
            elif path == "/api/debrief/unload":
                result = service.unload_debrief()
            elif path == "/api/control/restart":
                body = self._json_body()
                if service.control is None:
                    self.send_error(404)
                    return
                # Preserve the current debug state unless the caller overrides.
                debug = body.get("debug")
                if debug is None:
                    debug = service.control.debug
                result = service.control.restart(
                    str(body.get("airfield") or service.airfield.name),
                    bool(debug))
                restart = bool(result.get("ok"))
            elif path == "/api/control/stop":
                # Stop without relaunch: tell the supervisor not to loop.
                if service.control is None:
                    self.send_error(404)
                    return
                service.control.stop()
                result = {"ok": True, "stopping": True}
                restart = True  # exit now; the wrapper stops on the sentinel
            else:
                self.send_error(404)
                return
            body = json.dumps(result).encode()
            self._send(200 if result.get("ok") else 400, body,
                       "application/json; charset=utf-8")
            # Only after the response is flushed: exit so the browser sees
            # "restarting" rather than a connection reset.
            if restart:
                service.control.run_pending()

        def _route(self) -> None:
            path = urlsplit(self.path).path
            if path == "/api/atc":
                body = json.dumps(service.snapshot(), separators=(",", ":")).encode()
                self._send(200, body, "application/json; charset=utf-8")
                return
            if path == "/api/debriefs":
                body = json.dumps({"debriefs": service.list_debriefs()}).encode()
                self._send(200, body, "application/json; charset=utf-8")
                return
            files = {
                "/": (WEB / "index.html", "text/html; charset=utf-8"),
                "/map.js": (WEB / "map.js", "text/javascript; charset=utf-8"),
                "/map.css": (WEB / "map.css", "text/css; charset=utf-8"),
            }
            if path.startswith("/overlays/") and path.endswith(".png"):
                overlay = OVERLAYS / Path(path).name
                if overlay.exists():
                    self._send(200, overlay.read_bytes(), "image/png")
                    return
            if path not in files:
                self.send_error(404)
                return
            file_path, content_type = files[path]
            self._send(200, file_path.read_bytes(), content_type)

    return Handler


def start_map_server(airfield: Airfield, brain: AtcBrain,
                     state: StateClient | None, port: int,
                     host: str = "0.0.0.0",
                     lock: threading.RLock | None = None,
                     log: Callable[[str], None] = print,
                     basemap: str | None = None,
                     tracks_file: Path | str | None = None,
                     replay: bool = False,
                     control: "ControlPlane | None" = None,
                     debrief_dir: Path | str | None = None,
                     ) -> tuple[ThreadingHTTPServer | None, MapService]:
    """Start the map server on a daemon thread.

    Returns (server, service). `server` is None (and a warning is logged) if the
    port is already in use, so a busy map port never takes the ATC bot down.
    The `service` is returned so the bot can push chatter-log entries to it.

    `tracks_file`, when set, restores saved trails on startup and autosaves them
    during the run (plus once at process exit). `debrief_dir` writes one file
    per pilot spawn instead of a single session file.
    """
    service = MapService(airfield, brain, state, lock, basemap=basemap,
                         tracks_file=tracks_file, replay=replay, control=control,
                         log=log, debrief_dir=debrief_dir)
    if service.replay and tracks_file is not None:
        log(f"[*] Debrief replay (read-only): {Path(tracks_file)}")
    elif tracks_file is not None or debrief_dir is not None:
        import atexit
        atexit.register(service.save_tracks)
        # A wall-clock timer, so the debrief is refreshed even if the
        # monitor tick or the map page never calls record()/snapshot().
        service.start_autosave()
        if tracks_file is not None:
            log(f"[*] Flight-track debrief file: {Path(tracks_file)} "
                f"(autosave every {TRACKS_SAVE_INTERVAL:.0f}s)")
        else:
            log(f"[*] Flight-track debrief dir: {Path(debrief_dir)} "
                f"(one file per pilot spawn, "
                f"autosave every {TRACKS_SAVE_INTERVAL:.0f}s)")
    try:
        server = ThreadingHTTPServer((host, port), make_handler(service))
    except OSError as error:
        log(f"[!] Map view disabled: cannot bind {host}:{port} ({error})")
        return None, service
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="atc-map")
    thread.start()
    log(f"[*] Map view on http://{host}:{port}/  (airfield {airfield.name})")
    return server, service


def main() -> None:
    import argparse

    from airspace import Airspace

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--airfield", default="Kutaisi")
    parser.add_argument("--airspace", default=None)
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--state-host", default="127.0.0.1")
    parser.add_argument("--state-port", type=int, default=10309)
    parser.add_argument("--no-state", action="store_true")
    parser.add_argument("--base-map", default=None,
                        help="base map layer: 'dcs' (DCS tiles) or 'osm' "
                             "(default: airspace.json map.base)")
    parser.add_argument("--tracks-file", default=None,
                        help="save/restore flight trails here (debrief). "
                             "Set automatically with --debug.")
    parser.add_argument("--replay", default=None,
                        help="load a saved debrief file and review it read-only")
    parser.add_argument("--list-debriefs", action="store_true",
                        help="list saved debrief files and exit")
    parser.add_argument("--debug", action="store_true",
                        help="save flight trails for after-action review")
    args = parser.parse_args()

    if args.list_debriefs:
        for path in sorted(DEBRIEF_DIR.glob("tracks_*.json")):
            print(path)
        return

    tracks_file = args.replay or args.tracks_file
    if tracks_file is None and args.debug:
        DEBRIEF_DIR.mkdir(parents=True, exist_ok=True)
        tracks_file = DEBRIEF_DIR / debrief_filename(args.airfield)

    airspace = Airspace.load(args.airspace) if args.airspace else Airspace.load()
    airfield = airspace.get(args.airfield)
    if airfield is None:
        raise SystemExit(f"airfield {args.airfield!r} not found in airspace.json")
    brain = AtcBrain(tower=airfield.tower, runway=airfield.active_runway,
                     ground=airfield.ground, control=airfield.control,
                     gates=list(airfield.gates), gate_locator=airfield.nearest_gate,
                     airfield=airfield)
    state = None if args.no_state else StateClient(args.state_host, args.state_port)
    server, _service = start_map_server(airfield, brain, state, args.port, args.host,
                                        basemap=args.base_map,
                                        tracks_file=tracks_file,
                                        replay=bool(args.replay))
    if server is None:
        raise SystemExit("map server could not start")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _service.save_tracks()


if __name__ == "__main__":
    main()
